"""
The scenario under test — built through the platform's OWN validator.

Rather than hand-writing 165 fields (and getting one of them subtly wrong), the
builder posts a minimal spec to POST /config/normalize and lets the platform
fill every default. That means the experiment can never run a config the product
would reject, and it inherits schema changes for free.

Geometry follows the measured engine envelope, not taste:
  · 3 cells      — 2-cell scenarios return no DU metrics at all
  · 3 UEs/cell   — the 2-4 band where the engine reports cleanly
  · UEs never on top of a base station — that returns zero metrics silently
  · static UEs   — the conflict under study is about power, and mobility would
                   add handover churn to every arm equally without informing it

The load is deliberately moderate: enough that the cells report real PRB usage,
low enough that an energy-saving xApp has something to give away.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

API = "http://localhost:8000"

# Spread over ~700 m so cell edges genuinely overlap: without overlap there is
# no coverage for a power cut to damage, and the conflict is not a conflict.
CELL_SITES = [
    ("c1", "Cell-1", -350.0, -200.0),
    ("c2", "Cell-2", 350.0, -200.0),
    ("c3", "Cell-3", 0.0, 350.0),
]

# Two clusters sit well inside a cell; the third sits out at the edge, which is
# what makes eSave and maxTP disagree about the SAME cell — low PRB there says
# "turn the power down", poor SINR there says "turn it up".
UE_CLUSTERS = [
    ("g1", "near-c1", -300.0, -60.0),
    ("g2", "near-c2", 300.0, -60.0),
    ("g3", "edge-c3", 60.0, 760.0),
]

# 36 dBm, not the 43 dBm the engine defaults to. At 43 the actuator is lopsided:
# maxTP has 1.5 steps of headroom to the 46 dBm ceiling while eSave has 11.5 to
# the floor, so any oscillation between them is really one xApp being clipped
# rather than two xApps disagreeing — and P3 would be averaging a clipped request
# against an unclipped one, which biases the synthesis arm that the study exists
# to evaluate. 36 dBm leaves ~10 dB up and ~16 dB down: both controllers can act
# several times without meeting a rail. It is also a realistic small-macro power
# for n78, so the choice costs no plausibility.
BASE_TX_DBM = 36.0


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{API}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit(
            f"{path} rejected the spec ({e.code}):\n{e.read().decode()[:800]}") from None


def build(
    seed: int,
    *,
    ues_per_cluster: int = 3,
    demand_mbps: float = 20.0,
    tx_dbm: float = BASE_TX_DBM,
    settings: dict | None = None,
    spread: float = 1.0,
) -> dict:
    """Return a fully-normalised config for one run of the conflict experiment."""
    spec = {
        "name": f"conflict-s{seed}",
        "description": "Energy vs capacity xApp conflict study",
        "base_stations": [
            {
                "id": cid, "name": name,
                # `spread` pushes the sites apart without moving the UE clusters
                # off their serving cells. At spread=1.0 the cells overlap so
                # heavily that the deployment is interference-limited: switching
                # a cell OFF improved energy AND throughput in 3/3 seeds, so
                # there was no energy-capacity tension left to study. Spreading
                # them is what makes a cell genuinely load-bearing.
                "position": {"x": x * spread, "y": y * spread, "z": 25.0},
                "radio": {
                    "tx_power_dbm": tx_dbm,
                    "band": {"label": "n78", "frequency_mhz": 3500,
                             "bandwidth_mhz": 50, "numerology": 2},
                },
            }
            for cid, name, x, y in CELL_SITES
        ],
        "ue_groups": [
            {
                "id": gid, "name": name, "count": ues_per_cluster,
                "spawn_position": {"x": x * spread, "y": y * spread},
                "mobility": {"pattern": "static", "speed_mps": 0.0},
                "services": [{"slice_type": "eMBB", "qos_id": 9,
                              "target_throughput_gbps": demand_mbps / 1000.0}],
            }
            for gid, name, x, y in UE_CLUSTERS
        ],
        # The REM box has to grow with the deployment. It does NOT come along for
        # free: at spread=1.8 the edge cluster lands at y=1368 while the default
        # area stops at y=1000, and /config/normalize accepts that without
        # complaint — base stations outside the area are rejected, UE spawn
        # positions are not. That is a silent-garbage input, so the box is scaled
        # here rather than trusted to a default.
        "area": {"x_min": -1000.0 * spread, "x_max": 1000.0 * spread,
                 "y_min": -1000.0 * spread, "y_max": 1000.0 * spread},
        # random_seed is the only knob the arms vary besides the controller, and
        # it reaches ns-3 as RngRun.
        "settings": {"random_seed": seed, **(settings or {})},
    }
    return _post("/config/normalize", spec)["config"]


if __name__ == "__main__":
    cfg = build(seed=1)
    bs = cfg["base_stations"]
    ue = cfg["ue_groups"]
    print(f"name          {cfg['name']}")
    print(f"seed          {cfg['settings'].get('random_seed')}")
    print(f"cells         {len(bs)} @ "
          + ", ".join(f"{b['name']}({b['position']['x']:.0f},{b['position']['y']:.0f})"
                      f" {b['radio']['tx_power_dbm']:.0f}dBm" for b in bs))
    print(f"UEs           {sum(g['count'] for g in ue)} in {len(ue)} groups")
    print(f"area          x[{cfg['area']['x_min']:.0f},{cfg['area']['x_max']:.0f}] "
          f"y[{cfg['area']['y_min']:.0f},{cfg['area']['y_max']:.0f}]")
    print(f"fields filled {len(json.dumps(cfg))} bytes of normalised config")
