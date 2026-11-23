#!/usr/bin/env python3
"""How correlated is what the AMC saw with what the block met? Measure it.

THE QUESTION. Porting the engine's own error model showed that the block
error rate cannot be reproduced by a curve alone
([[nsg-mi-error-model-ported]]). The engine's AMC walks the MCS up until the
TBLER of a 12-symbol block exceeds a literal 0.1 and steps back, so it
selects an index sitting AT about 10% error for the SINR it was handed. If
the block then meets an INDEPENDENT draw from the same distribution, the
expectation must land well above 10% — the curve is convex the wrong way.
The engine reports 10.11% on the reference, so the two are not independent.
They are also not identical, or the answer would be the target itself.

WHAT IS MEASURED. Model the pair as jointly normal in dB with the engine's
OWN measured spread:

    S_amc = mu + sigma * (rho * z + sqrt(1 - rho^2) * w)
    S_dec = mu + sigma * z

The AMC picks `m(S_amc)` by the engine's own walk; the block is decoded at
`S_dec` with the engine's own curve at the engine's OWN measured block size.
Everything in that sentence is taken from the engine except `rho`, and `rho`
is then SOLVED for so the predicted block error rate equals the measured
one. One number per scenario, out of numbers the engine reported.

WHAT IT IS FOR. Not to produce a constant to ship — a single rho fitted to
one scenario is the trap this repo keeps a cautionary tale about
(`window_model.BEAMFORMING_LOSS_DB`). It is to answer whether the six
scenarios agree on it. If they do, the correlation is a property of the
engine's estimator and the block error rate becomes predictable. If they do
not, the correlation is one more thing that varies per scenario and the
spread predictor's boundary simply moves.

    python3 tools/amc_correlation.py
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

import mi_error_model as mi
import tier_criterion as tc
import window_model as wm
from sinr_spread import spread_of

GREEN, YELLOW, RED, DIM, RESET = (
    "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m")


def predicted_bler(mu: float, sigma: float, rho: float, tb_bytes_: float,
                   n_rb: int, nodes: int = 11) -> float:
    """E[ BLER(S_dec, m(S_amc)) ] over the joint distribution."""
    import numpy as np
    x, w = np.polynomial.hermite.hermgauss(nodes)
    root2 = math.sqrt(2.0)
    norm = math.pi                     # two Gauss-Hermite dimensions
    tot = 0.0
    tail = math.sqrt(max(0.0, 1.0 - rho * rho))
    for zi, wz in zip(x, w):
        s_dec = mu + sigma * root2 * zi
        sinr_lin = 10 ** (s_dec / 10.0)
        for wi, ww in zip(x, w):
            s_amc = mu + sigma * (rho * root2 * zi + tail * root2 * wi)
            m = wm.pick_mcs_engine(s_amc, n_rb)
            tot += wz * ww * (1.0 if m < 0 else mi.tb_bler(sinr_lin, m, tb_bytes_))
    return tot / norm


def solve_rho(target: float, mu: float, sigma: float, tb_bytes_: float,
              n_rb: int) -> tuple[float | None, float, float]:
    """The rho whose prediction matches `target`, by bisection.

    Returns (rho, prediction at rho=0, prediction at rho=1) so a target
    outside that range is visible as such rather than silently clamped.
    """
    lo = predicted_bler(mu, sigma, 0.0, tb_bytes_, n_rb)
    hi = predicted_bler(mu, sigma, 1.0, tb_bytes_, n_rb)
    if not (min(lo, hi) <= target <= max(lo, hi)):
        return None, lo, hi
    a, b = 0.0, 1.0
    for _ in range(24):
        mid = (a + b) / 2
        v = predicted_bler(mu, sigma, mid, tb_bytes_, n_rb)
        # the prediction falls as rho rises
        if v > target:
            a = mid
        else:
            b = mid
    return (a + b) / 2, lo, hi


def target_above(target: float, lo: float, hi: float) -> bool:
    """Is the measurement outside what ANY correlation can produce?"""
    return target > max(lo, hi)


def main() -> int:
    path = os.path.join(HERE, "tier_breadth.json")
    d = json.load(open(path))
    print(f"{'case':13}{'mu dB':>7}{'sigma':>7}{'B/blk':>8}{'A BLER%':>9}"
          f"{'rho=0':>8}{'rho=1':>8}{'rho':>7}")
    rhos = []
    for case, row in d.items():
        fa = row.get("frames_a")
        if not fa:
            continue
        got = spread_of(fa)
        a = tc.harvest(fa)
        bler = a.get("block error rate")
        vol, blocks = a.get("QosFlow.PdcpPduVolumeDL"), a.get("TB.TotNbrDl.1")
        if not (got and bler and vol and blocks):
            print(f"{case:13}  not enough of the engine's own numbers")
            continue
        mu, sigma, _ = got
        # The engine's OWN block size, not a modelled one.
        tb_bytes_ = vol / blocks
        n_rb = wm.rb_count(50.0, 2)
        rho, lo, hi = solve_rho(bler / 100.0, mu, sigma, tb_bytes_, n_rb)
        print(f"{case:13}{mu:>7.1f}{sigma:>7.2f}{tb_bytes_:>8,.0f}{bler:>9.2f}"
              f"{lo * 100:>8.1f}{hi * 100:>8.1f}"
              + (f"{rho:>7.3f}" if rho is not None else f"{'—':>7}"))
        if rho is not None:
            rhos.append((case, rho))
        elif target_above(bler / 100.0, lo, hi):
            print(f"  {DIM}  ^ {case} is worse than FULL INDEPENDENCE "
                  f"({bler:.1f}% measured against {lo * 100:.1f}% at rho=0). "
                  f"No correlation can explain it, so something else is "
                  f"widening the tail — most likely the spread itself: the "
                  f"histogram's two open bins are given the centre of a 6 dB "
                  f"bin, which UNDERSTATES sigma when the tails are long, and "
                  f"buildings is where the tails are longest.{RESET}")

    print()
    if len(rhos) < 2:
        print(f"  {YELLOW}too few scenarios bracket their own measurement to "
              f"say anything about whether rho is shared{RESET}")
        return 0
    vals = [r for _, r in rhos]
    print(f"  rho across {len(vals)} scenarios: {min(vals):.3f} to "
          f"{max(vals):.3f}, mean {statistics.fmean(vals):.3f}")

    # A NARROW SPREAD IN A STEEP FUNCTION IS NOT AGREEMENT. Between rho=0
    # and rho=1 the predicted error rate falls from ~23% to ~0.1%, so rho
    # values agreeing to +/-0.05 may still predict wildly different error
    # rates. The claim worth making is the other way round: does ONE rho,
    # taken from the OTHER scenarios, predict this one's measurement?
    print(f"\n  LEAVE-ONE-OUT — each case predicted from the mean rho of the "
          f"others\n")
    print(f"  {'case':13}{'rho used':>10}{'predicted %':>13}{'measured %':>12}"
          f"{'ratio':>8}")
    ratios = []
    for case, _ in rhos:
        others = [r for c, r in rhos if c != case]
        rho_lo = statistics.fmean(others)
        row = d[case]
        mu, sigma, _n = spread_of(row["frames_a"])
        a = tc.harvest(row["frames_a"])
        meas = a["block error rate"]
        tb_bytes_ = a["QosFlow.PdcpPduVolumeDL"] / a["TB.TotNbrDl.1"]
        pred = predicted_bler(mu, sigma, rho_lo, tb_bytes_,
                              wm.rb_count(50.0, 2)) * 100
        r = pred / meas
        ratios.append(r)
        col = GREEN if 0.5 <= r <= 2 else YELLOW if 0.25 <= r <= 4 else RED
        print(f"  {case:13}{rho_lo:>10.3f}{pred:>13.2f}{meas:>12.2f}"
              f"{col}{r:>8.2f}{RESET}")

    worst = max(max(ratios), 1 / min(ratios))

    # HOW SHARP IS THE KNIFE? The verdict below turns on whether one rho
    # carries, and the reader needs to know how precisely rho would have to
    # be known for that to mean anything.
    mu, sigma, _n = spread_of(d[rhos[0][0]]["frames_a"])
    a0 = tc.harvest(d[rhos[0][0]]["frames_a"])
    tbb = a0["QosFlow.PdcpPduVolumeDL"] / a0["TB.TotNbrDl.1"]
    r0 = statistics.fmean(vals)
    n_rb = wm.rb_count(50.0, 2)
    near = [(r, predicted_bler(mu, sigma, r, tbb, n_rb) * 100)
            for r in (r0 - 0.03, r0, min(0.999, r0 + 0.03))]
    print(f"\n  sensitivity at rho = {r0:.3f}, on {rhos[0][0]}: "
          + "  ".join(f"rho {r:.3f} -> {b:.2f}%" for r, b in near))
    swing = max(b for _, b in near) / max(1e-9, min(b for _, b in near))
    print(f"  {DIM}+/-0.03 in rho is {swing:.1f}x in the answer, so a model "
          f"resting on this number rests on a knife edge whatever the "
          f"leave-one-out says.{RESET}")
    print()
    if worst <= 2.0:
        print(f"  {GREEN}ONE CORRELATION PREDICTS THEM ALL{RESET} to within "
              f"{worst:.2f}x, held out. The block error rate is then a "
              f"function of the spread and this one number, and the spread "
              f"is measurable from the engine's own histogram — which is a "
              f"different and much smaller open item than predicting the "
              f"error rate itself.")
    else:
        print(f"  {YELLOW}IT DOES NOT CARRY{RESET} — held out, one rho is "
              f"{worst:.1f}x off on the worst case. The narrow spread of the "
              f"fitted values was the steepness of the curve near rho=1, not "
              f"agreement: between rho=0 and rho=1 the prediction falls from "
              f"~23% to ~0.1%, so values agreeing to a few hundredths still "
              f"disagree about the answer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
