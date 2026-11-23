"""Phase 2b validation on the /sim/run (batch-run) horizon.

The short :8090 path only advances ~0.8 s of sim time, which is far too little
for any mobility lever: at the 40 m/s ceiling a UE moves 32 m there, against
~250 m to the nearest cell boundary. /sim/run drives the LIVE bridge instead,
with a 900 s wall cap, so it reaches a much longer sim horizon — this measures
what that horizon actually is and whether the 2b levers move inside it.

Runs the real /sim/run code path (run_batch_simulation on the config the
platform would build), not a hand-written engine payload.

  usage:  python3 test_waypoint_ho_run.py <variant> [wall_cap_s]
  variants: waypoints-loop | waypoints-noloop | walk-loop | hysteresis | a2
Run from the platform distro; reads the scenario log via the engine distro.
"""
import asyncio
import json
import re
import subprocess
import sys
import time

sys.path.insert(0, "/home/chancelor/nsg_ai_platform")
sys.path.insert(0, "/home/chancelor/nsg_ai_platform/nsg_ai")

from nsg_ai.schema import NSGSimulationConfig            # noqa: E402
from nsg_ai.main import nsoran_params                    # noqa: E402
from nsg_ai.nsoran_live import run_batch_simulation      # noqa: E402

BAND = {"label": "n78", "frequency_mhz": 3500, "bandwidth_mhz": 50}


def config(waypoints: bool, check_ms: int, hyst: float = 0.0, a2=None) -> NSGSimulationConfig:
    """3 cells 500 m apart in a line; commuters starting on cell 1's side of the
    cell-1/cell-2 boundary and driving straight east across it at 40 m/s."""
    mob = {"pattern": "vehicle", "speed_mps": 40.0}
    if waypoints:
        mob["waypoints"] = [
            {"position": {"x": -250.0, "y": 0.0, "z": 1.5}, "speed_mps": 40.0},
            {"position": {"x": 350.0, "y": 0.0, "z": 1.5}, "speed_mps": 40.0},
        ]
        mob["waypoint_mode"] = "pingpong"
    return NSGSimulationConfig(**{
        "name": "2b cell-edge traverse",
        "base_stations": [
            {"id": f"bs{i}", "name": f"Cell {i}",
             "position": {"x": x, "y": -300.0, "z": 25.0},
             "radio": {"band": BAND, "tx_power_dbm": 44.0}}
            for i, x in enumerate((-500.0, 0.0, 500.0), start=1)
        ],
        "ue_groups": [{
            "id": "commuters", "name": "Commuters", "count": 6,
            "spawn_position": {"x": -300.0, "y": -300.0},
            "mobility": mob,
            "services": [{"service_type": "video_streaming", "slice_type": "eMBB",
                          "qos_id": 9, "target_throughput_gbps": 0.01}],
        }],
        "settings": {"duration_s": 120, "ho_check_period_ms": check_ms,
                     "ho_a3_entry": True, "ho_hysteresis_db": hyst,
                     "ho_a2_threshold_db": a2, "ho_sinr_margin_db": 3.0},
    })


VARIANTS = {
    "waypoints-loop":   dict(waypoints=True,  check_ms=100),
    "waypoints-noloop": dict(waypoints=True,  check_ms=0),
    "walk-loop":        dict(waypoints=False, check_ms=100),
    "hysteresis":       dict(waypoints=True,  check_ms=100, hyst=8.0),
    "a2":               dict(waypoints=True,  check_ms=100, a2=5.0),
}


def scenario_log() -> str:
    return subprocess.run(
        ["/mnt/c/Windows/System32/wsl.exe", "-d", "Ubuntu-2004-nsoran", "-e", "bash", "-lc",
         "docker exec nsg-engine cat /tmp/scn0.log"],
        capture_output=True, text=True).stdout


async def main() -> None:
    name = sys.argv[1]
    wall = float(sys.argv[2]) if len(sys.argv) > 2 else 900.0
    cfg = config(**VARIANTS[name])
    params = nsoran_params(cfg)
    t0 = time.monotonic()
    res = await run_batch_simulation(cfg, params, frames_target=600, wall_cap_s=wall)
    wall_used = time.monotonic() - t0

    scn = scenario_log()
    pts = {}
    for m in re.finditer(r"IMSI (\d+) is ([\d.]+):([\d.]+):[\d.]+ at time ([\d.]+)", scn):
        pts.setdefault(int(m.group(1)), []).append(
            (float(m.group(4)), float(m.group(2)), float(m.group(3))))
    horizon, nets = 0.0, []
    for tr in pts.values():
        tr.sort()
        if len(tr) < 2:
            continue
        horizon = max(horizon, tr[-1][0])
        nets.append(((tr[-1][1] - tr[0][1]) ** 2 + (tr[-1][2] - tr[0][2]) ** 2) ** 0.5)
    ho = re.findall(r"\[NSG-HO\] t=([\d.]+) imsi=(\d+) from=(\d+) to=(\d+) trigger=(\w+)", scn)
    wp = re.search(r"\[NSG\] waypoint mobility: (\d+) UE", scn)

    out = {
        "variant": name,
        "status": res["status"],
        "error": res.get("error"),
        "frames": res["n_frames"],
        "wall_s": round(wall_used, 1),
        "sim_horizon_s": round(horizon, 2),
        "sim_per_wall_pct": round(100 * horizon / wall_used, 3) if wall_used else 0,
        "mean_net_displacement_m": round(sum(nets) / len(nets), 1) if nets else 0.0,
        "waypoint_ues": int(wp.group(1)) if wp else 0,
        "ho_decisions": len(ho),
        "ho_by_trigger": {t: [x[4] for x in ho].count(t) for t in sorted({x[4] for x in ho})},
        "ho_imsis": sorted({int(x[1]) for x in ho}),
        "engine_handovers": (res["frames"][-1].get("handovers") if res["frames"] else None),
    }
    print(json.dumps(out, indent=2))
    with open(f"/tmp/2b_{name}.json", "w") as f:
        json.dump(out, f)


asyncio.run(main())
