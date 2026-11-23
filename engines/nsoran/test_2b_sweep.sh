#!/bin/bash
# Phase 2b behavioural sweep on the /sim/run horizon (~25 min, 5 x 300 s).
#
# Each variant changes ONE thing on identical geometry — 3 cells 500 m apart,
# 6 UEs starting 50 m inside the cell-1/cell-2 boundary at 40 m/s. What it has
# to show:
#
#   waypoints-noloop  same motion, ho_check_ms=0  -> ZERO handovers. This is the
#                     inherited engine: ns-O-RAN upstream commented out the RRC's
#                     periodic handover re-evaluation, so the RAN decides once at
#                     t=0 and no amount of motion can ever hand a UE over.
#   waypoints-loop    the same run with the loop restored -> real handovers.
#   walk-loop         loop on, no waypoints -> the random walk DIFFUSES (roughly
#                     a third of the net displacement at the same speed) and
#                     hovers near the boundary, so it churns ping-pong handovers
#                     instead of traversing. Waypoints decide WHERE a UE goes;
#                     they are not what makes handover possible.
#   hysteresis        +8 dB A3 hysteresis -> markedly fewer handovers.
#   a2                A2 at 5 dB -> most decisions carry the A2 trigger.
#
# Run from the platform distro (Ubuntu), with the engine container up.
set -u
cd "$(dirname "$0")/../.." || exit 1
WALL=${1:-300}
PY=./venv/bin/python

for v in waypoints-loop waypoints-noloop walk-loop hysteresis a2; do
  echo "=== $v (${WALL}s wall) ==="
  timeout $((WALL + 200)) $PY engines/nsoran/test_waypoint_ho_run.py "$v" "$WALL" || echo "  RUN FAILED"
  sleep 5
done

$PY - <<'EOF'
import json, sys
V = ["waypoints-loop", "waypoints-noloop", "walk-loop", "hysteresis", "a2"]
r = {}
for v in V:
    try:
        r[v] = json.load(open(f"/tmp/2b_{v}.json"))
    except OSError:
        print(f"missing result for {v}"); sys.exit(1)

print(f"\n  {'variant':18s} {'horizon':>8s} {'net m':>7s} {'HO':>4s} {'triggers':>14s} {'engine HO':>10s}")
for v in V:
    d = r[v]
    trig = ",".join(f"{k}:{n}" for k, n in d["ho_by_trigger"].items()) or "-"
    print(f"  {v:18s} {d['sim_horizon_s']:7.1f}s {d['mean_net_displacement_m']:7.0f} "
          f"{d['ho_decisions']:4d} {trig:>14s} {str(d['engine_handovers']):>10s}")

base = r["waypoints-loop"]
checks = [
    ("the HO evaluation loop is the blocker",
     r["waypoints-noloop"]["ho_decisions"] == 0 and base["ho_decisions"] > 0,
     f"noloop {r['waypoints-noloop']['ho_decisions']} -> loop {base['ho_decisions']} decisions, "
     f"on the same {r['waypoints-noloop']['mean_net_displacement_m']:.0f} m of motion"),
    ("waypoints traverse, the random walk diffuses",
     base["mean_net_displacement_m"] > 2 * r["walk-loop"]["mean_net_displacement_m"],
     f"{base['mean_net_displacement_m']:.0f} m vs {r['walk-loop']['mean_net_displacement_m']:.0f} m "
     f"net at the same speed"),
    ("waypoints are installed, the walk variant has none",
     base["waypoint_ues"] > 0 and r["walk-loop"]["waypoint_ues"] == 0,
     f"{base['waypoint_ues']} waypoint UEs vs {r['walk-loop']['waypoint_ues']}"),
    ("A3 hysteresis suppresses handovers",
     r["hysteresis"]["ho_decisions"] < base["ho_decisions"],
     f"{base['ho_decisions']} -> {r['hysteresis']['ho_decisions']} decisions at +8 dB"),
    ("A2 fires and is attributed",
     r["a2"]["ho_by_trigger"].get("A2", 0) > r["a2"]["ho_by_trigger"].get("A3", 0),
     f"A2 {r['a2']['ho_by_trigger'].get('A2', 0)} vs A3 {r['a2']['ho_by_trigger'].get('A3', 0)}"),
]
print()
ok = True
for label, passed, why in checks:
    ok &= passed
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:44s} {why}")
h = max(d["sim_horizon_s"] for d in r.values())
w = max(d["wall_s"] for d in r.values())
print(f"\n  measured horizon: {h:.1f} s of sim in {w:.0f} s wall "
      f"({100 * h / w:.1f}% of real time) — the /sim/run wall cap of 900 s "
      f"reaches roughly {900 * h / w:.0f} s of sim time.")
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
EOF
