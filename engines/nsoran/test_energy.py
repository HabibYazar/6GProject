"""Measured-energy differential: does the engine's radio energy model respond
to Energy_state cell sleeps? Two identical runs, only gnb_powers differs."""
import json
import urllib.request

GP = "1550.0,1750.0;2050.0,1750.0;1550.0,2250.0;2050.0,2250.0;1800.0,2000.0"
UP = ("2050,2000;1950,2170;1750,2240;1580,2130;1550,1930;"
      "1650,1780;1850,1760;2020,1850;1700,2100;1900,2050")
SL = ";".join(["9,10"] * 10)


def run(label, powers):
    body = json.dumps({
        "sim_time": 15, "n_gnb": 5, "n_ue": 10, "bw_hz": "50000000.0",
        "gnb_positions": GP, "ue_positions": UP,
        "gnb_powers": powers, "ue_slices": SL,
    }).encode()
    req = urllib.request.Request("http://localhost:8090/run", data=body,
                                 headers={"Content-Type": "application/json"})
    # back-to-back runs can race the engine lock's release — wait busy out
    import time
    for _ in range(8):
        r = json.load(urllib.request.urlopen(req, timeout=300))
        if r.get("status") != "busy":
            break
        time.sleep(10)
    e = r.get("energy") or {}
    print(f"  {label}: status={r.get('status')}  total={e.get('total_j')} J  "
          f"span={e.get('span_s')} s  meanP={e.get('mean_power_w')} W")
    print(f"     per-cell J: {e.get('per_cell_j')}")
    return e


a = run("A) 5 cells up    ", "44;44;44;44;44")
b = run("B) 2 cells asleep", "0;44;44;0;44")

ta, tb = a.get("total_j"), b.get("total_j")
if ta and tb:
    print(f"\n  sleep saving: {ta - tb:+.1f} J ({100 * (ta - tb) / ta:.1f}%)")
    print("RESULT:", "PASS - measured energy responds to cell sleep"
          if tb < ta * 0.97 else
          "INFO - energy model is NOT sleep-sensitive (fixed draw per cell)")
else:
    print("RESULT: FAIL - no energy data")
