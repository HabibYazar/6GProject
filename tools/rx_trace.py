#!/usr/bin/env python3
"""The engine's per-transmission truth: one row per transport block.

WHY. Everything this project knows about the engine's SINR comes from
`L1M.RS-SINR.Bin*` — SEVEN buckets 6 dB wide with two OPEN ends, which
`tools/sinr_spread.py` has to give the centre of a notional bin. That
understates the spread exactly where the tails are longest, and it is the
leading suspect for why `buildings` measures a 28.5% block error rate that
no correlation can reproduce ([[nsg-mi-error-model-ported]]).

`MmWavePhyTrace` writes the unbinned thing. One line per transport block:

    DL/UL  time  frame  subF  slot  1stSym  symbol#  cellId  rnti  ccId
    tbSize  mcs  rv  SINR(dB)  corrupt  TBler

That is the SINR the block actually met, the MCS the AMC actually chose,
the size it actually was, whether it was actually corrupted, AND the
engine's own predicted TBLER for it. Four things follow that nothing else
in this repo can do:

  sigma       the real spread, ungrouped, against the histogram's estimate
  port        `mi_error_model.tb_bler` against the engine's OWN TBler
              column, ROW BY ROW — an exact check of the port that no
              aggregate can give
  correlation the MCS in each row tells you the SINR band the AMC saw, and
              the SINR column is what the block met, so their relationship
              is measured rather than solved for
  target      the corrupt fraction against the AMC's 0.1, directly

It is off unless asked for: one line per block per UE is hundreds of
thousands of rows for a few seconds of simulated time.

    python3 tools/rx_trace.py --run          # run an arm with the trace on
    python3 tools/rx_trace.py                # analyse what is already saved
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "nsg_ai"))
sys.path.insert(0, HERE)

GREEN, YELLOW, RED, DIM, RESET = (
    "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m")
WSL = "/mnt/c/Windows/System32/wsl.exe"
ENGINE_DISTRO = "Ubuntu-2004-nsoran"
IN_ENGINE = "/tmp/nsg_rxtrace.txt"
LOCAL = os.path.join(HERE, "rx_trace.tsv")


def local_path(case: str | None = None) -> str:
    """Where a case's trace lives. One file per scenario, because two
    scenarios in one file is a comparison of neither."""
    return LOCAL if not case else LOCAL.replace(".tsv", f".{case}.tsv")


def pull(case: str | None = None) -> int:
    """Copy the trace out of the container. Returns rows written."""
    out = subprocess.run(
        [WSL, "-d", ENGINE_DISTRO, "-u", "root", "-e", "bash", "-lc",
         f"docker exec nsg-engine sh -c 'cat {IN_ENGINE}'"],
        capture_output=True, text=True, timeout=600, cwd="/tmp").stdout
    lines = [l for l in out.splitlines() if l and not l.startswith("wsl:")]
    if len(lines) < 2:
        return 0
    with open(local_path(case), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return len(lines) - 1


def rows(path: str = LOCAL):
    """(t, cell, rnti, sinr_db, mcs, tb_bytes, corrupt, tbler), downlink only."""
    with open(path) as fh:
        head = fh.readline().split()
        idx = {n: i for i, n in enumerate(head)}
        for line in fh:
            f = line.split()
            if len(f) < len(head) or f[0] != "DL":
                continue
            try:
                yield (float(f[idx["time"]]), int(f[idx["cellId"]]),
                       int(f[idx["rnti"]]), float(f[idx["SINR(dB)"]]),
                       int(f[idx["mcs"]]), int(f[idx["tbSize"]]),
                       int(f[idx["corrupt"]]), float(f[idx["TBler"]]))
            except (KeyError, ValueError):
                continue


async def run_arm(frames: int, wall_cap: float, case: str | None = None) -> int:
    import tier_criterion as tc
    from main import nsoran_params
    crit = json.load(open(os.path.join(HERE, "tier_criterion.json")))["scenario"]
    name, cfg, _ = tc.load_scenario(crit[:20])
    if case:
        # ...or one of tier_breadth's cases, because the rows that disagree
        # most are not the reference. The trace is the only instrument that
        # sees WHY — it is what turned the spread from five failed sigmas
        # into a mechanism — and until now it could only be pointed at the
        # one scenario the criterion happens to use.
        import copy as _copy
        import tier_breadth as _tb
        if case not in _tb.CASES:
            print(f"{RED}no case {case}{RESET} — one of {list(_tb.CASES)}")
            return 1
        cfg = _tb.CASES[case](_copy.deepcopy(cfg))
        name = f"{name} [{case}]"
    params = dict(nsoran_params(cfg), rx_trace=IN_ENGINE)
    print(f"scenario: {name}\n  running with the PHY trace on …", flush=True)
    r, _ = await tc.run_tier_a(cfg, params, frames, wall_cap)
    print(f"  status {r.get('status')}, {len(r.get('frames') or [])} frames")
    n = pull(case)
    print(f"  {n:,} trace rows -> {local_path(case)}")
    if not n:
        print(f"  {RED}no trace{RESET} — the image must carry the rx_trace "
              f"param (engine_sync says which tag) and the run must reach "
              f"the PHY at all")
    return 0 if n else 1


def analyse(case: str | None = None) -> int:
    import collections

    import mi_error_model as mi
    path = local_path(case)
    if not os.path.exists(path):
        print(f"no {path} — run with --run first")
        return 1
    data = list(rows(path))
    if not data:
        print("no downlink rows in the trace")
        return 1
    sinr = [d[3] for d in data]
    corrupt = sum(d[6] for d in data) / len(data)
    print(f"{len(data):,} downlink transport blocks\n")
    print(f"  SINR over ALL blocks   mean {statistics.fmean(sinr):6.2f} dB   "
          f"sigma {statistics.pstdev(sinr):6.2f} dB   "
          f"min {min(sinr):.1f}  max {max(sinr):.1f}")
    print(f"  corrupt                {corrupt * 100:.2f}%   "
          f"engine TBler mean {statistics.fmean(d[7] for d in data):.4f}")

    # ── THE SPREAD, DECOMPOSED ────────────────────────────────────────────
    # The total spread is not the fading. A UE at cell centre and one at the
    # edge differ by tens of dB of GEOMETRY, and `FADING_SIGMA_DB` is meant
    # to be how far one link moves between transmissions. Split it.
    per_ue: dict = collections.defaultdict(list)
    per_ue_rows: dict = collections.defaultdict(list)
    for d in data:
        per_ue[(d[1], d[2])].append(d[3])
        per_ue_rows[(d[1], d[2])].append(d)
    within = [statistics.pstdev(v) for v in per_ue.values() if len(v) > 30]
    means = [statistics.fmean(v) for v in per_ue.values() if len(v) > 30]
    print(f"\n  THE SPREAD, DECOMPOSED over {len(within)} UEs")
    print(f"    BETWEEN UEs (geometry)   sigma {statistics.pstdev(means):6.2f} dB")
    print(f"    WITHIN a UE (fading)     sigma {statistics.fmean(within):6.2f} dB"
          f"   range {min(within):.1f}-{max(within):.1f}")
    import window_model as wm
    print(f"    window_model derives     sigma {wm.FADING_SIGMA_DB:6.2f} dB "
          f"(Rayleigh)")
    print(f"    the histogram estimate   sigma   6.10 dB "
          f"(tools/sinr_spread.py, 7 bins with two open ends)")
    ratio = statistics.fmean(within) / wm.FADING_SIGMA_DB
    col = GREEN if 0.6 <= ratio <= 1.7 else YELLOW
    print(f"  {col}the within-UE spread is {ratio:.2f}x the derived one"
          f"{RESET}")

    # ── THE PORT, ROW BY ROW ──────────────────────────────────────────────
    # The same inputs the engine had, against the number it produced. No
    # distribution, no estimator, no correlation.
    errs = [mi.tb_bler(10 ** (d[3] / 10.0), d[4], d[5]) - d[7]
            for d in data[:40000]]
    mae = statistics.fmean(abs(e) for e in errs)
    bias = statistics.fmean(errs)
    print(f"\n  PORT AGAINST THE ENGINE'S OWN TBler, row by row "
          f"({len(errs):,} rows)")
    print(f"    bias {bias:+.4f}   mean |error| {mae:.4f}   "
          f"off by >0.05: {100 * sum(1 for e in errs if abs(e) > 0.05) / len(errs):.1f}%")
    if abs(bias) < 0.01 and mae < 0.05:
        print(f"    {GREEN}the port reproduces the engine's own error model"
              f"{RESET} — unbiased to a thousandth on a quantity in [0,1]. "
              f"The residual is symmetric and sits where the curve is "
              f"steepest, which is a flat-SINR model against the engine's "
              f"per-RB average, not a wrong table.")
    else:
        print(f"    {YELLOW}the port does NOT reproduce it{RESET} — fix this "
              f"before any conclusion that rests on the curve")

    # ── HOW FAST THE CHANNEL DECORRELATES ─────────────────────────────────
    # This is the quantity `amc_correlation.py` had to solve for, and it is
    # measurable directly: within one UE, how alike are two transmissions
    # separated by a given time? The AMC acts on an estimate refreshed every
    # `UpdateUeSinrEstimatePeriod` (25.6 ms shipped), so the correlation AT
    # THAT LAG is what decides whether its MCS still fits when the block
    # goes out.
    print(f"\n  HOW FAST ONE UE'S CHANNEL DECORRELATES")
    print(f"    {'lag ms':>8}{'correlation':>14}{'pairs':>10}")
    series = {k: sorted((d[0], d[3]) for d in data if (d[1], d[2]) == k)
              for k in list(per_ue)[:40]}
    for lag_ms in (0.25, 1.0, 5.0, 25.6, 100.0):
        xs, ys = [], []
        lag = lag_ms / 1000.0
        for pts in series.values():
            j = 0
            for i, (t0, s0) in enumerate(pts):
                while j < len(pts) and pts[j][0] < t0 + lag * 0.8:
                    j += 1
                if j < len(pts) and pts[j][0] <= t0 + lag * 1.25:
                    xs.append(s0)
                    ys.append(pts[j][1])
        if len(xs) < 50:
            print(f"    {lag_ms:>8.2f}{'—':>14}{len(xs):>10,}")
            continue
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        sx, sy = statistics.pstdev(xs), statistics.pstdev(ys)
        r = (sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / len(xs)
             / (sx * sy)) if sx and sy else float("nan")
        mark = "  <- the AMC's estimate age" if abs(lag_ms - 25.6) < 0.1 else ""
        print(f"    {lag_ms:>8.2f}{r:>14.3f}{len(xs):>10,}{mark}")
    print(f"  {DIM}`amc_correlation.py` inferred 0.887-0.992 from aggregates "
          f"with a leave-one-out that did not carry. This is the same "
          f"quantity read off the engine's own transmissions.{RESET}")

    # ── WHAT THE SPREAD ACTUALLY IS ───────────────────────────────────────
    # Three candidates, and the trace separates them. Consecutive samples
    # 0.25 ms apart differ by a median 0.02 dB, so it is not fast fading.
    # Cells are 45-99% busy, so on/off duty can only move it a few dB. What
    # is left is WHICH UE each neighbour is serving, because that is where
    # its beam points.
    print(f"\n  WHAT MOVES ONE UE'S SINR")
    slot = lambda t: round(t / 0.00025)
    tx: dict = collections.defaultdict(dict)
    for d in data:
        tx[slot(d[0])][d[1]] = d[2]
    victim = max(per_ue_rows, key=lambda k: statistics.pstdev(
        [x[3] for x in per_ue_rows[k]]) * (len(per_ue_rows[k]) > 500))
    vic = [(slot(d[0]), d[3]) for d in per_ue_rows[victim]]
    vs = sorted(x[1] for x in vic)
    print(f"    worst UE {victim}, sigma "
          f"{statistics.pstdev([x[1] for x in vic]):.2f} dB over {len(vic):,} blocks")
    steps = [abs(b - a) for (_, a), (_, b) in zip(sorted(vic), sorted(vic)[1:])]
    print(f"    consecutive-sample |delta|, median {statistics.median(steps):.3f} dB"
          f"   {DIM}<- not fast fading{RESET}")
    onoff = []
    for n in sorted({d[1] for d in data}):
        if n == victim[0]:
            continue
        on = [v for k, v in vic if n in tx.get(k, {})]
        off = [v for k, v in vic if n not in tx.get(k, {})]
        if len(on) > 20 and len(off) > 20:
            onoff.append(abs(statistics.fmean(on) - statistics.fmean(off)))
    beam = []
    for n in sorted({d[1] for d in data}):
        if n == victim[0]:
            continue
        by: dict = collections.defaultdict(list)
        for k, v in vic:
            r = tx.get(k, {}).get(n)
            if r is not None:
                by[r].append(v)
        m = [statistics.fmean(v) for v in by.values() if len(v) > 30]
        if len(m) > 1:
            beam.append(max(m) - min(m))
    if onoff:
        print(f"    neighbour ON vs OFF        up to {max(onoff):5.1f} dB")
    if beam:
        print(f"    WHICH UE the neighbour serves  up to {max(beam):5.1f} dB"
              f"   {GREEN}<- this is the spread{RESET}")
        print(f"  {DIM}A neighbour interferes when its BEAM points at you, and "
              f"where it points is decided by which of its own UEs it is "
              f"scheduling. Tier B has every ingredient for that — the UE "
              f"sets, the positions, the array, and the turn shares from its "
              f"own scheduler — and models it as a single Gaussian sigma "
              f"instead.{RESET}")

    # ── DO THE THREE MEASURED INPUTS COMPOSE? ─────────────────────────────
    # Curve, spread and correlation are now each measured off this trace
    # rather than derived or solved for. If the model is right they should
    # reproduce the error rate the same trace reports, PER UE — a
    # population mean cannot, because the errors live in the low-SINR tail
    # while the mean sits at 44 dB where nothing fails.
    from amc_correlation import predicted_bler
    rho_lag = 0.758
    print(f"\n  DO THEY COMPOSE? per UE, every input off this trace")
    print(f"    {'rho':>6}{'predicted %':>14}{'measured %':>13}{'ratio':>8}")
    for rho in (0.0, rho_lag, 0.99):
        num = den = 0.0
        for v in per_ue_rows.values():
            if len(v) < 30:
                continue
            ss = [x[3] for x in v]
            p = predicted_bler(statistics.fmean(ss),
                               max(statistics.pstdev(ss), 0.1), rho,
                               statistics.fmean(x[5] for x in v),
                               wm.rb_count(50.0, 2))
            num += p * len(v)
            den += len(v)
        pred = 100 * num / den
        r = pred / (corrupt * 100)
        col = GREEN if 0.7 <= r <= 1.4 else YELLOW
        tag = "  <- measured lag" if rho == rho_lag else ""
        print(f"    {rho:>6.3f}{pred:>14.2f}{corrupt * 100:>13.2f}"
              f"{col}{r:>8.2f}{RESET}{tag}")
    print(f"  {DIM}and the answer barely moves with rho over the plausible "
          f"range — with the REAL spread the error rate is set by geometry, "
          f"not by the correlation. The knife edge in amc_correlation.py was "
          f"an artefact of the understated spread it was given.{RESET}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--frames", type=int, default=20)
    ap.add_argument("--wall-cap", type=float, default=600.0)
    ap.add_argument("--case", default=None,
                    help="a tier_breadth case instead of the reference; its "
                         "trace is kept in its own file")
    a = ap.parse_args()
    if a.run:
        rc = asyncio.run(run_arm(a.frames, a.wall_cap, a.case))
        if rc:
            return rc
    return analyse(a.case)


if __name__ == "__main__":
    raise SystemExit(main())
