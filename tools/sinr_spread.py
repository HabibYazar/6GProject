#!/usr/bin/env python3
"""The engine's OWN per-transmission SINR spread, out of its own histogram.

`window_model.FADING_SIGMA_DB` cites "5.2 to 8.1 dB measured across five
breadth runs" and nothing in the repo computed it — the number was carried in
a comment. This computes it, so the claim can be re-checked whenever the
stored frames change and so a spread can be FED to a model rather than
guessed at.

The engine counts every transmission into `L1M.RS-SINR.Bin*`, whose edges
`mmwave-phy-trace.h` documents as <-6, -6..0, 0..6, 6..12, 12..18, 18..24,
>24 dB — seven bins, and seven Bin names appear in the frames. The two open
bins are given the centre of a 6 dB-wide bin, which is the one assumption
here and which biases the spread DOWNWARD if the tails are long.

** AND THE BIAS IS NOT SMALL — MEASURED 2026-09-21. ** The engine's
per-transmission trace (`tools/rx_trace.py`) puts the SINR between -21.6
and +78.1 dB with a mean of 44.4, so nearly the whole population falls in
the single ">24" bin and this tool reports roughly the width of that bin
whatever the channel does. It gave 2.89-8.13 dB where the trace gives a
within-UE spread of 12.68 dB. Use `rx_trace.py` when the engine can be run;
this remains useful only for frames already on disk, and then as a LOWER
BOUND rather than a measurement.

    python3 tools/sinr_spread.py
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "nsg_ai"))
sys.path.insert(0, HERE)

BINS = [("L1M.RS-SINR.Bin34", -9.0), ("L1M.RS-SINR.Bin46", -3.0),
        ("L1M.RS-SINR.Bin58", 3.0), ("L1M.RS-SINR.Bin70", 9.0),
        ("L1M.RS-SINR.Bin82", 15.0), ("L1M.RS-SINR.Bin94", 21.0),
        ("L1M.RS-SINR.Bin127", 27.0)]


def spread_of(frames: list) -> tuple[float, float, float] | None:
    """(mean dB, sigma dB, samples) over every transmission in these frames."""
    tot: dict[float, float] = {}
    for f in frames:
        m = f.get("measurements") or {}
        for name, centre in BINS:
            v = m.get(name)
            if v and v.get("mean") is not None:
                tot[centre] = tot.get(centre, 0.0) + v["mean"] * v.get("count", 1)
    if len(tot) < 3:
        return None
    n = sum(tot.values())
    if n <= 0:
        return None
    mu = sum(c * w for c, w in tot.items()) / n
    var = sum(w * (c - mu) ** 2 for c, w in tot.items()) / n
    return mu, math.sqrt(var), n


def main() -> int:
    import tier_criterion as tc
    path = os.path.join(HERE, "tier_breadth.json")
    if not os.path.exists(path):
        print("no tier_breadth.json — run tools/tier_breadth.py first")
        return 1
    d = json.load(open(path))
    print(f"{'case':14}{'mean dB':>9}{'sigma dB':>10}{'samples':>12}"
          f"{'its BLER %':>12}")
    sigmas = []
    for case, row in d.items():
        fa = row.get("frames_a")
        if not fa:
            continue
        got = spread_of(fa)
        if not got:
            print(f"{case:14}  no histogram in the stored frames")
            continue
        mu, sig, n = got
        bler = tc.harvest(fa).get("block error rate")
        sigmas.append(sig)
        print(f"{case:14}{mu:>9.1f}{sig:>10.2f}{n:>12,.0f}"
              f"{(f'{bler:.2f}' if bler else '—'):>12}")
    if sigmas:
        print(f"\n  sigma across cases: {min(sigmas):.2f} to {max(sigmas):.2f} dB, "
              f"mean {statistics.fmean(sigmas):.2f}")
        print(f"  window_model.FADING_SIGMA_DB is "
              f"{10.0 / math.log(10) * math.pi / math.sqrt(6.0):.2f} — Rayleigh, "
              f"derived, not fitted to this")
        print(f"\n  The spread varies {max(sigmas) / min(sigmas):.1f}x across these "
              f"scenarios, which is why one constant cannot serve all of them and "
              f"why predicting it is the open item rather than choosing it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
