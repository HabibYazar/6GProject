"""
The two xApps under study, and the four conflict-resolution policies.

Both xApps are deliberately trivial rule-based controllers. The study is not
about whether either is a good energy or capacity manager — it is about what
happens when two independent controllers write the SAME actuator in the same
control interval. Simple rules make the conflict legible: every action is
traceable to one threshold crossing, so a disputed cell can be explained in one
line rather than attributed to a black box.

  eSave  lightly-loaded cell  -> ask for LESS power
  maxTP  poorly-served cell   -> ask for MORE power

They collide precisely where a cell is both lightly loaded and badly served,
which is what the edge cluster in scenario.py manufactures on purpose.

Actuator note: cell_power is clamped to integer dBm in 0..46, so every policy
lands on a 1 dB grid. STEP_DB and the P3 weight are chosen so that weighted
synthesis lands ON that grid rather than being silently rounded into agreement
with one of its inputs — otherwise P3 would be indistinguishable from P0/P1 for
reasons of arithmetic rather than reasons of policy.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

# Kept in step with scenario.BASE_TX_DBM — see the note there for why 36 and not
# the engine's 43: at 43 dBm maxTP is clipped against the ceiling almost
# immediately and every downstream number is contaminated by that, P3 worst of
# all. 36 gives 5 steps up and 8 steps down.
BASE_DBM = 36
MIN_DBM = 20
MAX_DBM = 46
STEP_DB = 2

PRB_LOW_PCT = 30.0     # below this a cell is "lightly loaded" -> eSave acts
SINR_LOW_DB = 8.0      # below this a cell is "poorly served"  -> maxTP acts

P3_WEIGHT = 0.5        # eSave's share in the synthesis; 0.5 with STEP_DB=2
                       # keeps the result on the integer dBm grid


@dataclass
class Request:
    xapp: str
    cell: int
    target_dbm: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"xapp": self.xapp, "cell": self.cell,
                "target_dbm": self.target_dbm, "reason": self.reason}


def _cell_rows(kpm: dict) -> list[dict]:
    return [c for c in (kpm.get("cells") or []) if c.get("cell") is not None]


def esave(kpm: dict, power: dict[int, int]) -> list[Request]:
    """Lower the power of any cell whose PRB utilisation is under the floor."""
    out = []
    for c in _cell_rows(kpm):
        prb = c.get("prb")
        if prb is None or prb >= PRB_LOW_PCT:
            continue
        cell = int(c["cell"])
        cur = power.get(cell, BASE_DBM)
        tgt = max(MIN_DBM, cur - STEP_DB)
        if tgt != cur:
            out.append(Request("eSave", cell, tgt, f"prb={prb:.1f}%<{PRB_LOW_PCT}"))
    return out


def maxtp(kpm: dict, power: dict[int, int]) -> list[Request]:
    """Raise the power of any cell whose worst served UE is under the SINR floor."""
    by_cell: dict[int, list[float]] = {}
    for u in (kpm.get("ues") or []):
        if u.get("cell") is None or u.get("sinr") is None:
            continue
        by_cell.setdefault(int(u["cell"]), []).append(float(u["sinr"]))
    out = []
    for cell, sinrs in by_cell.items():
        worst = min(sinrs)
        if worst >= SINR_LOW_DB:
            continue
        cur = power.get(cell, BASE_DBM)
        tgt = min(MAX_DBM, cur + STEP_DB)
        if tgt != cur:
            out.append(Request("maxTP", cell, tgt, f"minSINR={worst:.1f}dB<{SINR_LOW_DB}"))
    return out


# ── conflict resolution ─────────────────────────────────────────────────────
#
# Each policy takes the requests aimed at ONE cell and returns the sequence of
# target values to actually write, plus a label for what it did. Returning more
# than one value is not a mistake: P2 deliberately writes twice, because the
# cost of rolling back is part of what the study measures.
#
# READ THIS BEFORE WRITING UP P0 vs P1.
#
# The request list is built eSave-first, always. The order is deterministic, so:
#
#   P0 "last writer wins"  -> maxTP wins EVERY contested cell
#   P1 "first writer wins" -> eSave wins EVERY contested cell
#
# These are therefore FIXED-PRIORITY policies, not a race. The names are the
# design document's; the mechanism is a static precedence chosen here. Report
# them as capacity-priority (P0) and energy-priority (P1) arbitration — that is
# a real and common O-RAN conflict-mitigation category, and it is what was
# measured. Calling them last/first-writer-wins would imply an arrival-order
# race that this harness never simulates.
#
# The consequence is sharpest on the discrete actuator: under P0 maxTP wins and
# asks for a cell that is already ON, so the write is a no-op and the arm issues
# ZERO commands across all three seeds. The energy saving is not clawed back by
# the conflict — it is pre-empted, and never happens at all.
#
# Modelling a genuine race means shuffling the request order per control
# interval from the run seed, which invalidates both matrices and costs a full
# rerun. Deferred deliberately; revisit if a reviewer asks for it.

def resolve(policy: str, cell: int, reqs: list[Request],
            cur: int) -> tuple[list[int], str]:
    if not reqs:
        return [], "none"
    if len(reqs) == 1:
        return [reqs[0].target_dbm], "uncontested"

    if policy == "P0":          # last writer wins
        return [reqs[-1].target_dbm], "lww"
    if policy == "P1":          # first writer holds the lock, the rest are dropped
        return [reqs[0].target_dbm], "blocked"
    if policy == "P2":          # apply, notice the clash, put it back
        return [reqs[0].target_dbm, cur], "rolledback"
    if policy == "P3":          # synthesise a single value from both requests
        by = {r.xapp: r.target_dbm for r in reqs}
        if "eSave" in by and "maxTP" in by:
            v = P3_WEIGHT * by["eSave"] + (1 - P3_WEIGHT) * by["maxTP"]
            return [int(round(v))], "synthesised"
        return [reqs[-1].target_dbm], "lww"
    raise ValueError(f"unknown policy {policy}")


def observation_key(kpm: dict) -> tuple:
    """What an xApp can actually distinguish from one indication to the next.

    The engine flushes KPM in bursts of about five indications that all describe
    the SAME state: identical total watts, identical per-cell rows. An xApp that
    acts on every indication therefore acts up to five times on one observation,
    and the oscillation count that comes out measures the client's polling rate
    rather than the conflict. Gating on this key makes one action per genuine
    observation, which is also what the design means by a control interval.
    """
    cells = tuple(sorted((c.get("cell"), c.get("prb"), c.get("sinr"))
                         for c in (kpm.get("cells") or [])))
    return ((kpm.get("energy") or {}).get("total_w"), cells)


def direction_flips(values: list[int]) -> int:
    """Reversals in a commanded sequence, ignoring repeats.

    A naive three-point sign test reads 46,46,44,44,46 as no reversal at all,
    because the zero differences break the chain — which is exactly the shape a
    burst-gated controller produces. Collapsing runs first is the difference
    between reporting a limit cycle and reporting nothing.
    """
    deltas = [b - a for a, b in zip(values, values[1:]) if b != a]
    return sum(1 for a, b in zip(deltas, deltas[1:]) if a * b < 0)


@dataclass
class Controller:
    """One arm of the experiment: which xApps run, and how clashes are settled.

    Holds the power state because the engine reports measured watts, not the
    dBm we last commanded — without tracking it here an xApp could not compute
    its own next step.

    Two gates decide when it is allowed to act at all:
      · the roster gate — nothing until every UE has attached, so each arm enters
        its measurement window from the same baseline power rather than from
        whatever the xApps did to a half-built network
      · the observation gate — nothing on a repeat of a state already seen
    """

    arm: str
    use_esave: bool = False
    use_maxtp: bool = False
    policy: str = "P0"
    expected_ues: int = 9
    power: dict[int, int] = field(default_factory=dict)
    journal: list[dict] = field(default_factory=list)
    settled: bool = False
    _last_key: tuple | None = None
    ramp_ticks: int = 0
    # Settled observations seen so far. This, not the indication count, is the
    # run's real length: it is how many times the controller got new information
    # and was allowed to act on it.
    measure_count: int = 0

    def __call__(self, kpm: dict):
        if not self.settled:
            if len(kpm.get("ues") or []) < self.expected_ues:
                self.ramp_ticks += 1
                return [], {"phase": "ramp", "ues": len(kpm.get("ues") or [])}
            self.settled = True

        key = observation_key(kpm)
        if key == self._last_key:
            return [], {"phase": "stale"}
        self._last_key = key
        self.measure_count += 1

        reqs: list[Request] = []
        if self.use_esave:
            reqs += esave(kpm, self.power)
        if self.use_maxtp:
            reqs += maxtp(kpm, self.power)

        cmds: list[dict] = []
        per_cell: dict[int, list[Request]] = {}
        for r in reqs:
            per_cell.setdefault(r.cell, []).append(r)

        contested = 0
        outcomes: list[dict] = []
        for cell, rs in sorted(per_cell.items()):
            cur = self.power.get(cell, BASE_DBM)
            targets, how = resolve(self.policy, cell, rs, cur)
            if len(rs) > 1:
                contested += 1
            for t in targets:
                cmds.append({"cmd": "cell_power", "cell": cell, "dbm": int(t)})
                self.power[cell] = int(t)
            outcomes.append({"cell": cell, "how": how, "n_requests": len(rs),
                             "targets": targets, "from_dbm": cur})

        note = {
            "phase": "measure",
            "requests": [r.as_dict() for r in reqs],
            "outcomes": outcomes,
            "contested_cells": contested,
            "power_after": dict(self.power),
        }
        self.journal.append(note)
        return cmds, note


# The seven arms of scenario S1 (continuous parameter: TX power).
#
# S2 (the discrete parameter, cell on/off) has SIX arms, not seven. P3 is a
# weighted synthesis of two requested values, and there is no value between "on"
# and "off" to synthesise — the arm is undefined by the design of the policy, not
# unimplemented. That is a result the paper states in prose; the code must never
# attempt it and report a runtime failure, because a design-time argument that
# surfaces as a stack trace reads like a bug in the harness.
ARMS_S1: dict[str, dict] = {
    "A_none":     {"use_esave": False, "use_maxtp": False},
    "B_esave":    {"use_esave": True,  "use_maxtp": False},
    "C_maxtp":    {"use_esave": False, "use_maxtp": True},
    "D_both_P0":  {"use_esave": True,  "use_maxtp": True, "policy": "P0"},
    "E_both_P1":  {"use_esave": True,  "use_maxtp": True, "policy": "P1"},
    "F_both_P2":  {"use_esave": True,  "use_maxtp": True, "policy": "P2"},
    "G_both_P3":  {"use_esave": True,  "use_maxtp": True, "policy": "P3"},
}


def make(arm: str) -> Controller:
    if arm not in ARMS_S1:
        raise SystemExit(f"unknown arm {arm}; have {sorted(ARMS_S1)}")
    return Controller(arm=arm, **ARMS_S1[arm])


# ── S2: the discrete actuator ───────────────────────────────────────────────
#
# Same two intents, same four policies, one actuator that cannot be averaged: a
# cell is on or it is off. This is the scenario that carries the paper's claim,
# because P3 has nothing to synthesise between "on" and "off" — the arm is absent
# by definition, not by omission.
#
# The rules are deliberately the S1 rules restated for a boolean:
#   eSave  an ON cell running under the PRB floor should sleep
#   maxTP  no cell may sleep while any UE is underserved
# They collide on exactly the cell S1 contested — one lightly loaded, in a network
# whose worst UE is below the SINR floor.

def esave_s2(kpm: dict, on: dict[int, bool]) -> list[Request]:
    out = []
    for c in _cell_rows(kpm):
        prb = c.get("prb")
        cell = int(c["cell"])
        if prb is None or prb >= PRB_LOW_PCT or not on.get(cell, True):
            continue
        out.append(Request("eSave", cell, 0, f"prb={prb:.1f}%<{PRB_LOW_PCT}"))
    return out


def maxtp_s2(kpm: dict, on: dict[int, bool]) -> list[Request]:
    sinrs = [float(u["sinr"]) for u in (kpm.get("ues") or [])
             if u.get("sinr") is not None]
    if not sinrs or min(sinrs) >= SINR_LOW_DB:
        return []
    reason = f"minSINR={min(sinrs):.1f}dB<{SINR_LOW_DB}"
    return [Request("maxTP", int(c["cell"]), 1, reason) for c in _cell_rows(kpm)]


def resolve_s2(policy: str, reqs: list[Request], cur: int) -> tuple[list[int], str]:
    if not reqs:
        return [], "none"
    if len(reqs) == 1:
        return [reqs[0].target_dbm], "uncontested"
    if policy == "P0":
        return [reqs[-1].target_dbm], "lww"
    if policy == "P1":
        return [reqs[0].target_dbm], "blocked"
    if policy == "P2":
        return [reqs[0].target_dbm, cur], "rolledback"
    if policy == "P3":
        # Unreachable: ARMS_S2 has no P3 arm. Raising here would turn a
        # design-time argument into a stack trace in the results.
        raise AssertionError("P3 is undefined on a discrete actuator; "
                             "S2 has six arms by design")
    raise ValueError(f"unknown policy {policy}")


@dataclass
class ControllerS2:
    arm: str
    use_esave: bool = False
    use_maxtp: bool = False
    policy: str = "P0"
    expected_ues: int = 9
    on: dict[int, bool] = field(default_factory=dict)
    journal: list[dict] = field(default_factory=list)
    settled: bool = False
    _last_key: tuple | None = None
    ramp_ticks: int = 0
    measure_count: int = 0

    def __call__(self, kpm: dict):
        if not self.settled:
            if len(kpm.get("ues") or []) < self.expected_ues:
                self.ramp_ticks += 1
                return [], {"phase": "ramp", "ues": len(kpm.get("ues") or [])}
            self.settled = True
        key = observation_key(kpm)
        if key == self._last_key:
            return [], {"phase": "stale"}
        self._last_key = key
        self.measure_count += 1

        reqs: list[Request] = []
        if self.use_esave:
            reqs += esave_s2(kpm, self.on)
        if self.use_maxtp:
            reqs += maxtp_s2(kpm, self.on)

        per_cell: dict[int, list[Request]] = {}
        for r in reqs:
            per_cell.setdefault(r.cell, []).append(r)

        cmds, outcomes, contested = [], [], 0
        for cell, rs in sorted(per_cell.items()):
            cur = 1 if self.on.get(cell, True) else 0
            targets, how = resolve_s2(self.policy, rs, cur)
            if len(rs) > 1:
                contested += 1
            for t in targets:
                if t == cur and how != "rolledback":
                    continue          # already in that state; no actuation
                cmds.append({"cmd": "cell_on" if t else "cell_off", "cell": cell})
                self.on[cell] = bool(t)
                cur = t
            outcomes.append({"cell": cell, "how": how, "n_requests": len(rs),
                             "targets": targets, "from_dbm": cur})

        note = {"phase": "measure", "requests": [r.as_dict() for r in reqs],
                "outcomes": outcomes, "contested_cells": contested,
                "power_after": {str(c): int(v) for c, v in self.on.items()}}
        self.journal.append(note)
        return cmds, note


ARMS_S2: dict[str, dict] = {
    "A_none":    {"use_esave": False, "use_maxtp": False},
    "B_esave":   {"use_esave": True,  "use_maxtp": False},
    "C_maxtp":   {"use_esave": False, "use_maxtp": True},
    "D_both_P0": {"use_esave": True,  "use_maxtp": True, "policy": "P0"},
    "E_both_P1": {"use_esave": True,  "use_maxtp": True, "policy": "P1"},
    "F_both_P2": {"use_esave": True,  "use_maxtp": True, "policy": "P2"},
    # no G: see resolve_s2
}
