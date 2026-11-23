"""E4: live-horizon MRO claim — with vehicle speeds and an aggressive HO margin,
the LIVE handover counter (serving-cell changes) must actually rise."""
import asyncio
import json
import time

import websockets


def cell(i, x, y):
    return {"id": i, "name": i, "position": {"x": x, "y": y, "z": 25},
            "radio": {"band": {"label": "NR", "frequency_mhz": 3500,
                               "bandwidth_mhz": 50, "numerology": 1},
                      "tx_power_dbm": 44,
                      "antenna": {"type": "cosine", "azimuth_deg": 0, "tilt_deg": 3,
                                  "gain_dbi": 17, "horizontal_beamwidth_deg": 65,
                                  "vertical_beamwidth_deg": 7.5}}}


CFG = {
    "name": "churn", "description": "",
    "base_stations": [cell("g1", 0, 0), cell("g2", 500, 0), cell("g3", 0, 500),
                      cell("g4", 500, 500), cell("g5", 250, 250)],
    "ue_groups": [{"id": "u1", "name": "cars", "count": 10,
                   "spawn_position": {"x": 250, "y": 250},
                   "mobility": {"pattern": "vehicle", "speed_mps": 25},
                   "services": [{"slice_type": "eMBB", "qos_id": 9,
                                 "target_throughput_gbps": 0.01}]}],
    "buildings": [],
    "area": {"x_min": -400, "x_max": 900, "y_min": -400, "y_max": 900, "resolution_m": 40},
    "settings": {"duration_s": 60, "report_interval_ms": 1000, "scenario": "UMa",
                 "shadowing_enabled": True, "fast_fading_enabled": False,
                 "random_seed": 42, "ho_sinr_margin_db": 1.0},
}


async def main():
    frames = 0
    ho_series = []
    t0 = time.time()
    async with websockets.connect("ws://localhost:8000/ws/e2-real",
                                  ping_interval=None, max_size=None) as ws:
        await ws.send(json.dumps({"config": CFG}))
        try:
            async with asyncio.timeout(290):
                while time.time() - t0 < 260:
                    msg = json.loads(await ws.recv())
                    if msg.get("type") != "kpm":
                        continue
                    frames += 1
                    ho = msg.get("handovers")
                    if ho is not None and (not ho_series or ho != ho_series[-1][1]):
                        ho_series.append((frames, ho))
                    if ho is not None and ho >= 5 and frames > 30:
                        break
        except Exception:
            pass  # window end / clean close both fine
    final = ho_series[-1][1] if ho_series else None
    print(f"  frames={frames}  handover progression={ho_series[:10]}")
    ok = frames > 20 and final is not None and final >= 2
    print(f"  final live handovers: {final}")
    print("RESULT:", "PASS - live horizon shows real handover churn"
          if ok else "FAIL/INCONCLUSIVE - churn below threshold in this window")
    return 0 if ok else 1


raise SystemExit(asyncio.run(main()))
