"""
The S1 run matrix — 7 arms x 3 seeds, sequential, resumable.

Each run stops on a CONDITION rather than a fixed indication count: it ends once
the controller has had MEASURE_TARGET settled observations. The attach ramp is
not the same length from seed to seed, so a fixed count would quietly give some
arms more control intervals than others, and the oscillation metric would then
partly measure run length.

Everything the paper's section 6 asks for is derived here from the settled window
only. The ramp is excluded on purpose: while UEs are still attaching, PRB and
SINR describe a network being built, and averaging over that would contaminate
both the reported metrics and any claim about what the thresholds saw.

Resumable by design — a run whose JSON already exists is skipped, so an
interrupted night continues rather than restarting.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

from harness import run
from scenario import build
from xapps import (ARMS_S1, ARMS_S2, Controller, ControllerS2,
                   direction_flips, BASE_DBM)

# S1 drives a continuous actuator (TX power in dBm); S2 drives a discrete one
# (the cell is on or off, recorded as 1/0). The metrics below read a commanded
# LEVEL either way, so the same oscillation machinery applies to both.
SCENARIO = "s2" if "--s2" in sys.argv else "s1"
ARMS = ARMS_S2 if SCENARIO == "s2" else ARMS_S1
MAKE = ControllerS2 if SCENARIO == "s2" else Controller

# Site spacing multiplier. At 1.0 the three cells overlap so heavily that the
# deployment is interference-limited — switching a cell off improved BOTH energy
# and throughput in 3 of 3 seeds, so there was no tension for a conflict policy
# to arbitrate. At 1.8 a measured probe put the cost of switching one cell off at
# -17.8% throughput, comfortably past the 6.9% noise floor. Runs at different
# spreads are NOT comparable and are kept in separate directories.
SPREAD = next((float(a.split("=", 1)[1]) for a in sys.argv
               if a.startswith("--spread=")), 1.0)

MEASURE_TARGET = 20       # settled control intervals per run
MAX_TICKS = 140           # hard cap so a stalled run cannot eat the night
EXPECTED_UES = 9
SEEDS = [1, 2, 3]
COOLDOWN_S = 20           # the engine runs one live session at a time
_base = "matrix" if SCENARIO == "s1" else f"matrix_{SCENARIO}"
OUT = Path(__file__).parent / "out" / (
    _base if SPREAD == 1.0 else f"{_base}_spread{SPREAD:g}".replace(".", ""))


def percentile(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def metrics(log, ctrl: Controller) -> dict:
    """Section 6's numbers, over the settled window only."""
    settled = [t for t in log.ticks if t.note.get("phase") in ("measure", "stale")]
    # A stale tick carries no new information but its KPM is still a valid
    # sample of the settled network, so it counts for averages and not for
    # control-interval counts.
    watts = [t.energy_w for t in settled if t.energy_w is not None]
    dl = [sum(u.get("thp_kbps") or 0 for u in t.ues) / 1000 for t in settled]
    per_ue = [(u.get("thp_kbps") or 0) / 1000 for t in settled for u in t.ues]

    traj: dict[int, list[int]] = {}
    for t in log.ticks:
        for c in t.applied:
            level = c.get("dbm")
            if level is None:                       # S2: on/off as 1/0
                level = 1 if c.get("cmd") == "cell_on" else 0
            traj.setdefault(int(c["cell"]), []).append(int(level))

    acted = [t for t in log.ticks if t.note.get("phase") == "measure"]
    contested = sum(1 for t in acted if t.note.get("contested_cells"))
    reqs = [r for t in acted for r in t.requests]
    outcomes = [o for t in acted for o in t.note.get("outcomes", [])]

    js = []
    for t in log.ticks:
        pc = t.per_cell_w
        if pc:
            js.append(sum(pc.values()))

    return {
        "mean_network_w": round(statistics.mean(watts), 2) if watts else None,
        "dl_mbps_mean": round(statistics.mean(dl), 2) if dl else None,
        "ue_thp_p5_mbps": round(percentile(per_ue, 5), 3) if per_ue else None,
        "ue_thp_median_mbps": round(statistics.median(per_ue), 3) if per_ue else None,
        "settled_ticks": len(settled),
        "control_intervals": ctrl.measure_count,
        "ramp_ticks": ctrl.ramp_ticks,
        "contested_intervals": contested,
        "requests_esave": sum(1 for r in reqs if r["xapp"] == "eSave"),
        "requests_maxtp": sum(1 for r in reqs if r["xapp"] == "maxTP"),
        "writes_total": sum(len(v) for v in traj.values()),
        "suppressed": sum(o["n_requests"] - 1 for o in outcomes
                          if o["how"] == "blocked"),
        "rolled_back": sum(1 for o in outcomes if o["how"] == "rolledback"),
        "synthesised": sum(1 for o in outcomes if o["how"] == "synthesised"),
        "power_var_by_cell": {str(c): round(statistics.pvariance(v), 3)
                              for c, v in traj.items() if len(v) > 1},
        "power_flips_by_cell": {str(c): direction_flips(v) for c, v in traj.items()},
        "power_range_by_cell": {str(c): [min(v), max(v)] for c, v in traj.items()},
        "rail_hits": sum(1 for v in traj.values() for x in v if x in (20, 46)),
        "wall_s": log.wall_s,
        "boot_s": round(log.boot_s, 1) if log.boot_s else None,
    }


def one(arm: str, seed: int) -> dict | None:
    path = OUT / f"{arm}-s{seed}.json"
    if path.exists():
        print(f"  skip {arm} seed {seed} (already done)")
        return json.loads(path.read_text())["metrics"]

    cfg = build(seed=seed, spread=SPREAD)
    ctrl = MAKE(arm=arm, expected_ues=EXPECTED_UES, **ARMS[arm])
    t0 = time.time()
    log = run(cfg, ctrl, max_ticks=MAX_TICKS,
              stop=lambda: ctrl.measure_count >= MEASURE_TARGET)

    if not log.ok:
        print(f"  FAILED {arm} seed {seed}: {log.error}")
        (OUT / f"{arm}-s{seed}.FAILED.json").write_text(json.dumps(
            {"arm": arm, "seed": seed, "error": log.error,
             "ticks": len(log.ticks), "status": log.status_lines[-5:]}, indent=1))
        return None

    m = metrics(log, ctrl)
    path.write_text(json.dumps({
        "arm": arm, "seed": seed, "base_dbm": BASE_DBM, "metrics": m,
        "ticks": [{"tick": t.tick, "wall_s": t.wall_s, "energy_w": t.energy_w,
                   "per_cell_w": t.per_cell_w, "cells": t.cells, "ues": t.ues,
                   "applied": t.applied, "note": t.note} for t in log.ticks],
    }, indent=1))
    print(f"  {arm} seed {seed}: {time.time() - t0:.0f}s  "
          f"{m['control_intervals']} intervals, {m['contested_intervals']} contested, "
          f"{m['mean_network_w']} W, {m['dl_mbps_mean']} Mbps")
    return m


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    arms = [a for a in sys.argv[1:] if not a.startswith("--")] or list(ARMS)
    plan = [(a, s) for s in SEEDS for a in arms]
    print(f"{SCENARIO.upper()} matrix: {len(arms)} arms x {len(SEEDS)} seeds = "
          f"{len(plan)} runs, {MEASURE_TARGET} control intervals each, "
          f"base {BASE_DBM} dBm")

    rows, t0 = [], time.time()
    for i, (arm, seed) in enumerate(plan, 1):
        print(f"[{i}/{len(plan)}] {arm} seed {seed}  "
              f"(elapsed {(time.time() - t0) / 60:.0f} min)", flush=True)
        m = one(arm, seed)
        if m:
            rows.append({"arm": arm, "seed": seed, **m})
        time.sleep(COOLDOWN_S)

    if rows:
        flat = [k for k in rows[0] if not isinstance(rows[0][k], dict)]
        csv = [",".join(flat)]
        csv += [",".join("" if r.get(k) is None else str(r.get(k)) for k in flat)
                for r in rows]
        (OUT / "summary.csv").write_text("\n".join(csv))
        (OUT / "summary.json").write_text(json.dumps(rows, indent=1))
        print(f"\n{len(rows)}/{len(plan)} runs OK in {(time.time() - t0) / 60:.0f} min")
        print(f"-> {OUT / 'summary.csv'}")
    return 0 if len(rows) == len(plan) else 1


if __name__ == "__main__":
    sys.exit(main())
