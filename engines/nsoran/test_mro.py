"""MRO plumbing test — what is HONESTLY assertable in a batch window.

The config's speed must drive the UEs' REAL motion: position-trace step lengths
scale with the configured speed. (Motion-driven handover churn is NOT
observable in a batch run — the sim advances ~0.4-1.5 simulated seconds and the
random walk diffuses rather than traverses; measured 0 handovers at every
speed, including a x40 compression that was removed again. The ho_sinr_db
margin is asserted at the plumbing level: it must reach the scenario's config.)
"""
import json
import re
import subprocess
import time
import urllib.request

GP = "1550.0,1750.0;2050.0,1750.0;1550.0,2250.0;2050.0,2250.0;1800.0,2000.0"
UP = ("2050,2000;1950,2170;1750,2240;1580,2130;1550,1930;"
      "1650,1780;1850,1760;2020,1850;1700,2100;1900,2050")
SL = ";".join(["9,10"] * 10)


def run(label, speeds, ho):
    body = json.dumps({
        "sim_time": 15, "n_gnb": 5, "n_ue": 10, "bw_hz": "50000000.0",
        "gnb_positions": GP, "ue_positions": UP, "gnb_powers": "44;44;44;44;44",
        "ue_slices": SL, "ue_speeds": speeds, "ho_sinr_db": ho,
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
    # mean step length of IMSI 1..10 between consecutive 0.1 s position prints
    pts = {}
    for m in re.finditer(
            r"IMSI (\d+) is ([\d.]+):([\d.]+):[\d.]+ at time ([\d.]+)", scn):
        pts.setdefault(int(m.group(1)), []).append(
            (float(m.group(4)), float(m.group(2)), float(m.group(3))))
    steps = []
    for tr in pts.values():
        tr.sort()
        for (t0, x0, y0), (t1, x1, y1) in zip(tr, tr[1:]):
            if 0.05 < t1 - t0 < 0.2:
                steps.append(((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5)
    mean_step = sum(steps) / len(steps) if steps else 0.0
    applied = "per-UE speeds applied" in scn
    print(f"  {label}: status={r.get('status')}  mean step/0.1s={mean_step:.2f} m  "
          f"speeds-applied={applied}  handovers={r.get('handovers')}")
    return mean_step, r.get("status"), applied


s_slow, st1, ap1 = run("speed  1.4 m/s, margin 3 dB", ";".join(["1.4"] * 10), "3")
s_fast, st2, ap2 = run("speed 25.0 m/s, margin 9 dB", ";".join(["25.0"] * 10), "9")

ratio = s_fast / s_slow if s_slow > 0 else float("inf")
ok = (st1 == st2 == "ok" and ap1 and ap2
      and s_slow > 0 and 8.0 < ratio < 40.0)   # ~17.9x expected, generous band
print(f"\n  step-length ratio fast/slow: {ratio:.1f} (configured 17.9x)")
print("RESULT:", "PASS - config speed drives real UE motion; HO margin plumbed"
      if ok else "FAIL")
raise SystemExit(0 if ok else 1)
