"""Call-dynamics differential: same offered rate, continuous vs bursty
(on=2s / off=20s exponential ≈ 9% duty cycle) — measured DL volume must drop
hard, and per-report-window rates must VARY (the whole point: load breathes)."""
import json
import re
import subprocess
import time
import urllib.request

GP = "1550.0,1750.0;2050.0,1750.0;1550.0,2250.0;2050.0,2250.0;1800.0,2000.0"
UP = ("2050,2000;1950,2170;1750,2240;1580,2130;1550,1930;"
      "1650,1780;1850,1760;2020,1850;1700,2100;1900,2050")


def run(label, slices):
    body = json.dumps({
        "sim_time": 15, "n_gnb": 5, "n_ue": 10, "bw_hz": "50000000.0",
        "gnb_positions": GP, "ue_positions": UP,
        "gnb_powers": "44;44;44;44;44", "ue_slices": slices,
    }).encode()
    req = urllib.request.Request("http://localhost:8090/run", data=body,
                                 headers={"Content-Type": "application/json"})
    for _ in range(8):
        r = json.load(urllib.request.urlopen(req, timeout=400))
        if r.get("status") != "busy":
            break
        time.sleep(10)
    m = r.get("measurements", {})
    thp = m.get("DRB.UEThpDl", {})
    # install log: did the engine actually build OnOff apps?
    scn = subprocess.run(["docker", "exec", "nsg-engine", "grep", "-c",
                          "call on/off", "/tmp/scn0.log"],
                         capture_output=True, text=True).stdout.strip()
    print(f"  {label}: status={r.get('status')}  thp mean={thp.get('mean')} kbps "
          f"(min={thp.get('min')} max={thp.get('max')} n={thp.get('count')})  "
          f"onoff-apps={scn}")
    return thp


# The backend time-compresses human-scale call times to a <=1 s period with the
# duty cycle preserved (the sim only advances ~1-2 SIM seconds per run); this
# engine-level test sends the compressed form directly: 2s/20s -> 0.091/0.909.
cont = ";".join(["9,20"] * 10)                    # continuous 20 Mbps
burst = ";".join(["9,20,0.091,0.909"] * 10)       # same rate, ~9% duty cycle

ta = run("continuous        ", cont)
tb = run("bursty (9% duty)  ", burst)

ok = True
if ta.get("mean") and tb.get("mean") is not None:
    ratio = tb["mean"] / ta["mean"]
    print(f"\n  thp ratio bursty/continuous: {ratio:.2f} (offered duty ~0.09)")
    ok &= 0.0 < ratio < 0.6      # bursty must carry far less at the same rate
else:
    ok = False
# variability: bursty windows must differ (some silent, some in a burst)
if tb.get("min") is not None and tb.get("max") and tb.get("max") > 0:
    spread = (tb["max"] - tb["min"]) / tb["max"]
    print(f"  bursty per-UE spread: min={tb['min']} max={tb['max']} "
          f"(spread {spread:.0%} of max)")
    ok &= spread > 0.3
print("\nRESULT:", "PASS - call dynamics shape real traffic" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
