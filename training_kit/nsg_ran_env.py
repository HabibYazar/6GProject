"""
NSG RAN — Gymnasium training environment

Wraps the NSG RIC simulation as a standard Gymnasium environment so you can train
a reinforcement-learning agent to control the network, THEN deploy the trained
model against the hosted RIC test (via the downloadable xApp connector).

This runs entirely on YOUR machine — training does millions of fast local steps,
nothing touches the NSG servers.

  observation : fixed-length float vector describing the current network state
                (per-cell load/power, per-slice QoS, global aggregates)
  action      : one discrete control action (no-op / toggle a cell / hand a UE
                group to its next-best cell / cap a slice's PRBs)
  reward      : goal-weighted — high QoS, low energy, no outages (tune the
                weights in the constructor for your objective)

Quick start:
    env = NsgRanEnv()                 # default urban scenario
    obs, _ = env.reset()
    obs, reward, terminated, truncated, info = env.step(env.action_space.sample())

Train with any RL library (Stable-Baselines3, RLlib, CleanRL…). See train_example.py.
"""
from __future__ import annotations

import numpy as np
import gymnasium as gym
from gymnasium import spaces

from ric_sim import RicSimulation
from schema import NSGSimulationConfig

_SLICES = ("eMBB", "uRLLC", "mMTC", "voice")


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


class NsgRanEnv(gym.Env):
    """Gymnasium env for RAN control. Built for one scenario (its cell/UE/slice
    counts fix the observation and action spaces)."""

    metadata = {"render_modes": []}

    def __init__(
        self,
        config: dict | NSGSimulationConfig | None = None,
        episode_steps: int = 50,
        reward_weights: dict | None = None,
    ):
        super().__init__()
        if config is None:
            config = _default_scenario()
        self.base_cfg = (
            config if isinstance(config, NSGSimulationConfig)
            else NSGSimulationConfig.model_validate(config)
        )
        self.episode_steps = episode_steps
        self.w = {"qos": 1.0, "energy": 0.3, "outage": 1.0, **(reward_weights or {})}

        self._cell_ids = [bs.id for bs in self.base_cfg.base_stations]
        self._ue_ids   = [ue.id for ue in self.base_cfg.ue_groups]

        # ── Action set (index → concrete control) ─────────────────────────────
        self._actions: list[tuple | None] = [None]                       # 0 = no-op
        for c in self._cell_ids:
            self._actions.append(("toggle_cell", c))                     # sleep/wake a cell
        for u in self._ue_ids:
            self._actions.append(("handover_next", u))                   # steer a UE group
        for s in _SLICES:
            self._actions.append(("cap_slice", s, 50))                   # throttle a slice
            self._actions.append(("cap_slice", s, 100))                  # restore a slice
        self.action_space = spaces.Discrete(len(self._actions))

        # ── Observation vector: 4 global + 4/cell + 3/slice ──────────────────
        obs_dim = 4 + 4 * len(self._cell_ids) + 3 * len(_SLICES)
        self.observation_space = spaces.Box(
            low=0.0, high=1.0, shape=(obs_dim,), dtype=np.float32
        )

        # Total configured power, for the energy term.
        self._total_power_w = sum(
            10 ** (bs.radio.tx_power_dbm / 10) / 1000.0 for bs in self.base_cfg.base_stations
        ) or 1.0

        self.sim: RicSimulation | None = None
        self._steps = 0

    # ── Gym API ────────────────────────────────────────────────────────────────

    def reset(self, *, seed: int | None = None, options: dict | None = None):
        super().reset(seed=seed)
        self.sim = RicSimulation(self.base_cfg)
        self._steps = 0
        kpm = self.sim.get_kpm_report()
        return self._encode(kpm), {}

    def step(self, action: int):
        assert self.sim is not None, "call reset() first"
        cmd = self._action_to_cmd(int(action))
        if cmd is not None:
            self.sim.apply_control(cmd)
        self.sim.tick()
        self._steps += 1

        kpm = self.sim.get_kpm_report()
        obs = self._encode(kpm)
        reward = self._reward(kpm)
        truncated = self._steps >= self.episode_steps
        return obs, reward, False, truncated, {"kpm": kpm}

    # ── action decoding ─────────────────────────────────────────────────────────

    def _action_to_cmd(self, action: int) -> dict | None:
        spec = self._actions[action]
        if spec is None:
            return None
        kind = spec[0]
        if kind == "toggle_cell":
            cell = spec[1]
            active = cell not in self.sim.disabled_cells
            return {"action": "cell_power", "cell": cell, "state": "off" if active else "on"}
        if kind == "handover_next":
            ue = spec[1]
            ranked = self.sim.ranked_cells_for(ue)
            current = self.sim.forced_serving.get(ue)
            # target = strongest cell that isn't the current forced one, else 2nd.
            target = next((c for c in ranked if c != current), ranked[0] if ranked else None)
            if target is None:
                return None
            return {"action": "handover", "ue_group": ue, "target_cell": target}
        if kind == "cap_slice":
            return {"action": "slice_quota", "slice": spec[1], "prb_percent": spec[2]}
        return None

    # ── observation + reward ──────────────────────────────────────────────────

    def _encode(self, kpm: dict) -> np.ndarray:
        by_slice = {s["slice"]: s for s in kpm["slices"]}
        obs: list[float] = []

        total_ues = sum(c["connected_ues"] for c in kpm["cells"])
        served_qos = [s["qos_score"] for s in kpm["slices"] if s["throughput_mbps"] > 0]
        obs += [
            min(1.0, total_ues / 500.0),
            _mean(served_qos) / 100.0,
            _mean([c["prb_used_pct"] for c in kpm["cells"]]) / 100.0,
            sum(c["power_w"] for c in kpm["cells"] if c["active"]) / (self._total_power_w * 1000 + 1e-9),
        ]
        for c in kpm["cells"]:
            obs += [
                1.0 if c["active"] else 0.0,
                c["prb_used_pct"] / 100.0,
                min(1.0, c["connected_ues"] / 200.0),
                min(1.0, c["power_w"] / 100.0),
            ]
        for s in _SLICES:
            sl = by_slice.get(s, {})
            obs += [
                sl.get("qos_score", 0.0) / 100.0,
                min(1.0, sl.get("throughput_mbps", 0.0) / 3000.0),
                sl.get("prb_pct", 100.0) / 100.0,
            ]
        return np.asarray(obs, dtype=np.float32)

    def _reward(self, kpm: dict) -> float:
        served_qos = [s["qos_score"] for s in kpm["slices"] if s["throughput_mbps"] > 0]
        mean_qos = _mean(served_qos) / 100.0
        active_power = sum(10 ** (bs.radio.tx_power_dbm / 10) / 1000.0
                           for bs in self.base_cfg.base_stations
                           if bs.id not in self.sim.disabled_cells)
        energy_frac = active_power / self._total_power_w
        outages = sum(1 for u in kpm["ue_groups"] if u["serving_cell"] is None)
        return (
            self.w["qos"] * mean_qos
            - self.w["energy"] * energy_frac
            - self.w["outage"] * outages
        )


def _default_scenario() -> NSGSimulationConfig:
    """A small urban scenario to train against out of the box."""
    return NSGSimulationConfig.model_validate({
        "name": "Training — Urban", "description": "3 cells, 3 UE groups, mixed slices.",
        "base_stations": [
            {"id": "cell-A", "name": "Cell-A", "position": {"x": 0, "y": 0, "z": 30},
             "radio": {"band": {"label": "N77", "frequency_mhz": 3600, "bandwidth_mhz": 100, "numerology": 1}, "tx_power_dbm": 46,
                       "antenna": {"type": "isotropic", "azimuth_deg": 0, "tilt_deg": 0, "gain_dbi": 15, "horizontal_beamwidth_deg": 360, "vertical_beamwidth_deg": 90}}},
            {"id": "cell-B", "name": "Cell-B", "position": {"x": 250, "y": 0, "z": 30},
             "radio": {"band": {"label": "N77", "frequency_mhz": 3600, "bandwidth_mhz": 100, "numerology": 1}, "tx_power_dbm": 46,
                       "antenna": {"type": "isotropic", "azimuth_deg": 0, "tilt_deg": 0, "gain_dbi": 15, "horizontal_beamwidth_deg": 360, "vertical_beamwidth_deg": 90}}},
            {"id": "cell-C", "name": "Cell-C", "position": {"x": 125, "y": 220, "z": 30},
             "radio": {"band": {"label": "N77", "frequency_mhz": 3600, "bandwidth_mhz": 100, "numerology": 1}, "tx_power_dbm": 46,
                       "antenna": {"type": "isotropic", "azimuth_deg": 0, "tilt_deg": 0, "gain_dbi": 15, "horizontal_beamwidth_deg": 360, "vertical_beamwidth_deg": 90}}},
        ],
        "ue_groups": [
            {"id": "embb", "name": "eMBB", "count": 60, "spawn_position": {"x": 40, "y": 20},
             "mobility": {"pattern": "pedestrian", "speed_mps": 1.4},
             "services": [{"slice_type": "eMBB", "qos_id": 8, "target_throughput_gbps": 0.05}]},
            {"id": "urllc", "name": "uRLLC", "count": 25, "spawn_position": {"x": 230, "y": 20},
             "mobility": {"pattern": "static", "speed_mps": 0},
             "services": [{"slice_type": "uRLLC", "qos_id": 1, "target_throughput_gbps": 0.02}]},
            {"id": "mmtc", "name": "mMTC", "count": 120, "spawn_position": {"x": 125, "y": 200},
             "mobility": {"pattern": "static", "speed_mps": 0},
             "services": [{"slice_type": "mMTC", "qos_id": 9, "target_throughput_gbps": 0.001}]},
        ],
        "buildings": [],
        "area": {"x_min": -150, "x_max": 400, "y_min": -150, "y_max": 400, "resolution_m": 40},
        "settings": {"duration_s": 60, "report_interval_ms": 1000, "scenario": "UMa",
                     "shadowing_enabled": True, "fast_fading_enabled": False, "random_seed": 42},
    })
