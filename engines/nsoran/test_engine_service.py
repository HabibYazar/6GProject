"""Unit-test the engine's run classifier against the REAL incident log text."""
import re
import sys

sys.path.insert(0, "/root/nsoran")
from engine_service import _classify, _DU_KEYS, _script_budget

CRASH_RE = r"^.*(NS_ASSERT failed|NS_FATAL|Segmentation fault|dumped core).*$"

# Verbatim from the run that made the leaderboard show a row of "—".
REAL_CRASH_LOG = """[NSG] per-UE slice traffic: 6 UE(s) from config
[NSG] UE 0 slice qci=9 rate=10 Mbps
NS_ASSERT failed, cond="m_ptr", msg="Attempted to dereference zero pointer", +0.158582140s 8 file=/home/builder/ns-O-RAN-flexric/mmwave-LENA-oran/src/core/model/ptr.h, line=718
NS_FATAL, terminating
terminate called without an active exception
"""
HEALTHY_LOG = "[NSG] 3 buildings installed\nSimulation finished\n"

# The PDCP-only set the crashed run actually returned.
PDCP_ONLY = {k: {"mean": 1.0} for k in
             ["DRB.PdcpSduBitRateDl", "DRB.PdcpSduDelayDl",
              "DRB.PdcpSduVolumeDl", "Tot.PdcpSduNbrDl"]}
FULL = dict(PDCP_ONLY, **{k: {"mean": 1.0} for k in _DU_KEYS})

# Observed for real: SINR arrived, throughput never did. An any()-of-DU-metrics
# gate called this "ok" and the leaderboard still rendered a blank row.
SINR_NO_THP = dict(PDCP_ONLY, **{"L3servingSINR": {"mean": 15.5}})

cases = [
    ("crashed run, PDCP only (the reported bug)", PDCP_ONLY,   REAL_CRASH_LOG, "crashed"),
    ("healthy run, full metrics",                 FULL,        HEALTHY_LOG,    "ok"),
    ("no metrics at all",                         {},          HEALTHY_LOG,    "no_data"),
    ("PDCP only, no crash logged",                PDCP_ONLY,   HEALTHY_LOG,    "partial"),
    # A crash logged late must NOT mask metrics that did arrive.
    ("crash logged but DU metrics present",       FULL,        REAL_CRASH_LOG, "ok"),
    ("SINR but no throughput (real 5-cell run)",  SINR_NO_THP, HEALTHY_LOG,    "partial"),
]

fails = 0
for name, meas, log, want in cases:
    got, err = _classify(meas, re.search(CRASH_RE, log, re.M))
    ok = got == want
    fails += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:42s} -> {got!r}" + (f" (want {want!r})" if not ok else ""))
    if got == "crashed":
        assert "dereference zero pointer" in err, "crash reason must reach the UI"

# The timeout that silently killed >=6-gNB validates.
print("\n  budget vs xApp window (old fixed budget was 180s):")
for n in (3, 5, 7):
    b, w = _script_budget({"n_gnb": n}), 40 + n * 15
    ok = b > w + 45          # window + the bounded E2-setup wait
    fails += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {n} gNB: window {w}s + boot<=45s -> budget {b}s")

print("\nRESULT:", "ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
