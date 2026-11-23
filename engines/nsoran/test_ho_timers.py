#!/usr/bin/env python3
"""Handover robustness timers reach ns-3 without aborting the run.

The trap this guards: ns-3 rejects an attribute value BEFORE the simulation
starts, which surfaces as the engine returning no_data with no scenario log —
indistinguishable from a dead engine unless you read /tmp/scn0.log. So the
assertion is not "the number changed something" (a static 20-tick run produces
no handovers to time out) but "ns-3 accepted it and the run completed".
"""
import sys

sys.path.insert(0, "/home/chancelor/nsg_ai_platform")
sys.path.insert(0, "/home/chancelor/nsg_ai_platform/nsg_ai")
sys.path.insert(0, "/home/chancelor/nsg_ai_platform/engines/nsoran")

from nsg_ai.main import nsoran_params            # noqa: E402
from nsg_ai.schema import NSGSimulationConfig    # noqa: E402
from nsg_test_util import Checker, run, thp      # noqa: E402

check = Checker()


def cfg(**settings) -> dict:
    return {
        "name": "ho-timers",
        "base_stations": [
            {"id": f"c{i}", "name": f"C{i}",
             "position": {"x": x, "y": 0.0, "z": 25.0},
             "radio": {"band": {"label": "b", "frequency_mhz": 3500,
                                "bandwidth_mhz": 50, "numerology": 2},
                       "tx_power_dbm": 44.0}}
            for i, x in enumerate([-350.0, 0.0, 350.0], start=1)],
        "ue_groups": [
            {"id": f"u{i}", "name": f"U{i}", "count": 3,
             "spawn_position": {"x": x, "y": 0.0},
             "services": [{"slice_type": "eMBB", "qos_id": 9,
                           "target_throughput_gbps": 0.04}]}
            for i, x in enumerate([-350.0, 0.0, 350.0], start=1)],
        "area": {"x_min": -900.0, "x_max": 900.0, "y_min": -900.0,
                 "y_max": 900.0, "resolution_m": 40.0},
        "settings": dict(settings),
    }


print("=== translation ===")
p = nsoran_params(NSGSimulationConfig(**cfg()))
check(p["ho_join_ms"] == 45.0 and p["ho_leave_ms"] == 45.0,
      f"defaults match the engine's own 45 ms: {p['ho_join_ms']}/{p['ho_leave_ms']}")

p = nsoran_params(NSGSimulationConfig(**cfg(ho_joining_timeout_ms=12.5,
                                            ho_leaving_timeout_ms=300.0)))
check(p["ho_join_ms"] == 12.5 and p["ho_leave_ms"] == 300.0,
      f"both timers reach the engine payload: {p['ho_join_ms']}/{p['ho_leave_ms']}")

if "--engine" in sys.argv:
    print("\n=== engine ===")
    # A short joining window is the interesting direction: it is what an MRO
    # test tightens until handovers start failing.
    d = run(nsoran_params(NSGSimulationConfig(**cfg(
        ho_joining_timeout_ms=5.0, ho_leaving_timeout_ms=5.0))))
    check(thp(d) > 0,
          f"ns-3 accepts 5 ms timers and the run completes: "
          f"{thp(d) / 1000:.1f} Mbps")

    d = run(nsoran_params(NSGSimulationConfig(**cfg(
        ho_joining_timeout_ms=2000.0, ho_leaving_timeout_ms=2000.0))))
    check(thp(d) > 0,
          f"ns-3 accepts 2 s timers and the run completes: "
          f"{thp(d) / 1000:.1f} Mbps")
else:
    print("\n(skipping engine runs; pass --engine to include them)")

sys.exit(check.report())
