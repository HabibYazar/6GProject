"""
NSG AI Platform — RIC Test Simulation Core (simplified E2 foundation)

The stateful, controllable RAN model behind the RIC test loop. Unlike the
stateless estimator in ns3_bridge (config → one metrics snapshot), this holds a
MUTABLE RAN state that:

  - reports KPIs upward   (E2SM-KPM analogue)  via  get_kpm_report()
  - accepts control down   (E2SM-RC analogue)   via  apply_control()
  - evolves over time                            via  tick()

An external xApp (the AI being tested) reads the KPM report, decides an action,
and sends a control command; we mutate state and the NEXT report reflects the
effect. That closed loop is what makes this a RIC *test* rather than a data
feed — the AI sees the consequence of its own decisions.

Physics (RSRP, Shannon throughput) is reused from ns3_bridge, so RIC-test
numbers stay consistent with the live-traffic estimator and the REM coverage
map — one shared physical model, three surfaces.

SIMPLIFIED E2: the report/control contract here is our own JSON schema, not
standards-compliant E2AP / ASN.1 / SCTP. Full O-RAN E2 compliance is a later
milestone; this lays the foundation with the same closed-loop *semantics* so a
user's xApp can be validated against realistic, controllable RAN scenarios.

Control actions supported in this foundation (the 4 strong-fit domains):
  handover     — Traffic Steering / load balancing: reassign a UE group's cell
  cell_power   — Energy Saving: turn a cell off/on or override its tx power
  slice_quota  — QoS / Slice SLA assurance: cap a slice's PRB share
  reset        — clear all control overrides, back to the config baseline
"""
from __future__ import annotations

import math

from schema import NSGSimulationConfig
import window_model
from partition_live import CELL_BASE
from ns3_bridge import (
    _ACTIVITY_FACTOR,
    _noise_floor_dbm,
    _received_power_dbm,
    link_metrics,
    spectral_overlap,
)


_UNSET = object()


class RicSimulation:
    """One stateful RAN instance under test. Not thread-safe; drive it from a
    single async task / connection."""

    def __init__(self, cfg: NSGSimulationConfig, window_s: float = 1.0):
        self.cfg = cfg
        self.tick_count = 0
        # How much airtime one report covers. The counters are EXTENSIVE —
        # blocks and bytes scale with it — so a report broadcast every 0.5 s
        # that claims a second of blocks is simply wrong.
        self.window_s = max(0.05, float(window_s or 1.0))
        # Where each group actually IS. The config's spawn position is where it
        # started; a quasi-static RAN that never moves cannot hand over, and a
        # handover-steering xApp has nothing to be tested against.
        self._pos: dict[str, tuple[float, float]] = {
            g.id: (g.spawn_position.x, g.spawn_position.y) for g in cfg.ue_groups}
        self._leg: dict[str, tuple[int, int]] = {}      # group -> (waypoint, direction)
        # Bytes still waiting per UE. Carried ACROSS windows, which is what
        # makes DRB.BufferSize.Qos a measurement rather than a constant.
        self._queue: dict[tuple, float] = {}
        self._queue_ul: dict[tuple, float] = {}
        self._params_base: dict | None = None
        # One shadowing/LOS realisation for the whole session — a new draw
        # every tick would flicker between channels that are each valid and
        # none of which is this scenario's. It is the CONFIG'S seed, which
        # this surface used to ignore: every scenario drew realisation 0
        # however the user set `random_seed`, while the slow tier honoured
        # it as `--rngRun`.
        try:
            self._seed = int(getattr(cfg.settings, "random_seed", 0) or 0)
        except (TypeError, ValueError):
            self._seed = 0
        self._serving_prev: dict[str, str | None] = {}
        self.handovers = 0              # cumulative, over the session
        self.handovers_last = 0         # in the window just reported

        # ── Mutable control state (set by apply_control) ──────────────────────
        # Cells the xApp has switched off (energy saving). Off cells don't serve
        # UEs and report 0 W.
        self.disabled_cells: set[str] = set()
        # Per-cell tx-power override in dBm (energy saving / power control).
        self.power_override: dict[str, float] = {}
        # UE-group → cell id: force a serving cell (handover / traffic steering).
        # Overrides the default best-server selection.
        self.forced_serving: dict[str, str] = {}
        # Slice → PRB share cap in percent (QoS / slice assurance). Throughput
        # for that slice is scaled down if the cap is below its natural demand.
        self.slice_prb_quota: dict[str, float] = {}

        # ── Injected anomalies (set by the OPERATOR, not the xApp) ────────────
        # Faults the AI-under-test must react to. Distinct from control state:
        # the xApp cannot clear these — it can only route/manage around them.
        # Faulted cells are offline like disabled ones, but cell_power "on"
        # can't revive them (a hardware failure, not an energy-saving choice).
        self.faulted_cells: set[str] = set()
        # Global traffic-demand multiplier (a surge/flash-crowd). 1.0 = nominal.
        self.demand_multiplier: float = 1.0

        self._cell_by_id = {bs.id: bs for bs in cfg.base_stations}

    @property
    def _offline_cells(self) -> set[str]:
        """Cells not carrying traffic: AI-disabled (energy saving) OR faulted
        (operator-injected failure). Serving-cell selection and interference
        both skip these."""
        return self.disabled_cells | self.faulted_cells

    def reset_controls(self) -> None:
        """Clear all RC control state (cells re-enabled, power/serving/quota overrides
        dropped) so the network returns to baseline — e.g. when the AI xApp is stopped,
        instead of leaving it in whatever (possibly all-cells-off) state the AI ended in.
        Operator-injected faults are left alone (they have their own 'clear anomalies')."""
        self.disabled_cells.clear()
        self.power_override.clear()
        self.forced_serving.clear()
        self.slice_prb_quota.clear()

    # ── Control plane (E2SM-RC analogue) ──────────────────────────────────────

    def apply_control(self, cmd: dict) -> dict:
        """
        Apply one control command from the xApp. Returns an ack dict:
          {"action": <str>, "success": <bool>, "detail": <str>}

        Command formats:
          {"action": "handover",    "ue_group": <id>, "target_cell": <id>}
          {"action": "cell_power",  "cell": <id>, "state": "off"|"on"}
          {"action": "cell_power",  "cell": <id>, "tx_power_dbm": <float>}
          {"action": "slice_quota", "slice": <"eMBB"|...>, "prb_percent": <0-100>}
          {"action": "reset"}
        """
        action = cmd.get("action")

        if action == "handover":
            ue_id, target = cmd.get("ue_group"), cmd.get("target_cell")
            if not any(ue.id == ue_id for ue in self.cfg.ue_groups):
                return self._ack(action, False, f"unknown ue_group '{ue_id}'")
            if target not in self._cell_by_id:
                return self._ack(action, False, f"unknown target_cell '{target}'")
            if target in self._offline_cells:
                reason = "faulted" if target in self.faulted_cells else "switched off"
                return self._ack(action, False, f"target_cell '{target}' is {reason}")
            self.forced_serving[ue_id] = target
            return self._ack(action, True, f"{ue_id} → {target}")

        if action == "cell_power":
            cell = cmd.get("cell")
            if cell not in self._cell_by_id:
                return self._ack(action, False, f"unknown cell '{cell}'")
            if "state" in cmd:
                if cmd["state"] == "off":
                    self.disabled_cells.add(cell)
                    # Any UE forced onto a now-off cell falls back to best-server.
                    self.forced_serving = {u: c for u, c in self.forced_serving.items() if c != cell}
                    return self._ack(action, True, f"{cell} off")
                if cmd["state"] == "on":
                    self.disabled_cells.discard(cell)
                    return self._ack(action, True, f"{cell} on")
                return self._ack(action, False, f"invalid state '{cmd['state']}'")
            if "tx_power_dbm" in cmd:
                self.power_override[cell] = float(cmd["tx_power_dbm"])
                return self._ack(action, True, f"{cell} tx_power={cmd['tx_power_dbm']} dBm")
            return self._ack(action, False, "cell_power needs 'state' or 'tx_power_dbm'")

        if action == "slice_quota":
            slice_name = cmd.get("slice")
            pct = cmd.get("prb_percent")
            if slice_name not in ("eMBB", "uRLLC", "mMTC", "voice"):
                return self._ack(action, False, f"unknown slice '{slice_name}'")
            if pct is None:
                return self._ack(action, False, "slice_quota needs 'prb_percent'")
            self.slice_prb_quota[slice_name] = max(0.0, min(100.0, float(pct)))
            return self._ack(action, True, f"{slice_name} PRB cap = {pct}%")

        if action == "reset":
            self.disabled_cells.clear()
            self.power_override.clear()
            self.forced_serving.clear()
            self.slice_prb_quota.clear()
            return self._ack(action, True, "all control overrides cleared")

        return self._ack(str(action), False, "unknown action")

    @staticmethod
    def _ack(action: str, success: bool, detail: str) -> dict:
        return {"action": action, "success": success, "detail": detail}

    # ── Fault-injection plane (operator perturbs the RAN; AI must respond) ─────

    def inject_anomaly(self, cmd: dict) -> dict:
        """
        Inject an operator-driven anomaly so the xApp can be tested on incident
        response, not just steady-state optimisation. Returns an ack dict.

          {"kind": "cell_fault",    "cell": <id>}    — a cell fails (offline; the
                                                        xApp can't power it back on)
          {"kind": "clear_fault",   "cell": <id>?}   — repair one cell, or all
          {"kind": "traffic_surge", "multiplier": <float>}  — scale demand (flash crowd)
          {"kind": "clear_surge"}                    — demand back to nominal
          {"kind": "clear_all"}                      — clear every anomaly
        """
        kind = cmd.get("kind")

        if kind == "cell_fault":
            cell = cmd.get("cell")
            if cell not in self._cell_by_id:
                return self._ack(kind, False, f"unknown cell '{cell}'")
            self.faulted_cells.add(cell)
            # UEs forced onto a now-failed cell fall back to best-server.
            self.forced_serving = {u: c for u, c in self.forced_serving.items() if c != cell}
            return self._ack(kind, True, f"{cell} FAULT")

        if kind == "clear_fault":
            cell = cmd.get("cell")
            if cell:
                self.faulted_cells.discard(cell)
                return self._ack(kind, True, f"{cell} repaired")
            self.faulted_cells.clear()
            return self._ack(kind, True, "all cell faults repaired")

        if kind == "traffic_surge":
            self.demand_multiplier = max(0.1, min(10.0, float(cmd.get("multiplier", 2.0))))
            return self._ack(kind, True, f"demand ×{self.demand_multiplier:g}")

        if kind == "clear_surge":
            self.demand_multiplier = 1.0
            return self._ack(kind, True, "surge cleared")

        if kind == "clear_all":
            self.faulted_cells.clear()
            self.demand_multiplier = 1.0
            return self._ack(kind, True, "all anomalies cleared")

        return self._ack(str(kind), False, f"unknown anomaly kind '{kind}'")

    # ── State evolution ───────────────────────────────────────────────────────

    def tick(self) -> None:
        """Advance the RAN by one report interval.

        This used to be a counter and nothing else — "quasi-static (no
        time-stepped mobility yet)" — which meant the serving cell was decided
        once and never revisited, so the console could not produce a single
        handover and a traffic-steering xApp had nothing to steer. UEs now
        move, and the next report resolves their serving cells afresh.
        """
        self.tick_count += 1
        self._advance_mobility()

    # ── Effective physics respecting control state ────────────────────────────

    def _effective_power_dbm(self, cell_id: str) -> float:
        bs = self._cell_by_id[cell_id]
        return self.power_override.get(cell_id, bs.radio.tx_power_dbm)

    def _xy(self, ue) -> tuple[float, float]:
        """Where this group is NOW, not where the config spawned it."""
        return self._pos.get(ue.id, (ue.spawn_position.x, ue.spawn_position.y))

    def _advance_mobility(self) -> None:
        """Move every group by one report interval.

        A group with waypoints walks them at the leg's own speed, honouring
        `waypoint_mode`. A group without them drifts at `speed_mps` on a fixed
        per-group bearing — the engine runs `RandomWalk2dOutdoorMobilityModel`
        there, and a deterministic bearing reproduces the RATE at which UEs
        cross cell boundaries without pretending to reproduce which UE crosses
        when. Handovers are a rate; matching them one for one against a random
        walk is not a thing either tier can do.
        """
        for i, ue in enumerate(self.cfg.ue_groups):
            x, y = self._xy(ue)
            wps = list(getattr(ue.mobility, "waypoints", None) or [])
            if wps:
                idx, direction = self._leg.get(ue.id, (0, 1))
                idx = max(0, min(len(wps) - 1, idx))
                tgt = wps[idx]
                speed = float(getattr(tgt, "speed_mps", None)
                              or ue.mobility.speed_mps or 0.0)
                tx, ty = tgt.position.x, tgt.position.y
                dx, dy = tx - x, ty - y
                dist = math.hypot(dx, dy)
                step = speed * self.window_s
                if dist <= step or dist == 0.0:
                    x, y = tx, ty
                    mode = getattr(ue.mobility, "waypoint_mode", "pingpong")
                    nxt = idx + direction
                    if nxt >= len(wps) or nxt < 0:
                        if mode == "repeat":
                            nxt, direction = 0, 1
                        elif mode == "pingpong":
                            direction = -direction
                            nxt = idx + direction
                        else:                       # "once" — stay put
                            nxt, direction = idx, 0
                    self._leg[ue.id] = (max(0, min(len(wps) - 1, nxt)), direction)
                else:
                    x += dx / dist * step
                    y += dy / dist * step
            else:
                speed = float(ue.mobility.speed_mps or 0.0)
                if speed <= 0:
                    continue
                bearing = (i * 2.399963)            # golden angle, spreads the group
                x += math.cos(bearing) * speed * self.window_s
                y += math.sin(bearing) * speed * self.window_s
            self._pos[ue.id] = (x, y)

    def _array_gain_db(self, cell_id: str) -> float:
        """This cell's panel gain, from the config the engine is handed."""
        mm = getattr(getattr(self._cell_by_id[cell_id].radio, "antenna", None),
                     "massive_mimo", None)
        return window_model.array_gain_db(getattr(mm, "rows", 8) or 8,
                                          getattr(mm, "cols", 8) or 8)

    def _uplink_sinr(self, rx_of: dict, serving_of: dict) -> dict:
        """Uplink SINR per UE GROUP, from the downlink powers already computed.

        Reciprocity gives the arrival at the cell without touching the channel
        again: `rx` is `cell_tx - pathloss`, so swapping the transmit power
        for `settings.ue_tx_power_dbm` gives what the UE puts into the same
        link. The gNB adds its array gain on receive, as it does on transmit.

        The interference is the part that is NOT the downlink mirrored. Every
        other cell's UEs transmit into this cell, and which of them holds the
        slot changes every time, so each neighbour contributes the MEAN
        arrival of its own members — the expectation under the round-robin —
        weighted by the spectrum the two carriers share.
        """
        st = self.cfg.settings
        ue_tx = float(getattr(st, "ue_tx_power_dbm", 30.0) or 30.0)
        gnb_nf = float(getattr(st, "gnb_noise_figure_db", 5.0) or 5.0)
        cells = {bs.id: bs for bs in self.cfg.base_stations}
        members: dict[str, list[str]] = {}
        for uid, cid in serving_of.items():
            if cid and uid in rx_of:
                members.setdefault(cid, []).append(uid)

        def arrival_mw(uid: str, cid: str) -> float:
            rx = rx_of[uid].get(cid)
            if rx is None:
                return 0.0
            return 10 ** ((rx - cells[cid].radio.tx_power_dbm + ue_tx) / 10)

        # Per victim cell, the mean arrival from each OTHER cell's members.
        interf_at: dict[str, float] = {}
        for cid, bs in cells.items():
            band = (bs.radio.band.frequency_mhz, bs.radio.band.bandwidth_mhz)
            tot = 0.0
            for other, mem in members.items():
                if other == cid or not mem:
                    continue
                o = cells[other].radio.band
                frac = spectral_overlap(band, (o.frequency_mhz, o.bandwidth_mhz))
                if frac <= 0:
                    continue
                tot += frac * sum(arrival_mw(u, cid) for u in mem) / len(mem)
            interf_at[cid] = tot

        out: dict[str, float] = {}
        for uid, cid in serving_of.items():
            if not cid or uid not in rx_of:
                continue
            bw = cells[cid].radio.band.bandwidth_mhz
            noise = 10 ** (_noise_floor_dbm(max(1.0, bw) * 1e6, gnb_nf) / 10)
            sig = arrival_mw(uid, cid) * 10 ** (self._array_gain_db(cid) / 10)
            if sig <= 0:
                continue
            out[uid] = 10 * math.log10(sig / (noise + interf_at.get(cid, 0.0)))
        return out

    def _rx_from(self, cell_id: str, x: float, y: float) -> float:
        """TOTAL received power from a cell at (x,y), respecting any tx-power
        override. Received power scales linearly (dB) with tx power, so we add
        the override delta to the config-power value rather than recomputing
        path loss. (This is total rx power, not RSRP — link_metrics derives
        per-RE RSRP and interference-aware SINR from it.)"""
        bs = self._cell_by_id[cell_id]
        # Pass the scenario's buildings so RIC-test KPIs respect the same obstacles
        # the user drew on the map (previously only the REM heatmap did).
        base = _received_power_dbm(bs, x, y, self.cfg.buildings)
        delta = self._effective_power_dbm(cell_id) - bs.radio.tx_power_dbm
        return base + delta

    def _tier_b_link_state(self) -> dict:
        """Every UE's serving cell, RSRP, SINR and uplink SINR, keyed the
        console's way — (group id, index within the group).

        `tier_b.link_state` numbers UEs 1..n in `ue_positions` order, and
        `_tier_b_params` writes that list in config-group order with each
        group's `count` expanded. That is the same convention
        `_rate_of_imsi_map` and the slice map already use, so an IMSI here
        means the same UE it means everywhere else.

        `forced_serving` is applied AFTER the tier has chosen, because it is
        an xApp's override of a decision and not an input to it — the RSRP
        and the interference are the same whichever cell is told to serve.
        """
        import tier_b
        params = self._tier_b_params()
        links = tier_b.link_state(params, seed=self._seed)
        order = [b.id for b in self.cfg.base_stations][:params["n_gnb"]]
        slots: list[tuple] = []
        for ue in self.cfg.ue_groups:
            for k in range(int(ue.count)):
                slots.append((ue.id, k))
        out: dict[tuple, dict] = {}
        for imsi, l in links.items():
            if imsi - 1 >= len(slots):
                continue
            key = slots[imsi - 1]
            cid = order[l["cell"]] if l["cell"] < len(order) else None
            if cid in self._offline_cells:
                cid = None
            forced = self.forced_serving.get(key[0])
            if forced and forced not in self._offline_cells:
                cid = forced
            out[key] = {"cell_id": cid, "rsrp": l["rsrp_dbm"],
                        "sinr": l["sinr_db"],
                        # How far the link MOVES inside the window — the
                        # mixture's own number, not a fading sigma. Without
                        # it the console runs `cell_window` at the default
                        # 5.57 dB against a SINR the mixture has corrected
                        # upward, and every block decodes: measured, the
                        # block error rate read 0.0% for every UE and the
                        # RLC queue emptied because nothing was ever lost.
                        "sd": l.get("sinr_sd_db"),
                        "sinr_ul": l.get("sinr_ul_db", l["sinr_db"])}
        return out

    def _tier_b_params(self) -> dict:
        """The engine payload for THIS TICK, with the console's overrides in it.

        THE CONSOLE'S RAN AND THE BATCH TIER WERE TWO DIFFERENT MODELS. This
        surface already computed its window through `window_model` — the
        engine's own AMC, transport-block size and scheduler split — but it
        got its CHANNEL from `ns3_bridge._received_power_dbm`, a log-distance
        estimator with no shadowing, no LOS/NLOS draw, one carrier for the
        whole scene and no antenna pattern. `tier_b` has all four. So the
        console was fast-tier MAC on estimator-grade PHY, and the two could
        not be compared with each other, let alone with the slow tier.

        `nsoran_params` is the shared translation both tiers already agree
        on, so the fix is to build it and let `tier_b.link_state` do the
        geometry. The overrides the console owns and the payload does not —
        a cell switched off, a runtime TX power, UEs that have walked — are
        written into the payload here rather than modelled twice.

        `engine_clamped=False`: the console is the fast tier and ns-3's
        GNB_MAX/UE_MAX are not its ceilings. The payload is marked and
        `run_batch_simulation` refuses it, so it cannot leak to the engine.
        """
        from main import nsoran_params
        if self._params_base is None:
            self._params_base = nsoran_params(self.cfg, engine_clamped=False)
        p = dict(self._params_base)
        order = [b.id for b in self.cfg.base_stations][:p["n_gnb"]]
        # A cell that is OFF is not a quiet cell. -200 dBm is numerically
        # dead in the linear sum, where its configured power would leave it
        # interfering with everything.
        p["gnb_powers"] = ";".join(
            f"{(-200.0 if cid in self._offline_cells else self._effective_power_dbm(cid)):.1f}"
            for cid in order)
        # ...and which ones, by index, so the energy model charges them the
        # OFF state rather than idle. Without it a cell an xApp switched off
        # kept drawing 431 W and the saving the action exists to produce did
        # not appear in the counter that measures it.
        p["cells_off"] = ",".join(str(i) for i, cid in enumerate(order)
                                  if cid in self._offline_cells)
        # Where the groups are NOW, in the SCENARIO frame.
        #
        # `nsoran_params` recentres the whole scene, so config coordinates
        # cannot be written in here — doing so put the UEs in one frame and
        # the cells in another and the console read -118 dBm where the link
        # is -78. Applying a DISPLACEMENT instead is frame-independent and
        # needs no offset at all.
        # MOVE THE UEs, DO NOT RE-PLACE THEM. `nsoran_params` does more than
        # translate: `_ue_scenario_positions` SPREADS a group's UEs around
        # its spawn point, then `_rebalance_ue_cells`, `_push_out_of_buildings`
        # and `_separate_ues` push individuals off cells and out of walls.
        # Writing the group's single coordinate into every one of its slots
        # threw all of that away and stacked six UEs on one point — 6 distinct
        # positions for 36 UEs — so a group could only ever attach as a block
        # and its members all saw the identical link.
        #
        # What the console owns is the group's DISPLACEMENT since spawn, so
        # that is what is applied: each UE keeps the placement the shared
        # translation gave it and moves with its group.
        base = [f.split(",") for f in p["ue_positions"].split(";") if f]
        xy, i = [], 0
        for ue in self.cfg.ue_groups:
            cx, cy = self._xy(ue)
            mx = cx - ue.spawn_position.x
            my = cy - ue.spawn_position.y
            for _ in range(int(ue.count)):
                if i < len(base):
                    f = base[i]
                    h = f[2] if len(f) > 2 else "1.5"
                    xy.append(f"{float(f[0]) + mx:.1f},{float(f[1]) + my:.1f},{h}")
                i += 1
        if xy:
            p["ue_positions"] = ";".join(xy)
        return p

    def _rx_from_all(self, x: float, y: float) -> list[tuple[object, float]]:
        """(BaseStation, rx_dbm) for every ENABLED cell at (x,y) — the candidate
        set for serving selection and co-channel interference."""
        return [
            (bs, self._rx_from(bs.id, x, y))
            for bs in self.cfg.base_stations
            if bs.id not in self._offline_cells
        ]

    def ranked_cells_for(self, ue_id: str) -> list[str]:
        """Cell ids serving a UE group's location, strongest received power first
        (enabled cells only). Used by the training env's handover actions to pick
        a next-best target."""
        ue = next((u for u in self.cfg.ue_groups if u.id == ue_id), None)
        if ue is None:
            return []
        ranked = sorted(
            self._rx_from_all(*self._xy(ue)),
            key=lambda t: t[1],
            reverse=True,
        )
        return [bs.id for bs, _ in ranked]

    def _serving_cell_for(self, ue) -> tuple[str | None, float]:
        """Resolve a UE group's serving cell id + total rx power, honoring
        handover overrides and skipping switched-off cells."""
        x, y = self._xy(ue)

        forced = self.forced_serving.get(ue.id)
        if forced and forced not in self._offline_cells:
            return forced, self._rx_from(forced, x, y)

        cand = [(bs, self._rx_from(bs.id, x, y))
                for bs in self.cfg.base_stations
                if bs.id not in self._offline_cells]
        if not cand:
            return None, float("-inf")
        # Across CARRIERS it is priority first and strength second. TS 38.304
        # reselects on an absolute priority per frequency, not on which cell
        # is loudest — rank by strength alone and every UE lands on the low
        # band, which propagates better, and the capacity layer sits empty.
        # Same rule as `tier_b._select_cell`, so the console's RAN and the
        # batch tier put a UE on the same carrier.
        freqs = {bs.radio.band.frequency_mhz for bs, _ in cand}
        if len(freqs) > 1:
            from mcs import link as mcs_link
            for f in sorted(freqs, reverse=True):
                here = [(bs, rx) for bs, rx in cand
                        if bs.radio.band.frequency_mhz == f]
                ok = [(bs, rx) for bs, rx in here
                      if not mcs_link(link_metrics(
                          bs, rx + self._array_gain_db(bs.id), cand)[1])["in_outage"]]
                if ok:
                    bs, rx = max(ok, key=lambda t: t[1])
                    return bs.id, rx
        bs, rx = max(cand, key=lambda t: t[1])
        return bs.id, rx

    # ── Reporting plane (E2SM-KPM analogue) ───────────────────────────────────

    def get_kpm_report(self) -> dict:
        """
        Current KPI snapshot for the xApp — everything it needs to make
        steering / energy / QoS decisions.

        {
          "tick": int,
          "cells":      [{id, name, active, faulted, tx_power_dbm, power_w,
                          prb_used_pct, throughput_mbps, connected_ues}],
          "ues":        [{imsi, cell, sinr, rsrp, thp_kbps, ul_kbps, prb, mcs,
                          bler_pct, queued_kb, slice, x, y}] — per UE, the
                         same shape the slow tier publishes
          "ue_groups":  [{id, name, serving_cell, rsrp_dbm, sinr_db,
                          throughput_mbps, count}],
          "slices":     [{slice, throughput_mbps, prb_pct, qos_score}],
          "control_state": {disabled_cells, power_override,
                            forced_serving, slice_prb_quota},
          "anomalies":  {faulted_cells, demand_multiplier}
        }
        """
        # ── The RAN, one KPM window at a time ─────────────────────────────────
        # Until Phase 2 this computed a Shannon capacity per UE group and
        # throttled it by a PRB count. Shannon has no modulation in it, so the
        # report could never carry the counters a real E2SM-KPM subscription
        # asks for, and the numbers drifted from the engine's for reasons
        # nobody could name. `window_model` is the engine's own chain — its
        # AMC tables, its transport-block size, its scheduler's flow split —
        # so this surface and the real engine now disagree only where the
        # model is known to be wrong, which `tools/tier_agreement.py` prints.
        activity_of, sinr_of, serving_of = {}, {}, {}
        # THE GEOMETRY IS `tier_b`'s NOW, not this file's. One call gives
        # every UE its serving cell, RSRP, downlink SINR and uplink SINR
        # from the 3GPP channel the batch tier uses — shadowing, the LOS
        # draw, per-cell carriers, the antenna pattern and UE-to-UE uplink
        # interference — instead of the log-distance estimator this console
        # used to call. See `_tier_b_params`.
        per_slot = self._tier_b_link_state()
        for i, ue in enumerate(self.cfg.ue_groups):
            activity = _ACTIVITY_FACTOR.get(ue.mobility.pattern.value, 0.6)
            # Gentle deterministic per-tick breathing (+/-8%, phased per group)
            # so the live view undulates instead of sitting flat — no RNG, so
            # runs stay reproducible and a control action still reads as a
            # clean step change.
            breathe = 1.0 + 0.08 * math.sin(0.5 * self.tick_count + i * 1.7)
            activity_of[ue.id] = activity * self.demand_multiplier * breathe
            # A group's UEs no longer have to share a serving cell — the
            # tier resolves each one. The group ROW still reports one, so
            # the row takes the cell most of its UEs are on and the mean of
            # their RSRP/SINR, while the scheduler below uses each UE's own.
            mine = [per_slot[k] for k in per_slot if k[0] == ue.id]
            if not mine:
                serving_of[ue.id] = None
                continue
            cells = [m["cell_id"] for m in mine if m["cell_id"]]
            if not cells:
                serving_of[ue.id] = None
                continue
            cell_id = max(set(cells), key=cells.count)
            serving_of[ue.id] = cell_id
            sinr_of[ue.id] = (
                sum(m["rsrp"] for m in mine) / len(mine),
                sum(m["sinr"] for m in mine) / len(mine))

        # A group is `count` UEs standing together, and the scheduler's
        # concurrency ceiling counts UEs, not groups — six groups of one and
        # one group of six are very different cells. Expand, then fold back.
        band_of = {bs.id: bs.radio.band for bs in self.cfg.base_stations}
        per_cell: dict[str, dict[tuple, float]] = {}
        demand_of: dict[tuple, float] = {}
        sinr_ul_of: dict[tuple, float] = {}
        for ue in self.cfg.ue_groups:
            cell_id = serving_of.get(ue.id)
            if cell_id is None or ue.id not in sinr_of:
                continue
            target_bps = sum(
                svc.target_throughput_gbps for svc in ue.services) * 1e9
            for k in range(int(ue.count)):
                key = (ue.id, k)
                slot = per_slot.get(key)
                if slot is None or not slot["cell_id"]:
                    continue
                # ITS OWN cell and ITS OWN SINR, not the group's. Folding
                # them together was a property of the old per-group
                # geometry, and it put every UE of a group on one cell even
                # when they stood hundreds of metres apart.
                per_cell.setdefault(slot["cell_id"], {})[key] = slot["sinr"]
                sinr_ul_of[key] = slot["sinr_ul"]
                demand_of[key] = target_bps * activity_of[ue.id]

        # Same levers the batch tier honours, off the same payload.
        params = self._tier_b_params()
        knobs = window_model.Knobs.of(params)
        window = self.window_s

        # A handover is the serving cell CHANGING, counted per UE the way the
        # engine counts it — a group of six that crosses a boundary together
        # is six handovers, not one. Both causes count: the geometry moving
        # under a UE, and an xApp steering it.
        # PER UE, not per group. The group row still reports one serving
        # cell, chosen by majority, and that vote FLICKERS when a group's
        # UEs are split near-evenly between two cells — six handovers
        # reported, then six back, for UEs that moved three metres. The
        # per-UE cell is now known, so the count is exact instead of a
        # group-level proxy, and a group whose UEs genuinely straddle a
        # boundary reports the ones that actually crossed.
        moved = 0
        for key, slot in per_slot.items():
            was = self._serving_prev.get(key, _UNSET)
            now = slot["cell_id"]
            if was is not _UNSET and was != now and was is not None and now is not None:
                moved += 1
            self._serving_prev[key] = now
        self.handovers_last = moved
        self.handovers += moved
        per_ue: dict[tuple, object] = {}
        per_ue_rows: list[dict] = []
        # IMSI is attach order across the whole config — the same convention
        # `_rate_of_imsi_map`, the slice map and `tier_b` all use, so an IMSI
        # here names the same UE it names everywhere else.
        imsi_of: dict[tuple, int] = {}
        _n = 0
        for _ue in self.cfg.ue_groups:
            for _k in range(int(_ue.count)):
                _n += 1
                imsi_of[(_ue.id, _k)] = _n
        cell_index = {b.id: i + CELL_BASE
                      for i, b in enumerate(self.cfg.base_stations)}
        # A deterministic ring so a group's UEs are drawn apart. The tier's
        # own spread lives in scenario coordinates inside `_tier_b_params`;
        # this is the same idea in config coordinates for the map, and it is
        # DETERMINISTIC so a UE does not jitter between frames.
        spread = [(round(28.0 * math.cos(i * 2.399963), 1),
                   round(28.0 * math.sin(i * 2.399963), 1))
                  for i in range(64)]
        cell_symbols: dict[str, float] = {}
        cell_watts: dict[str, float] = {}
        samples: dict[str, list[float]] = {}
        order = [b.id for b in self.cfg.base_stations]
        for cell_id, sinrs in per_cell.items():
            band = band_of[cell_id]
            sds = [per_slot[k]["sd"] for k in per_slot
                   if per_slot[k]["cell_id"] == cell_id
                   and per_slot[k].get("sd") is not None]
            cw = window_model.cell_window(
                cell_id, sinrs, {k: demand_of[k] for k in sinrs},
                band.bandwidth_mhz, band.numerology, window_s=window,
                decay_db=(sum(sds) / len(sds)) if sds else None,
                backlog_bytes={k: self._queue.get(k, 0.0) for k in sinrs},
                ue_sinr_ul_db={k: sinr_ul_of[k] for k in sinrs
                               if k in sinr_ul_of},
                ul_backlog_bytes={k: self._queue_ul.get(k, 0.0) for k in sinrs},
                knobs=knobs)
            for u in cw.ues:
                per_ue[u.imsi] = u
                self._queue[u.imsi] = u.queued_bytes
                self._queue_ul[u.imsi] = u.queued_ul_bytes
                cell_symbols[cell_id] = cell_symbols.get(cell_id, 0.0) + u.symbol_share
                for name, v in u.measurements().items():
                    samples.setdefault(name, []).append(v)
            # The RU's own draw, per the engine's state currents. The console
            # used to report RADIATED power here — 10 W at 40 dBm — beside a
            # slow tier reporting kilowatts for the identical scenario.
            ci = order.index(cell_id) if cell_id in order else 0
            cell_watts[cell_id] = window_model.cell_power_w(
                cw, window_model.RuPower.of(params, ci), window,
                off=cell_id in self._offline_cells)

        cell_tput:  dict[str, float] = {bs.id: 0.0 for bs in self.cfg.base_stations}
        cell_ues:   dict[str, float] = {bs.id: 0.0 for bs in self.cfg.base_stations}
        slice_tput: dict[str, float] = {"eMBB": 0.0, "uRLLC": 0.0, "mMTC": 0.0, "voice": 0.0}
        slice_sat_num: dict[str, float] = {k: 0.0 for k in slice_tput}
        slice_active:  dict[str, float] = {k: 0.0 for k in slice_tput}

        ue_rows = []
        for ue in self.cfg.ue_groups:
            cell_id = serving_of.get(ue.id)
            active = ue.count * activity_of[ue.id]
            if cell_id is None or ue.id not in sinr_of:
                ue_rows.append({
                    "id": ue.id, "name": ue.name, "serving_cell": None,
                    "rsrp_dbm": None, "sinr_db": None,
                    "throughput_mbps": 0.0, "count": ue.count,
                })
                # Outage UEs drag QoS down (0 satisfaction).
                for svc in ue.services:
                    slice_active[svc.slice_type.value] += active
                continue

            rsrp, sinr_db = sinr_of[ue.id]
            delivered_mbps = sum(
                (per_ue[(ue.id, k)].volume_bytes * 8 / 1e6 / window)
                for k in range(int(ue.count)) if (ue.id, k) in per_ue)
            target_mbps = sum(
                svc.target_throughput_gbps for svc in ue.services) * 1000.0

            group_tput = 0.0
            for svc in ue.services:
                share = ((svc.target_throughput_gbps * 1000.0 / target_mbps)
                         if target_mbps > 0 else 1.0 / max(1, len(ue.services)))
                got = delivered_mbps * share
                cap = self.slice_prb_quota.get(svc.slice_type.value)
                if cap is not None:
                    got *= cap / 100.0
                slice_tput[svc.slice_type.value] += got
                slice_active[svc.slice_type.value] += active
                want = svc.target_throughput_gbps * 1000.0 * active
                if want > 0:
                    slice_sat_num[svc.slice_type.value] += min(1.0, got / want) * active
                group_tput += got

            cell_tput[cell_id] += group_tput
            cell_ues[cell_id] += active
            ue_rows.append({
                "id": ue.id, "name": ue.name, "serving_cell": cell_id,
                "rsrp_dbm": round(rsrp, 1), "sinr_db": round(sinr_db, 1),
                "throughput_mbps": round(group_tput, 2), "count": ue.count,
            })

            # PER UE, in the shape the slow tier publishes.
            #
            # This tier resolves every UE individually now, and `ue_groups`
            # folds that back to one row per group because the group row is
            # what the control surface acts on. The FOLD threw away the
            # thing the live map is for — where each UE stands and which
            # cell it is on — so the fast tier's map had to be a schematic
            # while the slow tier's was a real metric plot of the same
            # scenario. Two views of one thing that cannot be compared is
            # the drift this codebase keeps finding; publishing the same
            # shape from both sides is what stops it.
            #
            # Positions are CONFIG coordinates, which is what the map
            # draws in: the tier computes in the recentred scenario frame,
            # so the group's displacement is applied to its spawn instead.
            gx, gy = self._xy(ue)
            slice_name = (ue.services[0].slice_type.value
                          if ue.services else "eMBB")
            for k in range(int(ue.count)):
                u = per_ue.get((ue.id, k))
                slot = per_slot.get((ue.id, k))
                if u is None or slot is None or not slot["cell_id"]:
                    continue
                per_ue_rows.append({
                    "imsi": imsi_of[(ue.id, k)],
                    "cell": cell_index.get(slot["cell_id"]),
                    "sinr": round(slot["sinr"], 1),
                    "rsrp": round(slot["rsrp"], 1),
                    "thp_kbps": round(u.volume_bytes * 8 / 1e3 / window, 1),
                    "ul_kbps": (round(u.volume_ul_bytes * 8 / 1e3 / window, 1)
                                if u.has_ul else None),
                    "sinr_ul": round(u.sinr_ul_db, 1) if u.has_ul else None,
                    "prb": u.prb_used,
                    "mcs": u.mcs,
                    "bler_pct": round(u.bler * 100, 1),
                    "queued_kb": round(u.queued_bytes / 1e3, 1),
                    "slice": slice_name,
                    # Spread inside the group, so six UEs are six dots and
                    # not one. The tier already places them apart; this is
                    # that placement expressed where the map can use it.
                    "x": round(gx + spread[k % len(spread)][0], 1),
                    "y": round(gy + spread[k % len(spread)][1], 1),
                })

        cell_rows = []
        for bs in self.cfg.base_stations:
            faulted = bs.id in self.faulted_cells
            active = bs.id not in self._offline_cells
            power_dbm = self._effective_power_dbm(bs.id)
            # A cell with no UEs never reached `cell_window`, so it has no
            # row above — it still draws, and if it is allowed to sleep that
            # is exactly the cell where sleeping pays.
            power_w = round(cell_watts.get(
                bs.id, 0.0 if not active else window_model.cell_power_w(
                    window_model.CellWindow(cell=0), window_model.RuPower.of(
                        params, order.index(bs.id) if bs.id in order else 0),
                    window, off=not active)), 1)
            cell_rows.append({
                "id": bs.id, "name": bs.name, "active": active, "faulted": faulted,
                "tx_power_dbm": round(power_dbm, 1), "power_w": power_w,
                # Occupancy of the cell's OFDM symbols, which is what the
                # scheduler actually rations — not a demand-over-supply ratio
                # that could read past 100%.
                "prb_used_pct": round(min(100.0, cell_symbols.get(bs.id, 0.0) * 100), 1),
                "throughput_mbps": round(cell_tput[bs.id], 2),
                "connected_ues": round(cell_ues[bs.id]),
            })

        slice_rows = []
        for s in ("eMBB", "uRLLC", "mMTC", "voice"):
            act = slice_active[s]
            slice_rows.append({
                "slice": s,
                "throughput_mbps": round(slice_tput[s], 2),
                "prb_pct": round(self.slice_prb_quota.get(s, 100.0), 1),
                "qos_score": round((slice_sat_num[s] / act * 100) if act > 0 else 0.0, 1),
            })

        # The counters a real E2SM-KPM subscription asks for, under the names
        # the engine reports them by, aggregated the way its bridge aggregates
        # them. Additive: every key this report carried before is unchanged.
        # PEE is per CELL, so it is folded in from the rows rather than from
        # the per-UE counters — including the cells with no UEs, which are
        # the ones an energy-saving xApp is looking for.
        for c in cell_rows:
            samples.setdefault("PEE.AvgPower", []).append(c["power_w"])
            samples.setdefault("PEE.Energy", []).append(
                round(c["power_w"] * window, 1))
        measurements = {
            name: {"count": len(v), "min": round(min(v), 2), "max": round(max(v), 2),
                   "mean": round(sum(v) / len(v), 2)}
            for name, v in sorted(samples.items())
        }

        return {
            "tick": self.tick_count,
            "cells": cell_rows,
            "ue_groups": ue_rows,
            # Per UE, same shape the slow tier publishes — see where it is
            # built for why the group fold is not enough.
            "ues": per_ue_rows,
            "slices": slice_rows,
            "measurements": measurements,
            # SIMULATED seconds covered, under the name the slow tier uses.
            # This tier's clock is exactly its windows — one per
            # `window_s` — where ns-3's is whatever it got through, so the
            # two are not the same guarantee. They ARE the same quantity,
            # and a dashboard that leads with time on one tier and not the
            # other cannot be read across them.
            "sim_t": round(self.tick_count * self.window_s, 2),
            "handovers": self.handovers,
            "handovers_window": self.handovers_last,
            "control_state": {
                "disabled_cells": sorted(self.disabled_cells),
                "power_override": self.power_override,
                "forced_serving": self.forced_serving,
                "slice_prb_quota": self.slice_prb_quota,
            },
            "anomalies": {
                "faulted_cells": sorted(self.faulted_cells),
                "demand_multiplier": round(self.demand_multiplier, 2),
            },
        }
