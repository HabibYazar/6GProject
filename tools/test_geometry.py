"""Guard the geometry the engine cannot survive: a UE sitting on a base station."""
import math
import sys

sys.path.insert(0, "/home/chancelor/nsg_ai_platform/nsg_ai")
from main import (NSGSimulationConfig, nsoran_params, _separate_ues,
                  _MIN_UE_BS_M, _SCEN_CENTER)


def cell(i, x, y):
    return {"id": i, "name": i, "position": {"x": x, "y": y, "z": 25},
            "radio": {"band": {"label": "NR", "frequency_mhz": 3500,
                               "bandwidth_mhz": 50, "numerology": 1},
                      "tx_power_dbm": 44,
                      "antenna": {"type": "cosine", "azimuth_deg": 0, "tilt_deg": 3,
                                  "gain_dbi": 17, "horizontal_beamwidth_deg": 65,
                                  "vertical_beamwidth_deg": 7.5}}}


def cfg(cells, spawn, n=10):
    return NSGSimulationConfig(**{
        "name": "t", "description": "",
        "base_stations": [cell(f"g{i}", x, y) for i, (x, y) in enumerate(cells)],
        "ue_groups": [{"id": "u1", "name": "u1", "count": n,
                       "spawn_position": {"x": spawn[0], "y": spawn[1]},
                       "mobility": {"pattern": "pedestrian", "speed_mps": 1.4},
                       "services": [{"slice_type": "eMBB", "qos_id": 9,
                                     "target_throughput_gbps": 0.01}]}],
        "buildings": [],
        "area": {"x_min": -400, "x_max": 900, "y_min": -400, "y_max": 900,
                 "resolution_m": 40},
        "settings": {"duration_s": 20, "report_interval_ms": 1000, "scenario": "UMa",
                     "shadowing_enabled": True, "fast_fading_enabled": False,
                     "random_seed": 42},
    })


def parse(s):
    """Positions as GROUND coordinates, x,y only.

    The engine writes UEs as `x,y,z` and the scenario's LTE hub is a bare pair,
    so comparing them raw raised "both points must have the same number of
    dimensions" — this file has not run since 2026-07-15 and nobody noticed,
    because nothing ran it.

    Two dimensions is also the RIGHT comparison, not just the compatible one:
    the check is "is a UE sitting on a base station", and every UE is at 1.5 m
    while every mast is at 25 m by construction. Including z would put a floor of
    23.5 m under every pair and the test would pass on geometry it exists to
    reject.
    """
    out = []
    for point in s.split(";"):
        if not point:
            continue
        vals = [float(v) for v in point.split(",")]
        out.append((vals[0], vals[1]))
    return out


def min_sep(ue_s, gnb_s):
    ues, gnbs = parse(ue_s), parse(gnb_s) + [(_SCEN_CENTER, _SCEN_CENTER)]
    return min(math.dist(u, g) for u in ues for g in gnbs)


fails = 0

# The unit itself: a UE exactly on a base station, and one 1 m away.
got = _separate_ues([(2000.0, 2000.0), (2001.0, 2000.0)], [(2000.0, 2000.0)])
for p in got:
    d = math.dist(p, (2000.0, 2000.0))
    ok = d >= _MIN_UE_BS_M - 1e-6
    fails += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] co-located UE pushed to {d:.1f} m (min {_MIN_UE_BS_M})")

# The real shape that killed the engine: 4 corners + a centre cell. The scene
# centroid lands on the centre cell, which the engine then pins on its LTE hub.
cases = [
    ("5 cells, centre cell on the UE spawn",
     [(0, 0), (500, 0), (0, 500), (500, 500), (250, 250)], (250, 250)),
    ("UE spawn exactly on a corner cell",
     [(0, 0), (500, 0), (0, 500), (500, 500)], (0, 0)),
    ("3 cells, spawn between them (was already fine)",
     [(0, 0), (400, 0), (0, 400)], (200, 200)),
]
for name, cells, spawn in cases:
    p = nsoran_params(cfg(cells, spawn))
    d = min_sep(p["ue_positions"], p["gnb_positions"])
    ok = d >= _MIN_UE_BS_M - 1e-6
    fails += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:44s} min UE↔BS = {d:.1f} m")

# No cell may sit on the engine's LTE anchor at the scene centre.
from main import _MIN_BS_HUB_M

print()
for name, cells, spawn in cases:
    p = nsoran_params(cfg(cells, spawn))
    gnbs = parse(p["gnb_positions"])
    d = min(math.dist(g, (_SCEN_CENTER, _SCEN_CENTER)) for g in gnbs)
    ok = d >= _MIN_BS_HUB_M - 1e-6
    fails += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:44s} min cell↔LTE hub = {d:.1f} m")

# The nudge must be a RIGID translation: every pairwise distance in the design
# has to survive it, or we would be silently simulating a different network.
print()
cells = [(0, 0), (500, 0), (0, 500), (500, 500), (250, 250)]
p = nsoran_params(cfg(cells, (250, 250)))
gnbs = parse(p["gnb_positions"])
worst = max(
    abs(math.dist(gnbs[i], gnbs[j]) - math.dist(cells[i], cells[j]))
    for i in range(len(cells)) for j in range(i + 1, len(cells))
)
ok = worst < 0.2      # only the 0.1 m formatting round-trip
fails += not ok
print(f"  [{'PASS' if ok else 'FAIL'}] shifted scene keeps every cell↔cell distance "
      f"(worst drift {worst:.3f} m)")

# No UE may START inside a building: the engine's RandomWalk2dOutdoorMobilityModel
# asserts on it and takes the whole run down at t=0.
from main import _building_rects, _scene_centroid, _clear_hub_centroid

print()
bcfg = cfg([(0, 0), (500, 0), (0, 500), (500, 500), (250, 250)], (250, 250))
bcfg.buildings = [__import__("schema").Building(**{
    "id": f"b{i}",
    "bounds": {"x_min": x, "x_max": x + 60, "y_min": y, "y_max": y + 60,
               "z_min": 0, "z_max": 30},
    "building_type": "office", "wall_material": "concrete",
    "floors": 8, "rooms_x": 4, "rooms_y": 4})
    for i, (x, y) in enumerate([(120, 120), (330, 120), (120, 330), (330, 330),
                                (220, 220)])]   # last one sits ON the UE spawn
p = nsoran_params(bcfg)
gnb_cfg = [(b.position.x, b.position.y) for b in bcfg.base_stations]
c = _clear_hub_centroid(gnb_cfg, *_scene_centroid(
    gnb_cfg + [(g.spawn_position.x, g.spawn_position.y) for g in bcfg.ue_groups]))
rects = _building_rects(bcfg.buildings, *c)
indoors = [u for u in parse(p["ue_positions"])
           if any(x0 <= u[0] <= x1 and y0 <= u[1] <= y1
                  for (x0, x1, y0, y1, *_) in rects)]
ok = not indoors
fails += not ok
print(f"  [{'PASS' if ok else 'FAIL'}] {'UEs evicted from buildings':44s} "
      f"{len(indoors)} of {p['n_ue']} still indoors  ({len(rects)} buildings, "
      f"one on the spawn)")

print("\nRESULT:", "ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
