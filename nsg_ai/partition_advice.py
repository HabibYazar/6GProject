"""What does splitting a scenario across processes buy, and what does it cost?

A scenario can be run as ONE ns-3 process, or split into N processes of whole
cells each. Splitting is the only speed lever with real headroom — every
single-process lever measured together is about 2.2x — but it is not free, and
what it costs depends on the shape.

Everything here is fitted to measurements on the shipped engine, and the numbers
it returns are ESTIMATES the caller must present as such. The point is to let
someone choose a shape knowingly instead of discovering the cost after a run.

MEASURED, 12 cells / 72 UEs / UEs at 20 m/s, speed = the slowest group's rate
because that is what paces a parallel run:

    groups  cells/grp   speed   handovers  served
       1        12       1.0x        2      35/72
       2         6       2.2x       31      41
       3         4       4.4x       32      40
       4         3       6.7x       31      43
       6         2      11.0x       21      51
      12         1      26.2x       10      62

Three things in that table are not obvious and drive everything below.

1. Two, three and four groups cost NOTHING in handovers. Neighbouring cells fall
   into the same group, so most handovers never cross a boundary. The loss only
   begins at two cells per group.

2. The UNPARTITIONED row is the worst one. It shows 2 handovers where its own
   partitions show 31, and serves 35 UEs where they serve 62 — at 12 cells the
   whole network is so slow (0.3 simulated seconds) that a 20 m/s UE moves 6 m
   and never reaches a cell edge. The "full fidelity" reference is too slow to
   exhibit the behaviour it is meant to be the reference for. So partition loss
   is real but SCALE-DEPENDENT; at 6 cells, where the whole run is fast enough,
   it goes the other way (14 handovers whole, 11 at 2x3, 2 at 6x1).

3. The handovers a one-cell group still reports are NOT cell-to-cell — there is
   no target cell in the process. Each partition keeps its own LTE anchor and the
   metric counts NGAP id churn, so they are anchor transitions. Reported
   separately here for that reason; merged into one number they make the
   one-cell shape look like it preserves handover.

THERE IS NO "STATIC SCENARIO" EXEMPTION, and it was tried. The idea was that a
motionless capacity test has no handovers to lose, so it could take the fast
shape freely. Two measurements killed it:

  * a speed of zero cannot be expressed — `_ue_speed_specs` replaces any
    non-positive speed with 1.4 m/s, so every UE in every scenario moves
  * and slow does not mean safe. In the end-to-end run UEs covered about six
    metres inside the horizon and the network still produced 7 handovers whole
    and 34 partitioned: near a boundary, shadowing triggers A3 on displacements
    far too small to predict from speed and cell spacing

So whether a scenario will produce handovers is not derivable from its config,
and this module does not pretend to derive it. The handover column states what
the SHAPE does, and the user decides whether their scenario cares.
"""
from __future__ import annotations

import math

import os
import re
import subprocess

# Cost grows as (UEs per process)^EXPONENT, so with balanced groups the speed-up
# is groups^EXPONENT. Fitted to the table above at ~7% mean error, and 1.37 is
# the SAME exponent measured independently from the UE cost curve (30 -> 150 UEs
# on 8 cells). Two unrelated measurements landing on one number is why this is a
# law here and not a lookup.
EXPONENT = 1.37
_FIT_ERROR_PCT = 7

# Handover survival by CELLS PER GROUP, measured — and the two-cell figure is a
# RANGE, not a number. At 12 cells it came out 21 of 31 (68%); at 6 cells, over
# a matched 3-second window, 8 of 17 (47%). Fewer cells means a larger share of
# each cell's neighbours sit outside its group, so retention gets WORSE as the
# network gets smaller at a fixed group size. The conservative end is used here:
# someone choosing a shape should be told the loss they might get, not the best
# case.
#
# The earlier 6-cell reading of "only 10% lost" was an artefact of comparing
# runs that covered different amounts of simulated time — the whole run had
# reached 1.1 simulated seconds against the split run's 6.1, so it simply had
# not had time to hand over yet. Matching the window moved it from -10% to -53%.
_HANDOVER_KEEP = {1: 0.0, 2: 0.47}          # anything >= 3 keeps them

# WHAT ">= 3 KEEPS THEM" RESTS ON: one measurement, at 12 cells, where 6, 4 and
# 3 cells per group all returned 31-32 handovers against the unpartitioned 2.
# It has NOT been re-checked at 6 cells over a matched window, and the two-cell
# spread above is a warning that it may not hold as well on a small network.


def physical_cores() -> int:
    """PHYSICAL cores, not threads.

    `os.cpu_count()` counts SMT threads — 12 on the 6-core machine this was
    measured on — and sizing the pool from it is a measured mistake: past the
    physical count the served-UE count collapses while every run still reports
    `ok` and the wall clock keeps improving. See nsg-partition-concurrency.
    """
    try:
        out = subprocess.run(["lscpu"], capture_output=True, text=True, timeout=5).stdout
        cores = re.search(r"^Core\(s\) per socket:\s*(\d+)", out, re.M)
        socks = re.search(r"^Socket\(s\):\s*(\d+)", out, re.M)
        if cores and socks:
            return max(1, int(cores.group(1)) * int(socks.group(1)))
    except Exception:
        pass
    # Halving the thread count is the right guess on any SMT machine and errs
    # towards fewer partitions, which is the safe direction.
    return max(1, (os.cpu_count() or 2) // 2)


def _divisors(n: int) -> list[int]:
    return [d for d in range(1, n + 1) if n % d == 0]


# UEs per cell the engine is known to survive. 20 per cell runs clean; at 25 and
# above there is an untouched NS_ASSERT null-pointer abort at t=0. A partition
# can exceed this while the whole run does not, because the split concentrates
# UEs — measured on a real six-cell scenario: 25 UEs/cell whole, 37.5 in the
# busiest partition of three, 75 in the busiest of six.
_UES_PER_CELL_SAFE = 20

# Path-loss exponent for the coupling estimate below. A UMa-ish 3.5. The number
# it feeds is a RATIO of received powers, so it is far less sensitive to this
# than an absolute prediction would be.
_PATHLOSS_EXP = 3.5


def interference_kept(params: dict, groups: int,
                      exponent: float = _PATHLOSS_EXP) -> float | None:
    """What share of each UE's interference SURVIVES this cut, 0..1.

    THIS IS THE QUESTION "is splitting valid for my scenario" reduces to. A
    partition does not approximate the network — it removes every cell outside
    the group from a UE's world entirely. Whether that matters depends only on
    how much of that UE's interference those cells were contributing, and that
    is geometry: computable from positions and powers, before anything runs.

    Measured to discriminate sharply, which is the point:

        one dense cluster of six cells      2 groups 59%   3 groups 28%
        three sites 8 km apart, 2 cells ea  2 groups 67%   3 groups 100%
        two clusters 3 km apart, 3 cells ea 2 groups 100%  3 groups 31%

    The natural cut scores ~100% and every other cut scores badly, so this finds
    the shape a scenario actually supports rather than the fastest one.

    It is NOT a SINR prediction. On the six-cell A/B run it implies +5.9 dB
    where the engine measured +11.35 — beamforming, shadowing and scheduling all
    sit between the two, and a run that is interference-limited on one side can
    be noise-limited on the other. The ratio is the honest output; converting it
    to decibels would claim a precision it does not have.
    """
    try:
        gpos = [tuple(float(v) for v in g.split(",")[:2])
                for g in (params.get("gnb_positions") or "").split(";") if g]
        upos = [tuple(float(v) for v in u.split(",")[:2])
                for u in (params.get("ue_positions") or "").split(";") if u]
    except ValueError:
        return None
    n = len(gpos)
    if not n or not upos or groups < 1 or n % groups:
        return None
    if groups == 1:
        return 1.0
    raw = (params.get("gnb_powers") or "").split(";")
    pw = [float(v) for v in raw] if len(raw) == n else [30.0] * n
    per = n // groups
    grp = [i // per for i in range(n)]

    kept = []
    for ux, uy in upos:
        rx = [10 ** (pw[c] / 10)
              / max(1.0, math.hypot(ux - gpos[c][0], uy - gpos[c][1])) ** exponent
              for c in range(n)]
        # Serving cell = strongest received, which is what a UE would pick.
        serv = max(range(n), key=lambda c: rx[c])
        total = sum(rx[c] for c in range(n) if c != serv)
        if total <= 0:
            continue
        inside = sum(rx[c] for c in range(n) if c != serv and grp[c] == grp[serv])
        kept.append(inside / total)
    return sum(kept) / len(kept) if kept else None


def removed_interference_dbm(params: dict, groups: int,
                             exponent: float = _PATHLOSS_EXP) -> float | None:
    """Absolute power of the interference a cut removes, median over the UEs.

    `interference_kept` answers "what fraction", which is a ratio and therefore
    robust. This answers "how much", which needs the whole link budget and is
    not: log-distance path loss with a free-space reference and isotropic
    elements, no shadowing, no beamforming, no scheduling. On the six-cell A/B
    it implies the removed interference sits ~1 dB above thermal noise and
    should cost ~2.7 dB of SINR, where the engine measured 11.35.

    So this is a SHAPE, not a level: it tracks how the removed power varies
    between scenarios and shapes, and the level is set by a measured offset
    (`_BURST_CALIBRATION_DB`) rather than trusted from here.
    """
    try:
        gpos = [tuple(float(v) for v in g.split(",")[:2])
                for g in (params.get("gnb_positions") or "").split(";") if g]
        upos = [tuple(float(v) for v in u.split(",")[:2])
                for u in (params.get("ue_positions") or "").split(";") if u]
        f_hz = float(params.get("freq_hz") or 3.6e9)
    except (ValueError, TypeError):
        return None
    n = len(gpos)
    if not n or not upos or groups < 2 or n % groups:
        return None
    raw = (params.get("gnb_powers") or "").split(";")
    pw = [float(v) for v in raw] if len(raw) == n else [30.0] * n
    pl0 = 20 * math.log10(4 * math.pi * f_hz / 3e8)
    per = n // groups
    grp = [i // per for i in range(n)]

    def prx(c, ux, uy):
        d = max(1.0, math.hypot(ux - gpos[c][0], uy - gpos[c][1]))
        return pw[c] - (pl0 + 10 * exponent * math.log10(d))

    out = []
    for ux, uy in upos:
        r = [prx(c, ux, uy) for c in range(n)]
        serv = max(range(n), key=lambda c: r[c])
        gone = [10 ** (r[c] / 10) for c in range(n)
                if c != serv and grp[c] != grp[serv]]
        if gone:
            out.append(10 * math.log10(sum(gone)))
    if not out:
        return None
    out.sort()
    return round(out[len(out) // 2], 1)


# What the coupling number means for whether a result can be believed.
def fidelity_verdict(kept: float | None) -> str:
    if kept is None:
        return "unknown"
    if kept >= 0.90:
        return "kept"            # the cut falls on a natural separation
    if kept >= 0.50:
        return "approximate"
    return "different network"


def partition_options(n_cells: int, n_ues: int, cores: int | None = None,
                      splits: dict[int, list[int]] | None = None,
                      params: dict | None = None) -> list[dict]:
    """Every shape this scenario can be split into, with its estimated trade.

    Shapes that would need more processes than there are physical cores are
    included but flagged `over_cores`, because a caller showing only the legal
    ones hides why the fast shape is missing.

    `splits` is the REAL per-partition UE count for each group count, from
    `partition_run.split_counts`. Without it this falls back to dividing the UE
    count by the group count, which is not what happens: UEs go to their nearest
    cell, so a six-cell scenario split three ways came out 75 / 25 / 50 against
    an advertised 50 each — and split six ways, 75 / 0 / 0 / 25 / 25 / 25, with
    one cell carrying 75 UEs and two processes simulating nothing at all.
    """
    cores = cores or physical_cores()
    splits = splits or {}
    out = []
    for g in _divisors(max(1, n_cells)):
        per_group = n_cells // g
        keep = _HANDOVER_KEEP.get(per_group, 1.0)
        # (overridden below when the geometry says nothing crossed this cut)
        real = splits.get(g) or []
        kept = interference_kept(params, g) if params else None
        # HANDOVER LOSS IS ABOUT HANDOVERS THAT WOULD HAVE CROSSED THE CUT, and
        # the same geometry answers both questions: a UE cannot hand over to a
        # cell whose signal it cannot hear. So when the cut leaves a network's
        # interference essentially intact, it is running where nothing crossed
        # — three sites 8 km apart split one per site lose no handovers, even
        # at two cells per group, because there were none between sites to
        # lose. Without geometry this falls back to the cells-per-group table,
        # which assumes the worst: one dense cluster.
        if kept is not None and kept >= 0.90:
            keep = 1.0
        busiest = max(real) if real else (n_ues / g if g else n_ues)
        mean = (sum(real) / len(real)) if real else (n_ues / g if g else n_ues)
        per_cell = busiest / per_group if per_group else busiest
        out.append({
            "groups": g,
            "cells_per_group": per_group,
            # The BUSIEST partition, because a parallel run is paced by its
            # slowest member — the average never runs anywhere.
            "ues_per_group": round(busiest, 1),
            "ues_split": real,
            "ues_per_cell_max": round(per_cell, 1),
            # Measured, not assumed: a split is even only if the busiest
            # partition is CLOSE to the average. 1.25 and not 1.5 — the real
            # 75 / 25 / 50 case sits exactly at 1.5x the mean and is plainly
            # not balanced.
            "balanced": bool(real) and busiest <= 1.25 * mean,
            "empty_groups": sum(1 for c in real if c == 0),
            # The busiest partition would carry a density with a known abort.
            # Absolute on purpose, so the UNPARTITIONED row says so too when the
            # scenario is already dense — hiding that would blame the split for
            # a problem the design brought with it.
            "over_density": per_cell > _UES_PER_CELL_SAFE,
            # How much MORE concentrated this shape is than running whole. This
            # is the part partitioning is responsible for.
            "density_vs_whole": (round(per_cell / (n_ues / n_cells), 2)
                                 if n_cells and n_ues else 1.0),
            # SPEED COMES FROM THE BUSIEST PARTITION, not the group count.
            # `groups ** EXPONENT` assumes the work divides evenly; the law is a
            # UE-count law (cost is per-UE, not per link pair), so the faithful
            # form is the ratio of the whole run's UEs to the busiest
            # partition's. On the evenly-split runs it was fitted to the two are
            # identical — 12 cells / 72 UEs at 4 groups gives 4 ** 1.37 = 6.8
            # and (72/18) ** 1.37 = 6.8, against 6.7 measured. They diverge
            # exactly where the old number lied: a 150-UE scenario split six
            # ways into 75 / 0 / 0 / 25 / 25 / 25 was advertised at 11.6x when
            # one process still carries 75 UEs and the real ceiling is 2.6x.
            "speed_x": round(max(1.0, (n_ues / busiest) ** EXPONENT), 1)
                       if real and busiest else round(g ** EXPONENT, 1),
            "speed_from": "measured split" if real and busiest else "even-split estimate",
            "handover": ("intact" if keep == 1.0 else
                         "reduced" if keep > 0 else "none"),
            "handover_keep_pct": round(keep * 100),
            "over_cores": g > cores,
            # How much of the network's interference this cut LEAVES IN PLACE.
            # The single most decision-relevant number here: a shape that keeps
            # it is a faster version of the same network, and one that does not
            # is a different network that happens to run faster.
            "interference_kept_pct": (None if kept is None else round(kept * 100, 1)),
            "fidelity": fidelity_verdict(kept),
            # SHOULD THE BURSTY TERM BE ON FOR THIS SHAPE? The rule follows from
            # what the term was measured to do, and it is narrow on purpose.
            #
            #   no cut, nothing removed  -> "not applicable"
            #   a natural cut            -> "no" — there is nothing missing, and
            #                               adding interference would invent it
            #   interference removed     -> "for error behaviour" — measured, it
            #                               takes block errors from 2.8% back to
            #                               10.0% against a whole run's 9.6%, and
            #                               moves throughput, PRB and modulation
            #                               by nothing at all
            "burst": ("not applicable" if g == 1 else
                      "no" if (kept is not None and kept >= 0.90) else
                      "for error behaviour"),
            "recommended": False,
        })
    rec = recommend(out, cores)
    for o in out:
        o["recommended"] = (o["groups"] == rec)
    return out


def recommend(options: list[dict], cores: int | None = None) -> int:
    """The most groups that keep handover intact and fit the physical cores.

    Handover, load balancing and coverage-compensated cell sleep are the RIC
    cases this product exists for, so a shape that breaks them is not a faster
    version of the scenario — it is a different one. Speed is maximised only
    within the shapes that keep it.
    """
    cores = cores or physical_cores()
    safe = [o for o in options if o["handover"] == "intact" and not o["over_cores"]
            # A shape whose busiest partition exceeds the density the engine
            # survives is not a faster run, it is an aborted one — and an empty
            # partition is a process simulating nothing while the others carry
            # its load. Neither may be RECOMMENDED; both stay selectable, with
            # the reason on the row.
            and not o.get("over_density") and not o.get("empty_groups")
            # AND IT MUST STILL BE THE SAME NETWORK. Measured on a six-cell
            # cluster over a matched window: three groups removed 74% of every
            # UE's interference, and the engine put 2.8% of transport blocks in
            # error against 9.6% whole, with UE throughput 51% higher and half
            # the handovers. That is not a faster run of the scenario, so it is
            # not what this recommends — it stays selectable, with the number
            # on its row.
            #
            # BLOCK ERRORS AND THROUGHPUT, NOT SINR. `L3servingSINR` is not a
            # usable metric for this and was written off once already: it is a
            # filtered RRC estimate of the serving link, and injecting real
            # interference into the data path moves it the WRONG WAY — measured,
            # a burst term that took block errors from 0% to 24.6% and cut
            # throughput 54% RAISED the reported SINR. An earlier version of
            # this comment quoted the SINR gap; that claim is withdrawn.
            and o.get("fidelity") in (None, "unknown", "kept")]
    return max((o["groups"] for o in safe), default=1)


def advise(n_cells: int, n_ues: int, cores: int | None = None,
           splits: dict[int, list[int]] | None = None,
           params: dict | None = None) -> dict:
    cores = cores or physical_cores()
    opts = partition_options(n_cells, n_ues, cores, splits, params)
    rec = recommend(opts, cores)
    return {
        "cells": n_cells,
        "ues": n_ues,
        "physical_cores": cores,
        "recommended_groups": rec,
        "options": opts,
        "model": {
            "speed_law": f"(UEs / busiest partition's UEs) ** {EXPONENT}",
            "fit_error_pct": _FIT_ERROR_PCT,
            "measured_on": "12 cells / 72 UEs / 20 m/s on nsg-engine:1.25. "
                           "OPTIMISTIC on the shipped engine: re-measured "
                           "2026-09-20 on nsg-engine:1.29-burst at 6 cells / "
                           "36 UEs, 3 groups gave 2.96x with the engine lease "
                           "and 3.19x without, against 3.6x predicted (4.18x on "
                           "1.25). The long-term beamforming cache in 1.29 "
                           "speeds a WHOLE run 1.54x and a two-cell partition "
                           "not at all, so it closed part of the gap the law "
                           "was fitted to. Needs a re-fit across shapes.",
        },
        "caveats": [
            "SPLITTING IS NOT AN APPROXIMATION. Every cell outside a group "
            "leaves that group's world completely: no interference from it, no "
            "handover to it. The 'interference kept' column says how much of "
            "your network that actually removes, and it is geometry — a cut "
            "along a natural separation keeps ~100%, a cut through a dense "
            "cluster keeps very little. Measured on six cells in one cluster, "
            "three groups kept 26% and put 2.8% of transport blocks in error "
            "against 9.6% whole, with 51% higher UE throughput and half the "
            "handovers. The same shape on three sites 8 km apart keeps 100%. "
            "Judge this on BLOCK ERRORS and throughput: L3servingSINR moves the "
            "WRONG WAY under real interference and is not usable here.",
            "THE BURSTY INTERFERENCE TERM restores ONE thing. Where a cut removes "
            "interference, running with it puts the missing cells back as a "
            "statistical term and brings block errors from 2.8% to 10.0% against "
            "a whole run's 9.6% — measured. It changes throughput, PRB use and "
            "the modulation split by nothing, and no setting of its two knobs "
            "does: every power that lowers throughput to the whole run's drags "
            "the link to QPSK, which the real network does not do. So turn it on "
            "for reliability and HARQ questions, leave it off for capacity ones, "
            "and never on a cut that removed nothing. Its level is calibrated on "
            "ONE scenario and should be re-fitted before it is trusted elsewhere.",
            "Speeds are ESTIMATES from a fitted law, not per-scenario measurements "
            "— and currently OPTIMISTIC: the law was fitted before the shipped "
            "engine gained a beamforming cache that makes a whole run 1.54x "
            "faster while doing nothing for a small partition. Re-measured at 3 "
            "groups: 2.96x against 3.6x predicted.",
            "The law counts SIMULATION only. A partitioned run also creates and "
            "removes its engine instances, measured at 15-17 s and almost flat in "
            "the number of groups — so a run of under a minute keeps much less of "
            "the quoted factor than a long one does.",
            "The UE column is the BUSIEST partition, not the average — and the "
            "split is often far from even. Cells are dealt out in config order "
            "and UEs follow their nearest cell, so a real six-cell scenario "
            "split three ways came out 75 / 25 / 50. Watch the split, not the "
            "mean: a partition past about "
            f"{_UES_PER_CELL_SAFE} UEs per cell hits an abort the whole run "
            "never reaches.",
            "An EMPTY partition usually means a handover pair was separated. UEs "
            "are assigned by which cell they are nearest AT THE START, so in a "
            "drive-past scenario every UE begins on the source cell and the "
            "TARGET cell lands in a process of its own with nothing in it — "
            "measured on a 3-cell / 8-UE handover demo split three ways: "
            "4 / 0 / 4. Nothing can be rebalanced away: moving a UE into a "
            "process that does not hold its nearest cell would make it attach "
            "somewhere else, which changes the scenario rather than splitting it.",
            "A parallel run is paced by the SLOWEST group, so an uneven UE split "
            "costs more than the shape does. Assign UEs to their nearest cell: "
            "measured 11-13 per group at 12 cells, against 4-68 if you group by "
            "where they happen to have attached.",
            "More groups than physical cores keeps improving wall clock while "
            "the served-UE count collapses and every run still reports ok. "
            "Watch served UEs, not wall clock.",
            "Handovers reported by a one-cell group are anchor transitions, not "
            "cell-to-cell handovers.",
            "At large cell counts the unpartitioned run is itself too slow to "
            "show handovers or attach every UE, so splitting can RAISE both.",
            "Whether YOUR scenario produces handovers cannot be read off the "
            "config: every UE moves (a zero speed becomes 1.4 m/s) and handovers "
            "were measured at six metres of travel. The column says what the "
            "shape does, not what your run will do.",
        ],
    }
