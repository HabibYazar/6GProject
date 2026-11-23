#!/usr/bin/env python3
"""How much SIMULATED TIME does a batch run actually observe?

The envelope used to answer this with a constant (`UE_PRACTICAL_MAX`) and a
fitted power law, and both were wrong for the same reason: every span they were
fitted to came through the xApp's hardcoded five-second KPM collection window,
so they described the window rather than the engine. nsg-engine:1.12 sizes that
window from the UE count, which changes the answer's shape entirely — so the
answer has to be re-measured rather than re-derived, and this is what measures it.

It builds ONE scenario and stretches it, so a cell count is not confounded with a
different topology: cells sit on the same 400 m grid, each gets its own UE group
spawned 40 m off the mast, and everything else comes from the same full platform
config a real run would send (`--base`).

    python3 tools/horizon_sweep.py                 # the grid behind the envelope
    python3 tools/horizon_sweep.py --cells 6 --ue 30 90 150
    python3 tools/horizon_sweep.py --out tools/horizon.1_12.json

Reads `energy.span_s`, which is the last simulated second the radio energy model
logged — i.e. how far the run got. Also records the KPM name count, because the
two move in OPPOSITE directions and only quoting the first would misrepresent a
dense scenario as a poorer measurement than a sparse one.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "nsg_ai"))

from nsg_ai.schema import NSGSimulationConfig       # noqa: E402
from nsg_ai.main import nsoran_params               # noqa: E402

DEFAULT_ENGINE = "http://127.0.0.1:8090/run"
CELL_PITCH_M = 400.0
# (cells, [total UE counts]) — the UE counts stay inside the bandwidth rule at
# 50 MHz (25 UEs/cell) and inside UE_MAX, so nothing here is clamped silently.
DEFAULT_GRID = [(3, [6, 18, 45, 75]),
                (6, [12, 30, 60, 90, 150]),
                (12, [24, 60, 120])]


def stretch(base: dict, n_cell: int, n_ue: int) -> dict:
    """`base` rebuilt with `n_cell` cells on the grid and `n_ue` UEs spread evenly."""
    cfg = json.loads(json.dumps(base))
    bs0, ug0 = cfg["base_stations"][0], cfg["ue_groups"][0]
    cells, groups = [], []
    for i in range(n_cell):
        x, y = (i % 3) * CELL_PITCH_M, (i // 3) * CELL_PITCH_M
        b = json.loads(json.dumps(bs0))
        b["id"], b["name"] = f"gnb-{i}", f"C{i}"
        b["position"] = {"x": x, "y": y, "z": bs0["position"].get("z", 25.0)}
        cells.append(b)
        g = json.loads(json.dumps(ug0))
        g["id"], g["name"] = f"g{i}", f"G{i}"
        g["spawn_position"] = {"x": x + 40.0, "y": y + 40.0}
        g["count"] = max(1, n_ue // n_cell)
        groups.append(g)
    cfg["base_stations"], cfg["ue_groups"] = cells, groups
    rows = (n_cell + 2) // 3
    cfg["area"] = {"x_min": -300.0, "x_max": 2 * CELL_PITCH_M + 300.0,
                   "y_min": -300.0, "y_max": rows * CELL_PITCH_M + 300.0,
                   "resolution_m": 25.0}
    return cfg


def run_once(engine: str, params: dict, retries: int = 10) -> tuple[dict, float]:
    """POST one run, waiting out a busy engine. Returns (response, wall seconds)."""
    t0 = time.time()
    d: dict = {}
    for _ in range(retries):
        req = urllib.request.Request(
            engine, data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=1800) as r:
                d = json.loads(r.read().decode())
        except Exception as exc:                     # noqa: BLE001 — report, don't raise
            d = {"status": f"ERR {type(exc).__name__}: {exc}"}
        if d.get("status") != "busy":
            break
        time.sleep(25)
    return d, time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.path.join(ROOT, "tools", "horizon_base.json"),
                    help="platform config to stretch (full product config, 50 MHz)")
    ap.add_argument("--engine", default=DEFAULT_ENGINE)
    ap.add_argument("--cells", type=int, help="one cell count instead of the grid")
    ap.add_argument("--ue", type=int, nargs="+", help="UE counts for --cells")
    ap.add_argument("--sim-time", type=int, default=60,
                    help="ns-3 stop time; only a ceiling, the window always binds first")
    ap.add_argument("--out", help="write the rows as JSON")
    args = ap.parse_args()

    base = json.load(open(args.base, encoding="utf-8"))
    grid = [(args.cells, args.ue)] if args.cells and args.ue else DEFAULT_GRID

    rows = []
    print(f"{'cells':>5} {'UEs':>5} {'/cell':>5} {'status':>8} {'span_s':>7} "
          f"{'wall_s':>7} {'names':>6} {'ind':>4} {'rc':>6}", flush=True)
    print("-" * 64, flush=True)
    for n_cell, ue_counts in grid:
        for n_ue in ue_counts:
            p = nsoran_params(NSGSimulationConfig(**stretch(base, n_cell, n_ue)))
            p["sim_time"], p["rng_run"] = args.sim_time, 1
            d, wall = run_once(args.engine, p)
            meas = d.get("measurements") or {}
            span = (d.get("energy") or {}).get("span_s")
            rc = (d.get("rc_control") or {}).get("closed_loop")
            # p["n_ue"] is the CLAMPED count the engine really ran — the request
            # can be narrowed by the bandwidth rule, and reporting the request
            # would attribute one scenario's horizon to a different one.
            rows.append({"n_gnb": n_cell, "n_ue": p["n_ue"], "status": d.get("status"),
                         "span_s": span, "wall_s": round(wall, 1), "names": len(meas),
                         "kpm_indications": d.get("kpm_indications"), "closed_loop": rc})
            print(f"{n_cell:>5} {p['n_ue']:>5} {p['n_ue'] // n_cell:>5} "
                  f"{str(d.get('status')):>8} {str(span):>7} {round(wall, 1):>7} "
                  f"{len(meas):>6} {str(d.get('kpm_indications')):>4} {str(rc):>6}",
                  flush=True)

    spans = [r["span_s"] for r in rows if r["span_s"]]
    if spans:
        print(f"\nsimulated horizon: {min(spans)} .. {max(spans)} s over "
              f"{len(rows)} points", flush=True)
        if max(spans) < 1.0:
            print("every point is SUB-SECOND — a batch run is a snapshot at every "
                  "size, so time-evolving scenarios belong on the live path.",
                  flush=True)
    if args.out:
        json.dump(rows, open(args.out, "w", encoding="utf-8"), indent=2)
        print("wrote", args.out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
