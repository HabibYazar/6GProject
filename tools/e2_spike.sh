#!/bin/bash
# PHASE 1 SPIKE — can a RAN that is NOT ns-3 speak real E2AP to our RIC?
#
# The fast tier's whole commercial claim rests on it keeping the real RIC and
# real E2AP while replacing what computes the RAN. Before building anything,
# this asks the question directly: run FlexRIC's own emulated gNB agent — a
# plain C program with no ns-3 in it — against our near-RT RIC, and point OUR
# PRODUCTION xApp at it. If real KPM counters come out, the architecture holds.
#
# It uses the host tree, so the product's container is untouched, and it builds
# only the emu_agent_gnb target — verified not to relink nearRT-RIC or the xApp.
#
#   wsl -d Ubuntu-2004-nsoran -u root -e bash tools/e2_spike.sh
#
# RESULT, 2026-09-20: it works. "Registered E2 Nodes = 1", and the xApp read
# DRB.UEThpDl, DRB.PdcpSduVolumeDL/UL, RRU.PrbTotDl/Ul and DRB.RlcSduDelayDl
# out of a RAN with no simulator behind it, at ~1.2 ms indication latency.
set -u
F=${FLEXRIC_DIR:-/root/nsoran/flexric}
RIC=$F/build/examples/ric/nearRT-RIC
AG=$F/build/examples/emulator/agent/emu_agent_gnb
XA=$F/build/examples/xApp/c/kpm_rc/xapp_kpm_rc
export LD_LIBRARY_PATH=/usr/local/lib:/usr/local/lib/flexric

if [ ! -x "$AG" ]; then
  echo "[spike] building emu_agent_gnb (this target only)"
  ( cd "$F/build" && cmake --build . --target emu_agent_gnb -j 4 ) || exit 1
fi

cleanup() { pkill -f emu_agent_gnb 2>/dev/null; pkill -f nearRT-RIC 2>/dev/null; }
trap cleanup EXIT
cleanup; sleep 1

"$RIC" > /tmp/spike_ric.log 2>&1 &  sleep 4
"$AG"  > /tmp/spike_agent.log 2>&1 & sleep 6
timeout "${SPIKE_SECS:-25}" "$XA" > /tmp/spike_xapp.log 2>&1

nodes=$(grep -c "Connected E2 nodes = 1" /tmp/spike_xapp.log)
names=$(grep -oE "^[A-Za-z]+\.[A-Za-z0-9._]+ =" /tmp/spike_xapp.log | sort -u | sed 's/ =//')
n=$(echo "$names" | grep -c .)

echo "E2 nodes the xApp saw : $nodes"
echo "counters with a value : $n"
echo "$names" | sed 's/^/  /'
grep -m1 "ind_msg latency" /tmp/spike_xapp.log | sed 's/^/  /'

# TWO MODES, and the pass criterion is not the same in both.
#
# With no feed the agent is upstream's emulator and every counter carries
# rand(), so seven of them appearing is the whole claim: E2AP works with no
# ns-3 under it.
#
# With a feed the claim is stronger and the count is LOWER. Tier B produces
# three of the seven; the other four now report E2SM-KPM's no-value rather
# than a plausible random number, so they print nothing. Four counters
# disappearing is the fix working, and the old `n >= 4` threshold would have
# called it a regression — it did, once, which is why this is here.
FEED=${NSG_FEED_PATH:-/tmp/nsg_tier_b_kpm.txt}
if [ -s "$FEED" ]; then
  echo "feed present          : $(grep -vc '^#' "$FEED") records"
  bad=0
  # A LIVE feed has moved on by the time this runs, so a value the xApp saw in
  # window 10 is not in window 40's file and exact membership is the wrong
  # test — it failed once for exactly that reason. The discriminator that
  # holds either way is the emulator's own range: fill_rnd_float_data gives
  # 5.0 to about 1029 and fill_rnd_int_data 0 to 1023, and Tier B's downlink
  # throughput is tens of thousands of kbps. A value below the ceiling of the
  # random generator is a value the feed did not supply.
  for key in DRB.UEThpDl; do
    got=$(grep -oE "^$key = [0-9.]+" /tmp/spike_xapp.log | awk '{print $3}' | sort -un)
    [ -z "$got" ] && { echo "  $key: the xApp received none"; bad=1; continue; }
    lo=$(echo "$got" | head -1); hi=$(echo "$got" | tail -1)
    rnd=$(awk -v l="$lo" 'BEGIN{print (l+0 < 1030) ? 1 : 0}')
    echo "  $key: $(echo "$got" | wc -w) distinct, range $lo - $hi"
    if [ "$rnd" = "1" ]; then
      echo "        inside the emulator's random range — the feed did not reach it"
      bad=1
    fi
  done
  # PRB values are small and recur, so membership still works there and is
  # the stronger check where it can be made.
  for key in RRU.PrbTotDl; do
    got=$(grep -oE "^$key = [0-9.]+" /tmp/spike_xapp.log | awk '{print $3}' | sort -u | head -40)
    [ -z "$got" ] && { echo "  $key: the xApp received none"; bad=1; continue; }
    miss=0
    for v in $got; do
      awk -v k="$key" -v want="$v" '$1==k && (want+0)==($3+0){f=1} END{exit !f}' "$FEED" \
        || miss=$((miss+1))
    done
    echo "  $key: $(echo "$got" | wc -w) distinct received, $miss not in the current feed"
    [ "$miss" -gt 2 ] && bad=1
  done
  for key in DRB.UEThpUl RRU.PrbTotUl DRB.RlcSduDelayDl DRB.PdcpSduVolumeUL; do
    if grep -q "^$key = " /tmp/spike_xapp.log; then
      echo "  $key: STILL RANDOM — Tier B does not produce it and it should"
      echo "        be reporting no value"
      bad=1
    fi
  done
  if [ "$nodes" -ge 1 ] && [ "$n" -ge 3 ] && [ "$bad" -eq 0 ]; then
    echo "SPIKE PASS — the fast tier's own numbers reached our xApp over real E2AP,"
    echo "             and every counter it cannot produce reported no value"
    exit 0
  fi
else
  if [ "$nodes" -ge 1 ] && [ "$n" -ge 4 ]; then
    echo "SPIKE PASS — a non-ns-3 RAN served real KPM over real E2AP to our own xApp"
    echo "             (no feed: these are the emulator's random values)"
    exit 0
  fi
fi
echo "SPIKE FAIL — see /tmp/spike_{ric,agent,xapp}.log"
exit 1
