"""
Pre-check 3 — does the conflict actually happen, and does it oscillate?

The offline read of a real run said it should: cell 4 sits at ~24% PRB (eSave's
trigger) while its worst UE sits at ~-1 dB SINR (maxTP's trigger), so both xApps
have standing to write it. That is a prediction from recorded data, not a
demonstration. This runs arm D (both xApps, last-writer-wins) live and checks
that the contested cell is really contested and that the commanded power moves
back and forth rather than settling.

If the requests never collide, every policy arm collapses onto the same
behaviour and the study has nothing to compare.
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path
import json

from harness import run
from scenario import build
from xapps import make, BASE_DBM

TOTAL = 50
ARM = "D_both_P0"
OUT = Path(__file__).parent / "out" / "check3.json"


def main() -> int:
    cfg = build(seed=1)
    ctrl = make(ARM)
    print(f"arm {ARM}: eSave + maxTP, policy {ctrl.policy}, {TOTAL} indications…")

    def show(t):
        if t.applied:
            acts = " ".join(f"c{c['cell']}->{c['dbm']}" for c in t.applied)
            print(f"  tick {t.tick:>3} {t.wall_s:>7.1f}s  contested="
                  f"{t.note.get('contested_cells', 0)}  {acts}", flush=True)

    log = run(cfg, ctrl, max_ticks=TOTAL, on_tick=show)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({
        "arm": ARM, "error": log.error, "wall_s": log.wall_s,
        "ticks": [{"tick": t.tick, "wall_s": t.wall_s, "energy_w": t.energy_w,
                   "applied": t.applied, "note": t.note,
                   "cells": t.cells, "ues": t.ues} for t in log.ticks],
    }, indent=1))
    print(f"\nraw -> {OUT}")
    if not log.ok:
        print(f"FAILED: {log.error}")
        return 1

    reqs = [r for t in log.ticks for r in t.requests]
    n_esave = sum(1 for r in reqs if r["xapp"] == "eSave")
    n_maxtp = sum(1 for r in reqs if r["xapp"] == "maxTP")
    contested_ticks = [t for t in log.ticks if t.note.get("contested_cells")]
    contested_cells = sorted({o["cell"] for t in log.ticks
                              for o in t.note.get("outcomes", []) if o["n_requests"] > 1})

    print(f"\n[requests] eSave {n_esave}, maxTP {n_maxtp}, total {len(reqs)}")
    print(f"[conflict] {len(contested_ticks)} of {len(log.ticks)} indications had a "
          f"contested cell; cells {contested_cells or '—'}")

    # The oscillation signal: the sequence of commanded values per cell.
    traj: dict[int, list[int]] = {}
    for t in log.ticks:
        for c in t.applied:
            traj.setdefault(int(c["cell"]), []).append(int(c["dbm"]))
    print("\n[commanded TX power per cell]")
    for cell, vals in sorted(traj.items()):
        flips = sum(1 for a, b, c in zip(vals, vals[1:], vals[2:])
                    if (b - a) * (c - b) < 0)
        var = statistics.pvariance(vals) if len(vals) > 1 else 0.0
        print(f"  cell {cell}: {len(vals)} writes  range {min(vals)}-{max(vals)} dBm  "
              f"variance {var:.2f}  direction-flips {flips}")
        print(f"           {vals}")

    ok = bool(contested_cells)
    print("\nVERDICT: " + (
        f"CONFLICT REPRODUCES on cell(s) {contested_cells} — the arms will differ"
        if ok else
        "NO CONFLICT — thresholds never collided, the policy arms would be identical"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
