#!/usr/bin/env python3
"""What must Tier B match, and how closely? Measure it, do not assert it.

A fast tier is only sellable if you can say WHERE it agrees with the slow one.
That statement needs a tolerance, and a tolerance cannot be picked from the air:

    Tier B cannot be asked to sit closer to Tier A than Tier A sits to itself.

So this tool has two halves.

  measure   Run the SAME config on Tier A several times and record the spread,
            per metric, AT THE RUN LENGTH the comparison will use. Two spreads
            are recorded separately because they mean different things: repeats
            at one seed (the engine's own noise) and repeats across seeds (the
            scenario's genuine sensitivity). The tolerance comes from the first;
            the second says how many repeats a claim needs.

  check     Run both tiers on one config and report, per metric, whether Tier B
            landed inside the band — with the band printed next to the number,
            so a pass is readable rather than asserted.

WHY THIS IS MEASURED AND NOT LOOKED UP. The recorded same-seed spread for this
engine is 0.1%, and in this same session the same configuration measured 8443
and 12939 kbps — 53% apart — on 12-frame runs. Both are true: the spread
depends on the run LENGTH, because a short run is dominated by which sampling
window it happened to catch. A criterion fitted at one length does not transfer
to another, so the length is an input here and is printed with every result.

    python3 tools/tier_criterion.py measure --frames 40 --repeats 3
    python3 tools/tier_criterion.py check   --frames 40
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nsg_ai"))

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tier_criterion.json")

# ── What is compared, and what is deliberately NOT ──────────────────────────
# Each entry is (dotted path into the frame's measurements, human name). Only
# metrics that survived this engine's own scrutiny are here.
IN_SCOPE = [
    ("DRB.UEThpDl",              "downlink per UE"),
    ("RRU.PrbUsedDl",            "spectrum in use"),
    ("TB.TotNbrDl.1",            "transport blocks sent"),
    ("TB.ErrTotalNbrDl.1",       "transport blocks in error"),
    ("TB.TotNbrDlInitial.64Qam", "blocks at 64QAM"),
    ("TB.TotNbrDlInitial.Qpsk",  "blocks at QPSK"),
    ("DRB.PdcpSduBitRateDl",     "PDCP bitrate downlink"),
    ("DRB.BufferSize.Qos",       "data queued"),
    ("QosFlow.PdcpPduVolumeDL",  "bytes delivered"),
    # THE UPLINK. Tier A has reported it since ul-reporting.patch and this
    # list never asked for it, so "the fast tier has no uplink" was measured
    # nowhere — the criterion could not have caught it either way. Volume and
    # bitrate are the same measurement over a one-second window, and both are
    # listed because the window is a config field and they part company the
    # moment it is not one second.
    ("DRB.PdcpSduVolumeUl",      "bytes delivered uplink"),
    ("DRB.PdcpSduBitRateUl",     "PDCP bitrate uplink"),
]
# WHOSE FAULT IS A DISAGREEMENT? A metric can be outside the band for three
# very different reasons and scoring them together produces a number that
# means nothing. Only `tier_b` metrics count toward the verdict; the others
# are printed with their reason so nobody has to rediscover it.
#
# The bar for taking a metric out of the score is hard evidence, not a
# plausible story. Two qualify. Everything else — including the block error
# rate, whose spread predictor is a real and bounded Tier B gap — stays in.
RESPONSIBILITY = {
    "RRU.PrbUsedDl": ("engine_metric",
        "the engine's own figure is inconsistent with its own transport "
        "blocks: bytes per block over the symbols this metric implies exceeds "
        "the MCS table's ceiling in five of six breadth cases, by 1.03-1.57x, "
        "so it under-reports the airtime that was used. It has form — "
        "prb-truncation-fix.patch caught it reading exactly 0.0 while UEs "
        "carried 8.9 Mbps"),
    "DRB.BufferSize.Qos": ("horizon",
        "MEASURED: the queue has a time constant of about four simulated "
        "seconds and the reference run covers two, so it is sampled halfway "
        "up a ramp. At kpm_collect_s=6 the per-UE queue goes 3.6 MB at "
        "sim_t 1.1, 6.0-8.6 at 2.1, and 10.2 at 4.0 — the 10 MB cap, which is "
        "where Tier B sits from its first window because it has no ramp. This "
        "is not a mechanism difference and not a metric fault; it is two "
        "points on the same transient. The real fix is to measure the "
        "criterion past it, which costs about 25 minutes an arm instead of 8. "
        "CONFIRMED by doing it: at span 8.9 s Tier A reads 10,485,564 against "
        "Tier B's 10,485,760 — a ratio of 1.00, where the 2 s measurement "
        "reads 0.60. The whole disagreement was the transient"),
    "ue_count": ("horizon",
        "the attach ramp, the same way round. `settled()` already drops the "
        "frames before the network fills, and the 2 s run still only reaches "
        "31.5 of 36 because it ends before the last UEs attach. CONFIRMED at "
        "span 8.9 s: Tier A reads 36 against Tier B's 36, a ratio of 1.01"),
    "handovers": ("structural",
        "the engine runs RandomWalk2dOutdoorMobilityModel wherever no "
        "waypoints are given. A deterministic walk can reproduce the RATE of "
        "boundary crossings and not which UE crosses when, in either "
        "direction, and the mobility breadth case measures 7.0x"),
}

# Derived, not a KPM counter.
DERIVED = ["ue_count", "handovers", "block error rate", "64QAM share"]

# CONTRACT vs REPORTED. Every metric above is measured and printed; only the
# contract ones carry a band Tier B must clear. The split is not taste, it is
# what the seed sensitivity costs — measured on 6 cells / 36 UEs at 40 frames,
# the seeds needed per side to prove agreement to ±15%:
#
#     data queued 1 · block error rate 2 · spectrum in use 3 · blocks sent 3
#     blocks at 64QAM 3 · handovers 3 · blocks in error 4       <- contract
#     downlink per UE 10 · PDCP bitrate 20 · blocks at QPSK 28  <- reported
#
# Binding the expensive three would make every comparison a half-day job, and a
# criterion nobody can afford to run is not a criterion. They are still printed,
# with their band, so a large disagreement is visible even though it does not
# fail the check.
CONTRACT = {
    "RRU.PrbUsedDl", "TB.TotNbrDl.1", "TB.ErrTotalNbrDl.1",
    "TB.TotNbrDlInitial.64Qam", "DRB.BufferSize.Qos",
    "ue_count", "handovers", "block error rate",
}

# Excluded ON PURPOSE, each with the measurement that disqualified it. A metric
# left out silently is a metric someone will put back.
EXCLUDED = {
    "DRB.PdcpSduDelayDl": "swings 14x across repeats of the SAME config and seed "
                          "(41-574 ms) against 1.1x for throughput — 4-5 samples, "
                          "the first carrying attach-time buffering",
    "DRB.PdcpSduDelayUl": "same sample-count problem as the downlink figure",
    "L3servingSINR":      "moves the WRONG WAY under real interference: a burst term "
                          "that took block errors from 0% to 24.6% and cut throughput "
                          "54% RAISED the reported SINR from 14.25 to 21.0 dB",
}


# EXTENSIVE counters grow with how long they were counted; INTENSIVE ones are
# already ratios and do not. The engine measures each UE from ITS OWN last
# counter reset (`GetLastResetTime(rnti, cellId)`) and the report carries no
# window length, so a UE that attached a moment ago contributes a number from
# a fortieth of a second beside one from a full second — and a mean over the
# two means nothing.
#
# MEASURED, and it was the whole of the fast tier's "block count is 2-4x too
# high": against the count-weighted mean the ratio reads 1.83 across twelve
# stored runs, against the full-window UE it reads 0.89. The scheduler was
# never the problem. One frame at sim_t 1.1 shows it on its own — eight UEs,
# min 0, mean 1291, max 4043, against a 4000-slot window.
# Every PER-UE counter, whatever its units. Two separate things make the
# engine's per-UE samples incomparable, and the report carries neither:
#
#   WINDOW. Each UE is measured from its own GetLastResetTime, so a UE that
#   attached a moment ago contributes a fortieth of a second beside one that
#   contributed a full second. This inflates ratios (PRB) as surely as it
#   deflates counts (blocks).
#
#   POPULATION. At any instant about half the UEs are idle and report zeros,
#   and Tier B has no idle UEs to match them with. A mean over the engine's
#   population and a mean over Tier B's are means over different things.
#
# The per-frame MAXIMUM is the only sample guaranteed to be both fully
# windowed and served, so it is the one place the two tiers can be compared
# like with like. What it does NOT do is describe the network: it ignores the
# idle population, which is a real property of the engine that Tier B does
# not model. Judge a model on it; do not quote it as a network average.
PER_UE = {
    "TB.TotNbrDl.1", "TB.ErrTotalNbrDl.1", "TB.TotNbrDlInitial",
    "TB.TotNbrDlInitial.Qpsk", "TB.TotNbrDlInitial.16Qam",
    "TB.TotNbrDlInitial.64Qam", "QosFlow.PdcpPduVolumeDL",
    "RRU.PrbUsedDl", "DRB.PdcpSduBitRateDl", "DRB.BufferSize.Qos",
    "DRB.UEThpDl",
    # The uplink. Tier A reports the first three out of the CU-UP per UE
    # (ul-reporting.patch); the last three are Tier B's own and exist to be
    # compared against a VIAVI capture, which reports them and Tier A does not.
    "DRB.PdcpSduVolumeUl", "DRB.PdcpSduBitRateUl", "DRB.PdcpSduDelayUl",
    "RRU.PrbUsedUl", "TB.TotNbrUl", "TB.ErrTotalNbrUl",
}
EXTENSIVE = PER_UE     # the older name, kept for callers


def wmean(frames: list, name: str) -> float | None:
    """The estimator this counter deserves.

    Per-UE counters get the upper mode of the per-frame MAXIMUM — see PER_UE
    for why a mean over them compares two different populations over two
    different windows. Anything else keeps the count-weighted mean, because a
    frame is one instant and not one sample.
    """
    if name in PER_UE:
        peaks = [m.get("max") for f in frames
                 if (m := (f.get("measurements") or {}).get(name))
                 and m.get("max") is not None]
        # Drop wrapped counters before anything else looks at them. The upper
        # mode would otherwise take 4.29e9 as the peak and every other value
        # would fall outside its factor-of-two window.
        peaks = [p for p in peaks if p and not looks_underflowed(p)]
        if not peaks:
            return None
        # The per-frame maxima are BIMODAL, and a median across all of them
        # lands in the gap between the modes rather than on either. A run's
        # early frames are reported moments after the UEs attach, so their
        # window is a fraction of a second and their maximum is a few hundred
        # blocks; later frames cover the full window and reach a few thousand.
        # Measured on the reference: the two modes are ~220 and ~3940 and the
        # median over everything reads 1724, which describes no window that
        # ever existed.
        #
        # So take the upper mode — the maxima within a factor of two of the
        # largest — which needs no knowledge of the report period and no
        # threshold from outside the data.
        top = max(peaks)
        return round(statistics.median([p for p in peaks if p >= 0.5 * top]), 3)
    num = den = 0.0
    for f in frames:
        m = (f.get("measurements") or {}).get(name)
        if m and m.get("mean") is not None and not looks_underflowed(m["mean"]):
            w = float(m.get("count") or 1)
            num += m["mean"] * w
            den += w
    return round(num / den, 3) if den else None


def pop_mean(frames: list, name: str) -> float | None:
    """The count-weighted mean, whatever `wmean` would do with this name.

    A RATIO between two per-UE counters must not be built from two maxima:
    those are the largest values each counter reached, and nothing says they
    came from the same UE. Tier B's buildings case showed it plainly — 3717
    blocks at 64QAM beside 3675 at QPSK, from a model where no single UE is
    ever at both. Ratios are window-independent already, so the population
    mean is both available and correct for them.
    """
    num = den = 0.0
    for f in frames:
        m = (f.get("measurements") or {}).get(name)
        if m and m.get("mean") is not None:
            w = float(m.get("count") or 1)
            num += m["mean"] * w
            den += w
    return (num / den) if den else None


def settled(frames: list) -> list:
    """The frames after the network has finished attaching.

    A run starts with nobody attached and fills up — measured on the
    reference, `ue_ids` goes 0, 4, 10, 16, 21, 24, 27 ... 35 over the run, so
    the mean over every frame is 20.1 where the settled value is 35. Tier B
    has all its UEs from the first window, so comparing the two compared a
    steady state against a startup transient and called the difference model
    error: ue_count read 93% out.

    The cut is self-contained — the first frame that reaches 80% of the run's
    own peak — so it needs no knowledge of how long attaching takes on a
    scenario nobody has run yet. A run that never settles keeps all its
    frames rather than silently reporting the tail of a ramp.
    """
    counts = [len(f.get("ue_ids") or []) for f in frames]
    peak = max(counts) if counts else 0
    if peak <= 0:
        return frames
    for i, c in enumerate(counts):
        if c >= 0.8 * peak:
            return frames[i:]
    return frames


# A uint32 that went below zero and wrapped. MEASURED: running the reference
# at 5 Mbps instead of 50 made DRB.BufferSize.Qos report 4,294,966,948, which
# is 2^32 - 348 — the engine computed a buffer occupancy of MINUS 348 bytes
# for a queue whose ceiling is 10 MB. At low load the counter underflows, and
# a value that size is not a measurement of anything.
UINT32_WRAP = 2 ** 32
UNDERFLOW_FLOOR = 2 ** 31


def looks_underflowed(v: float | None) -> bool:
    return v is not None and v >= UNDERFLOW_FLOOR


def frames_carrying(frames: list, name: str) -> int:
    return sum(1 for f in frames if (f.get("measurements") or {}).get(name))


def name_collision(frames: list, name: str, ratio: float = 10.0,
                   typical_frames: float | None = None) -> dict | None:
    """Is one metric name carrying two different quantities?

    MEASURED on QosFlow.PdcpPduVolumeDL: it appears in twice as many frames as
    every other DU counter, and its values fall into two clusters fifty times
    apart — 2 to 53 kB in the frames with no transport-block counters, 0 to
    3.1 MB in the frames that have them. The engine declares the name in BOTH
    its CU-UP and its DU report headers, and the bridge merges them.

    A count-weighted mean over that is a mean over two different measurements.
    The upper-mode estimator happens to isolate the larger cluster, which is
    luck rather than design, and nothing was checking.

    Returns the two cluster means when the gap exceeds `ratio`, else None.
    """
    # A value gap alone is not evidence. An attach ramp produces one too —
    # early frames carry small queues and late ones large, and the detector
    # flagged DRB.BufferSize.Qos and TB.ErrTotalNbrDl.1 on exactly that. The
    # signature of two SOURCES is that the name appears in more frames than
    # its siblings, because each source reports it once.
    here = frames_carrying(frames, name)
    if typical_frames and here < 1.5 * typical_frames:
        return None
    vals = [m.get("mean") for f in frames
            if (m := (f.get("measurements") or {}).get(name))
            and m.get("mean")]
    if len(vals) < 4:
        return None
    vals.sort()
    # The widest multiplicative gap between neighbours splits the clusters.
    best, at = 1.0, None
    for i in range(1, len(vals)):
        lo, hi = vals[i - 1], vals[i]
        if lo > 0 and hi / lo > best:
            best, at = hi / lo, i
    if best < ratio or at is None:
        return None
    return {"ratio": round(best, 1),
            "low_n": at, "low_max": vals[at - 1],
            "high_n": len(vals) - at, "high_min": vals[at]}


def harvest(frames: list) -> dict:
    # Collisions are looked for in what the ENGINE SENT, before the settle
    # cut, because the cut can remove one cluster entirely and make a name
    # carrying two quantities look clean. It does exactly that for
    # QosFlow.PdcpPduVolumeDL, whose small cluster lives in the early frames —
    # the harvest is safe and the warning would have vanished with it.
    present = [frames_carrying(frames, n) for n, _ in IN_SCOPE]
    typical = statistics.median([p for p in present if p]) if any(present) else None
    collisions = {n: c for n, _ in IN_SCOPE
                  if (c := name_collision(frames, n, typical_frames=typical))}
    frames = settled(frames)
    out = {n: wmean(frames, n) for n, _ in IN_SCOPE}
    # How many frames each metric was actually present in. A spread computed
    # over three arms looks equally solid whether each arm saw the metric in
    # thirty frames or in one — DRB.PdcpSduBitRateDl turns out to appear in
    # two frames of forty, and none after the network settles.
    out["_present"] = {n: frames_carrying(frames, n) for n, _ in IN_SCOPE}
    out["_collision"] = collisions
    out["_underflow"] = {
        n: sum(1 for f in frames
               if looks_underflowed(((f.get("measurements") or {}).get(n) or {}).get("max")))
        for n, _ in IN_SCOPE}
    out["_underflow"] = {n: c for n, c in out["_underflow"].items() if c}
    ue = [len(f.get("ue_ids") or []) for f in frames]
    out["ue_count"] = round(statistics.fmean(ue), 3) if ue else None
    ho = [f.get("handovers") or 0 for f in frames]
    out["handovers"] = max(ho) if ho else None
    # From the POPULATION, not from two unrelated maxima — see pop_mean.
    err, tot = pop_mean(frames, "TB.ErrTotalNbrDl.1"), pop_mean(frames, "TB.TotNbrDl.1")
    out["block error rate"] = round(err / tot * 100, 3) if (err and tot) else None
    q, s16, s64 = (pop_mean(frames, f"TB.TotNbrDlInitial.{m}")
                   for m in ("Qpsk", "16Qam", "64Qam"))
    mod_tot = sum(v for v in (q, s16, s64) if v)
    out["64QAM share"] = round(s64 / mod_tot * 100, 3) if (s64 and mod_tot) else None
    ts = [f.get("sim_t") for f in frames if f.get("sim_t") is not None]
    out["_sim_span_s"] = round(max(ts) - min(ts), 2) if len(ts) > 1 else 0.0
    return out


# Expected range of n normal samples, in standard deviations (Hartley's d2).
# Used to get a sigma out of a peak-to-peak on very few runs, which is all a
# ten-minute-per-run engine affords.
_D2 = {2: 1.128, 3: 1.693, 4: 2.059, 5: 2.326, 6: 2.534, 7: 2.704, 8: 2.847}


def band_from_range(range_pct: float | None, n: int, k: float = 2.0) -> float | None:
    """The band Tier A's own seed-mean sits in, as a percentage.

    NOT the same-seed spread. That measures the engine's DETERMINISM — two runs
    of one seed agree to 0.1% here — and determinism is not resolution. The
    scenario itself moves 18-67% when the seed changes, so a model that lands
    within 5% of ONE seed's answer has fitted that seed, not the scenario.

    What Tier B has to match is Tier A's mean ACROSS seeds, and the band is the
    uncertainty of that mean: sigma from the observed range, then k standard
    errors. It narrows as sqrt(n), which is why this file also reports how many
    seeds a given band costs.
    """
    if range_pct is None or n < 2:
        return None
    sigma = range_pct / _D2.get(n, 2.847)
    return round(k * sigma / (n ** 0.5), 1)


def seeds_for(range_pct: float | None, n: int, target_pct: float,
              k: float = 2.0) -> int | None:
    if range_pct is None or n < 2 or target_pct <= 0:
        return None
    sigma = range_pct / _D2.get(n, 2.847)
    return max(1, -(-int((k * sigma / target_pct) ** 2 * 100) // 100) or 1)


def spread(values: list[float]) -> dict:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return {"n": len(vals), "mean": vals[0] if vals else None, "pct": None}
    m = statistics.fmean(vals)
    return {
        "n": len(vals),
        "mean": round(m, 3),
        "min": round(min(vals), 3),
        "max": round(max(vals), 3),
        # Peak-to-peak against the mean: the band a single run can land in,
        # which is what a tolerance has to cover — not a standard deviation.
        "pct": round((max(vals) - min(vals)) / abs(m) * 100, 1) if m else None,
    }


async def run_tier_a(cfg, params, frames_target, wall_cap, addr="127.0.0.1:8091"):
    from nsoran_live import run_batch_simulation
    t0 = time.time()
    r = await run_batch_simulation(cfg, params, frames_target=frames_target,
                                   wall_cap_s=wall_cap, live_addr=addr)
    return r, round(time.time() - t0, 1)


def load_scenario(name: str):
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    from nsg_ai import store
    from nsg_ai.schema import NSGSimulationConfig
    import main as M
    scn = next((s for s in store.load_scenarios() if s["name"].startswith(name)), None)
    if scn is None:
        raise SystemExit(f"no saved scenario starts with {name!r}")
    cfg = NSGSimulationConfig(**scn["config"])
    return scn["name"], cfg, M.nsoran_params(cfg)


def _write_criterion(name, frames_n, repeats, same, varied, *,
                     cells=0, ues=0, floor_pct=5.0):
    """Derive the criterion from harvested arms and write it.

    Split out of `cmd_measure` so `rederive` can rebuild the same file
    from saved frames without touching the engine.
    """
    keys = [n for n, _ in IN_SCOPE] + DERIVED
    crit = {
        "scenario": name, "frames": frames_n, "repeats": repeats,
        "cells": cells, "ues": ues,
        "engine_image": os.environ.get("NSG_ENGINE_IMAGE", "nsg-engine:1.30-guard"),
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "excluded": EXCLUDED,
        "sim_span_s": round(statistics.fmean(
            [h["_sim_span_s"] for h in same + varied if h.get("_sim_span_s")]), 2)
            if any(h.get("_sim_span_s") for h in same + varied) else None,
        "metrics": {},
    }
    label = {n: h for n, h in IN_SCOPE}
    print(f"\n{'metric':<26} {'same seed':>17} {'across seeds':>17} "
          f"{'band':>10} {'n±15%':>7} {'n±10%':>7}  role")
    for k in keys:
        s_same = spread([h.get(k) for h in same])
        s_var = spread([h.get(k) for h in varied])
        band = band_from_range(s_var.get("pct"), s_var["n"])
        tol = max(floor_pct, band or floor_pct)
        need15 = seeds_for(s_var.get("pct"), s_var["n"], 15.0)
        need10 = seeds_for(s_var.get("pct"), s_var["n"], 10.0)
        crit["metrics"][k] = {
            "label": label.get(k, k), "same_seed": s_same, "across_seeds": s_var,
            "band_pct": band, "tolerance_pct": round(tol, 1),
            "seeds_for_15pct": need15, "seeds_for_10pct": need10,
            "contract": k in CONTRACT,
            "underflowed_frames": next(
                (h["_underflow"][k] for h in same + varied
                 if k in h.get("_underflow", {})), None),
            "name_collision": next(
                (h["_collision"][k] for h in same + varied
                 if k in h.get("_collision", {})), None),
            "responsibility": RESPONSIBILITY.get(k, ("tier_b", ""))[0],
            "responsibility_why": RESPONSIBILITY.get(k, ("tier_b", ""))[1],
            # Only meaningful for a counter the engine reports directly. A
            # derived figure (ue_count, handovers, the rates) is computed from
            # the frames rather than carried by them, so counting its
            # "presence" would flag every one of them as missing.
            "frames_present": (round(statistics.fmean(
                [h.get("_present", {}).get(k, 0) for h in same + varied]), 1)
                if k in dict(IN_SCOPE) else None),
        }
        f = lambda d: f"{d['pct']:.1f}% (n={d['n']})" if d.get("pct") is not None else "—"
        mark = "contract" if k in CONTRACT else DIM + "reported" + RESET
        print(f"{label.get(k,k):<26} {f(s_same):>17} {f(s_var):>17} "
              f"{tol:>9.1f}% {str(need15):>7} {str(need10):>7}  {mark}")

    with open(OUT, "w") as fh:
        json.dump(crit, fh, indent=2)
    print(f"\nwritten to {OUT}")
    print(f"{DIM}band = 2 standard errors of Tier A's mean ACROSS SEEDS, floored at "
          f"{floor_pct}%. The same-seed column is the engine's determinism, not its "
          f"resolution: matching one seed to 0.1% would be fitting that seed. The last two "
          f"columns are what a tighter band costs in seeds, per side.{RESET}")
    return 0


async def cmd_measure(args) -> int:
    name, cfg, params = load_scenario(args.scenario)
    if getattr(args, "collect_s", None):
        from main import nsoran_params
        cfg.settings.kpm_collect_s = args.collect_s
        params = nsoran_params(cfg)
        print(f"{YELLOW}collect window forced to {args.collect_s} s{RESET} — "
              f"measuring past the queue transient, not inside it")
    print(f"scenario: {name}")
    print(f"  {params['n_gnb']} cells / {params['n_ue']} UEs, "
          f"frames={args.frames}, repeats={args.repeats}\n")

    same, varied = [], []

    raw = []
    for i in range(args.repeats):
        p = dict(params, rng_run=params.get("rng_run", 42))
        r, wall = await run_tier_a(cfg, p, args.frames, args.wall_cap)
        raw.append({"arm": "same", "rep": i + 1, "status": r.get("status"),
                    "frames": r["frames"]})
        _save_raw(raw)
        h = harvest(r["frames"])
        same.append(h)
        print(f"  same seed   rep{i+1}  {r['status']:<11} span {h['_sim_span_s']:>4.2f} s  "
              f"wall {wall:>5.0f} s", flush=True)
        await asyncio.sleep(10)
    for i in range(args.repeats):
        p = dict(params, rng_run=100 + i)
        r, wall = await run_tier_a(cfg, p, args.frames, args.wall_cap)
        raw.append({"arm": f"seed{100 + i}", "rep": i + 1, "status": r.get("status"),
                    "frames": r["frames"]})
        _save_raw(raw)
        h = harvest(r["frames"])
        varied.append(h)
        print(f"  seed {100+i}    rep{i+1}  {r['status']:<11} span {h['_sim_span_s']:>4.2f} s  "
              f"wall {wall:>5.0f} s", flush=True)
        await asyncio.sleep(10)

    # A run that produced no usable span contributed nothing, and a spread
    # quietly computed over the survivors reads exactly like a spread over all
    # of them. Say how many arms actually landed.
    lost = [("same seed", i + 1) for i, h in enumerate(same) if not h.get("_sim_span_s")]
    lost += [("varied seed", i + 1) for i, h in enumerate(varied) if not h.get("_sim_span_s")]
    if lost:
        print(f"\n{YELLOW}{len(lost)} of {2*args.repeats} runs produced no usable span "
              f"({', '.join(f'{a} rep{b}' for a, b in lost)}) — every spread below rests "
              f"on fewer samples than requested, and the n is printed with it.{RESET}")

    return _write_criterion(name, args.frames, args.repeats, same, varied,
                            cells=params["n_gnb"], ues=params["n_ue"],
                            floor_pct=args.floor_pct)


async def cmd_check(args) -> int:
    """Run both tiers on one config and report where Tier B landed.

    Tier B does not exist yet as a KPM source. Until it does this reports the
    criterion and says plainly that the comparison could not be made, rather
    than printing a table of dashes that reads like a result.
    """
    if not os.path.exists(OUT):
        print(f"{RED}no criterion yet{RESET} — run: "
              f"python3 tools/tier_criterion.py measure --frames {args.frames}")
        return 1
    crit = json.load(open(OUT))
    print(f"criterion measured {crit['measured_at']} on {crit['engine_image']}")
    print(f"  {crit['scenario']}  ·  {crit['cells']} cells / {crit['ues']} UEs  ·  "
          f"frames={crit['frames']}, {crit['repeats']} repeats per arm\n")

    name, cfg, params = load_scenario(args.scenario)
    if crit["frames"] != args.frames:
        print(f"{YELLOW}the criterion was measured at frames={crit['frames']} and you asked "
              f"for {args.frames}.{RESET} The spread depends on the run LENGTH — a short run "
              f"is dominated by which sampling window it caught — so a band fitted at one "
              f"length does not transfer. Re-measure, or compare at {crit['frames']}.")
        return 1

    print("Tier A …", flush=True)
    a, wall_a = await run_tier_a(cfg, params, args.frames, args.wall_cap)
    ha = harvest(a["frames"])
    print(f"  {a['status']}  span {ha['_sim_span_s']} s  wall {wall_a} s\n")

    hb = tier_b_harvest(cfg, params, args.frames, ha.get("_sim_span_s"))
    if hb is None:
        print(f"{YELLOW}Tier B has no KPM source yet, so there is nothing to compare.{RESET}")
        print("What it has to clear when it does:\n")
        print(f"  {'metric':<26} {'Tier A now':>14} {'band':>10}   {'Tier B must land in':>26}")
        for k, m in crit["metrics"].items():
            v, tol = ha.get(k), m["tolerance_pct"]
            if v is None:
                continue
            lo, hi = v * (1 - tol / 100), v * (1 + tol / 100)
            print(f"  {m['label']:<26} {v:>14.2f} {tol:>9.1f}%   {lo:>11.2f} … {hi:<11.2f}")
        print(f"\n{DIM}excluded on purpose: {', '.join(crit['excluded'])}{RESET}")
        return 0

    fails = []
    print(f"  {'metric':<26} {'Tier A':>12} {'Tier B':>12} {'diff':>8} {'band':>8}")
    for k, m in crit["metrics"].items():
        va, vb, tol = ha.get(k), hb.get(k), m["tolerance_pct"]
        if va is None or vb is None:
            print(f"  {m['label']:<26} {'—':>12} {'—':>12}")
            continue
        d = (vb - va) / abs(va) * 100 if va else 0.0
        ok = abs(d) <= tol
        if not ok and m.get("contract"):
            fails.append(m["label"])
        col = GREEN if ok else (RED if m.get("contract") else YELLOW)
        print(f"  {m['label']:<26} {va:>12.2f} {vb:>12.2f} "
              f"{col}{d:>+7.1f}%{RESET} {tol:>7.1f}%")
    print()
    if fails:
        print(f"{RED}outside the band:{RESET} {', '.join(fails)}")
        return 1 if args.check else 0
    print(f"{GREEN}Tier B is inside Tier A's own spread on every metric in scope{RESET}")
    return 0


def tier_b_harvest(cfg, params, frames_target, span_s=None):
    """Tier B's frames in the same shape, harvested identically.

    Tier B is handed THE SAME `params` Tier A is launched with, not the config
    they were derived from, so both tiers see the same clamped counts and the
    same evicted, recentred positions. Any difference that survives is the
    model, which is the whole point of measuring it.

    The offered load comes from the same per-IMSI rate map the engine's traffic
    generators are built from, because a cell that is symbol-limited and a cell
    that is demand-limited report very different block counts.
    """
    import statistics as _st

    import tier_b
    from nsoran_live import _rate_of_imsi_map
    demand = {i: mbps * 1e6 for i, mbps in _rate_of_imsi_map(cfg).items()}
    # Tier B's OWN realisation spread is 16-35% on the spectrum figure and
    # 8-27% on volume — the shadowing sigma is 4 to 7.8 dB, so one draw is a
    # sample. Tier A is compared as a mean over seeds and Tier B was compared
    # as a single draw, which is not the same question. It costs 3.5 ms a
    # window to fix.
    # MATCHED TO TIER A'S SIMULATED TIME, not to its frame count. Forty
    # frames of Tier A is about two simulated seconds — that is the whole
    # horizon a batch run buys — and forty windows of Tier B is forty
    # SECONDS at the default window. A per-window counter does not care;
    # anything that ACCUMULATES does, and the queue was measuring the
    # difference: Tier B pinned at its RLC buffer in every breadth case
    # while the engine sat at 55-65% of it, which read as a systematic
    # 1.7-1.9x model error and was 20x of extra simulated time.
    n_win = frames_target
    if span_s and span_s > 0:
        n_win = max(1, round(float(span_s) / max(tier_b.window_s(params), 1e-9)))
    else:
        print(f"  {YELLOW}no recorded span{RESET} — Tier B runs "
              f"{frames_target} WINDOWS against Tier A's {frames_target} "
              f"REPORTS, which is not the same simulated time. Re-measure "
              f"the criterion to record `sim_span_s`.")
    runs = [harvest(tier_b.frames(params, n_win, demand_bps=demand, seed=s))
            for s in range(TIER_B_REALISATIONS)]
    out = {}
    for k in set().union(*(r.keys() for r in runs)):
        vals = [r[k] for r in runs if isinstance(r.get(k), (int, float))]
        out[k] = _st.fmean(vals) if vals else runs[0].get(k)
    return out


# How many shadowing realisations Tier B is averaged over. Eight puts the
# standard error of its mean at about a third of a single draw's spread,
# which is enough for the widest column (spectrum, 35%) to stop dominating
# the comparison, and it costs under a second.
TIER_B_REALISATIONS = 8

RAW_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "tier_criterion_raw.json")


def _save_raw(runs: list) -> None:
    """Keep every run's frames, written after each one.

    The criterion is derived from these, and the derivation has now changed
    twice — once when the count-weighted mean turned out to average windows of
    different lengths, once when the median across frames turned out to land
    between two modes. Each change cost a full re-measure: six engine runs,
    fifty minutes, because only the summary was kept. Keeping the frames makes
    the next re-derivation free, and makes a run that dies in the sixth arm
    cost one arm instead of six.
    """
    try:
        with open(RAW_OUT, "w") as fh:
            json.dump(runs, fh)
    except OSError as exc:
        print(f"{YELLOW}could not save raw frames: {exc}{RESET}")


SWEEP_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "tier_seed_sensitivity.json")


def cmd_rederive(args) -> int:
    """Rebuild the criterion from saved frames. No engine, no waiting.

    The derivation has changed three times now — the count-weighted mean, the
    median across frames, and the attach ramp — and the first two each cost
    six engine runs and fifty minutes because only the summary was kept.
    """
    if not os.path.exists(RAW_OUT):
        print(f"{RED}no saved frames at {RAW_OUT}{RESET} — run `measure` once.")
        return 1
    runs = json.load(open(RAW_OUT))
    # The raw file is whatever ran LAST, and the criterion is whatever was
    # derived last — they are not necessarily the same measurement. An
    # overnight run at a longer horizon overwrote the frames, and a rederive
    # then rebuilt a 2 s criterion out of 8.9 s data without saying a word.
    spans = [h["_sim_span_s"] for r in runs
             if (h := harvest(r["frames"])).get("_sim_span_s")]
    if spans and os.path.exists(OUT):
        prior = json.load(open(OUT)).get("sim_span_s")
        if prior and abs(statistics.fmean(spans) - prior) > 0.5:
            print(f"{RED}the saved frames cover {statistics.fmean(spans):.1f} "
                  f"simulated seconds and the criterion on disk was measured "
                  f"over {prior:.1f}{RESET} — these are different "
                  f"measurements. Move one aside before re-deriving.")
            return 1
    same = [harvest(r["frames"]) for r in runs if r["arm"] == "same"]
    varied = [harvest(r["frames"]) for r in runs if r["arm"] != "same"]
    if not same or not varied:
        print(f"{RED}saved frames cover only {[r['arm'] for r in runs]}{RESET}")
        return 1
    crit = json.load(open(OUT))
    _write_criterion(crit["scenario"], crit["frames"], len(same), same, varied,
                     cells=crit.get("cells", 0), ues=crit.get("ues", 0))
    print(f"re-derived from {len(runs)} saved run(s) — no engine time")
    return 0


async def cmd_sweep(args) -> int:
    """Does a BIGGER scenario average out the seed sensitivity?

    The bands in the criterion are wide because this scenario moves 18-67%
    between seeds, and every percent of band costs seeds: proving downlink per
    UE to ±10% needs 22 runs a side. If a denser scenario averages the draw out,
    the reference should be the denser one and every future band gets cheaper.
    If it does not, seed count is the only lever and that is worth knowing
    before budgeting for it.

    THIS IS AN OVERNIGHT JOB — about eight hours at three sizes and five seeds,
    because run time rises with UEs at an exponent of ~1.37. So it saves after
    EVERY run and skips what it already has: a job that dies in hour six and
    reports nothing is worth less than one that reports five hours of results.
    """
    import copy
    from nsg_ai.schema import NSGSimulationConfig
    import main as M

    done = {}
    if os.path.exists(SWEEP_OUT) and not args.restart:
        done = json.load(open(SWEEP_OUT)).get("runs", {})
        if done:
            print(f"{DIM}resuming: {len(done)} run(s) already recorded{RESET}")

    name, cfg, base = load_scenario(args.scenario)
    sizes = [int(x) for x in args.ues.split(",")]
    print(f"scenario: {name}  ·  sizes {sizes}  ·  {args.seeds} seeds each  "
          f"·  frames={args.frames}\n")

    for ues in sizes:
        for si in range(args.seeds):
            seed = 200 + si
            key = f"{ues}ue_seed{seed}"
            if key in done:
                print(f"  {key:<18} {DIM}already recorded{RESET}", flush=True)
                continue
            # Stretch the saved scenario to this UE count the same way the
            # sweep harness does, so the geometry stays comparable.
            c2 = copy.deepcopy(cfg)
            per = max(1, ues // max(1, len(c2.ue_groups)))
            for g in c2.ue_groups:
                g.count = per
            params = dict(M.nsoran_params(c2), rng_run=seed)
            t0 = time.time()
            try:
                r, wall = await run_tier_a(cfg, params, args.frames, args.wall_cap)
                h = harvest(r["frames"])
                done[key] = {"ues": params["n_ue"], "cells": params["n_gnb"],
                             "seed": seed, "status": r["status"], "wall_s": wall,
                             **{k: v for k, v in h.items()}}
                print(f"  {key:<18} {r['status']:<12} n_ue={params['n_ue']:<4} "
                      f"span {h['_sim_span_s']:>4.2f}  wall {wall:>6.0f} s", flush=True)
            except Exception as exc:  # noqa: BLE001
                done[key] = {"ues": ues, "seed": seed, "status": "error",
                             "error": str(exc)[:200], "wall_s": round(time.time()-t0, 1)}
                print(f"  {key:<18} {RED}error{RESET} {str(exc)[:80]}", flush=True)
            # After EVERY run, not at the end.
            with open(SWEEP_OUT, "w") as fh:
                json.dump({"scenario": name, "frames": args.frames,
                           "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "runs": done}, fh, indent=2)
            await asyncio.sleep(15)

    print(f"\n{'size':<12} {'n':>3}  " + "  ".join(f"{l[:13]:>13}" for _, l in IN_SCOPE[:4]))
    for ues in sizes:
        rows = [v for v in done.values()
                if v.get("status") in ("done", "wall-capped") and v.get("ues")
                and abs(v["ues"] - ues) <= max(2, ues * 0.1)]
        if len(rows) < 2:
            print(f"{str(ues)+' UE':<12} {len(rows):>3}  {DIM}too few to spread{RESET}")
            continue
        cells = [spread([r.get(k) for r in rows]) for k, _ in IN_SCOPE[:4]]
        print(f"{str(ues)+' UE':<12} {len(rows):>3}  " +
              "  ".join(f"{(str(c['pct'])+'%') if c.get('pct') is not None else '—':>13}"
                        for c in cells))
    print(f"\nwritten to {SWEEP_OUT}")
    print(f"{DIM}If the percentages fall as UEs rise, make the bigger scenario the "
          f"reference. If they do not, seeds are the only lever.{RESET}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("measure", help="measure Tier A's own repeatability")
    m.add_argument("--collect-s", type=int, default=None,
                   help="engine collect window (settings.kpm_collect_s). The "
                        "default auto window gives ~2 simulated seconds, and "
                        "the queue's time constant is about four — measuring "
                        "inside that transient is why data queued cannot be "
                        "scored. 20 gives ~5 s and costs roughly 25 min an arm.")
    m.add_argument("--scenario", default="Partition test")
    m.add_argument("--frames", type=int, default=40)
    m.add_argument("--repeats", type=int, default=3)
    m.add_argument("--wall-cap", type=float, default=900.0, dest="wall_cap")
    m.add_argument("--floor-pct", type=float, default=5.0, dest="floor_pct")
    c = sub.add_parser("check", help="compare Tier B against the measured band")
    c.add_argument("--scenario", default="Partition test")
    c.add_argument("--frames", type=int, default=40)
    c.add_argument("--wall-cap", type=float, default=900.0, dest="wall_cap")
    c.add_argument("--check", action="store_true",
                   help="non-zero exit when a metric is outside its band")
    sub.add_parser("rederive", help="rebuild the criterion from saved frames, no engine")
    w = sub.add_parser("sweep", help="does a bigger scenario average out the seeds?")
    w.add_argument("--scenario", default="Partition test")
    w.add_argument("--ues", default="36,72,144", help="UE counts to compare")
    w.add_argument("--seeds", type=int, default=5)
    w.add_argument("--frames", type=int, default=40)
    w.add_argument("--wall-cap", type=float, default=5400.0, dest="wall_cap")
    w.add_argument("--restart", action="store_true", help="ignore recorded runs")
    args = ap.parse_args()
    if args.cmd == "measure":
        return asyncio.run(cmd_measure(args))
    if args.cmd == "check":
        return asyncio.run(cmd_check(args))
    if args.cmd == "rederive":
        return cmd_rederive(args)
    if args.cmd == "sweep":
        return asyncio.run(cmd_sweep(args))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
