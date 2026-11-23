"""RU sleep states (Phase 2a): does an idle cell really descend into a sleep
depth, draw the right current, and save energy without costing throughput?

Three identical runs — same topology, same traffic, only the sleep profile
differs. Cells 2 and 3 are placed away from the UE cluster, so they end up with
no UEs to serve: exactly the cells a real energy-saving controller would put
down. Nothing is switched off (gnb_powers is full everywhere), so any energy
difference comes from the sleep state machine alone.

  A) sleep off      — today's behaviour: idle cells stay fully awake
  B) light sleep    — ~50% of the idle draw, 1 ms to wake
  C) deep sleep     — ~33% of the idle draw, 10 ms to wake
"""
import json
import time
import urllib.request

# 4 cells; the UEs sit around the first two, so cells 3-4 have nothing to serve.
GP = "1700.0,1900.0;1900.0,1900.0;2600.0,2600.0;1300.0,1300.0"
UP = ("1750,1950;1800,1880;1850,1930;1900,1960;1780,1900;"
      "1830,1850;1880,1990;1930,1880")
SL = ";".join(["9,10"] * 8)
POW = "44;44;44;44"


def run(label, sleep_spec):
    body = {
        "sim_time": 15, "n_gnb": 4, "n_ue": 8, "bw_hz": "50000000.0",
        "gnb_positions": GP, "ue_positions": UP,
        "gnb_powers": POW, "ue_slices": SL,
    }
    if sleep_spec:
        body["gnb_sleep"] = sleep_spec
        body["sleep_wake_load_pct"] = 60
    req = urllib.request.Request(
        "http://localhost:8090/run", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    # back-to-back runs can race the engine lock's release — wait busy out
    for _ in range(8):
        r = json.load(urllib.request.urlopen(req, timeout=400))
        if r.get("status") != "busy":
            break
        time.sleep(10)
    e = r.get("energy") or {}
    s = r.get("sleep") or {}
    thp = (r.get("measurements") or {}).get("DRB.UEThpDl") or {}
    print(f"  {label}: status={r.get('status')}  {e.get('mean_power_w')} W "
          f"({e.get('total_j')} J over {e.get('span_s')} s)  "
          f"thp={thp.get('mean')} kbps")
    print(f"     per-cell J : {e.get('per_cell_j')}")
    if s:
        print(f"     layer asleep {s.get('layer_sleep_pct')}% of airtime, "
              f"{s.get('sleep_events')} sleeps / {s.get('wake_events')} wakes")
        for cell, d in (s.get("per_cell") or {}).items():
            print(f"     cell {cell}: depth={d['depth']} state={d['state']} "
                  f"asleep {d['sleep_pct']}% ({d['sleep_s']} s) ues={d['ues']}")
    return e, s, thp


a, _, ta = run("A) sleep off   ", "")
b, sb, tb = run("B) light sleep ", "0,200,1;0,200,1;2,200,1;2,200,1")
c, sc, tc = run("C) deep sleep  ", "0,200,10;0,200,10;3,200,10;3,200,10")

# Compare MEAN POWER, not total Joules: each run reaches a slightly different
# simulated time inside its fixed wall-clock window, so the totals are over
# different spans. Watts is the comparable (and the physically meaningful) number.
ja, jb, jc = a.get("mean_power_w"), b.get("mean_power_w"), c.get("mean_power_w")
print()
ok = True
if ja and jb and jc:
    print(f"  light sleep saving: {ja - jb:+.1f} W ({100 * (ja - jb) / ja:+.1f}%)")
    print(f"  deep  sleep saving: {ja - jc:+.1f} W ({100 * (ja - jc) / ja:+.1f}%)")
    # Deeper sleep must save strictly more than light sleep, or the depth is
    # decorative. Both must beat the no-sleep baseline.
    ok = jb < ja * 0.97 and jc < jb
else:
    ok = False
    print("  no energy data")
if not (sb.get("sleep_events") or 0) or not (sc.get("sleep_events") or 0):
    ok = False
    print("  no cell ever entered a sleep state")
if ta.get("mean") and tc.get("mean") and tc["mean"] < ta["mean"] * 0.8:
    print(f"  WARNING: throughput dropped {ta['mean']} -> {tc['mean']} kbps — "
          f"the sleeping cells were carrying traffic")
print("RESULT:", "PASS - sleep depth is a real, measurable energy lever"
      if ok else "FAIL - see above")
