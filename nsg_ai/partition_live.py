"""Run a LIVE, streaming scenario as N processes and merge the streams.

`partition_run` does this for the batch path (`/ric/validate-nsoran`), which
asks one question and gets one answer. This is the harder half, and it is what
Campaign and the RIC Test real engine need: those drive `run_batch_simulation`,
which holds a socket open and collects a KPM FRAME PER TICK. Partitioning them
means merging N time-series into one, not N numbers into one.

Three things make that more than a concatenation, and all three are silent
failures if skipped — the merged run still renders, it just describes a network
that never existed.

IDENTITIES COLLIDE. Every partition is its own ns-3 process, so each numbers its
cells from the same base and hands its first UE IMSI 1. Merging raw would stack
partition 2's UE 1 on partition 1's and collapse six cells into two. Both are
renumbered back to the whole run's identities using the `_cell_index` /
`_ue_index` that `assign_groups` records.

CLOCKS DIVERGE. Partitions do not advance at the same rate — measured 0.7 to 1.1
simulated seconds on one three-way run — so frame i of one is not frame i of
another. Frames are aligned on `sim_t` (the engine's own simulated clock) with
the last frame at or before each instant carried forward, and the merged
timeline is cut to the window every partition actually covered.

SLICE TAGS ARE POSITIONAL. `_slice_of_imsi_map` maps IMSI to slice by attach
order over the WHOLE config, so a partition reading it would tag its UE 1 with
the first UE group's slice no matter which UE that actually is. Each partition
gets its own map, derived from the whole map through `_ue_index`.

What this does NOT recover is the physics a split removes: a UE is not
interfered with by cells outside its process, and cannot hand over to them. That
trade belongs to the shape and is reported by `partition_advice` and carried on
the result — it is not something the merge can undo.
"""
from __future__ import annotations

import asyncio
import logging

from partition_run import assign_groups, _SHAPE_VERDICT

logger = logging.getLogger(__name__)


def submap(whole: dict[int, str], ue_index: list[int]) -> dict[int, str]:
    """The partition's own imsi -> slice map.

    Whole-run IMSI is `position + 1`; the partition restarts at 1, so its IMSI j
    is whole-run UE `ue_index[j-1]`.
    """
    return {j + 1: whole[i + 1] for j, i in enumerate(ue_index) if (i + 1) in whole}


# The engine's first mmWave cell id. The scenario is built as one LTE hub — cell
# 1, the anchor every UE attaches to first — plus N mmWave cells numbered from
# 2, which is why the UI seeds its cell list with `2 + i` and why a partition of
# two cells reports 2 and 3 no matter which two cells of the network they are.
#
# THIS USED TO BE INFERRED, with min() over the cells a frame reported, and that
# was wrong in a way that looked like it worked. A cell only appears in a frame
# once it HAS a UE, so the first frame can easily hold only the higher of the
# two — the base was then learned as 3, every partition's cell 2 failed the
# offset check and stayed 2, and three partitions' first cells stacked onto one
# id. Observed live: "cell 2 · 12 UE" for a network whose cells hold four each,
# and a control command for cell 2 dropped as belonging to no process.
#
# So it is a constant, from the scenario's construction, and available before
# any frame arrives — which is also what lets a control command work at t=0.
# `check_cell_base` keeps the assumption honest instead of silent.
CELL_BASE = 2


def check_cell_base(results: list[dict]) -> str | None:
    """Did any partition report a cell id the assumed base cannot explain?

    Returns a note for `partition_notes`, or None. Cheap insurance: if the
    engine's numbering ever moves, this says so instead of silently folding
    cells onto each other.
    """
    ids = {c["cell"] for r in results for f in (r.get("frames") or [])
           for c in (f.get("cells") or []) if c.get("cell") is not None}
    low = [i for i in ids if i < CELL_BASE]
    return (f"cells {sorted(low)} are below the assumed first-cell id {CELL_BASE} — "
            f"the engine's numbering has changed and the merged cell ids are "
            f"not trustworthy") if low else None


def global_cell(local, cell_index: list[int], base: int | None):
    """A partition's own cell id, as the whole run numbers it."""
    if local is None or base is None:
        return local
    off = int(local) - base
    return base + cell_index[off] if 0 <= off < len(cell_index) else local


def local_cell(gid, cell_index: list[int], base: int | None):
    """The inverse, for sending a control command back to the right process."""
    if gid is None or base is None:
        return None
    try:
        off = cell_index.index(int(gid) - base)
    except ValueError:
        return None
    return base + off


def relabel_frame(f: dict, cell_index: list[int], ue_index: list[int],
                  base: int | None) -> dict:
    """One frame, rewritten into whole-run cell ids and IMSIs."""
    def gc(x):
        return global_cell(x, cell_index, base)

    def gu(imsi):
        j = int(imsi) - 1
        return ue_index[j] + 1 if 0 <= j < len(ue_index) else imsi

    g = dict(f)
    g["ues"] = [{**u, "imsi": gu(u.get("imsi")), "cell": gc(u.get("cell"))}
                for u in (f.get("ues") or [])]
    g["cells"] = [{**c, "cell": gc(c.get("cell"))} for c in (f.get("cells") or [])]
    g["ue_ids"] = [gu(i) for i in (f.get("ue_ids") or [])]
    e = f.get("energy")
    if e and e.get("per_cell"):
        g["energy"] = {**e, "per_cell": {str(gc(int(k))): v for k, v in e["per_cell"].items()}}
    sl = f.get("sleep")
    if sl and sl.get("per_cell"):
        g["sleep"] = {**sl, "per_cell": {str(gc(int(k))): v for k, v in sl["per_cell"].items()}}
    return g


def _relabel(result: dict, cell_index: list[int], ue_index: list[int],
             base: int | None) -> dict:
    """Rewrite one partition's frames into whole-run cell ids and IMSIs."""
    return {**result, "frames": [relabel_frame(f, cell_index, ue_index, base)
                                 for f in (result.get("frames") or [])]}


class Rolling:
    """One partition's newest known state, accumulated PER NAME.

    A frame is not a complete observation of its partition. The platform emits
    one per KPM indication message, each gNB sends its own CU-CP report at its
    own time, and most frames in between carry only positions or energy —
    measured on a 50-window run, 11 frames of 39 carried `DRB.UEThpDl` at all.

    So "the latest frame" is the wrong unit to carry forward. Taking it whole
    threw away every counter the chosen frame happened not to contain: a split
    run came back with 13 measurement names against the same scenario's 88 run
    whole, and the Compare chart showed blanks for throughput, PRB and every
    transport-block counter. Carrying each NAME forward keeps them, which is
    what the live view has always done for its own headline cards.
    """

    __slots__ = ("meas", "ues", "cells", "slices", "handovers", "energy",
                 "sleep", "sim_t", "fresh_cells")

    def __init__(self) -> None:
        self.meas: dict[str, dict] = {}
        self.ues: dict = {}
        self.cells: dict = {}
        self.slices: dict[str, float] = {}
        self.handovers = 0
        self.energy: dict | None = None
        self.sleep: dict | None = None
        self.sim_t: float | None = None
        self.fresh_cells: list = []

    def feed(self, f: dict) -> None:
        for name, m in (f.get("measurements") or {}).items():
            # A name present but empty must not erase a real earlier value.
            if isinstance(m, dict) and m.get("mean") is not None:
                self.meas[name] = m
        for u in (f.get("ues") or []):
            self.ues[u.get("imsi")] = u
        for c in (f.get("cells") or []):
            self.cells[c.get("cell")] = c
        for name, kbps in (f.get("slices") or {}).items():
            self.slices[name] = kbps
        if f.get("handovers") is not None:
            self.handovers = f["handovers"]
        if f.get("energy"):
            self.energy = f["energy"]
        if f.get("sleep"):
            self.sleep = f["sleep"]
        if f.get("sim_t") is not None:
            self.sim_t = f["sim_t"]
        if f.get("kpm_fresh_cells"):
            self.fresh_cells = f["kpm_fresh_cells"]

    def frame(self) -> dict:
        return {
            "measurements": self.meas,
            "ues": list(self.ues.values()),
            "cells": list(self.cells.values()),
            "slices": dict(self.slices),
            "handovers": self.handovers,
            "energy": self.energy,
            "sleep": self.sleep,
            "sim_t": self.sim_t,
            "kpm_fresh_cells": self.fresh_cells,
        }


def _merge_frames(per_part: list[list[dict]]) -> list[dict]:
    """Align N relabelled frame streams on `sim_t` and union them.

    The merged timeline runs from the instant every partition has produced a
    frame to the instant the SLOWEST one stopped — the window all of them
    actually cover. Quoting anything wider would report one partition's late
    frames as if the rest of the network had been simulated that far.
    """
    streams = [[f for f in fs if f.get("sim_t") is not None] for fs in per_part]
    if not streams or any(not s for s in streams):
        # No simulated clock to align on (an old engine, or a partition that
        # produced nothing). Fall back to index alignment, cut to the shortest.
        n = min((len(fs) for fs in per_part), default=0)
        return [union_frames([fs[i] for fs in per_part], i) for i in range(n)]

    for s in streams:
        s.sort(key=lambda f: f["sim_t"])
    start = max(s[0]["sim_t"] for s in streams)
    end = min(s[-1]["sim_t"] for s in streams)
    instants = sorted({f["sim_t"] for s in streams for f in s
                       if start <= f["sim_t"] <= end})

    cursors = [0] * len(streams)
    rolling = [Rolling() for _ in streams]
    started = [False] * len(streams)
    out = []
    for tick, t in enumerate(instants):
        for i, s in enumerate(streams):
            # EVERY frame up to this instant is folded in, not just the last
            # one. Several frames share a simulated instant and they carry
            # different counters; keeping only the newest discards the rest.
            while cursors[i] < len(s) and s[cursors[i]]["sim_t"] <= t:
                rolling[i].feed(s[cursors[i]])
                started[i] = True
                cursors[i] += 1
        if not all(started):
            continue
        out.append(union_frames([r.frame() for r in rolling], tick, sim_t=t))
    return out


def union_frames(frames: list[dict], tick: int, sim_t: float | None = None) -> dict:
    """One instant of the whole network, from one frame per partition.

    Public because the LIVE proxy calls it per arriving frame, where there is no
    finished stream to merge — only the newest frame each partition has sent.
    """
    ues = [u for f in frames for u in (f.get("ues") or [])]
    cells = [c for f in frames for c in (f.get("cells") or [])]

    meas: dict = {}
    for f in frames:
        for name, s in (f.get("measurements") or {}).items():
            if not isinstance(s, dict):
                continue
            meas.setdefault(name, []).append(s)
    measurements = {}
    for name, ss in meas.items():
        # Count-weighted, never averaged: partitions hold different numbers of
        # UEs, so averaging their means compares different denominators.
        wsum = sum((s.get("count") or 0) for s in ss)
        vals = [(s.get("mean"), s.get("count") or 0) for s in ss if s.get("mean") is not None]
        mins = [s["min"] for s in ss if s.get("min") is not None]
        maxs = [s["max"] for s in ss if s.get("max") is not None]
        measurements[name] = {
            "count": wsum,
            "mean": (round(sum(v * w for v, w in vals) / wsum, 2)
                     if wsum and vals else None),
            "min": min(mins) if mins else None,
            "max": max(maxs) if maxs else None,
        }

    slices: dict[str, float] = {}
    for f in frames:
        for name, kbps in (f.get("slices") or {}).items():
            slices[name] = round(slices.get(name, 0.0) + float(kbps or 0.0), 1)

    energy_cells: dict = {}
    total_w = 0.0
    has_energy = False
    for f in frames:
        e = f.get("energy") or {}
        if e:
            has_energy = True
            total_w += float(e.get("total_w") or 0.0)
            energy_cells.update(e.get("per_cell") or {})

    sleep_cells: dict = {}
    has_sleep = False
    for f in frames:
        sl = f.get("sleep") or {}
        if sl:
            has_sleep = True
            sleep_cells.update(sl.get("per_cell") or {})

    out = {
        "type": "kpm",
        "tick": tick,
        "measurements": measurements,
        "ues": ues,
        "cells": sorted(cells, key=lambda c: (c.get("cell") is None, c.get("cell"))),
        "slices": slices,
        "handovers": sum(f.get("handovers") or 0 for f in frames),
        "ue_ids": [u.get("imsi") for u in ues],
        # A frame is not an independent observation; the flags travel so the
        # merged stream can still be de-duplicated the same way.
        "kpm_fresh": any(f.get("kpm_fresh") for f in frames),
        "kpm_fresh_cells": sorted({c for f in frames
                                   for c in (f.get("kpm_fresh_cells") or [])}),
        "partitioned": True,
        "partitions": len(frames),
    }
    if sim_t is not None:
        out["sim_t"] = sim_t
    if has_energy:
        out["energy"] = {"total_w": round(total_w, 1), "per_cell": energy_cells}
    if has_sleep:
        out["sleep"] = {
            "per_cell": sleep_cells,
            "cells_asleep": sum(1 for d in sleep_cells.values()
                                if isinstance(d, dict) and d.get("state") == "asleep"),
            "wake_events": sum((d.get("wakes") or 0) for d in sleep_cells.values()
                               if isinstance(d, dict)),
        }
    return out


def merge_live(results: list[dict], payloads: list[dict]) -> dict:
    """Combine N `run_batch_simulation` results into one, saying what it cost."""
    relabelled = [_relabel(r, p.get("_cell_index") or [], p.get("_ue_index") or [],
                           CELL_BASE)
                  for r, p in zip(results, payloads)]
    frames = _merge_frames([r["frames"] for r in relabelled])

    statuses = [r.get("status") for r in results]
    # The worst outcome wins: a merged run that says "done" while one of its
    # partitions timed out is the kind of result that gets quoted for months.
    order = ["error", "busy", "engine-closed", "timeout", "wall-capped", "done"]
    status = next((s for s in order if s in statuses), statuses[0] if statuses else "error")

    cells_per_group = payloads[0].get("n_gnb") if payloads else None
    verdict = _SHAPE_VERDICT.get(cells_per_group, "intact") if cells_per_group else None
    notes = [f"run as {len(results)} processes of {cells_per_group} cell(s); "
             f"handover {verdict}"]
    if verdict == "none":
        notes.append("ONE CELL PER PROCESS: no UE had a target to hand over to, so "
                     "handover, load balancing and coverage-compensated cell sleep "
                     "did not happen. Valid for a capacity study, not a RIC control test.")
    elif verdict == "reduced":
        notes.append("TWO CELLS PER PROCESS: handovers crossing a group boundary did "
                     "not happen — measured, between a third and a half of them go missing, and the smaller the network the worse it gets.")
    spans = [r.get("n_frames") or 0 for r in results]
    if frames:
        notes.append(f"{len(frames)} merged frames from partitions of {spans} — the "
                     f"timeline is cut to the window every partition covered")
        # The merged count is much SMALLER than the raw counts and that is not a
        # loss. A live frame is not an independent observation: the platform
        # emits one per KPM indication message, and measured, that is ~11 per
        # simulated second per cell against a ~1 Hz E2 round. Aligning on `sim_t`
        # collapses those duplicates, so a merged stream carries one frame per
        # distinct simulated instant. Saying it here because `ticks` in the
        # summary then looks alarmingly low next to a whole run's, and the
        # difference is the de-duplication, not the horizon.
        raw = sum(spans)
        if raw > len(frames) * 1.5:
            notes.append(
                f"the merged stream carries ONE frame per distinct simulated instant "
                f"({len(frames)} of {raw} raw frames) — the engine emits several frames "
                f"per instant and aligning on sim_t collapses them, so do not compare "
                f"this tick count with an unpartitioned run's")
    if len(set(statuses)) > 1:
        notes.append(f"partitions ended differently: {statuses}")
    drift = check_cell_base(results)
    if drift:
        notes.append(drift)

    return {
        "status": status,
        "error": next((r.get("error") for r in results if r.get("error")), None),
        "frames": frames,
        "status_lines": [ln for r in results for ln in (r.get("status_lines") or [])][-60:],
        "n_frames": len(frames),
        "fault_events": [e for r in results for e in (r.get("fault_events") or [])],
        "partitioned": True,
        "partitions": len(results),
        "partition_shape": {"groups": len(results), "cells_per_group": cells_per_group,
                            "handover": verdict},
        "partition_notes": notes,
        "per_partition": [
            {"status": r.get("status"), "n_frames": r.get("n_frames"),
             "cells": p.get("n_gnb"), "ues": p.get("n_ue")}
            for r, p in zip(results, payloads)
        ],
    }


async def run_partitioned_live(cfg, params: dict, groups: int, addrs: list[str],
                               frames_target: int = 80, wall_cap_s: float = 900.0,
                               on_progress=None, burst: bool = False) -> dict:
    """Drive `groups` live sessions concurrently and merge their streams.

    `addrs` are LIVE bridge addresses (host:port), one per partition — never two
    partitions on one instance: the engine serves one session at a time and
    writes fixed /tmp paths, so sharing means one run reading the other's input.
    """
    from nsoran_live import run_batch_simulation, _slice_of_imsi_map

    # `burst` puts the cells a partition cannot see back as a statistical
    # interference term. Off by default: it is calibrated on ONE scenario, and
    # a term that is wrong is worse than a term that is absent — an absent one
    # is at least visible in `interference_kept`.
    payloads = assign_groups(params, groups, burst=burst)
    if len(addrs) < len(payloads):
        raise ValueError(f"{len(payloads)} partitions need {len(payloads)} live engine "
                         f"instances; got {len(addrs)}")
    whole = _slice_of_imsi_map(cfg)

    # Progress is reported for the SLOWEST partition, because that is the one
    # pacing the run. Reporting the fastest would show a run finishing while
    # most of the network is still going.
    state: dict[int, dict] = {}

    def progress_for(i):
        def cb(info):
            state[i] = info
            if on_progress and state:
                slowest = min(state.values(), key=lambda d: d.get("n_frames", 0))
                on_progress({**slowest, "partitions": len(payloads),
                             "per_partition": [state.get(k, {}).get("n_frames", 0)
                                               for k in range(len(payloads))]})
        return cb

    results = await asyncio.gather(*[
        run_batch_simulation(cfg, p, frames_target=frames_target,
                             wall_cap_s=wall_cap_s, on_progress=progress_for(i),
                             live_addr=addrs[i],
                             slice_map=submap(whole, p.get("_ue_index") or []))
        for i, p in enumerate(payloads)
    ], return_exceptions=True)

    out = []
    for i, r in enumerate(results):
        if isinstance(r, BaseException):
            logger.exception("partition %d failed", i, exc_info=r)
            out.append({"status": "error", "error": str(r), "frames": [],
                        "status_lines": [], "n_frames": 0, "fault_events": []})
        else:
            out.append(r)
    merged = merge_live(out, payloads)
    b = payloads[0].get("burst_dbm") if payloads else None
    if b is not None:
        merged["burst"] = {"dbm": b, "load": payloads[0].get("burst_load")}
        merged["partition_notes"].append(
            f"bursty external interference ON at {b} dBm / load "
            f"{payloads[0].get('burst_load')} — the cells outside each group, put "
            f"back as a statistical term. Calibrated on ONE scenario against its "
            f"whole run's block-error rate; it reproduces errors, NOT throughput.")
    return merged
