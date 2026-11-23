#!/bin/bash
# NSG spike: can 5G-LENA nr emit a KPM indication through our FlexRIC?
#
# Upstream registers the subscription callback on NrGnbNetDevice and leaves
# //BuildAndSendReportMessage(params); commented out against a function that was
# never written, so an nr gNB connects, accepts a subscription and reports
# nothing. This runs the nr scenario that already has E2ModeNr/E2TermIp wired
# against the same RIC the mmWave engine uses.
set -u
NS3DIR=/home/builder/ns-O-RAN-flexric/mmwave-LENA-oran
BIN=$NS3DIR/build/scratch/ns3.42-orange-rf-channel-reconfiguration-default
RIC=/root/nsoran/flexric/build/examples/ric/nearRT-RIC
XAPP=/root/nsoran/flexric/build/examples/xApp/c/kpm_rc/xapp_kpm_rc

pkill -9 -x nearRT-RIC 2>/dev/null
pkill -9 -x xapp_kpm_rc 2>/dev/null
pkill -9 -f orange-rf-channel 2>/dev/null
sleep 1

stdbuf -oL -eL "$RIC" >/tmp/nr_ric.log 2>&1 &
sleep 5
if grep -q 36421 /tmp/nr_ric.log; then
  echo "[1] RIC up on SCTP 36421"
else
  echo "[1] RIC FAILED"; tail -5 /tmp/nr_ric.log; exit 1
fi

# enableE2FileLogging=false is required: in file-logging mode the device writes
# CSV instead of sending E2, and the new report builder returns early.
runuser -u builder -- bash -lc "cd $NS3DIR && timeout -s KILL 150 env LD_LIBRARY_PATH=$NS3DIR/build/lib $BIN --e2TermIp=127.0.0.1 --e2nrEnabled=true --enableE2FileLogging=false" >/tmp/nr_scn.log 2>&1 &
sleep 30
echo "[2] E2 SETUP-REQUESTs seen by the RIC: $(grep -c 'E2 SETUP-REQUEST' /tmp/nr_ric.log)"
echo "[2] scenario alive: $(pgrep -f orange-rf-channel >/dev/null && echo yes || echo NO)"

timeout -s KILL 50 "$XAPP" >/tmp/nr_xapp.log 2>&1
echo "[3] indications built by nr: $(grep -c 'NSG-NR-KPM' /tmp/nr_scn.log)"
grep 'NSG-NR-KPM' /tmp/nr_scn.log | head -3
echo "[4] xApp log lines: $(wc -l < /tmp/nr_xapp.log)"
grep -icE 'indication|E2SM|KPM' /tmp/nr_xapp.log
pkill -9 -f orange-rf-channel 2>/dev/null
pkill -9 -x nearRT-RIC 2>/dev/null
echo "[done]"
