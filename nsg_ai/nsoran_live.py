"""
NSG live external-xApp proxy (Phase 2).

Bridges a WebSocket client — the customer's own xApp, or the browser observer —
to the ns-O-RAN LIVE bridge running in the engine container (TCP :8091). This is
what puts a customer's AI in the loop of the REAL engine over the accessible
WebSocket surface (no FlexRIC SDK needed on their side):

  client WS  --control JSON-->  this proxy  --line-->  live bridge (:8091)
                                                       -> FIFO -> bridge xApp
                                                       -> real E2SM-RC -> live RAN
  client WS  <--KPM frames--    this proxy  <--stdout-- live bridge (KPM)

The bridge streams the xApp's raw KPM stdout; we parse it into per-indication
JSON frames (same canonicalisation as engine_service.py so per-UE/cell values
collapse). Control text from the client is forwarded verbatim as a command line
(the bridge xApp fires one real RC CONTROL per line).
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import time
import uuid
from typing import Callable

from fastapi import WebSocket, WebSocketDisconnect

import kpm_health
from engine_envelope import (clamp_gnb, clamp_ue, ue_alloc, config_bw_mhz,
                             config_numerology)
from llm_agent import decide_xapp_actions
# The engine's first mmWave cell id. Imported at module level because both the
# proxy and its per-partition log tags need it before any frame arrives.
from partition_live import CELL_BASE
from schema import NSGSimulationConfig

logger = logging.getLogger(__name__)

# Where the container live bridge listens (mirrored localhost across WSL distros).
_LIVE_ADDR = os.environ.get("NSG_NSORAN_LIVE", "127.0.0.1:8091")

_MEAS_RE = re.compile(r"^\s*([\w.:\-]+)\s*=\s*(-?[\d.]+)")
_UE_RE = re.compile(r"amf_ue_ngap_id = (\d+)")
# Non-KPM metadata to drop from the measurement set (mirrors engine_service.py).
_META = {"Address", "ID", "Nodes", "PORT", "nodes", "amf_ue_ngap_id", "latency", "ran_ue_id"}


def _canon(nm: str) -> str:
    nm = re.sub(r"3gpp_cell_\d+_UEID_\d+", "", nm)   # L3servingSINR per cell/UE
    nm = nm.replace("_Filter", "")
    nm = re.sub(r"\.UEID$", "", nm)
    return nm


def _summarise(meas: dict[str, list[float]]) -> dict:
    return {
        n: {"count": len(v), "min": round(min(v), 2),
            "max": round(max(v), 2), "mean": round(sum(v) / len(v), 2)}
        for n, v in sorted(meas.items())
    }


# L3servingSINR3gpp_cell_C_UEID_000M = V  → serving cell C, UE IMSI M, SINR V.
_SINR_UE_RE = re.compile(r"L3servingSINR3gpp_cell_(\d+)_UEID_0*(\d+)\s*=\s*(-?[\d.]+)")
_THP_RE = re.compile(r"^\s*DRB\.UEThpDl\S*\s*=\s*(-?[\d.]+)")
_PRB_RE = re.compile(r"^\s*RRU\.PrbUsedDl\S*\s*=\s*(-?[\d.]+)")
_UL_RE = re.compile(r"^\s*DRB\.PdcpSduBitRateUl\S*\s*=\s*(-?[\d.]+)")
_DELAY_RE = re.compile(r"^\s*DRB\.PdcpSduDelayDl\S*\s*=\s*(-?[\d.]+)")
# Energy lines the bridge tails out of the ns-3 log: cumulative J + per-print
# delta per cell (the sim prints every 0.1 simulated seconds).
_ENERGY_RE = re.compile(r"\[NSG-ENERGY\] cell=(\d+) j=([\d.eE+-]+) diff=([\d.eE+-]+)")
# Real UE positions (bridge-tailed), in the engine's SCENARIO coordinates.
# `cell` is ns-3's own "UE connected to Cell", printed every 0.1 SIMULATED
# seconds — the authoritative serving cell, and the reason this regex captures a
# fourth group. See _SERVING_FROM_POS below for why that matters.
_POS_RE = re.compile(
    r"\[NSG-POS\] imsi=(\d+) x=(-?[\d.]+) y=(-?[\d.]+) cell=(\d+) t=([\d.]+)")
# An [NSG-POS] line reports the LTE anchor and the mmWave cells through the same
# `cell=` field. Which id is the anchor depends on scenario build order, so it is
# learned from the stream (see Parser.mmwave_cells), never assumed.
# RU sleep state per cell (bridge-tailed): why a cell's measured power dropped.
_SLEEP_RE = re.compile(
    r"\[NSG-SLEEP\] cell=(\d+) depth=(\w+) state=(\w+) sleeps=(\d+) wakes=(\d+) "
    r"sleep_s=([\d.eE+-]+) sleep_pct=([\d.eE+-]+)")
# The engine pins its scene at this centre; positions convert back to CONFIG
# coordinates as cfg = scen - CENTER + scene_centroid (see main._apply_offset).
_SCEN_CENTER = 2000.0

# A live session has no natural end — the operator can leave the tab open for
# hours — so the frames kept for persistence are capped. At the measured ~10
# frames per simulated second this is a long session, and the cap protects the
# backend's memory rather than the database.
_LIVE_MAX_FRAMES = 20000


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _cells_from_ues(ues: list[dict], energy: dict | None = None,
                    sleep: dict | None = None) -> list[dict]:
    """Aggregate per-UE rows into a per-cell table (by serving cell), joined
    with the latest measured per-cell power when the bridge has tailed it."""
    by_cell: dict[int, dict] = {}
    # A sleeping cell has no UEs — it must still appear in the table, or the
    # one row that explains the energy saving is the one row that goes missing.
    for c in (sleep or {}):
        by_cell.setdefault(c, {"cell": c, "ues": 0, "thp": 0.0, "prb": 0.0,
                               "ul": 0.0, "sinr": []})
    for u in ues:
        c = u.get("cell")
        if c is None:
            continue
        d = by_cell.setdefault(c, {"cell": c, "ues": 0, "thp": 0.0, "prb": 0.0,
                                   "ul": 0.0, "sinr": []})
        d["ues"] += 1
        d["thp"] += u.get("thp_kbps") or 0.0
        d["prb"] += u.get("prb") or 0.0
        d["ul"] += u.get("ul_kbps") or 0.0
        if u.get("sinr") is not None:
            d["sinr"].append(u["sinr"])
    energy = energy or {}
    sleep = sleep or {}
    return [
        {"cell": d["cell"], "ues": d["ues"], "thp_kbps": round(d["thp"], 1),
         "prb": round(d["prb"], 1), "ul_kbps": round(d["ul"], 1),
         "sinr": round(sum(d["sinr"]) / len(d["sinr"]), 1) if d["sinr"] else None,
         "power_w": (energy.get(d["cell"]) or {}).get("w"),
         # RU sleep state: "awake" / "asleep" / "waking" + the depth it uses.
         "sleep_state": (sleep.get(d["cell"]) or {}).get("state"),
         "sleep_depth": (sleep.get(d["cell"]) or {}).get("depth"),
         "sleep_pct": (sleep.get(d["cell"]) or {}).get("sleep_pct")}
        for d in sorted(by_cell.values(), key=lambda x: x["cell"])
    ]


class _KpmAccumulator:
    """Turns the bridge's raw xApp stdout into one JSON frame per KPM indication.
    Keeps per-UE rows (serving cell, SINR, DL/UL throughput, PRB, PDCP delay,
    slice tag), a per-cell table, per-slice DL totals, a running handover count
    (serving-cell changes between frames) and the latest measured per-cell
    energy the bridge tails out of the ns-3 log — plus the collapsed
    `measurements` for the headline cards. A "KPM ind_msg" line marks the
    boundary; per-UE metrics accrue to the UE opened by its `amf_ue_ngap_id`."""

    def __init__(self, slice_of_imsi: dict[int, str] | None = None,
                 scene_center: tuple[float, float] | None = None) -> None:
        self.meas: dict[str, list[float]] = {}
        self.ues: dict[int, dict] = {}   # keyed by IMSI
        self.cur: dict | None = None     # UE block currently being read
        self.tick = 0
        self.slice_of_imsi = slice_of_imsi or {}
        self.scene_center = scene_center           # for scen -> config coords
        self.pos: dict[int, dict] = {}             # imsi -> {x, y} (config frame)
        self.serving: dict[int, int] = {}      # imsi -> last serving cell
        self.handovers = 0                     # cell changes observed live
        self.energy: dict[int, dict] = {}      # cell -> {j, w}
        self.sleep: dict[int, dict] = {}       # cell -> RU sleep state
        # Which cells are mmWave, learned from the stream rather than assumed.
        # An [NSG-POS] line reports the LTE anchor and the mmWave cells with the
        # same `cell=` field, so telling them apart matters — but the anchor's id
        # is a property of scenario build order, not something to hardcode. Two
        # sources in the stream are mmWave-only and say so themselves: the energy
        # line ("Total energy consumption for mmWave cell N") and the L3serving
        # KPM line, which only a MmWaveEnbNetDevice emits. Verified against a
        # live capture: the anchor appears in neither.
        self.mmwave_cells: set[int] = set()
        # Sample identity, published on every frame — see the frame dict.
        # Each gNB sends its OWN CU-CP report, so a frame carries a fresh
        # measurement for the reporting cell only; every other cell's numbers in
        # that same frame are the previous round's, repeated. Recording WHICH
        # cells reported is therefore the useful signal — a bare boolean would
        # let a consumer keep 24 frames and believe it had 24 samples per cell
        # when it has 8. `last_sim_t` is the newest time on the [NSG-POS] tail.
        self.kpm_fresh_cells: set[int] = set()
        self.last_sim_t: float | None = None
        # Only the CU-CP blocks name the IMSI (via the L3servingSINR line); the
        # CU-UP blocks (UL bitrate, PDCP delay) carry only the NGAP connection
        # id — without this mapping their metrics were silently dropped.
        self.amf2imsi: dict[int, int] = {}
        # Different containers land in different indications, so single frames
        # can be partial; rows are merged into this cache and every frame emits
        # the full last-known row per UE.
        self.rowcache: dict[int, dict] = {}

    def _flush_cur(self) -> None:
        if self.cur:
            imsi = self.cur.get("imsi")
            if imsi is None:
                imsi = self.amf2imsi.get(self.cur.get("amf"))
                if imsi is not None:
                    self.cur["imsi"] = imsi
            if imsi is not None:
                if self.cur.get("amf") is not None:
                    self.amf2imsi[self.cur["amf"]] = imsi
                prev = self.ues.get(imsi)
                if prev:
                    prev.update({k: v for k, v in self.cur.items() if v is not None})
                else:
                    self.ues[imsi] = self.cur
        self.cur = None

    def feed(self, line: str) -> dict | None:
        me = _ENERGY_RE.search(line)
        if me:  # measured energy from the radio energy model (bridge tail)
            cell = int(me.group(1))
            self.mmwave_cells.add(cell)   # the engine labels these "mmWave cell"
            # the sim prints every 0.1 simulated seconds -> diff/0.1 = watts
            self.energy[cell] = {"j": round(float(me.group(2)), 1),
                                 "w": round(float(me.group(3)) / 0.1, 1)}
            return None
        msl = _SLEEP_RE.search(line)
        if msl:  # RU sleep state (bridge tail) — explains the power drop
            self.sleep[int(msl.group(1))] = {
                "depth": msl.group(2), "state": msl.group(3),
                "sleeps": int(msl.group(4)), "wakes": int(msl.group(5)),
                "sleep_s": round(float(msl.group(6)), 2),
                "sleep_pct": round(float(msl.group(7)), 1),
            }
            return None
        mp = _POS_RE.search(line)
        if mp:  # real UE position (bridge tail), scenario -> config coords
            x, y = float(mp.group(2)), float(mp.group(3))
            if self.scene_center:
                x = x - _SCEN_CENTER + self.scene_center[0]
                y = y - _SCEN_CENTER + self.scene_center[1]
            t_sim = float(mp.group(5))
            self.pos[int(mp.group(1))] = {
                "x": round(x, 1), "y": round(y, 1),
                # Serving cell and the sim time it was true at. Both were being
                # captured by the regex and thrown away.
                "cell": int(mp.group(4)), "t": t_sim,
            }
            if self.last_sim_t is None or t_sim > self.last_sim_t:
                self.last_sim_t = t_sim
            return None

        if "KPM ind_msg" in line:
            self._flush_cur()
            frame = None
            if self.meas or self.ues:
                # merge this indication's (possibly partial) rows over the
                # last-known state, so every frame carries complete UE rows
                for imsi, row in self.ues.items():
                    prev = self.rowcache.get(imsi)
                    if prev:
                        prev.update({k: v for k, v in row.items() if v is not None})
                    else:
                        self.rowcache[imsi] = dict(row)
                # COPY each row. `rowcache` entries are long-lived and mutated
                # in place on every indication, so handing the frame the cache's
                # own dicts aliased every emitted frame to the same objects: a
                # stored run came back with all 111 frames byte-identical, the
                # frame at sim_t 0.5 carrying a UE row stamped sim_t 10.4. The
                # live stream never showed it (each frame is serialised as it
                # goes out) — only the SAVED history, where every UE sat frozen
                # at its final position and cell, so no movement and no handover
                # could be read back out of a completed run.
                rows = [dict(u) for u in
                        sorted(self.rowcache.values(), key=lambda u: u["imsi"])]
                for u in rows:  # attach the latest real position (config frame)
                    p = self.pos.get(u["imsi"])
                    if p:
                        u["x"], u["y"] = p["x"], p["y"]
                        # SERVING CELL comes from here, not from the KPM
                        # L3servingSINR line. Both describe the same thing, but
                        # the KPM one only refreshes when the owning gNB sends
                        # its next CU-CP message — once per second — while a
                        # frame is emitted on EVERY "KPM ind_msg", nine times a
                        # round. Between refreshes the old cell was repeated as
                        # if it were a fresh observation, so a UE kept being
                        # attributed to a cell it had already left: measured, a
                        # handover completing at t=3.2525 stayed invisible for
                        # ~8 frames. [NSG-POS] carries ns-3's own answer every
                        # 0.1 s, so the same handover shows up in the next one.
                        u["sim_t"] = p["t"]
                        # Anything not known to be a mmWave cell is the LTE
                        # anchor: the UE is on LTE and no mmWave cell carries it.
                        # Erring this way is the safe direction — an unrecognised
                        # cell withholds traffic from the rollup rather than
                        # attributing it to a cell that may not have carried it.
                        on_mmwave = p["cell"] in self.mmwave_cells
                        u["on_lte"] = not on_mmwave
                        # None keeps such a UE out of the per-cell rollup without
                        # dropping its row: the anchor is not one of the cells
                        # this platform reports or controls.
                        u["cell"] = p["cell"] if on_mmwave else None
                slices: dict[str, float] = {}
                for u in rows:
                    sl = self.slice_of_imsi.get(u["imsi"])
                    if sl is not None:
                        u["slice"] = sl
                        slices[sl] = round(
                            slices.get(sl, 0.0) + (u.get("thp_kbps") or 0.0), 1)
                    prev = self.serving.get(u["imsi"])
                    if prev is not None and u.get("cell") is not None and u["cell"] != prev:
                        self.handovers += 1
                    if u.get("cell") is not None:
                        self.serving[u["imsi"]] = u["cell"]
                frame = {
                    "type": "kpm",
                    "tick": self.tick,
                    "measurements": _summarise(self.meas),
                    "ues": rows,
                    "cells": _cells_from_ues(rows, self.energy, self.sleep),
                    "slices": slices,              # per-slice DL kbps (real)
                    "handovers": self.handovers,   # serving-cell changes so far
                    "energy": {
                        "total_w": round(sum(e["w"] for e in self.energy.values()), 1),
                        "per_cell": {str(c): e for c, e in sorted(self.energy.items())},
                    } if self.energy else None,
                    "sleep": {
                        "per_cell": {str(c): d for c, d in sorted(self.sleep.items())},
                        "cells_asleep": sum(1 for d in self.sleep.values()
                                            if d["state"] == "asleep"),
                        "wake_events": sum(d["wakes"] for d in self.sleep.values()),
                    } if self.sleep else None,
                    "ue_ids": [u["imsi"] for u in rows],
                    # ── Sample identity. A frame is NOT an independent
                    # observation, and without these two fields there is no way
                    # to tell. The platform emits a frame per "KPM ind_msg", but
                    # each gNB sends three messages per indication round and only
                    # CU-CP carries the KPM report. Measured over 90 frames /
                    # 8.0 s of simulation: 10.6 frames per simulated second
                    # against a ~1 Hz E2 round, and 5.0 frames per distinct
                    # position timestamp. So counting frames as samples
                    # overstates n by ~10x for anything out of the E2 report
                    # (throughput, PRB, SINR, delay) and ~5x for position and
                    # energy. Group by `sim_t`, or select on `kpm_fresh_cells`.
                    #
                    # Measured 8.0 s / 90 frames with 3 mmWave cells: 24 frames
                    # carried a report — exactly 3 cells x 8 rounds — over 8
                    # distinct sim_t. So per CELL the honest sample count is 8,
                    # not 24 and certainly not 90.
                    "sim_t": self.last_sim_t,
                    "kpm_fresh": bool(self.kpm_fresh_cells),
                    "kpm_fresh_cells": sorted(self.kpm_fresh_cells),
                }
            self.meas, self.ues, self.cur = {}, {}, None
            self.kpm_fresh_cells = set()
            self.tick += 1
            return frame

        mu = _UE_RE.search(line)
        if mu:                       # new UE block
            self._flush_cur()
            self.cur = {"amf": int(mu.group(1))}
            return None
        ms = _SINR_UE_RE.search(line)
        if ms and self.cur is not None:
            self.kpm_fresh_cells.add(int(ms.group(1)))  # this cell just reported
            self.mmwave_cells.add(int(ms.group(1)))  # only mmWave gNBs emit this
            self.cur["cell"] = int(ms.group(1))
            self.cur["imsi"] = int(ms.group(2))
            self.cur["sinr"] = float(ms.group(3))
        elif self.cur is not None:
            mt = _THP_RE.match(line)
            if mt:
                self.cur["thp_kbps"] = float(mt.group(1))
            else:
                mp = _PRB_RE.match(line)
                if mp:
                    self.cur["prb"] = float(mp.group(1))
                else:
                    mul = _UL_RE.match(line)
                    if mul:
                        self.cur["ul_kbps"] = float(mul.group(1))
                    else:
                        md = _DELAY_RE.match(line)
                        if md:
                            # the builder reports in 0.1 ms units
                            #
                            # TREAT THIS AS AN INSTANT, NEVER AS A LEVEL. Measured
                            # 2026-09-14: six batch runs of the SAME config with the
                            # SAME seed gave DRB.PdcpSduDelayDl means of 41, 42, 43,
                            # 57, 65 and 574 ms — a 14x spread with nothing changed,
                            # against 1.1x for throughput and 1.2x for PRB. The mean
                            # is built from four or five samples and the first one
                            # carries attach-time buffering, so whether the window
                            # caught it decides the answer. Comparing two runs on
                            # delay is reading that coin flip.
                            self.cur["delay_ms"] = round(float(md.group(1)) / 10.0, 2)

        mm = _MEAS_RE.match(line)    # collapsed aggregate (headline cards)
        if mm:
            try:
                val = float(mm.group(2))
            except ValueError:
                return None
            c = _canon(mm.group(1))
            if c and c not in _META:
                self.meas.setdefault(c, []).append(val)
        return None


# ── Built-in AI xApp on the REAL engine ───────────────────────────────────────
# The same LLM xApp that drives the analytical sandbox can drive the real engine:
# its prompt speaks cells/ue_groups and it emits cell_power/handover — which map
# 1:1 onto the real levers. These two adapters bridge the shapes.

def _to_llm_kpm(frame: dict, offline: set, faulted: set | None = None) -> dict:
    """Real-engine KPM frame → the cells/ue_groups shape the xApp prompt expects.
    Real ids are numeric: a cell is its cell id, a "UE group" is one UE's IMSI.
    `faulted` cells were killed by the OPERATOR — the AI must route around them,
    not try to wake them (same semantics as the analytical sandbox)."""
    faulted = faulted or set()
    down = offline | faulted
    cells = [
        {"id": str(c["cell"]), "name": f"cell {c['cell']}",
         "active": str(c["cell"]) not in down, "faulted": str(c["cell"]) in faulted,
         "prb_used_pct": round(c.get("prb") or 0.0, 1),
         "throughput_mbps": round((c.get("thp_kbps") or 0.0) / 1000.0, 2),
         "ul_mbps": round((c.get("ul_kbps") or 0.0) / 1000.0, 2),
         # MEASURED draw from the radio energy model — the AI's energy goals
         # should optimise real watts, not a config-derived guess.
         "power_w_measured": c.get("power_w"),
         "connected_ues": c.get("ues", 0)}
        for c in frame.get("cells", [])
    ]
    # Cells that are down report no UEs, so they vanish from the frame — keep them
    # visible (inactive) or the AI can never reason about them.
    for cid in sorted(down):
        if not any(c["id"] == cid for c in cells):
            cells.append({"id": cid, "name": f"cell {cid}", "active": False,
                          "faulted": cid in faulted, "prb_used_pct": 0.0,
                          "throughput_mbps": 0.0, "ul_mbps": 0.0,
                          "power_w_measured": None, "connected_ues": 0})
    ue_groups = [
        {"id": str(u["imsi"]), "name": f"UE {u['imsi']}",
         "serving_cell": str(u["cell"]) if u.get("cell") is not None else None,
         "sinr_db": u.get("sinr"), "rsrp_dbm": None,
         "throughput_mbps": round((u.get("thp_kbps") or 0.0) / 1000.0, 2),
         "ul_mbps": round((u.get("ul_kbps") or 0.0) / 1000.0, 2),
         "delay_ms": u.get("delay_ms"),
         "slice": u.get("slice"), "count": 1}
        for u in frame.get("ues", [])
    ]
    slices = [{"slice": name, "throughput_mbps": round(kbps / 1000.0, 2)}
              for name, kbps in sorted((frame.get("slices") or {}).items())]
    energy = frame.get("energy") or {}
    return {"tick": frame.get("tick", 0), "cells": cells, "ue_groups": ue_groups,
            "slices": slices,                       # real per-slice DL totals
            "handovers": frame.get("handovers") or 0,
            "total_power_w_measured": energy.get("total_w"),
            "control_state": {"disabled_cells": sorted(offline), "power_override": {},
                              "forced_serving": {}, "slice_prb_quota": {}}}


# When the config doesn't name a slice's 5QI, fall back to sane defaults
# (bearer QCIs the engine's EpsBearer mapping supports: 1..9).
_SLICE_QCI_DEFAULT = {"eMBB": 9, "uRLLC": 3, "mMTC": 8, "voice": 1}


def _slice_of_imsi_map(config: NSGSimulationConfig | None) -> dict[int, str]:
    """imsi -> slice name, mirroring the backend's slot allocation (ue_alloc) so
    the live per-UE rows carry the same slice each UE's traffic was built with.
    IMSI = slot index + 1 (attach order)."""
    if not config or not config.ue_groups:
        return {}
    ues = list(config.ue_groups)
    # same n_gnb as main._nsoran_params, or the IMSI slots misalign
    n_ue = clamp_ue(sum(int(g.count) for g in ues),
                    clamp_gnb(len(config.base_stations or [])),
                    config_bw_mhz(config), config_numerology(config))
    out: dict[int, str] = {}
    imsi = 1
    for g, k in zip(ues, ue_alloc(ues, n_ue)):
        svcs = list(g.services or [])
        name = str(getattr(svcs[0].slice_type, "value", svcs[0].slice_type)) if svcs else "eMBB"
        for _ in range(k):
            if imsi > n_ue:
                break
            out[imsi] = name
            imsi += 1
    return out


def _rate_of_imsi_map(config: NSGSimulationConfig | None) -> dict[int, float]:
    """imsi -> offered DL rate (Mbps), for the QoS-satisfaction score."""
    if not config or not config.ue_groups:
        return {}
    ues = list(config.ue_groups)
    # same n_gnb as main._nsoran_params, or the IMSI slots misalign
    n_ue = clamp_ue(sum(int(g.count) for g in ues),
                    clamp_gnb(len(config.base_stations or [])),
                    config_bw_mhz(config), config_numerology(config))
    out: dict[int, float] = {}
    imsi = 1
    for g, k in zip(ues, ue_alloc(ues, n_ue)):
        rate = sum(max(0.0, float(s.target_throughput_gbps)) for s in (g.services or [])) * 1000.0
        rate = min(50.0, max(0.5, rate)) if rate > 0 else 3.0
        for _ in range(k):
            if imsi > n_ue:
                break
            out[imsi] = rate
            imsi += 1
    return out


async def run_live_traffic(websocket: WebSocket, cfg, params: dict | None) -> None:
    """Analytics 'Live Traffic', measured on the REAL engine.

    Streams LiveTrafficFrame-shaped ticks derived from the running ns-O-RAN
    RAN's KPM instead of the analytical estimator: per-slice DL Mbps come from
    each UE's real slice tag, power is the MEASURED radio-energy-model draw the
    bridge tails out of the ns-3 log, PRB% is used-vs-available across the
    reporting cells, and QoS is each UE's measured-vs-offered satisfaction.
    Read-only — no control path; closing the socket ends the session. The
    engine boots for ~40 s before the first frame, and only one live session
    can exist (shared lock with the RIC Test tab)."""
    host, _, port = _LIVE_ADDR.partition(":")
    try:
        reader, writer = await asyncio.open_connection(host, int(port or "8091"))
    except OSError as exc:
        await websocket.send_json(
            {"type": "error", "message": f"real engine unreachable at {_LIVE_ADDR}: {exc}"})
        return
    if params is not None:
        writer.write((json.dumps({"nsg_params": params}) + "\n").encode())
        await writer.drain()

    acc = _KpmAccumulator(_slice_of_imsi_map(cfg),
                          tuple((params or {}).get("_scene_center") or ()) or None)
    rate_of_imsi = _rate_of_imsi_map(cfg)
    tick_out = 0
    try:
        while True:
            raw = await reader.readline()
            if not raw:
                await websocket.send_json(
                    {"type": "error", "message": "live engine session ended"})
                break
            line = raw.decode("utf-8", "replace")
            if "[BUSY]" in line:
                await websocket.send_json(
                    {"type": "error",
                     "message": "engine busy — stop the RIC Test live session first"})
                break
            if line.startswith("[NSG-ERROR]"):
                await websocket.send_json(
                    {"type": "error", "message": line.replace("[NSG-ERROR] ", "")})
                break
            frame = acc.feed(line)
            if not frame:
                continue
            ues = frame.get("ues") or []
            if not ues:
                continue
            slices = {k: round(v / 1000.0, 2)
                      for k, v in (frame.get("slices") or {}).items()}
            for name in ("eMBB", "uRLLC", "mMTC", "voice"):
                slices.setdefault(name, 0.0)
            tput = round(sum(slices.values()), 2)
            cells = frame.get("cells") or []
            prb_pct = round(min(100.0, 100.0 * sum((u.get("prb") or 0.0) for u in ues)
                                / (139.0 * max(1, len(cells)))), 1)
            sinrs = [u["sinr"] for u in ues if u.get("sinr") is not None]
            sinr = round(sum(sinrs) / len(sinrs), 1) if sinrs else 0.0
            sats = [min(1.0, ((u.get("thp_kbps") or 0.0) / 1000.0) / rate_of_imsi[u["imsi"]])
                    for u in ues if rate_of_imsi.get(u["imsi"])]
            qos = round(100.0 * sum(sats) / len(sats), 1) if sats else 0.0
            power = float((frame.get("energy") or {}).get("total_w") or 0.0)
            await websocket.send_json({
                "time": tick_out, **slices, "tput": tput,
                "power": round(power, 1), "prb": prb_pct, "qos": qos,
                "conn_ues": len(ues),
                # the engine measures SINR, not RSRP — the UI relabels the
                # signal tile when source == "nsoran"; rsrp mirrors the value
                # so old consumers keep a number.
                "sinr": sinr, "rsrp": sinr,
                "energy_eff": round(tput / power, 3) if power > 0 else 0.0,
                "handovers": frame.get("handovers") or 0,
                "source": "nsoran",
            })
            tick_out += 1
    except (WebSocketDisconnect, OSError, RuntimeError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def _slice_qci_map(config: NSGSimulationConfig | None) -> dict:
    """slice_type -> the QCI its bearers were ACTUALLY created with (the config's
    qos_id, which _ue_slice_specs sends to the engine per UE)."""
    out = dict(_SLICE_QCI_DEFAULT)
    for g in (config.ue_groups if config else []):
        for s in (g.services or []):
            try:
                out[str(getattr(s.slice_type, "value", s.slice_type))] = int(s.qos_id)
            except (TypeError, ValueError):
                continue
    return out


def _to_real_cmd(action: dict, slice_qci: dict | None = None) -> dict | None:
    """xApp action → real-engine command. slice_quota is REAL since the MAC
    scheduler got per-QCI symbol ceilings (E2SM-RC style 2); only `reset` has no
    engine actuator and is dropped."""
    try:
        a = action.get("action")
        if a == "cell_power":
            cell = int(action["cell"])
            # Graded power when the AI gives a dBm; on/off is just the two ends of
            # the same Energy_state actuator (0 dBm = sleep).
            dbm = action.get("dbm")
            if dbm is not None:
                return {"cmd": "cell_power", "cell": cell, "dbm": max(0, min(46, int(dbm)))}
            return {"cmd": "cell_on" if action.get("state") == "on" else "cell_off", "cell": cell}
        if a == "handover":
            return {"cmd": "handover", "ue": int(action["ue_group"]), "cell": int(action["target_cell"])}
        if a == "slice_quota":
            qci = (slice_qci or _SLICE_QCI_DEFAULT).get(str(action.get("slice")))
            if qci is None:
                return None
            pct = max(0, min(100, int(action["prb_percent"])))
            return {"cmd": "slice_quota", "qci": int(qci), "pct": pct}
    except (KeyError, TypeError, ValueError):
        return None
    return None


class _LiveSession:
    """One REAL engine session, shared by every client watching/driving it.

    The engine bridge accepts a single TCP client, so the backend holds exactly
    ONE connection and fans it out: the browser creates the session with ITS
    config, an external xApp JOINS by id, and both see the same KPM and can send
    control. Without this an external xApp had to start its own session — which
    ran the engine's DEFAULT scenario and locked the UI out entirely (no live
    view, no inject, no Compare save).
    """

    def __init__(self, writer, offline: set, faulted: set, latest: dict,
                 partition: dict | None = None) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.writer = writer
        self.clients: set[WebSocket] = set()
        self.offline = offline
        self.faulted = faulted
        self.latest = latest
        # A PARTITIONED session is several ns-3 processes, and a control command
        # names a cell that lives in exactly one of them. Carried on the session
        # rather than closed over in the proxy because a joined external xApp
        # sends commands too, and it only ever sees the session.
        # {"writers": [...], "cell_index": [[...], ...], "base": {"v": int|None}}
        self.partition = partition

    async def send(self, msg: dict) -> bool:
        """Send one control command to the process that owns its cell.

        Returns False when nothing could receive it, so the caller can say so
        instead of the command vanishing. On an unpartitioned session this is
        the plain write it always was.
        """
        if not self.partition:
            try:
                self.writer.write((json.dumps(msg) + "\n").encode())
                await self.writer.drain()
                return True
            except OSError:
                return False

        writers = self.partition["writers"]
        base = self.partition["base"].get("v")
        cell = msg.get("cell")
        targets: list[tuple] = []
        if cell is None:
            # No cell named — a session-wide command. Every process gets it,
            # because each one is a whole RAN as far as it knows.
            targets = [(w, msg) for w in writers]
        else:
            from partition_live import local_cell
            for w, idx in zip(writers, self.partition["cell_index"]):
                loc = local_cell(cell, idx, base)
                if loc is not None:
                    targets = [(w, {**msg, "cell": loc})]
                    break
        if not targets:
            # Say WHICH of the two reasons it was. "No process holds this cell"
            # is true both when the map is not learned yet (no KPM frame has
            # arrived, so the engine's cell numbering is unknown) and when the
            # cell is genuinely outside the run — and those need different
            # responses from whoever sent the command.
            why = ("no KPM frame has arrived yet, so the engine's cell numbering "
                   "is still unknown — wait for the first report"
                   if base is None else
                   f"the run holds cells "
                   f"{[base + c for idx in self.partition['cell_index'] for c in idx]}")
            await self.broadcast({
                "type": "live_status",
                "line": f"[partition] cell {cell} reached no process — command dropped "
                        f"({why})"})
            return False
        ok = False
        for w, payload in targets:
            try:
                w.write((json.dumps(payload) + "\n").encode())
                await w.drain()
                ok = True
            except OSError:
                continue
        return ok

    async def broadcast(self, msg: dict) -> None:
        """Send to every attached client; drop the ones whose socket died."""
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001 — client vanished mid-send
                self.clients.discard(ws)

    def roster(self) -> list[str]:
        return [getattr(ws, "_nsg_label", "client") for ws in self.clients]


# Live real-engine sessions by id. At most one is active (the engine takes a
# lock), but the dict keeps the lookup explicit for joiners.
_LIVE_SESSIONS: dict[str, _LiveSession] = {}


async def _attach_to_live_session(websocket: WebSocket, sid: str, label: str) -> None:
    """An external xApp joins the operator's live session: it drives the SAME
    engine run the UI is watching, so its actions are observable and it inherits
    the operator's scenario instead of the engine default."""
    session = _LIVE_SESSIONS.get(sid)
    if session is None:
        await websocket.send_json({
            "type": "error",
            "message": f"live session '{sid}' not found — start one from the RIC Test tab first"})
        return
    websocket._nsg_label = label  # type: ignore[attr-defined]
    session.clients.add(websocket)
    # Broadcast, not send: the OPERATOR must see in their log that an external
    # xApp attached to their run — that arrival is the thing they are testing.
    await session.broadcast({"type": "live_status",
                             "line": f"[join] '{label}' joined this live session"})
    await session.broadcast({"type": "live_roster", "session_id": sid,
                             "clients": session.roster()})
    # hand the newcomer the newest frame so it can decide immediately
    if session.latest.get("frame"):
        try:
            await websocket.send_json(session.latest["frame"])
        except Exception:  # noqa: BLE001
            pass
    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                return
            raw = (raw or "").strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                await websocket.send_json({"type": "live_status",
                                           "line": "[join] malformed command ignored"})
                continue
            if not isinstance(msg, dict) or not msg.get("cmd"):
                await websocket.send_json({"type": "live_status",
                                           "line": "[join] expected {'cmd': ...}"})
                continue
            # A joiner may not revive a cell the operator faulted.
            if msg.get("cmd") == "cell_on" and str(msg.get("cell")) in session.faulted:
                await websocket.send_json({
                    "type": "live_status",
                    "line": f"[fault] cell {msg.get('cell')} is faulted — cannot be woken"})
                continue
            if not await session.send(msg):
                return
            if msg.get("cmd") == "cell_off":
                session.offline.add(str(msg.get("cell")))
            elif msg.get("cmd") == "cell_on":
                session.offline.discard(str(msg.get("cell")))
            # everyone (including the operator's UI) sees what the xApp did
            await session.broadcast({"type": "live_status",
                                     "line": f"[{label}] {json.dumps(msg)}"})
    finally:
        session.clients.discard(websocket)
        await session.broadcast({"type": "live_status",
                                 "line": f"[join] '{label}' left this live session"})
        await session.broadcast({"type": "live_roster", "session_id": sid,
                                 "clients": session.roster()})


def _live_summary(frames: list[dict]) -> dict:
    """Headline numbers for a live run, so the history list is readable without
    loading every frame. Deliberately small and derived only from what the live
    frames already carry."""
    if not frames:
        return {}
    last = frames[-1]
    dl = [sum((u.get("thp_kbps") or 0.0) for u in (f.get("ues") or []))
          for f in frames]
    tot_w = [f["energy"]["total_w"] for f in frames
             if isinstance(f.get("energy"), dict)
             and f["energy"].get("total_w") is not None]
    sim_t = [f["sim_t"] for f in frames if f.get("sim_t") is not None]
    return {
        "frames": len(frames),
        "cells": len(last.get("cells") or []),
        "ues": len(last.get("ues") or []),
        "handovers": last.get("handovers"),
        "dl_mbps_mean": round(sum(dl) / len(dl) / 1000.0, 2) if dl else None,
        "dl_mbps_max": round(max(dl) / 1000.0, 2) if dl else None,
        "power_w_mean": round(sum(tot_w) / len(tot_w), 1) if tot_w else None,
        # The honest sample count: frames are ~10x over-sampled against the 1 Hz
        # E2 round, so a reader that counts frames overstates n. See the
        # kpm_fresh_cells / sim_t fields on each frame.
        "sim_span_s": round(max(sim_t) - min(sim_t), 2) if len(sim_t) > 1 else None,
        "kpm_reports": sum(1 for f in frames if f.get("kpm_fresh")),
    }


async def run_live_proxy(websocket: WebSocket, derive_params: Callable | None = None) -> None:
    """Proxy one WebSocket client to the live engine bridge until either side
    closes. Assumes the socket is already accepted.

    The client's FIRST message is the scenario config, so the live session runs
    the user's ACTUAL design (cell/UE count, band, scale, positions, power) — not
    a fixed default. Best-effort: if it's missing/invalid, the engine defaults."""
    params = None
    slice_qci = dict(_SLICE_QCI_DEFAULT)
    slice_of_imsi: dict[int, str] = {}
    first = None
    try:
        first = await asyncio.wait_for(websocket.receive_json(), timeout=15)
    except Exception:
        first = None

    # {"join": "<id>"} → an external xApp attaches to the operator's live session
    # (one engine run, operator's config, fully observable) instead of starting
    # its own on the engine default.
    if isinstance(first, dict) and first.get("join"):
        await _attach_to_live_session(websocket, str(first["join"]),
                                      str(first.get("label") or "xapp"))
        return

    if derive_params is not None and first is not None:
        try:
            raw = first.get("config", first) if isinstance(first, dict) else first
            cfg = NSGSimulationConfig.model_validate(raw)
            params = derive_params(cfg)
            slice_qci = _slice_qci_map(cfg)  # slice names -> the bearers' real QCIs
            slice_of_imsi = _slice_of_imsi_map(cfg)  # imsi -> slice tag for live rows
        except Exception:
            params = None  # fall back to the engine's default scenario

    # SPLIT THIS SESSION ACROSS PROCESSES? The client asks for a shape alongside
    # its config. A live RIC session is the case where the horizon hurts most —
    # the operator is watching a clock that advances at a few percent of real
    # time — and at three or more cells per group the split costs no handovers,
    # which is the behaviour a RIC session exists to exercise.
    groups = 1
    if isinstance(first, dict):
        try:
            groups = max(1, int(first.get("partition_groups") or 1))
        except (TypeError, ValueError):
            groups = 1
    n_gnb = int((params or {}).get("n_gnb") or 0)
    if groups > 1 and (params is None or not n_gnb or n_gnb % groups):
        await websocket.send_json({
            "type": "live_status",
            "line": f"[partition] {groups} groups does not divide {n_gnb} cell(s) "
                    f"— running whole, as one process"})
        groups = 1

    lease = None
    readers: list = []
    writers: list = []
    payloads: list[dict] = []
    if groups > 1:
        import partition_pool
        from partition_run import assign_groups
        payloads = assign_groups(params, groups)
        await websocket.send_json({
            "type": "live_status", "state": "session-starting",
            "line": f"[partition] bringing up {groups} engine instances "
                    f"(takes about 15 s)"})
        try:
            lease = partition_pool.aleased(groups)
            addrs = await lease.__aenter__()
        except Exception as exc:  # noqa: BLE001
            await websocket.send_json(
                {"type": "error", "message": f"could not start {groups} engine "
                                             f"instances: {exc}"})
            return
    else:
        addrs = [_LIVE_ADDR]
        payloads = [params] if params is not None else [None]

    try:
        for a in addrs:
            h, _, pt = a.partition(":")
            r, w = await asyncio.open_connection(h, int(pt or "8091"))
            readers.append(r)
            writers.append(w)
    except OSError as exc:
        if lease is not None:
            await lease.__aexit__(None, None, None)
        await websocket.send_json(
            {"type": "error", "message": f"live engine unreachable at {addrs}: {exc}"}
        )
        return
    reader, writer = readers[0], writers[0]

    # Hand the derived scenario knobs to the bridge as the first line (before any
    # control), so run_live_session.sh starts the user's scenario.
    for w, pl in zip(writers, payloads):
        if pl is not None:
            w.write((json.dumps({"nsg_params": pl}) + "\n").encode())
            await w.drain()

    await websocket.send_json(
        {"type": "live_status", "state": "session-starting",
         "engine": "ns-O-RAN (FlexRIC + ns-3 mmWave, real E2AP/SCTP)"}
    )
    center = tuple((params or {}).get("_scene_center") or ()) or None
    if groups > 1:
        from partition_live import submap
        accs = [_KpmAccumulator(submap(slice_of_imsi, pl.get("_ue_index") or []), center)
                for pl in payloads]
    else:
        accs = [_KpmAccumulator(slice_of_imsi, center)]
    acc = accs[0]
    latest: dict = {"frame": None}
    # Each partition's newest known state. NOT its newest frame: a frame is not
    # a complete observation — each gNB sends its own CU-CP report at its own
    # time and most frames in between carry only positions or energy, so
    # replacing the whole state with the newest frame discards every counter
    # that frame happened not to contain. Measured on the batch path before
    # this was fixed: 13 measurement names survived a split run against 88 in
    # the same scenario run whole.
    from partition_live import Rolling
    rolling_parts: list = [Rolling() for _ in accs]
    seen_parts: list = [False] * len(accs)
    # The engine's first mmWave cell id — a constant of how the scenario is
    # built (one LTE hub at 1, mmWave cells from 2), NOT something to learn from
    # the data. Learning it was a real defect: a cell only appears in a frame
    # once it has a UE, so the first frame could hold only the higher cell, the
    # base came out 3, and three partitions' cells folded onto one id while
    # every control command for the real cell 2 was dropped as unowned.
    # Knowing it up front is also what lets control work before the first frame.
    cell_base: dict = {"v": CELL_BASE}
    merged_tick: dict = {"n": 0}
    # ── Live sessions are RUNS too, and until now they vanished. /sim/run has
    # always persisted; this path never did, which is why the conflict
    # experiment had to build its own JSON store beside the platform. Same
    # runs + run_frames tables, same store.save_run — no schema change.
    #
    # Only the OPERATOR records: a client that sent {"join": id} returned above
    # before reaching here, so an attached xApp can never open a second run for
    # the same engine session.
    recorded: list[dict] = []
    run_id = f"live-{uuid.uuid4().hex[:12]}"
    started_at = _now_iso()
    status_lines: list[str] = []
    offline: set = set()          # cells slept by control (the engine can't report this)
    faulted: set = set()          # cells the OPERATOR killed — the AI may not wake them
    ai_task: asyncio.Task | None = None

    # Publish this run as a joinable session so an external xApp can attach to it
    # (same engine run, operator's config) instead of starting a second one.
    session = _LiveSession(
        writer, offline, faulted, latest,
        partition=({"writers": writers,
                    "cell_index": [pl.get("_cell_index") or [] for pl in payloads],
                    "base": cell_base} if groups > 1 else None))
    session.clients.add(websocket)
    websocket._nsg_label = "operator"  # type: ignore[attr-defined]
    _LIVE_SESSIONS[session.id] = session
    await websocket.send_json({"type": "live_session", "session_id": session.id})

    async def send_cmd(rc: dict) -> None:
        # A faulted cell is broken, not sleeping: refuse to wake it, so an injected
        # incident stays an incident the xApp has to route around.
        if rc.get("cmd") == "cell_on" and str(rc.get("cell")) in faulted:
            await session.broadcast(
                {"type": "live_status", "line": f"[fault] cell {rc.get('cell')} is faulted — cannot be woken"})
            return
        await session.send(rc)
        if rc.get("cmd") == "cell_off":
            offline.add(str(rc.get("cell")))
        elif rc.get("cmd") == "cell_on":
            offline.discard(str(rc.get("cell")))

    async def ai_loop(goal: str, llm_config: dict | None) -> None:
        """The built-in LLM xApp driving the REAL engine: read the live KPM, decide,
        and fire real E2SM-RC control. LLM calls are slow/costly → decide every few
        seconds, not per indication."""
        recent: list = []
        try:
            while True:
                await asyncio.sleep(6.0)
                frame = latest["frame"]
                if not frame or not frame.get("cells"):
                    continue
                # One bad decision cycle (an LLM hiccup or an unexpected KPM shape)
                # must NOT kill the AI for the rest of the session — skip and retry.
                try:
                    acts = await asyncio.to_thread(
                        decide_xapp_actions, goal, _to_llm_kpm(frame, offline, faulted),
                        recent, llm_config
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("AI xApp decision cycle failed")
                    await session.broadcast({"type": "live_status",
                                             "line": f"[AI] decision skipped ({type(exc).__name__})"})
                    continue
                for a in acts or []:
                    rc = _to_real_cmd(a, slice_qci)
                    if rc is None:
                        await session.broadcast(
                            {"type": "live_status", "line": f"[AI] no actuator for {a.get('action')} — skipped"})
                        continue
                    await send_cmd(rc)
                    recent.append(a)
                    await session.broadcast({"type": "live_status", "line": f"[AI] {json.dumps(rc)}"})
                del recent[:-10]
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("real-engine AI xApp loop crashed")

    async def kpm_out(part: int = 0) -> None:
        # live bridge stdout -> parsed KPM frames + status lines -> WS
        #
        # One task PER PARTITION. Each feeds its own accumulator (a partition
        # numbers its cells and IMSIs from the same base as every other one, so
        # a shared accumulator would stack them), relabels the frame into
        # whole-run identities, and publishes the union of the newest frame from
        # each process. A live merge has no finished stream to align on, so the
        # view is "everything as of now" rather than a reconstructed timeline.
        rdr = readers[part]
        try:
            while True:
                raw = await rdr.readline()
                if not raw:
                    await session.broadcast({"type": "live_status", "state": "engine-closed"})
                    return
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if "[BUSY]" in line:
                    # the engine runs ONE session; a collision used to leave the
                    # UI silently stuck at "session-starting"
                    await session.broadcast(
                        {"type": "error",
                         "message": "engine busy — another live session is running "
                                    "(Live Traffic or RIC Test); stop it and retry"})
                    return
                if line.startswith("[NSG-ERROR]"):
                    await session.broadcast(
                        {"type": "error", "message": line.replace("[NSG-ERROR] ", "")})
                    return
                if line.startswith("[NSG-BRIDGE]") or line.startswith("[live]"):
                    # SAY WHICH PROCESS. Three bridges write into one log and
                    # their lines interleave: "ctrl cmd rx: cell 3" is a
                    # different cell depending on which partition said it, and
                    # without the tag there is no way to tell — which is exactly
                    # the question anyone debugging a partitioned session asks.
                    if groups > 1:
                        cells_here = [CELL_BASE + c
                                      for c in (payloads[part].get("_cell_index") or [])]
                        line = f"[p{part} cells {cells_here}] {line}"
                    if len(status_lines) < 500:
                        status_lines.append(line)
                    await session.broadcast({"type": "live_status", "line": line})
                else:
                    frame = accs[part].feed(line)
                    if frame:
                        if groups > 1:
                            from partition_live import relabel_frame, union_frames
                            frame = relabel_frame(
                                frame, payloads[part].get("_cell_index") or [],
                                payloads[part].get("_ue_index") or [], cell_base["v"])
                            rolling_parts[part].feed(frame)
                            seen_parts[part] = True
                            seen = [r.frame() for r, ok in zip(rolling_parts, seen_parts) if ok]
                            merged_tick["n"] += 1
                            frame = union_frames(seen, merged_tick["n"],
                                                 sim_t=frame.get("sim_t"))
                            # Say how much of the network this view actually
                            # covers. Early on only some processes have reported,
                            # and a partial view that does not announce itself
                            # reads as cells having gone dark.
                            frame["partitions_reporting"] = len(seen)
                        latest["frame"] = frame      # newest state for the AI xApp
                        # Keep it for persistence. Bounded: a live session has no
                        # natural end, so an unbounded list would grow for as
                        # long as the operator leaves the tab open.
                        if len(recorded) < _LIVE_MAX_FRAMES:
                            recorded.append(frame)
                        # every attached client (UI + any external xApp) sees it
                        await session.broadcast(frame)
        except Exception:  # WS closed mid-send, or engine socket error
            return

    async def ctrl_in() -> None:
        # WS messages: {"ai_xapp": {...}} starts/stops the built-in AI on this real
        # session; anything else is a control command forwarded to the bridge.
        nonlocal ai_task
        while True:
            try:
                cmd = await websocket.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                return
            cmd = (cmd or "").strip()
            if not cmd:
                continue
            try:
                msg = json.loads(cmd)
            except ValueError:
                msg = None
            if isinstance(msg, dict) and "ai_xapp" in msg:
                spec = msg.get("ai_xapp") or {}
                if spec.get("enabled"):
                    if ai_task is None:
                        goal = str(spec.get("goal") or "keep QoS high and save energy where possible")
                        ai_task = asyncio.create_task(ai_loop(goal, spec.get("llm")))
                        await websocket.send_json({"type": "ai_status", "running": True, "goal": goal})
                elif ai_task is not None:
                    ai_task.cancel()
                    ai_task = None
                    await websocket.send_json({"type": "ai_status", "running": False})
                continue
            # {"inject": {"cell": N}} — the OPERATOR kills a cell. Same actuator as
            # cell_off, but it is marked faulted: the xApp sees a broken cell it must
            # route around and cannot wake. This is the incident an AI is tested on.
            if isinstance(msg, dict) and "inject" in msg:
                spec = msg.get("inject") or {}
                try:
                    cell = int(spec.get("cell"))
                except (TypeError, ValueError):
                    continue
                faulted.add(str(cell))
                await send_cmd({"cmd": "cell_off", "cell": cell})
                # broadcast so a joined xApp sees the incident it must react to
                await session.broadcast(
                    {"type": "live_status", "line": f"[fault] operator injected: cell {cell} is DOWN"})
                continue
            try:
                # Route real commands through send_cmd so the faulted-cell guard and
                # the offline bookkeeping apply to CLIENT commands too, not just the
                # AI's — otherwise a raw cell_on would quietly revive a faulted cell.
                if isinstance(msg, dict) and msg.get("cmd"):
                    await send_cmd(msg)
                elif isinstance(msg, dict):
                    await session.send(msg)
                else:
                    for w in writers:
                        w.write((cmd + "\n").encode())
                        await w.drain()
            except OSError:
                return

    outs = [asyncio.create_task(kpm_out(i)) for i in range(len(readers))]
    t_in = asyncio.create_task(ctrl_in())
    if groups > 1:
        await session.broadcast({
            "type": "live_partition",
            "groups": groups,
            "cells_per_group": payloads[0].get("n_gnb"),
            "handover": ("none" if payloads[0].get("n_gnb") == 1 else
                         "reduced" if payloads[0].get("n_gnb") == 2 else "intact"),
        })
    try:
        await asyncio.wait({*outs, t_in}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in outs:
            t.cancel()
        # The operator owns the run: when they leave, the engine session ends and
        # any joined xApp is told (its socket then closes with the handler).
        _LIVE_SESSIONS.pop(session.id, None)
        session.clients.discard(websocket)
        await session.broadcast({"type": "live_status", "state": "engine-closed"})
        # Persist the session as a run. Best-effort on purpose: a database that
        # is down must not turn a finished experiment into a failed WebSocket.
        if recorded:
            try:
                import store  # local import keeps the module importable without a DB
                store.save_run({
                    "id": run_id,
                    "config": (first.get("config", first)
                               if isinstance(first, dict) else None) or {},
                    "requested": {"source": "live", "session_id": session.id,
                                  "params": params},
                    "status": "completed",
                    "n_frames": len(recorded),
                    "summary": _live_summary(recorded),
                    "status_lines": status_lines or None,
                    "started_at": started_at,
                    "finished_at": _now_iso(),
                    "frames": recorded,
                })
                logger.info("live run %s persisted (%d frames)", run_id, len(recorded))
            except Exception:
                logger.exception("live run %s could not be persisted", run_id)
        if ai_task is not None:
            ai_task.cancel()
        t_in.cancel()
        for w in writers:
            w.close()
            try:
                await w.wait_closed()
            except Exception:
                pass
        # Release the extra engine instances LAST, after their sockets are shut:
        # removing a container out from under an open session leaves ns-3 holding
        # the flock, and the next run meets a wedged engine.
        if lease is not None:
            try:
                await lease.__aexit__(None, None, None)
            except Exception:
                logger.exception("partition lease could not be released")


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — BATCH SIMULATION RUN
# Run a scenario to completion HEADLESS (no interactive control) and collect the
# FULL per-tick dataset, reusing the exact live-bridge frame pipeline above: the
# engine already produces rich per-cell / per-UE / per-slice frames — batch just
# runs it to a target window count and keeps every frame instead of streaming.
# No engine-image change; all platform-side, over the same :8091 bridge.
# ═══════════════════════════════════════════════════════════════════════════════

def _emit_progress(cb, frames: list, target: int, status_lines: list,
                   fault_events: list, t0: float) -> None:
    """Publish what the run has produced SO FAR.

    Deliberately derived from the frames themselves rather than from a timer: a
    percentage that advances while the engine produces nothing would be worse
    than no percentage at all. `stage` comes from the bridge's own status lines,
    which is the only honest source for "what is it doing right now".
    """
    if cb is None:
        return
    last = frames[-1] if frames else {}
    ues = last.get("ues") or []
    cells = last.get("cells") or []
    try:
        cb({
            "n_frames": len(frames),
            "frames_target": target,
            "pct": round(min(100.0, 100.0 * len(frames) / max(1, target)), 1),
            "last_tick": last.get("tick"),
            "elapsed_s": round(time.monotonic() - t0, 1),
            "ues_reporting": len(ues),
            "cells_reporting": len(cells),
            "dl_mbps": round(sum((u.get("thp_kbps") or 0.0) for u in ues) / 1000.0, 2),
            "power_w": ((last.get("energy") or {}) or {}).get("total_w"),
            "handovers": last.get("handovers"),
            "anomalies_fired": len(fault_events),
            "stage": (status_lines[-1] if status_lines else None),
        })
    except Exception:  # noqa: BLE001
        pass  # progress reporting must never break the run it reports on


async def run_batch_simulation(
    cfg: NSGSimulationConfig,
    params: dict | None,
    frames_target: int = 80,
    # 900 s is a DEFAULT, not a limit. The live path is the one that can run long
    # — it streams KPM out as it arrives rather than holding a request open — so
    # this is the knob a multi-hour run moves, and the engine's own cap follows it
    # (see bridge_secs below) instead of truncating underneath.
    wall_cap_s: float = 900.0,
    on_progress=None,
    live_addr: str | None = None,
    slice_map: dict[int, str] | None = None,
) -> dict:
    """Drive the live bridge headlessly, collecting every KPM frame until
    `frames_target` windows are captured (or the engine closes / caps out).
    Returns the raw per-tick frames + status. `params` is the engine payload from
    nsoran_params(config); `cfg` is only needed for the slice-tag map.

    `slice_map` overrides that map, and a PARTITIONED run must pass one.
    `_slice_of_imsi_map` keys on attach order across the whole config, but a
    partition is its own ns-3 process and restarts at IMSI 1 — so the default
    map would tag every partition's first UE with the first UE group's slice
    regardless of which UE it actually is, and the per-slice totals would be
    wrong in a way nothing downstream could detect."""
    if (params or {}).get("_tier_b_only"):
        # `nsoran_params(engine_clamped=False)` builds a scene the fast tier
        # can compute and ns-3 cannot: past GNB_MAX, past UE_MAX, or on a
        # numerology the mmWave stack asserts on. Sending it would not fail
        # cleanly — it would run a DIFFERENT scenario and report it as this
        # one, which is the failure mode this repo has shipped before.
        raise ValueError(
            "this payload was built with engine_clamped=False and is for the "
            "fast tier only; rebuild it with nsoran_params(cfg) to run ns-3")
    acc = _KpmAccumulator(_slice_of_imsi_map(cfg) if slice_map is None else slice_map,
                          tuple((params or {}).get("_scene_center") or ()) or None)
    # The engine instance to drive. Defaults to the single configured engine, but
    # a campaign hands in one instance from a pool: runs are independent, and one
    # engine serves one session at a time, so throughput comes from more engines
    # rather than from making one of them faster. Taking this as an argument (not
    # the module global) is what lets concurrent runs target different instances.
    addr = live_addr or _LIVE_ADDR
    host, _, port = addr.partition(":")
    try:
        reader, writer = await asyncio.open_connection(host, int(port or "8091"))
    except OSError as exc:
        return {"status": "error", "error": f"live engine unreachable at {addr}: {exc}",
                "frames": [], "status_lines": [], "n_frames": 0}

    if params is not None:
        # A batch run has no impatient human in front of it, and the engine holds
        # its lock until the PREVIOUS run's process exits — seconds after that run
        # reported "done". Waiting is therefore the correct behaviour here, and
        # not waiting is what made the second run of every multi-run campaign come
        # back "busy" and score as a failed scenario.
        params = {**params, "lock_wait_s": params.get("lock_wait_s", 90)}
        # TELL THE ENGINE HOW LONG WE INTEND TO STAY. Its own session cap used to
        # be a fixed 1200 s that nothing here set, so any wall cap above twenty
        # minutes ended with the engine killing the xApp first — and this loop
        # reported that as "engine-closed", which reads as the engine failing
        # rather than as a ceiling nobody chose. Give it the wall cap plus a
        # margin so OUR cap is always the one that bites, and "wall-capped" keeps
        # meaning what it says.
        params.setdefault("bridge_secs", int(wall_cap_s) + 300)
        writer.write((json.dumps({"nsg_params": params}) + "\n").encode())
        await writer.drain()

    collected: list = []
    status_lines: list = []
    fault_events: list = []   # staged faults the engine reported as FIRED
    err: str | None = None
    status = "done"
    t0 = time.monotonic()
    try:
        while len(collected) < frames_target:
            remaining = wall_cap_s - (time.monotonic() - t0)
            if remaining <= 0:
                # NOT the same failure as a quiet engine below. ns-3 mmWave runs
                # at a few percent of real time, so any run asking for a long
                # sim horizon ENDS here by design — the frames_target is a
                # ceiling, the wall cap is what actually bounds the run.
                status = "wall-capped"
                break
            try:
                # A quiet gap longer than this (or the wall cap) means the engine
                # is stuck — stop rather than hang forever.
                #
                # 300 s, not 120. The gap between indications GROWS under
                # sustained back-to-back load: measured across forty consecutive
                # sessions it went from a 57-75 s maximum in the first ten to
                # 119.3 s by the fortieth, and recovered on its own after a
                # pause. At the old ceiling that left seven-tenths of a second of
                # margin, and a long campaign — the one feature guaranteed to
                # produce sustained load — would start scoring healthy runs as
                # "timeout". A dead engine is still caught; it just takes longer
                # to say so, which is the right trade when the alternative is
                # discarding real results.
                # Waited for in SHORT slices rather than one long block, so
                # progress keeps advancing while the engine is quiet. Starting a
                # large scenario is minutes of silence — the E2 registration
                # budget alone is 90s + 4s per UE, so 35 UEs means nearly four
                # minutes before the first window — and a run that publishes
                # nothing in that window is indistinguishable from a stuck one.
                # The 300s quiet ceiling is unchanged; it is now accumulated
                # instead of being one timeout.
                quiet = 0.0
                raw = None
                while quiet < min(remaining, 300):
                    try:
                        raw = await asyncio.wait_for(reader.readline(), timeout=3.0)
                        break
                    except asyncio.TimeoutError:
                        quiet += 3.0
                        _emit_progress(on_progress, collected, frames_target,
                                       status_lines, fault_events, t0)
                if raw is None:
                    # WHICH of the two ended it. The quiet budget is capped by
                    # the remaining wall time, so a run that simply reached its
                    # budget while waiting for the next indication came back
                    # "timeout" — which reads as a broken engine, and led to a
                    # wall-capped campaign run being filed as a scenario
                    # result. Only call it a timeout if there was budget left.
                    status = ("timeout" if remaining > 300
                              else "wall-capped")
                    break
            except asyncio.TimeoutError:
                status = "timeout"
                break
            if not raw:
                status = "engine-closed"
                break
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if "[BUSY]" in line:
                err = "engine busy — another session (Live Traffic / RIC Test) is running; stop it and retry"
                status = "busy"
                break
            if line.startswith("[NSG-ERROR]"):
                err = line.replace("[NSG-ERROR] ", "")
                status = "error"
                break
            # A staged fault that actually FIRED. Kept apart from the status log
            # because it answers a question no metric can: an anomaly whose
            # start_s lands beyond the run's simulated horizon never happens, and
            # the run then returns healthy-looking metrics and passes its
            # thresholds. Measured: 10505.89 kbps with the fault at start_s=5.0
            # and without it — identical. Observation is the only proof.
            if line.startswith("[NSG-FAULT]"):
                fault_events.append(line)
                continue
            if line.startswith("[NSG-BRIDGE]") or line.startswith("[live]"):
                status_lines.append(line)
                _emit_progress(on_progress, collected, frames_target, status_lines,
                               fault_events, t0)
                continue
            frame = acc.feed(line)
            if frame:
                collected.append(frame)
                # A batch run is minutes of silence otherwise: the caller shows a
                # spinner and the operator cannot tell a working run from a stuck
                # one. Everything published here is data we already receive and
                # were discarding until the run ended.
                _emit_progress(on_progress, collected, frames_target, status_lines,
                               fault_events, t0)
    finally:
        # Closing the socket ends the bridge session (its trap tears ns-3 + RIC
        # down and releases the engine lock), so a batch run frees the engine.
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    return {"status": status, "error": err, "frames": collected,
            "status_lines": status_lines[-60:], "n_frames": len(collected),
            "fault_events": fault_events}


def summarise_batch_frames(frames: list, fault_events: list | None = None,
                           anomalies_configured: int = 0) -> dict:
    """Fold the per-tick frames into run-level KPIs + per-slice / per-measurement
    aggregates, so a finished run is immediately useful without re-scanning all
    frames. The raw frames stay the ground-truth time-series.

    `fault_events` / `anomalies_configured` add the one fact no KPI carries:
    whether the scenario's staged faults actually HAPPENED. They go into the
    summary rather than a side channel so a campaign can put a threshold on them
    (`anomalies_fired >= 1`) — the same machinery that guards every other claim.
    """
    if not frames:
        return {"ticks": 0}

    meas_acc: dict = {}
    for f in frames:
        for name, s in (f.get("measurements") or {}).items():
            if isinstance(s, dict) and s.get("mean") is not None:
                meas_acc.setdefault(name, []).append(float(s["mean"]))
    # Drop wrapped counters before averaging, and SAY how many went. A value
    # quietly removed misleads as much as a wrong one kept — see kpm_health.
    measurements, dropped = {}, {}
    for n, v in meas_acc.items():
        kept, lost = kpm_health.clean_samples(n, v)
        if lost:
            dropped[n] = lost
        if kept:
            measurements[n] = {
                "mean": round(sum(kept) / len(kept), 2), "min": round(min(kept), 2),
                "max": round(max(kept), 2), "ticks": len(kept)}

    slice_acc: dict = {}
    for f in frames:
        for name, kbps in (f.get("slices") or {}).items():
            slice_acc.setdefault(name, []).append(float(kbps or 0.0))
    slices = {
        n: {"mean_mbps": round(sum(v) / len(v) / 1000.0, 2),
            "max_mbps": round(max(v) / 1000.0, 2)}
        for n, v in slice_acc.items()
    }

    dl_per_tick = [sum((u.get("thp_kbps") or 0.0) for u in (f.get("ues") or [])) for f in frames]
    ue_counts = [len(f.get("ue_ids") or f.get("ues") or []) for f in frames]

    # HOW MUCH SIMULATED TIME THIS RUN ACTUALLY COVERS. Without it the only
    # horizon a user sees is a tick count, and ticks are KPM windows — about 13
    # to the simulated second, not one each. That is the whole of "I asked for
    # 120 frames, how many seconds is that?": nothing answered it, so a run that
    # covered three seconds looked like one that covered a hundred and twenty.
    sim_ts = [f.get("sim_t") for f in frames if f.get("sim_t") is not None]
    sim_span = round(max(sim_ts) - min(sim_ts), 2) if len(sim_ts) > 1 else None

    return {
        "ticks": len(frames),
        "tick_range": [frames[0].get("tick"), frames[-1].get("tick")],
        "sim_span_s": sim_span,
        "sim_t_range": [min(sim_ts), max(sim_ts)] if sim_ts else None,
        "dl_mbps_mean": round(sum(dl_per_tick) / len(dl_per_tick) / 1000.0, 2) if dl_per_tick else 0.0,
        "dl_mbps_max": round(max(dl_per_tick) / 1000.0, 2) if dl_per_tick else 0.0,
        "ue_count_mean": round(sum(ue_counts) / len(ue_counts), 1) if ue_counts else 0,
        "handovers_total": frames[-1].get("handovers"),
        "energy_final": frames[-1].get("energy"),
        "measurements": measurements,
        # What the engine's own counters get wrong, attached to the run that
        # carries them rather than left in a document nobody opens.
        "telemetry_caveats": kpm_health.caveats_for(measurements),
        "telemetry_dropped": dropped,
        "slices": slices,
        # The staged-fault audit. `fired` counts events the ENGINE reported, so a
        # scenario that configured a fault the run never reached shows
        # configured>0 with fired=0 — the silent false PASS, made visible.
        "anomalies_configured": anomalies_configured,
        "anomalies_fired": len(fault_events or []),
        "anomaly_events": list(fault_events or [])[:40],
    }
