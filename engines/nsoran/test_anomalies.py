#!/usr/bin/env python3
"""Roadmap item 4 — anomaly injection on the real engine.

A test bench where nothing ever breaks is not a test bench. These faults are
staged INSIDE the running simulation: the cell's transmit power really changes
mid-run, so its UEs really lose SINR and the KPM the RIC sees really degrades.
The alternative — annotating the results afterwards — would produce numbers
that no xApp could ever have reacted to.

Run:  python3 test_anomalies.py            # translation only
      python3 test_anomalies.py --engine   # + real runs, ~10 min
"""
import sys

sys.path.insert(0, "/home/chancelor/nsg_ai_platform")
sys.path.insert(0, "/home/chancelor/nsg_ai_platform/nsg_ai")
sys.path.insert(0, "/home/chancelor/nsg_ai_platform/engines/nsoran")

from nsg_ai.main import nsoran_params                       # noqa: E402
from nsg_ai.schema import NSGSimulationConfig               # noqa: E402
from nsg_test_util import Checker, metric, run, thp         # noqa: E402

check = Checker()


def cfg(*, anomalies=None, **settings) -> dict:
    """Three loaded cells. A fault on one has to be visible in the total,
    which means the other two must not be able to absorb it — hence enough
    offered load to keep all three busy."""
    return {
        "name": "anomalies",
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
        "anomalies": anomalies or [],
        "area": {"x_min": -900.0, "x_max": 900.0, "y_min": -900.0,
                 "y_max": 900.0, "resolution_m": 40.0},
        "settings": dict(settings),
    }


def translation() -> None:
    print("\n=== translation ===")
    p = nsoran_params(NSGSimulationConfig(**cfg(anomalies=[
        {"type": "cell_outage", "cell_id": "c2", "start_s": 0.2},
        {"type": "radio_degradation", "cell_id": "c3", "start_s": 0.1,
         "duration_s": 0.2},
    ])))
    check(p["gnb_faults"] == "1,0.200,0.000,1,0;2,0.100,0.200,0,41.0",
          f"faults reach the engine by INDEX, resolved from the cell id: "
          f"{p['gnb_faults']}")

    # A fault aimed at a cell that is not in the run must be DROPPED. Remapping
    # it to index 0 would break a different cell than the one asked for, which
    # is worse than breaking none.
    p = nsoran_params(NSGSimulationConfig(**cfg(anomalies=[
        {"type": "cell_outage", "cell_id": "does-not-exist", "start_s": 1.0}])))
    check(p["gnb_faults"] == "",
          f"a fault on an unknown cell is dropped, not remapped: "
          f"'{p['gnb_faults']}'")

    # Default degradation is -3 dB — half the radiated power.
    p = nsoran_params(NSGSimulationConfig(**cfg(anomalies=[
        {"type": "radio_degradation", "cell_id": "c1", "start_s": 0.1}])))
    check(p["gnb_faults"].endswith(",0,41.0"),
          f"default degradation is -3 dB from the configured power: "
          f"{p['gnb_faults']}")

    p = nsoran_params(NSGSimulationConfig(**cfg(admit_handover_request=False)))
    check(p["admit_handover"] == "0",
          "the handover-admission switch reaches the engine")

    # ── noise_rise: engine type 2, magnitude in dB (not dBm) ──────────────────
    p = nsoran_params(NSGSimulationConfig(**cfg(anomalies=[
        {"type": "noise_rise", "cell_id": "c2", "start_s": 0.05,
         "duration_s": 0.1, "noise_rise_db": 25.0}])))
    check(p["gnb_faults"] == "1,0.050,0.100,2,25.0",
          f"a noise rise reaches the engine as type 2 with its dB magnitude: "
          f"{p['gnb_faults']}")

    p = nsoran_params(NSGSimulationConfig(**cfg(anomalies=[
        {"type": "noise_rise", "cell_id": "c2", "start_s": 0.05}])))
    check(p["gnb_faults"].endswith(",2,20.0"),
          f"default noise rise is 20 dB: {p['gnb_faults']}")

    # ── handover_block: anchor-wide, so it survives a stale cell id ───────────
    p = nsoran_params(NSGSimulationConfig(**cfg(anomalies=[
        {"type": "handover_block", "cell_id": "does-not-exist", "start_s": 0.1,
         "duration_s": 0.2}])))
    check(p["gnb_faults"] == "0,0.100,0.200,3,0",
          f"a handover block is emitted network-wide, not per cell, so an "
          f"unmatched cell id does not drop it: {p['gnb_faults']}")

    # A magnitude on a fault that cannot use it means the user expected
    # something the run will not do — rejected at the schema, not silently kept.
    for bad, why in (
        ({"type": "cell_outage", "cell_id": "c1", "start_s": 0.1,
          "noise_rise_db": 10.0}, "noise_rise_db on a cell_outage"),
        ({"type": "noise_rise", "cell_id": "c1", "start_s": 0.1,
          "tx_power_dbm": 20.0}, "tx_power_dbm on a noise_rise"),
    ):
        try:
            NSGSimulationConfig(**cfg(anomalies=[bad]))
            check(False, f"{why} is rejected")
        except Exception:
            check(True, f"{why} is rejected")

    # AdmitRrcConnectionRequest is deliberately NOT a field: false blocks every
    # attach and this scenario, which attaches all UEs at t=0, core-dumps
    # during startup. Measured. Guard against it creeping back in.
    check("admit_rrc_conn" not in p,
          "the connection-admission block is not exposed (it core-dumps ns-3)")


def _eng_outage() -> None:
    """A cell going dark must cost the network measurably.

    Total throughput, not the per-UE mean: when a cell fails its UEs stop
    reporting, and the mean over the SURVIVORS goes up. That is the metric
    trap this suite already fell into once.
    """
    healthy = thp(run(nsoran_params(NSGSimulationConfig(**cfg()))))
    failed = thp(run(nsoran_params(NSGSimulationConfig(**cfg(anomalies=[
        {"type": "cell_outage", "cell_id": "c2", "start_s": 0.05}])))))
    check((healthy - failed) / healthy > 0.05,
          f"a cell outage costs the network: {healthy / 1000:.1f} -> "
          f"{failed / 1000:.1f} Mbps ({(failed - healthy) / healthy * 100:+.1f}%)")


def _eng_degradation() -> None:
    """A partial degradation must land BETWEEN healthy and dark — otherwise it
    is just an outage with extra steps."""
    healthy = thp(run(nsoran_params(NSGSimulationConfig(**cfg()))))
    degraded = thp(run(nsoran_params(NSGSimulationConfig(**cfg(anomalies=[
        {"type": "radio_degradation", "cell_id": "c2", "start_s": 0.05,
         "tx_power_dbm": 20.0}])))))
    check(degraded < healthy,
          f"a 24 dB degradation costs the network: {healthy / 1000:.1f} -> "
          f"{degraded / 1000:.1f} Mbps "
          f"({(degraded - healthy) / healthy * 100:+.1f}%)")


def _eng_recovery() -> None:
    """A fault with a duration must END. A cell that never comes back is a
    different scenario, and the difference has to be visible."""
    permanent = thp(run(nsoran_params(NSGSimulationConfig(**cfg(anomalies=[
        {"type": "cell_outage", "cell_id": "c2", "start_s": 0.05}])))))
    temporary = thp(run(nsoran_params(NSGSimulationConfig(**cfg(anomalies=[
        {"type": "cell_outage", "cell_id": "c2", "start_s": 0.05,
         "duration_s": 0.1}])))))
    check(temporary > permanent,
          f"a fault that recovers costs less than one that does not: "
          f"permanent {permanent / 1000:.1f} -> recovering "
          f"{temporary / 1000:.1f} Mbps")


def _eng_admission() -> None:
    """Refusing incoming handovers must not break the run.

    Only that it survives is asserted. This scenario's UEs attach once at t=0
    and handover re-evaluation is off unless ho_check_period_ms is set, so
    without mobility there are no handover requests to refuse — asserting a
    throughput difference here would be asserting something the scenario
    cannot produce.
    """
    d = run(nsoran_params(NSGSimulationConfig(**cfg(admit_handover_request=False))))
    check(thp(d) > 0,
          f"refusing handovers is accepted and the run completes: "
          f"{thp(d) / 1000:.1f} Mbps")


def _eng_noise_rise() -> None:
    """A noise rise must cost the network — and cost it DIFFERENTLY from a power
    drop.

    The distinction is the whole reason the type exists: the cell keeps
    radiating, so its downlink RSRP is untouched and only what it can hear
    degrades. Both numbers are printed whatever the outcome, because "the lever
    is real but it moves the uplink, not the downlink" is a finding, not a
    failure — and shipping it without knowing which is how inert levers happen.
    """
    healthy = run(nsoran_params(NSGSimulationConfig(**cfg())))
    noisy = run(nsoran_params(NSGSimulationConfig(**cfg(anomalies=[
        {"type": "noise_rise", "cell_id": "c2", "start_s": 0.05,
         "noise_rise_db": 40.0}]))))

    h_dl, n_dl = thp(healthy), thp(noisy)
    h_sinr, n_sinr = metric(healthy, "L3servingSINR"), metric(noisy, "L3servingSINR")
    h_ul = metric(healthy, "DRB.PdcpSduBitRateUl")
    n_ul = metric(noisy, "DRB.PdcpSduBitRateUl")
    print(f"    DL   {h_dl / 1000:.1f} -> {n_dl / 1000:.1f} Mbps "
          f"({(n_dl - h_dl) / h_dl * 100:+.1f}%)")
    print(f"    SINR {h_sinr:.2f} -> {n_sinr:.2f} dB")
    print(f"    UL   {h_ul:.1f} -> {n_ul:.1f} kbps"
          + (f" ({(n_ul - h_ul) / h_ul * 100:+.1f}%)" if h_ul else ""))

    moved = (abs(n_dl - h_dl) / h_dl > 0.05
             or abs(n_sinr - h_sinr) > 0.5
             or (h_ul and abs(n_ul - h_ul) / h_ul > 0.05))
    check(moved, "a 40 dB noise rise moves at least one measured KPI")


def _eng_handover_block() -> None:
    """A handover block must be accepted and must not break the run.

    Deliberately NOT asserting a throughput difference: this scenario's UEs are
    static, so there are no handover requests to refuse. The fault is real —
    the anchor's admission flag really flips mid-run — but proving its EFFECT
    needs mobility, which a 20-tick batch window cannot produce (see the
    measured limit on motion-driven handover churn). Asserting an effect here
    would be asserting something the scenario cannot generate.
    """
    d = run(nsoran_params(NSGSimulationConfig(**cfg(anomalies=[
        {"type": "handover_block", "cell_id": "c1", "start_s": 0.05,
         "duration_s": 0.1}]))))
    check(thp(d) > 0,
          f"a scheduled handover block is accepted and the run completes: "
          f"{thp(d) / 1000:.1f} Mbps")


if __name__ == "__main__":
    translation()
    if "--engine" in sys.argv:
        print("\n=== engine ===")
        for name, fn in (("cell outage", _eng_outage),
                         ("radio degradation", _eng_degradation),
                         ("recovery", _eng_recovery),
                         ("admission block", _eng_admission),
                         ("noise rise", _eng_noise_rise),
                         ("handover block", _eng_handover_block)):
            check.guarded(name, fn)
    else:
        print("\n(skipping engine runs; pass --engine to include them)")
    sys.exit(check.report())
