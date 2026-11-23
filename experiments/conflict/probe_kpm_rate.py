"""
The over-sampling factor, measured without confusing it with constancy.

probe_kpm_freshness.py measures how often a field's VALUE changes. That conflates
two different things: a frame repeated because the platform emits nine per
indication round (real redundancy), and a value that simply did not change
because nothing happened (correct). Serving cell changed on 2.1% of frame pairs
in that run — handovers are rare, so that is the truth, not 47x over-sampling.

This measures the redundancy directly instead. Every UE row now carries `sim_t`,
the simulated time of the [NSG-POS] line it came from (0.1 s granularity). Frames
sharing a sim_t are redundant by construction, whatever the values happen to be:

  frames / distinct sim_t          -> over-sampling vs the 10 Hz log-tail sources
  frames / simulated second        -> over-sampling vs the ~1 Hz E2 KPM round

Those two numbers are what an experiment needs to convert a frame count into an
honest sample count.
"""
from __future__ import annotations

import statistics
import sys
from collections import Counter

from harness import run
from scenario import build

TOTAL = 90


def main() -> int:
    cfg = build(seed=1)

    def controller(kpm: dict):
        return []

    print(f"collecting {TOTAL} frames…")
    log = run(cfg, controller, max_ticks=TOTAL)
    if not log.ok:
        print(f"FAILED: {log.error}")
        return 1

    # sim_t is attached to every UE row by the [NSG-POS] merge; one value per frame
    per_frame_t: list[float | None] = []
    for t in log.ticks:
        ts = {u.get("sim_t") for u in t.ues if u.get("sim_t") is not None}
        per_frame_t.append(max(ts) if ts else None)

    known = [t for t in per_frame_t if t is not None]
    if not known:
        print("INCONCLUSIVE: no sim_t on any frame — is the [NSG-POS] merge live?")
        return 1

    counts = Counter(known)
    distinct = len(counts)
    span = max(known) - min(known)

    print(f"\nframes                 {len(log.ticks)}")
    print(f"  with a sim_t         {len(known)}")
    print(f"distinct sim_t values  {distinct}")
    print(f"simulated span         {span:.1f} s")

    print(f"\nOVER-SAMPLING")
    print(f"  frames per sim_t     {len(known)/distinct:.1f}x   "
          f"(vs the 10 Hz [NSG-POS]/[NSG-ENERGY] tail)")
    if span > 0:
        per_sec = len(known) / span
        print(f"  frames per sim second {per_sec:.1f}x   "
              f"(vs the ~1 Hz E2 KPM round)")

    hist = Counter(counts.values())
    print(f"\n  how many frames share one sim_t:")
    for n in sorted(hist):
        print(f"    {n:>2} frame(s): {'#' * min(40, hist[n])} ({hist[n]})")
    print(f"  median {statistics.median(counts.values()):.0f}, max {max(counts.values())}")

    # The platform now labels which frames actually carried a KPM report. If the
    # label is right, the fresh count should land near frames / oversampling.
    labelled = [t for t in log.ticks if t.kpm_fresh is not None]
    if labelled:
        fresh = [t for t in labelled if t.kpm_fresh]
        print(f"\nkpm_fresh LABEL (platform-supplied)")
        print(f"  labelled frames      {len(labelled)}")
        print(f"  carrying a report    {len(fresh)}  ({len(fresh)/len(labelled):.1%})")
        if fresh:
            print(f"  implied sample rate  1 in {len(labelled)/len(fresh):.1f} frames")
        fresh_t = {t.sim_t for t in fresh if t.sim_t is not None}
        print(f"  distinct sim_t among fresh frames: {len(fresh_t)}")
        # The number that actually matters: per cell, how many reports arrived.
        per_cell: dict[int, int] = {}
        for t in fresh:
            for c in t.kpm_fresh_cells:
                per_cell[c] = per_cell.get(c, 0) + 1
        if per_cell:
            print("  reports per CELL (this is the honest sample count):")
            for c in sorted(per_cell):
                print(f"    cell {c}: {per_cell[c]} reports in {len(log.ticks)} frames"
                      f"  -> {len(log.ticks)/per_cell[c]:.1f}x over-sampled")
    else:
        print("\nkpm_fresh: not present — restart the backend to pick up the "
              "nsoran_live change")

    print("\nWhat to do with it: an experiment treating each frame as an\n"
          "independent sample overstates n by the first factor for position and\n"
          "energy, and by the second for anything that comes from the E2 KPM\n"
          "report (throughput, PRB, SINR, delay). Filter on kpm_fresh, or group\n"
          "by sim_t, instead of counting ticks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
