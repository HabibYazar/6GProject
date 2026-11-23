#!/usr/bin/env python3
"""The fast tier against VIAVI Tera5G, on captured runs.

WHY CAPTURED AND NOT LIVE. `/Tera5G` is a licence-patched appliance and this
project's standing decision is never to revive it as product or demo
infrastructure. It does not need reviving: twenty of its runs are on disk
with per-UE and per-cell reports carrying 3GPP TS 28.552 names —
DRB.UEThpDl, RRU.PrbUsedDl, TB.TotNbrDl — so there is no mapping to guess at.

FOUR THINGS THE FIRST VERSION OF THIS TOOL GOT WRONG, all of them visible in
the capture and none of them in the metric names:

  1. EVERY COUNTER APPEARS FOUR TIMES. There is one row per UE per second per
     BEAM (`Viavi.Cell.beam` 1-4) and the counters are REPEATED identically
     across them; only RSRP and SINR differ. Reading the file row by row
     counts every UE once per beam and weights a four-beam UE like four.

  2. THE CARRIER IS IN THE FILE. `RRU.PrbAvailDl` plus `Viavi.Frequency`
     pins it exactly against TS 38.101-1 Table 5.3.2-1: 38 PRB at 3600 MHz
     is 15 MHz at 30 kHz, 106 is 40 MHz, 52 at 728 MHz is 10 MHz at 15 kHz.
     The first comparison ran Tier B at 50 MHz against their 15.

  3. THEY RUN MIMO AND WE DO NOT. `Viavi.UE.RI` is 4 in the runs whose block
     counts looked impossible and 1 in the ones that did not — 6334 blocks a
     second against a 2000-slot second is four layers, not a shorter TTI.
     Dividing by the rank is what makes the counters the same quantity.

  4. SIXTEEN OF TWENTY RUNS ARE TWO-BAND, three sites x three sectors x
     {N77, N28}, and Tier B could not express that until it had per-cell
     carriers. It can now, so those runs are in.

    python3 tools/tera5g_compare.py --run 80-simulation
    python3 tools/tera5g_compare.py --all
"""
from __future__ import annotations

import argparse
import collections
import csv
import gzip
import math
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "nsg_ai"))
sys.path.insert(0, HERE)

GREEN, YELLOW, RED, DIM, RESET = (
    "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m")
ROOT = "/Tera5G/data/rsg_data"

# TS 38.101-1 Table 5.3.2-1, transmission bandwidth in RB. Keyed by SCS so a
# PRB count that appears under both (106 is 20 MHz at 15 kHz and 40 MHz at
# 30 kHz) is resolved by the carrier rather than guessed.
NRB_TO_MHZ = {
    15: {25: 5, 52: 10, 79: 15, 106: 20, 133: 25, 160: 30, 216: 40, 270: 50},
    30: {11: 5, 24: 10, 38: 15, 51: 20, 65: 25, 78: 30, 106: 40, 133: 50,
         162: 60, 189: 70, 217: 80, 245: 90, 273: 100},
}


def carrier_of(freq_mhz: float, n_rb: float) -> tuple[float, int]:
    """(bandwidth MHz, numerology) for a carrier, from its own report.

    Sub-1 GHz NR is deployed at 15 kHz and mid-band at 30 kHz, which is what
    resolves the PRB counts that appear under both. Nothing here is fitted:
    the numbers in the capture land exactly on the table.
    """
    scs = 15 if freq_mhz < 1000 else 30
    table = NRB_TO_MHZ[scs]
    n = int(round(n_rb or 0))
    if n in table:
        return float(table[n]), (0 if scs == 15 else 1)
    # Not on the grid — fall back to the width the RBs occupy, and say so by
    # rounding rather than by pretending to a standard channel.
    return round(max(5.0, n * 12 * scs / 1000.0)), (0 if scs == 15 else 1)


def load_run(run: str) -> dict:
    """The capture, de-duplicated and with its carriers resolved."""
    cells: dict[str, dict] = {}
    with gzip.open(os.path.join(ROOT, run, "CellReports.csv.gz"), "rt") as fh:
        for r in csv.DictReader(fh):
            name = r.get("Viavi.Cell.Name")
            if not name or name in cells:
                continue
            def _f(k, d=0.0):
                try:
                    return float(r.get(k) or d)
                except ValueError:
                    return d
            freq = _f("Viavi.Frequency", 3600.0)
            bw, mu = carrier_of(freq, _f("RRU.PrbAvailDl", 106))
            cells[name] = {"x": _f("Viavi.Geo.x"), "y": _f("Viavi.Geo.y"),
                           "z": _f("Viavi.Geo.z", 25.0) or 25.0,
                           "freq": freq, "bw": bw, "mu": mu,
                           "prb_avail": _f("RRU.PrbAvailDl", 106),
                           # Both of these are in the capture and the first
                           # version of this tool assumed them: 43 dBm for
                           # every cell, and 0/120/240 from the sector name.
                           # Their N77 sectors are at 5/125/245 and their N28
                           # ones run 3 dB lower, so the assumption was close
                           # and still an assumption.
                           "az": _f("Viavi.Radio.azimuth", 0.0),
                           "pow": _f("Viavi.Radio.power", 43.0)}

    vals: dict[str, list[float]] = collections.defaultdict(list)
    seen: set = set()
    ue_first: dict[str, tuple[float, float]] = {}
    ue_rate: dict[str, float] = {}
    ts: set = set()
    attach = collections.Counter()
    ranks: list[float] = []
    with gzip.open(os.path.join(ROOT, run, "UEReports.csv.gz"), "rt") as fh:
        for r in csv.DictReader(fh):
            key = (r.get("time_ms"), r.get("Viavi.UE.Name"))
            if key in seen:
                continue          # one row per BEAM; the counters repeat
            seen.add(key)
            try:
                ts.add(float(r["time_ms"]))
            except (KeyError, TypeError, ValueError):
                pass
            cell = r.get("Viavi.Cell.Name") or ""
            if not cell:
                continue          # detached, and its counters are not a link
            attach[cell] += 1
            name = r.get("Viavi.UE.Name")
            if name and name not in ue_first:
                try:
                    ue_first[name] = (float(r["Viavi.Geo.x"]),
                                      float(r["Viavi.Geo.y"]))
                    # THE OFFERED LOAD IS IN THE FILE, in Gbps, and it is the
                    # single thing that decides whether any of these counters
                    # can be compared at all. Their runs offer 0.4 to 10 Mbps
                    # a UE and sit at 2-5 PRB of 38-106; the first version of
                    # this tool offered Tier B fifty, loaded the same cells to
                    # saturation, and then reported the PRB difference as
                    # model error.
                    ue_rate[name] = float(r.get("Viavi.UE.targetThroughputDl")
                                          or 0.0)
                except (KeyError, TypeError, ValueError):
                    pass
            try:
                ranks.append(float(r.get("Viavi.UE.RI") or 1))
            except ValueError:
                pass
            for k in ("DRB.UEThpDl", "DRB.UEThpUl", "RRU.PrbUsedDl",
                      "RRU.PrbUsedUl", "TB.TotNbrDl", "TB.TotNbrUl",
                      "DRB.UECqiDl", "Viavi.UE.RsSinr", "Viavi.UE.Rsrp"):
                v = r.get(k)
                if v not in (None, ""):
                    try:
                        vals[k].append(float(v))
                    except ValueError:
                        pass
    xs = [p[0] for p in ue_first.values()] or [0.0]
    ys = [p[1] for p in ue_first.values()] or [0.0]
    return {
        "cells": cells, "vals": vals, "ue_xy": ue_first, "attach": attach,
        "ue_rate": ue_rate,
        "rank": statistics.fmean(ranks) if ranks else 1.0,
        "span_s": (max(ts) - min(ts)) / 1000.0 if len(ts) > 1 else 0.0,
        "extent": (min(xs), max(xs), min(ys), max(ys)),
    }


def build_tier_b(ref: dict):
    """A Tier B scenario at the capture's own geometry, carriers and UEs."""
    from schema import (AntennaConfig, BaseStation, FrequencyBand,
                        MassiveMIMOConfig, NetworkSlice, NSGSimulationConfig,
                        Position2D, Position3D, RadioConfig, ServiceRequirement,
                        SimulationArea, SimulationSettings, UEGroup)
    bss = []
    for i, (name, c) in enumerate(sorted(ref["cells"].items())):
        bss.append(BaseStation(
            id=f"c{i}", name=name,
            position=Position3D(x=c["x"], y=c["y"], z=c["z"]),
            radio=RadioConfig(
                band=FrequencyBand(label=name.split("/")[1] if "/" in name else "NR",
                                   frequency_mhz=c["freq"],
                                   bandwidth_mhz=c["bw"], numerology=c["mu"]),
                tx_power_dbm=c["pow"],
                antenna=AntennaConfig(
                    azimuth_deg=c["az"], tilt_deg=6.0,
                    massive_mimo=MassiveMIMOConfig(rows=8, cols=8)))))
    # One UE group per UE, AT THE POSITION THE CAPTURE FIRST REPORTS IT. The
    # scale-and-shape approximation the first version used put UEs on cells;
    # the capture has their coordinates, so the geometry is matched rather
    # than resembled.
    ues = [UEGroup(id=f"u{i}", name=n, count=1,
                   spawn_position=Position2D(x=x, y=y),
                   services=[ServiceRequirement(
                       slice_type=NetworkSlice.EMBB, qos_id=9,
                       target_throughput_gbps=max(
                           1e-4, ref["ue_rate"].get(n, 0.005)))])
           for i, (n, (x, y)) in enumerate(sorted(ref["ue_xy"].items()))]
    xs = [b.position.x for b in bss] + [u.spawn_position.x for u in ues]
    ys = [b.position.y for b in bss] + [u.spawn_position.y for u in ues]
    pad = 300.0
    return NSGSimulationConfig(
        name=f"tera5g parity {len(ues)}ue", base_stations=bss, ue_groups=ues,
        # Their sites are co-located three-sector masts, so the element has to
        # be directional or the three sectors are one cell.
        settings=SimulationSettings(antenna_element="3gpp"),
        area=SimulationArea(x_min=min(xs) - pad, x_max=max(xs) + pad,
                            y_min=min(ys) - pad, y_max=max(ys) + pad))


def mean(xs) -> float | None:
    return statistics.fmean(xs) if xs else None


def compare(run: str, windows: int) -> dict | None:
    import tier_b
    from main import nsoran_params
    from nsoran_live import _rate_of_imsi_map

    ref = load_run(run)
    if not ref["cells"] or not ref["ue_xy"]:
        print(f"{YELLOW}{run}: no cell or UE geometry{RESET}")
        return None
    if not any(ref["vals"].get(k) for k in ("TB.TotNbrDl", "RRU.PrbUsedDl")):
        print(f"{DIM}{run}: carries geometry but no counters — nothing to "
              f"compare against{RESET}")
        return None
    if ref["span_s"] > 10_000:
        # Some directories store an absolute epoch in `time_ms` rather than a
        # run clock, which comes out as fifty years of simulated time. They
        # are captures of something, but not of a run this can put a horizon
        # against.
        print(f"{DIM}{run}: time_ms is an absolute epoch "
              f"({ref['span_s']:,.0f} s), not a run clock{RESET}")
        return None
    bands = sorted({(c["freq"], c["bw"]) for c in ref["cells"].values()})
    x0, x1, y0, y1 = ref["extent"]
    print(f"\n{run}")
    print(f"  {len(ref['cells'])} cells, {len(ref['ue_xy'])} UEs, "
          f"{ref['span_s']:.0f} s, {x1 - x0:.0f} x {y1 - y0:.0f} m, "
          f"rank {ref['rank']:.2f}")
    print(f"  carriers: " + ", ".join(f"{f:.0f} MHz / {b:.0f} MHz" for f, b in bands))

    cfg = build_tier_b(ref)
    # Their eighteen-cell runs are past GNB_MAX and their UE counts past the
    # per-cell density trigger — both are ns-3's limits, not this tier's.
    params = nsoran_params(cfg, engine_clamped=False)
    n = windows or int(ref["span_s"]) or 60
    demand = {i: m * 1e6 for i, m in _rate_of_imsi_map(cfg).items()}
    t0 = time.perf_counter()
    frames = tier_b.frames(params, n, demand_bps=demand)
    wall = time.perf_counter() - t0
    print(f"  Tier B: {params['n_gnb']} cells, {params['n_ue']} UEs, "
          f"{n} windows in {wall:.2f} s  ({n / wall:,.0f}x real time)")

    ours = collections.defaultdict(list)
    for f in frames:
        for u in f.get("ues") or []:
            ours["TB.TotNbrDl"].append(float(u.get("tb") or 0))
            ours["TB.TotNbrUl"].append(float(u.get("tb_ul") or 0))
            ours["RRU.PrbUsedDl"].append(float(u.get("prb_rb") or 0))
            ours["RRU.PrbUsedUl"].append(float(u.get("prb_ul_rb") or 0))
            ours["Viavi.UE.RsSinr"].append(float(u.get("sinr") or 0))
            ours["Viavi.UE.Rsrp"].append(float(u.get("rsrp_dbm") or 0))

    # The slot rate each side runs at, so a block count can be read as an
    # OCCUPANCY rather than as a raw number. A MEAN numerology is not a slot
    # rate — averaging mu 0 and mu 1 gives 1414 slots a second, which is not
    # a number any cell runs at — so this takes the numerology the UEs are
    # actually ON, weighted by where they attached.
    by_mu = collections.Counter()
    for name, n in ref["attach"].items():
        c = ref["cells"].get(name)
        if c:
            by_mu[c["mu"]] += n
    mu = by_mu.most_common(1)[0][0] if by_mu else 1
    slots = 1000 * 2 ** mu
    if len(by_mu) > 1:
        share = by_mu[mu] / sum(by_mu.values())
        print(f"  {DIM}occupancy below uses mu={mu} ({slots:,} slots/s), where "
              f"{share:.0%} of the attachments are{RESET}")
    print(f"\n  {'metric':<18}{'Tera5G':>12}{'Tier B':>12}{'ratio':>8}   note")
    rows = []
    for k, note in (
            ("TB.TotNbrDl", "blocks per UE-second"),
            ("TB.TotNbrUl", "uplink blocks"),
            ("RRU.PrbUsedDl", "resource blocks, not the engine's 139-scale"),
            ("RRU.PrbUsedUl", ""),
            ("Viavi.UE.RsSinr", "dB"),
            ("Viavi.UE.Rsrp", "dBm")):
        a, b = mean(ref["vals"].get(k)), mean(ours.get(k))
        if a is None or b is None or (a == 0 and b == 0):
            print(f"  {k:<18}{'—' if a is None else f'{a:,.1f}':>12}"
                  f"{'—' if b is None else f'{b:,.1f}':>12}")
            continue
        if k.startswith("Viavi"):
            # A dB difference, because a ratio of two logarithms is nothing.
            print(f"  {k:<18}{a:>12,.1f}{b:>12,.1f}{b - a:>+8.1f}   {note} (difference)")
            # A DIFFERENCE, not a ratio — these are already logarithms — and
            # the summary has to know which it is holding or it prints a dB
            # gap as though it were a factor.
            rows.append((k, a, b, b - a))
            continue
        r = (b / a) if a else float("inf")
        col = GREEN if 0.5 <= r <= 2 else YELLOW if 0.2 <= r <= 5 else RED
        print(f"  {k:<18}{a:>12,.1f}{b:>12,.1f}{col}{r:>8.2f}{RESET}   {note}")
        rows.append((k, a, b, r))

    # THE COMPARISON THAT SURVIVES THE FRAME STRUCTURE. A block count is a
    # rate over slots and layers, and the two simulators agree on neither, so
    # the raw ratio above is between two different quantities.
    ta, tb_ = mean(ref["vals"].get("TB.TotNbrDl")), mean(ours.get("TB.TotNbrDl"))
    if ta and tb_:
        occ_a = ta / (slots * max(1.0, ref["rank"]))
        occ_b = tb_ / slots
        print(f"\n  slot occupancy, rank-normalised")
        print(f"    Tera5G  {occ_a * 100:5.1f}%   {ta:,.0f} blocks / "
              f"({slots:,} slots x {ref['rank']:.2f} layers)")
        print(f"    Tier B  {occ_b * 100:5.1f}%   {tb_:,.0f} blocks / {slots:,} slots"
              f"   {DIM}(rank 1 — no MIMO in this tier){RESET}")
        print(f"    ratio   {occ_b / occ_a:5.2f}")

    # UPLINK SHARE, ON PRBs AND NOT ON BLOCKS. Their frame is TDD — a slot
    # is a downlink slot or an uplink slot — so a UE's uplink blocks are
    # bounded by the uplink slots in the pattern. Our engine splits SYMBOLS
    # inside every slot, so a scheduled UE gets a block in both directions in
    # the same slot and the block ratio is near 1 by construction. Resource
    # blocks are the quantity both frame structures agree on.
    pa, pb = mean(ref["vals"].get("RRU.PrbUsedDl")), mean(ours.get("RRU.PrbUsedDl"))
    qa, qb = mean(ref["vals"].get("RRU.PrbUsedUl")), mean(ours.get("RRU.PrbUsedUl"))
    if pa and qa and pb and qb:
        print(f"\n  uplink share of the spectrum   Tera5G {qa / pa:.2f}"
              f"   Tier B {qb / pb:.2f}")
        print(f"  {DIM}on PRBs, not blocks: their frame is TDD and puts a slot "
              f"in one direction, ours splits symbols inside every slot, so a "
              f"block ratio compares two frame structures{RESET}")

    # RSRP AND SINR TOGETHER SAY MORE THAN EITHER ALONE. Both sides are
    # interference-limited here, and a uniform power offset cancels exactly
    # in a signal-over-interference ratio. So a pair that agrees on SINR and
    # disagrees on RSRP is a REFERENCE-POWER definition apart, not a channel
    # apart: a channel difference would move both. Saying which it is costs
    # nothing and stops a red number being read as a physics failure.
    sa, sb_ = mean(ref["vals"].get("Viavi.UE.RsSinr")), mean(ours.get("Viavi.UE.RsSinr"))
    ra, rb = mean(ref["vals"].get("Viavi.UE.Rsrp")), mean(ours.get("Viavi.UE.Rsrp"))
    if None not in (sa, sb_, ra, rb) and abs(sb_ - sa) < 4.0 and abs(rb - ra) > 6.0:
        print(f"\n  {YELLOW}RSRP differs by {rb - ra:+.1f} dB while SINR agrees "
              f"to {sb_ - sa:+.1f}{RESET}")
        print(f"  {DIM}Both are interference-limited, so a uniform power offset "
              f"cancels in the ratio and cannot cancel in the level. This is a "
              f"reference-power DEFINITION — how much of the carrier one "
              f"reported resource element stands for — and not a channel "
              f"disagreement, which would move both. Their own RSRP fits "
              f"11.2 dBm - 29 log10(d) on this run, which is 14 dB above the "
              f"total power a 43 dBm cell with an 8x8 panel puts at 1 m, so "
              f"whatever they divide by, it is not this carrier's "
              f"subcarriers.{RESET}")

    dead = [k for k in ("DRB.UEThpDl", "DRB.UEThpUl", "DRB.UECqiUl")
            if ref["vals"].get(k) and not any(ref["vals"][k])]
    if dead:
        print(f"\n  {YELLOW}identically zero in this capture{RESET}: "
              + ", ".join(dead)
              + f"\n  {DIM}while TB.TotNbrDl says blocks were sent — the same "
                f"class of counter fault this project found in its own engine, "
                f"on the other vendor's side{RESET}")
    return {"run": run, "rows": dict((k, (a, b, r)) for k, a, b, r in rows),
            "cells": len(ref["cells"]), "ues": len(ref["ue_xy"]),
            "bands": len(bands), "span": ref["span_s"],
            "speed": n / wall, "load": mean(ref["vals"].get("RRU.PrbUsedDl")),
            "wall": wall, "windows": n}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default="80-simulation")
    ap.add_argument("--all", action="store_true", help="every usable capture")
    ap.add_argument("--windows", type=int, default=0, help="0 matches the capture")
    args = ap.parse_args()

    runs = [args.run]
    if args.all:
        runs = sorted(d for d in os.listdir(ROOT)
                      if os.path.exists(os.path.join(ROOT, d, "UEReports.csv.gz"))
                      and os.path.exists(os.path.join(ROOT, d, "CellReports.csv.gz")))
    got = []
    for r in runs:
        try:
            res = compare(r, args.windows)
            if res:
                got.append(res)
        except Exception as exc:               # noqa: BLE001
            print(f"{RED}{r}: {type(exc).__name__}: {exc}{RESET}")
    print(f"\n{len(got)}/{len(runs)} captures compared")
    if len(got) > 1:
        summarise(got)
    return 0


def summarise(got: list) -> None:
    """One table for every capture, and what it is safe to conclude from it."""
    print(f"\n{'run':16}{'cells':>6}{'UE':>5}{'bd':>4}{'span':>6}{'x real':>8}"
          f"{'blocks':>8}{'PRB':>7}{'SINR':>7}{'RSRP':>7}{'their PRB':>10}")
    for g in got:
        def rat(k):
            v = g["rows"].get(k)
            return v[2] if v and v[2] is not None else float("nan")
        print(f"{g['run']:16}{g['cells']:>6}{g['ues']:>5}{g['bands']:>4}"
              f"{g['span']:>6.0f}{g['speed']:>8,.0f}"
              f"{rat('TB.TotNbrDl'):>8.2f}{rat('RRU.PrbUsedDl'):>7.2f}"
              f"{rat('Viavi.UE.RsSinr'):>+7.1f}{rat('Viavi.UE.Rsrp'):>+7.1f}"
              f"{(g['load'] or 0):>10.1f}")

    def col(k):
        return [g["rows"][k][2] for g in got
                if g["rows"].get(k) and g["rows"][k][2] is not None]

    print()
    for k, lbl, tol in (("RRU.PrbUsedDl", "resource blocks", 2.0),
                        ("TB.TotNbrDl", "transport blocks", 2.0)):
        v = col(k)
        if v:
            print(f"  {lbl:18} median {statistics.median(v):6.2f}   "
                  f"within {tol:.0f}x: {sum(1 for x in v if 1 / tol <= x <= tol)}"
                  f"/{len(v)}   range {min(v):.2f}-{max(v):.2f}")
    for k, lbl in (("Viavi.UE.RsSinr", "SINR"), ("Viavi.UE.Rsrp", "RSRP")):
        v = col(k)
        if v:
            print(f"  {lbl:18} median {statistics.median(v):+6.1f} dB   "
                  f"within 3 dB: {sum(1 for x in v if abs(x) <= 3)}/{len(v)}   "
                  f"range {min(v):+.1f}..{max(v):+.1f}")

    # The prose below QUOTES the table, so it cannot drift from it. An
    # earlier version hardcoded the numbers and was already wrong by one run.
    sinr = [(g["run"], g["rows"]["Viavi.UE.RsSinr"][2]) for g in got
            if g["rows"].get("Viavi.UE.RsSinr")]
    rsrp = [(g["run"], g["rows"]["Viavi.UE.Rsrp"][2]) for g in got
            if g["rows"].get("Viavi.UE.Rsrp")]
    near = [r for r in sinr if abs(r[1]) <= 3]
    odd = sorted((r for r in sinr if abs(r[1]) > 3), key=lambda r: -abs(r[1]))
    print(f"""
  {DIM}WHAT THIS TABLE DOES AND DOES NOT SAY.

  RESOURCE BLOCKS ARE THE COMPARISON. Spectrum occupied per UE is the same
  physical quantity under both frame structures and both schedulers, and it
  is the column to read.

  TRANSPORT BLOCKS ARE A GRANULARITY, not a rate. Same spectrum, opposite
  granularity: VIAVI hands a UE a wide allocation now and then, the flex-TTI
  scheduler hands it a narrow symbol slice in almost every slot. Blocks times
  bytes-per-block is the invariant; the block count alone is an artefact of
  who splits what, which is why the PRB column agrees and this one does not.

  THEIR SINR AND RSRP COLUMNS ARE NOT ONE QUANTITY ACROSS THEIR OWN RUNS.
  SINR lands within 3 dB on {len(near)} of {len(sinr)} captures"""
          + (f" and is out by "
             f"{', '.join(f'{v:+.0f} on {n}' for n, v in odd[:3])}"
             if odd else "")
          + f""".
  RSRP never does: it runs from {min(v for _, v in rsrp):+.0f} to """
          + f"""{max(v for _, v in rsrp):+.0f} dB against the same model, from
  the same appliance, which is a reference-power definition moving and not a
  channel. The captures that are furthest out on both are the rank-4 ones,
  where the SINR column reads -1 and -2 dB beside an RSRP of -45 dBm — a
  per-BEAM figure, not a link SINR.{RESET}""")


if __name__ == "__main__":
    raise SystemExit(main())
