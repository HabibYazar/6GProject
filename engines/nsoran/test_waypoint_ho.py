"""Phase 2b PLUMBING test — what is honestly assertable in a short batch window.

The :8090 batch path advances only ~0.8 s of sim time. At the 40 m/s ceiling
that is 32 m of travel against ~250 m to the nearest cell boundary, so no
mobility lever can show its BEHAVIOUR here — a random walk has not even turned
yet, so it covers the same distance as a straight path. Two things are still
provable, and they are the two that used to be silently broken:

  1. the waypoint path is installed and the motion is DIRECTED — every UE moves
     along the configured axis, which the diffusive random walk does not do;
  2. the RRC handover evaluation loop is the blocker. ns-O-RAN upstream
     commented out the periodic re-evaluation at the end of
     LteEnbRrc::TriggerUeAssociationUpdate, so with ho_check_ms=0 the RAN
     decides once at t=0 and NOTHING afterwards can hand a UE over; restoring
     the loop produces decisions on the same geometry.

The behavioural proof (real motion-driven handovers, and the hysteresis / A2
levers moving the count) needs the long horizon and lives in
test_waypoint_ho_run.py, which drives the /sim/run path.

Run inside Ubuntu-2004-nsoran (needs docker for the scenario log).
"""
import json
import math
import re
import subprocess
import time
import urllib.request

# 3 cells in a line, 500 m apart, all clear of the engine's LTE hub at (2000,2000).
GP = "1500.0,1700.0;2000.0,1700.0;2500.0,1700.0"
# 6 UEs bunched on cell 1's side of the cell-1/cell-2 midpoint (x=1750).
UE_XY = [(1700, 1640), (1690, 1700), (1710, 1760),
         (1660, 1670), (1730, 1730), (1670, 1780)]
UP = ";".join(f"{x},{y}" for x, y in UE_XY)
SL = ";".join(["9,10"] * len(UE_XY))
SPEED = 40.0            # m/s, the platform's envelope ceiling
LEG_M = 500.0           # straight EAST, then back
LEG_S = LEG_M / SPEED
WP = ";".join(
    f"{x},{y},1.5,0.000|{x + LEG_M},{y},1.5,{LEG_S:.3f}|{x},{y},1.5,{2 * LEG_S:.3f}"
    for x, y in UE_XY
)


def run(label, waypoints, check_ms):
    body = json.dumps({
        "sim_time": 20, "n_gnb": 3, "n_ue": len(UE_XY), "bw_hz": "50000000.0",
        "gnb_positions": GP, "ue_positions": UP, "gnb_powers": "44;44;44",
        "ue_slices": SL, "ue_speeds": ";".join([str(SPEED)] * len(UE_XY)),
        "ue_waypoints": waypoints, "ho_check_ms": check_ms,
        "ho_a3_entry": "1", "ho_hyst_db": "0", "ho_a2_db": "-1000",
    }).encode()
    req = urllib.request.Request("http://localhost:8090/run", data=body,
                                 headers={"Content-Type": "application/json"})
    for _ in range(8):
        r = json.load(urllib.request.urlopen(req, timeout=400))
        if r.get("status") != "busy":
            break
        time.sleep(10)
    scn = subprocess.run(["docker", "exec", "nsg-engine", "cat", "/tmp/scn0.log"],
                         capture_output=True, text=True).stdout

    pts = {}
    for m in re.finditer(r"IMSI (\d+) is ([\d.]+):([\d.]+):[\d.]+ at time ([\d.]+)", scn):
        pts.setdefault(int(m.group(1)), []).append(
            (float(m.group(4)), float(m.group(2)), float(m.group(3))))
    horizon, along = 0.0, []
    for tr in pts.values():
        tr.sort()
        if len(tr) < 2:
            continue
        horizon = max(horizon, tr[-1][0])
        dx, dy = tr[-1][1] - tr[0][1], tr[-1][2] - tr[0][2]
        d = math.hypot(dx, dy)
        if d > 1.0:
            along.append(dx / d)       # +1 = straight east (the configured axis)
    axis = sum(along) / len(along) if along else 0.0
    ho = re.findall(r"\[NSG-HO\] t=[\d.]+ imsi=\d+ ", scn)
    wp = re.search(r"\[NSG\] waypoint mobility: (\d+) UE", scn)
    print(f"  {label}")
    print(f"    status={r.get('status')}  sim horizon={horizon:.2f} s  "
          f"waypoint-UEs={wp.group(1) if wp else 0}  "
          f"mean along-axis fraction={axis:+.2f}  HO decisions={len(ho)}")
    return {"status": r.get("status"), "axis": axis, "ho": len(ho),
            "wp": int(wp.group(1)) if wp else 0, "horizon": horizon}


print("Phase 2b plumbing — waypoint install + handover evaluation loop\n")
a = run("A  waypoints, ho_check_ms=0   (inherited: decide once at t=0)", WP, "0")
b = run("B  waypoints, ho_check_ms=100 (periodic re-evaluation restored)", WP, "100")
c = run("C  random walk, ho_check_ms=100 (no waypoints, same speed)", "", "100")

print()
ok_wp = a["wp"] == len(UE_XY) and c["wp"] == 0
ok_dir = a["axis"] > 0.9 and abs(c["axis"]) < 0.9
ok_loop = a["ho"] == 0 and b["ho"] > 0
print(f"  waypoints installed on every UE (A {a['wp']}, C {c['wp']}): "
      f"{'PASS' if ok_wp else 'FAIL'}")
print(f"  motion follows the configured axis (A {a['axis']:+.2f} vs walk C "
      f"{c['axis']:+.2f}): {'PASS' if ok_dir else 'FAIL'}")
print(f"  the HO evaluation loop is the blocker (A {a['ho']} -> B {b['ho']} "
      f"decisions): {'PASS' if ok_loop else 'FAIL'}")
print(f"\n  batch-path sim horizon: {max(a['horizon'], b['horizon'], c['horizon']):.2f} s "
      f"— too short for HO behaviour; see test_waypoint_ho_run.py")
ok = a["status"] == b["status"] == c["status"] == "ok" and ok_wp and ok_dir and ok_loop
print("RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
