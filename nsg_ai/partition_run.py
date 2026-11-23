"""Run one scenario as N ns-3 processes of whole cells, and merge the results.

Splitting is the only speed lever with real headroom (see partition_advice for
the measured trade). This is the execution half: cut the engine payload into N,
dispatch them concurrently to N engine instances, and put the answers back
together as one run.

Two things here are easy to get wrong and both were measured the hard way.

THE PAYLOAD IS SLICED, NEVER REGENERATED. `nsoran_params` translates every
position by the shared scene centroid, so building a smaller config with two
cells removed lands the whole network on different coordinates — it would
simulate a different network and call the difference a partitioning effect. Every
per-cell and per-UE string is cut out of the WHOLE run's payload instead.

MEANS ARE WEIGHTED, NEVER AVERAGED. Each partition reports a mean over its own
population, and those populations differ in size. Averaging the means compares
different denominators — the same mistake that made a throughput gap look like
physics three separate times in one day. `measurements` carries `count`, so those
merge exactly; `bler_pct` does not carry its block counts, so it can only be
estimated, and the merged result says so rather than quietly rounding.
"""
from __future__ import annotations

import concurrent.futures
import copy
import json
import math
import urllib.request

# One run needs one core. Past the PHYSICAL core count wall clock keeps improving
# while the served-UE count collapses and every run still reports ok, so the cap
# belongs in the code and not in a comment.
from partition_advice import physical_cores


def _xy(s: str) -> tuple[float, float]:
    a = s.split(",")
    return float(a[0]), float(a[1])


def assign_groups(params: dict, groups: int, burst: bool = False) -> list[dict]:
    """Cut one engine payload into `groups` payloads of whole cells.

    Cells are dealt out in order and each UE goes to its NEAREST cell, hence to
    that cell's group. Nearest-cell is deliberate: grouping by where UEs happened
    to ATTACH in a previous run gave 4-68 UEs per group (most of them parked on
    the LTE anchor because they never finished attaching), while nearest-cell
    gave 11-13. A parallel run is paced by its slowest member, so the split being
    even matters more than which shape is chosen.

    THIS IS NOT A BALANCER AND MUST NOT BECOME ONE. The split follows geometry,
    so it can come out badly uneven — measured, a 3-cell / 8-UE handover demo
    split three ways gives 4 / 0 / 4, and a 6-cell / 150-UE scenario split six
    ways gives 75 / 0 / 0 / 25 / 25 / 25. Evening those out would mean putting a
    UE in a process that does not contain its nearest cell, and ns-3 would then
    attach it to a different cell: a different scenario reported under the same
    name, which is worse than a slow one. An empty partition usually means a
    handover PAIR was separated — every UE starts on the source cell and the
    target ends up alone — and the honest response is to pick a shape that keeps
    them together, which `partition_advice` says on the row.

    What COULD be improved without touching physics is which cells share a
    group: they are dealt out in config order, which is arbitrary, where a
    spatial ordering would keep real neighbours together. That changes handover
    retention, so it needs measuring before it is changed.
    """
    gpos = params["gnb_positions"].split(";")
    upos = params["ue_positions"].split(";")
    n_cells = len(gpos)
    if groups < 1 or groups > n_cells or n_cells % groups:
        raise ValueError(f"{groups} groups does not divide {n_cells} cells evenly")

    per = n_cells // groups
    cell_groups = [list(range(i * per, (i + 1) * per)) for i in range(groups)]
    gxy = [_xy(g) for g in gpos]
    home = [min(range(n_cells), key=lambda c: (gxy[c][0] - ux) ** 2 + (gxy[c][1] - uy) ** 2)
            for ux, uy in (_xy(u) for u in upos)]

    out = []
    for cells in cell_groups:
        sel = [i for i, h in enumerate(home) if h in cells]
        p = copy.deepcopy(params)
        p["n_gnb"], p["n_ue"] = len(cells), len(sel)
        # WHICH cells and UEs of the whole run these are. Every partition is its
        # own ns-3 process, so the engine numbers its cells from the same base
        # and hands every partition an IMSI 1 — merging without this mapping
        # silently collapses six cells into two and stacks partition 2's UE 1 on
        # partition 1's. Underscore-prefixed like `_scene_center`, which the
        # engine already ignores.
        p["_cell_index"], p["_ue_index"] = list(cells), list(sel)
        p["gnb_positions"] = ";".join(gpos[c] for c in cells)
        p["ue_positions"] = ";".join(upos[i] for i in sel)
        # Every slot-aligned string has to be cut with the same knife, or a
        # four-UE partition receives an eight-slot list and reads another
        # partition's speeds, slices or waypoints.
        for key in _UE_SLOTTED:
            v = p.get(key)
            if isinstance(v, str) and v:
                q = v.split(";")
                if len(q) == len(upos):
                    p[key] = ";".join(q[i] for i in sel)
        for key in _CELL_SLOTTED:
            v = p.get(key)
            if isinstance(v, str) and v:
                q = v.split(";")
                if len(q) == n_cells:
                    p[key] = ";".join(q[c] for c in cells)
        if p.get("gnb_faults"):
            p["gnb_faults"] = slice_faults(p["gnb_faults"], cells)
        if burst:
            b = burst_for(params, groups)
            if b:
                p.update(b)
        out.append(p)
    return out


# WHICH PAYLOAD FIELDS ARE SLOT-ALIGNED, by name and with a reason — because
# guessing the names is what broke this. The list used to read "ue_paths",
# "ue_services", "gnb_antennas", "ue_heights"; not one of those four keys
# exists in the payload, while `ue_waypoints` and `gnb_antenna`, which do,
# were not in it. So every partition received the WHOLE run's waypoint list,
# and ns-3 places a UE at its first waypoint — which silently overrides the
# position. Measured: a partition given its own four UE positions reported the
# whole run's first four instead, and shifting its positions 400 m changed
# nothing at all.
#
# A length rule alone would not be safe either: `buildings` came out with
# exactly as many entries as there were cells in one real scenario, and
# slicing it would hand each partition a third of the geometry. So the sets
# are explicit, and `tools/test_partition_run.py` fails if a payload grows a
# ';'-list of matching length that appears in none of them.
_UE_SLOTTED = ("ue_speeds", "ue_slices", "ue_waypoints")
_CELL_SLOTTED = ("gnb_powers", "gnb_antenna", "gnb_sleep", "gnb_bands")
# Deliberately NOT sliced, with the reason:
#   buildings     — geometry, and every partition simulates the same scene
#   slice_quotas  — per SLICE, not per cell or UE
#   gnb_faults    — one entry per FAULT, each naming a cell index; remapped by
#                   slice_faults() rather than cut
_NOT_SLOTTED = ("buildings", "slice_quotas", "gnb_faults")
# gnb_positions / ue_positions are cut above, before this table is reached.
_HANDLED_ELSEWHERE = ("gnb_positions", "ue_positions")


def slice_faults(spec: str, cells: list[int]) -> str:
    """Keep the faults that belong to this partition, renumbered to its cells.

    A fault entry is 'cell,start_s,duration_s,type,value' where `cell` is the
    engine's CELL INDEX. Passing the whole schedule to every partition would
    fire each fault in every process, on whichever cell happened to sit at that
    index — a fault aimed at cell 5 landing on cell 0 of three different runs.

    Type 3 is a handover block, which belongs to the LTE anchor rather than to
    a cell (see `_fault_spec`): every partition has its own anchor, so it is
    kept everywhere and its cell field left alone.
    """
    out = []
    for entry in spec.split(";"):
        if not entry.strip():
            continue
        f = entry.split(",")
        if len(f) >= 4 and f[3].strip() == "3":
            out.append(entry)
            continue
        try:
            g = int(float(f[0]))
        except (ValueError, IndexError):
            continue
        if g in cells:
            out.append(",".join([str(cells.index(g))] + f[1:]))
    return ";".join(out)


# BURSTY EXTERNAL INTERFERENCE: putting back, as a statistical term, the cells
# a partition cannot see. Burstiness is the mechanism and not a detail —
# measured on one partition, a term at load 0.2 took block errors from 0% to
# 24.6% while costing 14% of throughput, where the same power spread constantly
# (load 1.0) cost 54% of throughput for 15% errors. Errors come from the TTIs
# that dip under the MCS threshold, not from a lower mean.
#
# THE LEVEL IS ONE MEASURED POINT, NOT A MODEL. The geometry says how much
# power a cut removes (`removed_interference_dbm`) but its absolute scale is
# not trustworthy — no shadowing, no beamforming, no scheduling. So the level
# is an OFFSET onto that estimate, fitted where the engine's block-error rate
# matched the whole run's:
#
#     6 cells / 36 UEs, 3 groups: geometry says -90.9 dBm removed, and the
#     ladder (-70 -> 7.0%, -62 -> 16.2%) crosses the whole run's 9.6% at about
#     -68 dBm. Offset = +23 dB.
#
# ONE scenario, ONE shape, ONE seed. It is applied only where the cut actually
# removes interference, it is reported on the result, and it must be re-fitted
# before it is trusted anywhere else.
#
# WHAT IT RESTORES, AND WHAT IT CANNOT. Validated on a full three-way split
# against the whole run over a matched window: block errors 2.8% -> 10.0%
# against the whole run's 9.6%. Throughput did not move (+51% before, +56%
# after), nor did PRB use or the modulation split.
#
# And no setting of the two knobs fixes that. The frontier, measured on one
# partition (target: 9.6% errors, 5442 kbps, 3.8% of blocks at QPSK):
#
#     -45 dBm  load 0.2   23.6%   15341 kbps    0.4% QPSK
#     -45      load 0.5   24.7%    8428         64%
#     -45      load 0.8   19.8%    5214         73%
#     -45      load 1.0    4.8%    5992         54%
#     -38      load 1.0   13.0%    4882         19%
#
# Every point that brings throughput down to the target drags the link to QPSK,
# and the real six-cell network does not: it holds 96% of its blocks at 64QAM
# and takes its losses THERE. Injected interference either produces errors
# without touching the reported CQI (bursty, low load) or lowers CQI and with
# it the MCS (constant). Real inter-cell interference does the first while the
# throughput gap comes from somewhere else.
#
# WHERE, IS UNATTRIBUTED. A shared-bottleneck story (each partition gets its
# own LTE anchor) fitted two points at thp x UEs = 195 and 196 — and then a
# controlled run at 4 / 8 / 14 UEs on fixed cells gave 87 / 100 / 181, constant
# on neither axis, with the same configuration measuring 8443 and 12939 kbps on
# two occasions. Twelve-frame runs are too noisy to settle it. Open question,
# not a conclusion.
_BURST_CALIBRATION_DB = 23.0
# Fraction of resource blocks the term hits per evaluation.
_BURST_LOAD = 0.2


def burst_for(params: dict, groups: int) -> dict | None:
    """The bursty-interference knobs for a partition of this shape, or None.

    None when the cut removes nothing worth replacing — a scenario split along
    a natural separation has no missing interference to put back, and adding
    some would be inventing it.
    """
    from partition_advice import interference_kept, removed_interference_dbm
    kept = interference_kept(params, groups)
    if kept is None or kept >= 0.90:
        return None
    removed = removed_interference_dbm(params, groups)
    if removed is None:
        return None
    return {"burst_dbm": round(removed + _BURST_CALIBRATION_DB, 1),
            "burst_load": _BURST_LOAD}


def split_counts(params: dict, groups: int) -> list[int]:
    """How many UEs each partition would ACTUALLY get, without running anything.

    The adviser used to divide the UE count by the group count and present that
    as "UEs each". It is not: UEs go to their nearest cell, and cells are dealt
    out in config order, so a real six-cell scenario split three ways came out
    75 / 25 / 50 against an advertised 50 / 50 / 50. That matters twice over — a
    parallel run is paced by its slowest member, and the busiest partition can
    carry a per-cell density the whole run never reaches.
    """
    try:
        return [p["n_ue"] for p in assign_groups(params, groups)]
    except (ValueError, KeyError, TypeError):
        return []


def _wmean(pairs: list[tuple[float, float]]) -> float | None:
    """Weighted mean. `pairs` is (value, weight)."""
    tot = sum(w for _, w in pairs if w)
    if not tot:
        return None
    return sum(v * w for v, w in pairs if w) / tot


# What a shape costs in handovers, by CELLS PER GROUP. Measured at 12 cells:
# 31/32/31 handovers at 6/4/3 cells per group, 21 at 2, and at 1 the only ones
# left are anchor transitions. Kept here as well as in partition_advice because
# the RESULT has to carry its own verdict — the picker's warning does not travel
# with a run that gets saved, exported and compared months later.
_SHAPE_VERDICT = {1: "none", 2: "reduced"}


def merge(results: list[dict], cells_per_group: int | None = None) -> dict:
    """Combine N partition results into one, saying what could not be combined.

    `cells_per_group` lets the merged result state the SHAPE it came from and
    what that shape does to handover. Without it a stored result is a set of
    numbers with no record of the trade that produced them.
    """
    ok = [r for r in results if r.get("status") == "ok"]
    spans = [(r.get("energy") or {}).get("span_s") for r in results]
    spans = [s for s in spans if s]

    names: dict[str, list[dict]] = {}
    for r in results:
        for name, m in (r.get("measurements") or {}).items():
            names.setdefault(name, []).append(m)

    measurements = {}
    for name, ms in names.items():
        counts = [m.get("count") or 0 for m in ms]
        vals = [(m.get("mean"), m.get("count") or 0) for m in ms if m.get("mean") is not None]
        mins = [m.get("min") for m in ms if m.get("min") is not None]
        maxs = [m.get("max") for m in ms if m.get("max") is not None]
        measurements[name] = {
            "count": sum(counts),
            "min": min(mins) if mins else None,
            "max": max(maxs) if maxs else None,
            # Exact: every partition reports how many samples its mean is over.
            "mean": round(_wmean(vals), 2) if vals else None,
        }

    energy_cells: dict = {}
    total_w = 0.0
    for r in results:
        e = r.get("energy") or {}
        for cell, v in (e.get("per_cell") or {}).items():
            energy_cells[str(cell)] = v
        total_w += float(e.get("total_w") or 0.0)

    ues = [r.get("ue_count") or 0 for r in results]
    bler = [( (r.get("derived") or {}).get("bler_pct"), r.get("ue_count") or 0) for r in results]
    bler = [(v, w) for v, w in bler if v is not None]

    merged = {
        "engine": (results[0].get("engine") if results else None),
        "partitioned": True,
        "partitions": len(results),
        "status": "ok" if len(ok) == len(results) and results else "partial",
        "ue_count": sum(ues),
        "handovers": sum(r.get("handovers") or 0 for r in results),
        "measurements": measurements,
        "energy": {"total_w": round(total_w, 2), "per_cell": energy_cells},
        "derived": {
            # UE-weighted, not exact: the engine reports bler_pct but not the
            # transport-block counts behind it, so the true denominator is not
            # available to weight by. Flagged in `merge_notes` rather than
            # presented as if it were the same number a single run would give.
            "bler_pct": round(_wmean(bler), 2) if bler else None,
            "bler_pct_is_estimate": True,
        },
        "partition_shape": {
            "groups": len(results),
            "cells_per_group": cells_per_group,
            # "intact" | "reduced" | "none" — see _SHAPE_VERDICT.
            "handover": (_SHAPE_VERDICT.get(cells_per_group, "intact")
                         if cells_per_group else None),
        },
        "per_partition": [
            {"status": r.get("status"), "ue_count": r.get("ue_count"),
             "handovers": r.get("handovers"),
             "span_s": (r.get("energy") or {}).get("span_s"),
             "bler_pct": (r.get("derived") or {}).get("bler_pct")}
            for r in results
        ],
        "merge_notes": [],
    }

    # The horizon is NOT one number. Partitions cover different amounts of
    # simulated time, and quoting the mean would overstate what the slowest one
    # actually saw — which is the horizon any cross-partition claim is limited to.
    if spans:
        merged["span_s_min"] = min(spans)
        merged["span_s_max"] = max(spans)
        merged["span_s"] = min(spans)
        if max(spans) - min(spans) > 0.05 * max(spans):
            merged["merge_notes"].append(
                f"partitions covered {min(spans)}-{max(spans)} simulated seconds; "
                f"span_s reports the SLOWEST, which is the horizon shared by all of them")
    if bler:
        merged["merge_notes"].append(
            "bler_pct is UE-weighted across partitions, not exact — the engine "
            "reports the percentage without the block counts to weight by")
    # The shape's own warning, on the RESULT. Previously only the one-cell case
    # said anything, and only here — so a run split two cells per group looked
    # unqualified, and any run at all looked unqualified once it had been saved
    # and reopened away from the picker that warned about it.
    verdict = merged["partition_shape"]["handover"]
    if verdict == "none":
        merged["merge_notes"].append(
            "ONE CELL PER PROCESS: a UE has no target to hand over to, so handover, "
            "load balancing and coverage-compensated cell sleep did not happen in this "
            "run. Any handover counted here is an anchor transition. Valid for a "
            "capacity study, not for a RIC control test.")
    elif verdict == "reduced":
        merged["merge_notes"].append(
            "TWO CELLS PER PROCESS: handovers crossing a group boundary did not happen "
            "— measured, between a third and a half of them go missing, and the smaller the network the worse it gets.")
    singles = sum(1 for r in results if (r.get("requested") or {}).get("n_gnb") == 1
                  or r.get("_cells") == 1)
    if singles and verdict != "none":
        merged["merge_notes"].append(
            f"{singles} partition(s) hold ONE cell, so any handover they report is an "
            f"anchor transition, not cell-to-cell — there is no target cell in the process")
    if len(ok) != len(results):
        merged["merge_notes"].append(
            f"{len(results) - len(ok)} of {len(results)} partitions did not return ok")
    return merged


def run_partitioned(params: dict, groups: int, ports: list[int],
                    timeout: int = 7200) -> dict:
    """Dispatch the partitions concurrently and merge what comes back.

    `ports` are engine instances — one run per instance, never two: the engine's
    launcher writes fifteen fixed /tmp paths, so two runs sharing an instance
    read each other's inputs.
    """
    payloads = assign_groups(params, groups)
    cap = physical_cores()
    if len(payloads) > min(len(ports), cap):
        raise ValueError(
            f"{len(payloads)} partitions need {len(payloads)} engine instances on "
            f"{len(payloads)} physical cores; have {len(ports)} instance(s) and {cap} core(s). "
            f"Past the physical count the served-UE count collapses while every run still "
            f"reports ok.")

    def one(idx_payload):
        idx, p = idx_payload
        req = urllib.request.Request(f"http://127.0.0.1:{ports[idx]}/run",
                                     data=json.dumps(p).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        d["_cells"] = p["n_gnb"]
        return d

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(payloads)) as ex:
        results = list(ex.map(one, enumerate(payloads)))
    return merge(results, cells_per_group=payloads[0]["n_gnb"])
