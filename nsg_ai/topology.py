"""Procedural topology generation — hex, Manhattan street grid, or scattered.

This is VIAVI's `Scenario_Generation` block: say "7 cells, hexagonal, 350 m
apart, 2 UEs each" and get a complete, runnable config back instead of placing
every cell by hand.

It is built platform-side rather than on ns-3's LteHexGridEnbTopologyHelper on
purpose. That helper POSITIONS the eNodeBs itself, while this scenario takes
exact positions from a file — the two would fight, and the platform would lose
sight of a geometry it needs for the map view, the UE placement guards and the
scene-centroid translation.

The point of the generator is not the arithmetic; it is that everything it emits
lands inside the engine's validated envelope BY CONSTRUCTION. Those limits were
learned the hard way and are documented in engines/nsoran:

  * 3-7 cells (above 7 the fork zeroes PRB and throughput),
  * enough UEs to load the cells but not to bury the sim, and at least two per
    cell so every cell reports DU metrics,
  * UEs never on top of a base station (a zero-distance link is a log10(0)) and
    never piled on one cell (the engine answers with zero DU metrics or aborts),
  * buildings that do not overlap each other (overlapping footprints NS_FATAL
    the run) and do not swallow a UE spawn (the outdoor mobility model asserts
    every UE starts outdoors).

So a generated scenario is one that runs, which is a stronger promise than one
that validates.
"""
from __future__ import annotations

import math
import random
from typing import Literal, Optional

from pydantic import BaseModel, Field

from engine_envelope import GNB_MAX, GNB_MIN, UE_MAX

# The engine pins its LTE anchor at the centre of its own scene, and a cell too
# close to it kills the run (measured: 141 m clear is fine, 25 m is not). The
# platform re-centres the whole design later, so the generator only has to avoid
# putting a cell at the design's own centroid.
_HUB_CLEAR_M = 160.0
# A UE this close to a cell is effectively on top of it.
_UE_CELL_CLEAR_M = 25.0


class TopologyRequest(BaseModel):
    """What to generate. Every field is clamped into the engine's envelope."""
    layout: Literal["hex", "manhattan", "scatter"] = Field(
        "hex",
        description="'hex' is the 3GPP hexagonal layout — a centre site with a "
                    "ring around it. 'manhattan' places cells along a street "
                    "grid and fills the blocks with buildings, so it exercises "
                    "real LOS/NLOS. 'scatter' is random placement with a "
                    "minimum separation, for a deliberately irregular network."
    )
    cells: int = Field(
        7, ge=GNB_MIN, le=GNB_MAX,
        description=f"Number of cells ({GNB_MIN}-{GNB_MAX}). Above {GNB_MAX} the "
                    f"engine reports zero PRB and throughput."
    )
    isd_m: float = Field(
        350.0, ge=100.0, le=1500.0,
        description="Inter-site distance in metres — the spacing between "
                    "neighbouring cells, and for 'manhattan' the block size."
    )
    ues_per_cell: int = Field(
        2, ge=1, le=8,
        description="UEs generated per cell. At least 2 is recommended: a cell "
                    "with no UE reports no DU metrics, which reads as a broken "
                    "run rather than an idle cell."
    )
    ue_spread_m: Optional[float] = Field(
        None,
        description="How far UEs sit from their cell. null = a third of the "
                    "inter-site distance, which keeps them inside their own "
                    "cell without hugging the tower."
    )
    tx_power_dbm: float = Field(
        44.0, ge=0.0, le=50.0, description="Per-cell transmit power."
    )
    cell_height_m: float = Field(
        25.0, ge=3.0, le=100.0, description="Cell antenna height above ground."
    )
    ue_height_m: float = Field(
        1.5, ge=0.5, le=150.0, description="UE antenna height above ground."
    )
    frequency_mhz: float = Field(3500.0, ge=700.0, le=40000.0)
    bandwidth_mhz: float = Field(50.0, ge=10.0, le=100.0)
    dl_mbps_per_ue: float = Field(
        20.0, gt=0.0, le=200.0,
        description="Offered downlink per UE. Load the cells: an unloaded "
                    "network makes most levers unmeasurable."
    )
    building_height_m: float = Field(
        24.0, ge=3.0, le=200.0,
        description="Height of the generated blocks. 'manhattan' only."
    )
    seed: Optional[int] = Field(
        None, description="Fix the layout's randomness. Also set as the run's "
                          "random_seed, so a generated scenario is reproducible "
                          "end to end."
    )
    name: Optional[str] = Field(None, description="Scenario name.")


def _hex_sites(n: int, isd: float) -> list[tuple[float, float]]:
    """One centre site plus a ring — the standard 3GPP layout. Kept off the
    exact origin so the centre cell does not land on the engine's LTE anchor."""
    pts = [(0.0, _HUB_CLEAR_M)]
    ring = n - 1
    for i in range(ring):
        a = 2 * math.pi * i / ring
        pts.append((isd * math.cos(a), isd * math.sin(a) + _HUB_CLEAR_M))
    return pts[:n]


def _manhattan_sites(n: int, isd: float) -> list[tuple[float, float]]:
    """Cells on street intersections of a square grid, filled row by row."""
    cols = math.ceil(math.sqrt(n))
    pts = []
    for i in range(n):
        r, c = divmod(i, cols)
        pts.append((c * isd, r * isd + _HUB_CLEAR_M))
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    # Re-centre on the origin, then lift clear of the hub.
    return [(x - cx, y - cy + _HUB_CLEAR_M) for x, y in pts]


def _scatter_sites(n: int, isd: float, rng: random.Random) -> list[tuple[float, float]]:
    """Random placement, rejecting anything closer than 60% of the ISD to an
    existing site — irregular but never degenerate."""
    span, pts, guard = isd * 1.6, [], 0
    while len(pts) < n and guard < 4000:
        guard += 1
        p = (rng.uniform(-span, span), rng.uniform(-span, span) + _HUB_CLEAR_M)
        if math.hypot(p[0], p[1] - _HUB_CLEAR_M) < _HUB_CLEAR_M * 0.5:
            continue                      # keep the middle clear of the anchor
        if all(math.dist(p, q) >= isd * 0.6 for q in pts):
            pts.append(p)
    while len(pts) < n:                   # pathological ISD — fall back to hex
        pts = _hex_sites(n, isd)
    return pts


def _blocks(sites: list, isd: float, height: float) -> list[dict]:
    """City blocks between the streets: one square per gap between adjacent
    sites, inset so it never touches a cell or a UE ring. Non-overlapping by
    construction, because overlapping footprints abort the ns-3 run."""
    xs = sorted({round(x, 1) for x, _ in sites})
    ys = sorted({round(y, 1) for _, y in sites})
    inset = isd * 0.22
    out = []
    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            x0, x1 = xs[i] + inset, xs[i + 1] - inset
            y0, y1 = ys[j] + inset, ys[j + 1] - inset
            if x1 - x0 < 20.0 or y1 - y0 < 20.0:
                continue
            out.append({
                "id": f"blk-{i}-{j}",
                "bounds": {"x_min": round(x0, 1), "x_max": round(x1, 1),
                           "y_min": round(y0, 1), "y_max": round(y1, 1),
                           "z_min": 0.0, "z_max": round(height, 1)},
                "building_type": "office", "wall_material": "concrete",
                "floors": max(1, min(30, int(height // 3))),
                "rooms_x": 3, "rooms_y": 3,
            })
    return out[:12]


# NOTE ON UE PLACEMENT — why this file does not place individual UEs.
#
# A first version computed a careful ring per cell: bearings spread apart, radii
# jittered, snapped to street axes so nobody started inside a block. All of it
# was thrown away. The platform's own translation (main._ue_scenario_positions)
# treats a group's spawn_position as a CENTRE and re-scatters that group's UEs
# over a disc sized from the cell spacing, then runs its own guards — eviction
# from buildings, separation from base stations, and a balanced re-scatter if
# any one cell would draw too many UEs (that last one exists because ~a dozen
# UEs on one cell corrupts the mmWave heap).
#
# Those guards are better informed than the generator: they run AFTER the scene
# has been translated into the engine's fixed area, so they see the geometry the
# engine will actually get. The generator's real levers are therefore the SPAWN
# POINT and the COUNT, and the honest thing is to give one group per cell
# spawned ON that cell and let the platform fan them out. Fighting it produced a
# worse result, not a better one — measured, the hand-built rings left a cell
# with no UEs at all.


def _platform_modules():
    """Reach `main` and `schema` whatever name they were imported under.

    Deferred because `main` imports this module, and name-agnostic because the
    package is entered two different ways: the server runs `uvicorn main:app`
    from inside nsg_ai/, where the module is `main`, while the test harness and
    tools import `nsg_ai.main`. A hardcoded `from nsg_ai.main import ...` runs
    green under the tests and raises ModuleNotFoundError on the live server.
    """
    import sys
    mods = []
    for short, dotted in (("main", "nsg_ai.main"), ("schema", "nsg_ai.schema")):
        mod = sys.modules.get(short) or sys.modules.get(dotted)
        if mod is None:
            mod = __import__(dotted, fromlist=["*"]) if _has_pkg() \
                else __import__(short)
        mods.append(mod)
    return mods[0], mods[1]


def _has_pkg() -> bool:
    import importlib.util
    return importlib.util.find_spec("nsg_ai") is not None


def _starved_cells(cfg: dict) -> int:
    """How many cells would end up with no UE nearer to them than to any other.

    Checked through the platform's OWN translation rather than against the
    generator's coordinates, because the generator's coordinates are not what
    the engine receives. Anything less is a guess about someone else's code.

    A cell can be starved even with a group spawned on it: the platform's disc
    has radius 0.6 x cell spacing, which reaches past the halfway line to a
    neighbour (0.5 x spacing), so a UE can land closer to that neighbour. In a
    symmetric layout the leakage cancels out; in an irregular one a cell with
    several close neighbours can lose both of its UEs. That is not a fatal run,
    but a cell reporting no DU metrics reads as a broken engine, and telling
    those two cases apart has cost us real debugging time before.
    """
    main, schema = _platform_modules()
    p = main.nsoran_params(schema.NSGSimulationConfig(**cfg))
    def _pts(key):
        return [tuple(float(v) for v in s.split(",")[:2])
                for s in p[key].split(";") if s]
    ues, gnbs = _pts("ue_positions"), _pts("gnb_positions")
    if not ues or not gnbs:
        return len(gnbs)
    served = {min(range(len(gnbs)), key=lambda i: math.dist(u, gnbs[i]))
              for u in ues}
    return len(gnbs) - len(served)


def generate(req: TopologyRequest) -> dict:
    """Build a complete, runnable NSGSimulationConfig dict.

    'scatter' is re-rolled until no cell is left without a UE — see
    _starved_cells. The seed still fixes the result: the retries walk a
    deterministic sequence from it.
    """
    if req.layout != "scatter":
        return _build(req, random.Random(req.seed))

    best, best_starved = None, 10**9
    for attempt in range(12):
        seed = req.seed if req.seed is None else req.seed + attempt * 7919
        cfg = _build(req, random.Random(seed))
        starved = _starved_cells(cfg)
        if starved == 0:
            return cfg
        if starved < best_starved:
            best, best_starved = cfg, starved
    # Every roll left a cell idle — hand back the least bad one and say so,
    # rather than pretending the promise held.
    best["description"] += (f" — WARNING: {best_starved} cell(s) start with no "
                            f"UE attached; they will report no DU metrics")
    return best


def _build(req: TopologyRequest, rng: random.Random) -> dict:
    """One roll of the layout."""
    n = max(GNB_MIN, min(GNB_MAX, req.cells))
    isd = float(req.isd_m)

    if req.layout == "hex":
        sites = _hex_sites(n, isd)
    elif req.layout == "manhattan":
        sites = _manhattan_sites(n, isd)
    else:
        sites = _scatter_sites(n, isd, rng)

    blocks = _blocks(sites, isd, req.building_height_m) if req.layout == "manhattan" else []

    # Total UEs are capped by the engine, so shrink the per-cell count rather
    # than emit a config the platform will silently truncate.
    per_cell = max(1, min(req.ues_per_cell, UE_MAX // n))

    band = {"label": "generated", "frequency_mhz": req.frequency_mhz,
            "bandwidth_mhz": req.bandwidth_mhz, "numerology": 2}
    base_stations, ue_groups = [], []
    for i, s in enumerate(sites, start=1):
        base_stations.append({
            "id": f"cell-{i}", "name": f"Cell {i}",
            "position": {"x": round(s[0], 1), "y": round(s[1], 1),
                         "z": req.cell_height_m},
            "radio": {"band": dict(band), "tx_power_dbm": req.tx_power_dbm},
        })
        # One group per cell, spawned ON the cell. The platform fans each group
        # out from there and applies its own guards — see the note above.
        ue_groups.append({
            "id": f"ue-{i}", "name": f"UEs on cell {i}", "count": per_cell,
            "spawn_position": {"x": round(s[0], 1), "y": round(s[1], 1)},
            "height_m": req.ue_height_m,
            "mobility": {"pattern": "pedestrian", "speed_mps": 1.4},
            "services": [{"service_type": "video_streaming", "slice_type": "eMBB",
                          "qos_id": 9,
                          "target_throughput_gbps": round(req.dl_mbps_per_ue / 1000.0, 6)}],
        })

    span = max(isd * (2.2 if req.layout != "manhattan" else 1.6), 400.0)
    return {
        "name": req.name or f"{req.layout} · {n} cells · {int(isd)} m ISD",
        "description": (f"Generated {req.layout} topology: {n} cells at {int(isd)} m, "
                        f"{per_cell} UE(s) each"
                        + (f", {len(blocks)} city blocks" if blocks else "")),
        "base_stations": base_stations,
        "ue_groups": ue_groups,
        "buildings": blocks,
        "area": {"x_min": -span, "x_max": span, "y_min": -span + _HUB_CLEAR_M,
                 "y_max": span + _HUB_CLEAR_M, "resolution_m": 40.0},
        "settings": {
            "duration_s": 120,
            "scenario": "UMi" if req.layout == "manhattan" else "UMa",
            "shadowing_enabled": True,
            # A generated scenario should be reproducible end to end, not just
            # in its layout.
            **({"random_seed": int(req.seed)} if req.seed is not None else {}),
        },
    }
