#!/usr/bin/env python3
"""Which of the ANALYTICAL-tier fields does anything still read?

`schema.TIER_SCOPED` marks fields the real engine ignores. The label was
written when there WAS an analytical tier — the Shannon sandbox behind the
RIC console — and that tier is gone: the console runs the fast tier now and
the coverage map is the only analytical thing left. So every one of these
fields is in one of three states and the badge says none of them:

    fast      the FAST TIER reads it — retag, it is not analytical-only
    coverage  only the REM coverage map reads it — the badge is still right
              but should say what reads it
    inert     nothing reads it — the field should go

THIS IS MEASURED, NOT READ. `param_audit` perturbs a field and diffs the
ENGINE PAYLOAD; this perturbs it and diffs what the FAST TIER PRODUCES,
which is a different question and the one that decides the badge. A field
whose value reaches a dict nobody consumes is inert however well-wired it
looks.

    python3 tools/tier_scoped_audit.py
"""
from __future__ import annotations

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "nsg_ai"))
sys.path.insert(0, HERE)

GREEN, YELLOW, RED, DIM, RESET = (
    "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m")

# (dotted path, a value that must MOVE the model if the field is read).
# Enums are given as ENUM MEMBERS, not their strings: pydantic rejects the
# string on assignment and the probe reports an error where the field may
# be perfectly live. Getting that wrong deletes a working field.
def _enums():
    from schema import AntennaType, BeamformingMode, MobilityPattern, NetworkSlice
    return {
        "ue_groups[].mobility.pattern": MobilityPattern.STATIC,
        "ue_groups[].services[].slice_type": NetworkSlice.URLLC,
        "base_stations[].radio.antenna.type": AntennaType.MASSIVE_MIMO,
        "base_stations[].radio.antenna.massive_mimo.beamforming":
            BeamformingMode.ANALOG,
    }


PERTURB = {
    "settings.fast_fading_enabled": False,
    "base_stations[].radio.antenna.gain_dbi": 25.0,
    "base_stations[].radio.antenna.horizontal_beamwidth_deg": 15.0,
    "base_stations[].radio.antenna.vertical_beamwidth_deg": 15.0,
    "base_stations[].radio.antenna.massive_mimo.digital_azimuth_deg": 75.0,
    "base_stations[].radio.antenna.massive_mimo.digital_tilt_deg": 40.0,
    "area.x_min": -4000.0,
    "area.x_max": 4000.0,
    "area.y_min": -4000.0,
    "area.y_max": 4000.0,
    "area.resolution_m": 40.0,
}


def _set(cfg, path: str, value) -> bool:
    """Set a dotted path, walking `[]` as every element. False if absent."""
    parts = path.split(".")
    targets = [cfg]
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        name, many = (part[:-2], True) if part.endswith("[]") else (part, False)
        nxt = []
        for t in targets:
            if not hasattr(t, name):
                return False
            if last:
                try:
                    setattr(t, name, value)
                except Exception:
                    return False
            else:
                got = getattr(t, name)
                nxt.extend(got if many else [got])
        targets = nxt
        if not last and not targets:
            return False
    return True


def fingerprint(cfg) -> str:
    """What the FAST TIER produces for this config, as a stable string.

    The console's report AND the batch tier's frames, because the two are
    separate consumers and a field can reach one and not the other — which
    is the drift this repo keeps finding.
    """
    import tier_b
    from main import nsoran_params
    from ric_sim import RicSimulation

    sim = RicSimulation(cfg, window_s=1.0)
    rep = None
    for _ in range(3):
        rep = sim.get_kpm_report()
        sim.tick()
    live = {
        "cells": [(c["id"], c["throughput_mbps"], c["prb_used_pct"], c["power_w"])
                  for c in rep["cells"]],
        "ues": [(u["imsi"], u["cell"], u["sinr"], u["rsrp"], u["thp_kbps"],
                 u.get("ul_kbps"), u["mcs"], u["slice"]) for u in rep["ues"]],
        "slices": [(s["slice"], s["throughput_mbps"], s["qos_score"])
                   for s in rep["slices"]],
    }
    params = nsoran_params(cfg, engine_clamped=False)
    batch = [f["measurements"] for f in tier_b.frames(params, 3)]
    return json.dumps({"live": live, "batch": batch}, sort_keys=True, default=str)


def coverage_fingerprint(cfg) -> str:
    """What the REM coverage map produces — the one analytical thing left."""
    try:
        from ns3_bridge import rsrp_heatmap
        pts = rsrp_heatmap(cfg)
        # THE WHOLE GRID, not its first points. Sampling a prefix made
        # `area.y_max` look inert while `y_min` moved — the first 400
        # points of a row-major grid never reach the far edge, so the
        # probe was measuring where it looked rather than what changed.
        # The COUNT matters too: extent and resolution change it.
        return json.dumps({
            "n": len(pts),
            "pts": [(round(p["x"], 1), round(p["y"], 1), round(p["rsrp"], 2))
                    for p in pts[::max(1, len(pts) // 400)]],
        }, sort_keys=True)
    except Exception as exc:                       # noqa: BLE001
        return f"error:{type(exc).__name__}:{exc}"


# ── EVERYTHING THE SIM OPTIONS TAB EDITS ────────────────────────────────────
# Not in TIER_SCOPED, so none of it carries a badge — and the question
# "which of these is slow-tier only?" had no answer anywhere. Same probe,
# same two consumers, so the tab can say which tier each control belongs to
# instead of leaving the reader to find out from a run.
SIM_OPTIONS = {
    "settings.duration_s": 999.0,
    "settings.report_interval_ms": 200.0,
    "settings.kpm_collect_s": 6.0,
    "settings.shadowing_enabled": False,
    "settings.random_seed": 4242,
    "area.resolution_m": 40.0,
    # `scenario` gets its own entry in _enums()
}


def main() -> int:
    from schema import PropagationScenario, TIER_SCOPED
    PERTURB.update(_enums())
    PERTURB.update(SIM_OPTIONS)
    PERTURB["settings.scenario"] = PropagationScenario.UMI
    import tier_criterion as tc
    _n, base, _p = tc.load_scenario("Partition test")

    def prepared(path: str):
        """A config where this field EXISTS, so the probe has something to
        move. The reference scenario's antennas carry `massive_mimo = None`
        and a cosine element, so the MIMO paths cannot be set on it at all —
        and a probe that cannot set a field reports nothing, which reads
        exactly like a field nothing uses. Prepare BOTH sides identically
        and the comparison stays about the one field under test."""
        from schema import AntennaType, MassiveMIMOConfig
        c = copy.deepcopy(base)
        if "massive_mimo" in path:
            for b in c.base_stations:
                b.radio.antenna.type = AntennaType.MASSIVE_MIMO
                if b.radio.antenna.massive_mimo is None:
                    b.radio.antenna.massive_mimo = MassiveMIMOConfig()
        return c

    print(f"reference: {_n}\n")
    print(f"{'field':52}{'fast tier':>11}{'coverage':>11}  verdict")
    verdicts: dict[str, str] = {}
    fields = list(TIER_SCOPED) + ["settings.scenario"] + list(SIM_OPTIONS)
    for path in fields:
        if path == "area.resolution_m" and path in TIER_SCOPED:
            pass  # listed once
        if path not in PERTURB:
            print(f"{path:52}{'no probe':>11}")
            continue
        if path in verdicts:
            continue
        ref = prepared(path)
        cfg = copy.deepcopy(ref)
        if not _set(cfg, path, PERTURB[path]):
            print(f"{path:52}{RED}{'not set':>11}{RESET}")
            continue
        try:
            moved_fast = fingerprint(cfg) != fingerprint(ref)
            moved_cov = coverage_fingerprint(cfg) != coverage_fingerprint(ref)
        except Exception as exc:                   # noqa: BLE001
            print(f"{path:52}{RED}{'error':>11}{RESET}  {type(exc).__name__}: {exc}")
            continue
        if moved_fast:
            v, col = "FAST TIER reads it — retag", GREEN
        elif moved_cov:
            v, col = "coverage map only — badge is right", YELLOW
        else:
            v, col = "INERT — nothing reads it", RED
        verdicts[path] = v
        print(f"{path:52}{('yes' if moved_fast else 'no'):>11}"
              f"{('yes' if moved_cov else 'no'):>11}  {col}{v}{RESET}")

    print()
    for kind in ("FAST TIER reads it — retag", "coverage map only — badge is right",
                 "INERT — nothing reads it"):
        n = sum(1 for v in verdicts.values() if v == kind)
        print(f"  {n:2} {kind}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
