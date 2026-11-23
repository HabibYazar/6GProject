"""
One short engine session, reported as a single JSON line.

Part of the slowdown diagnosis: the accumulator that matters increments once per
session regardless of how long the session runs, so the cheap probe is many short
sessions rather than a reproduction of the original forty-session curve. This
script is deliberately only the session half — the container sampling lives on
the other side of the WSL distro boundary and is driven by the caller, which can
reach both distros.

Usage:  python3 probe_slowdown.py <session-number>
"""
from __future__ import annotations

import json
import statistics
import sys

from harness import run
from scenario import build

TICKS = 10          # enough for a gap distribution, short enough to be cheap


def main() -> int:
    n = sys.argv[1] if len(sys.argv) > 1 else "?"
    log = run(build(seed=1), None, max_ticks=TICKS)
    w = [t.wall_s for t in log.ticks]
    g = [round(b - a, 2) for a, b in zip(w, w[1:])]
    big = [x for x in g if x > 1.0]
    print(json.dumps({
        "session": n,
        "ticks": len(log.ticks),
        "wall_s": log.wall_s,
        "boot_s": round(log.boot_s, 1) if log.boot_s else None,
        "gap_max": max(big) if big else 0.0,
        "gap_median": round(statistics.median(big), 2) if big else 0.0,
        "n_gaps": len(big),
        "error": log.error,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
