#!/usr/bin/env python3
"""Roadmap item 3 — the antenna element family, and the rest of LteEnbRrc.

Two things:

  1. Beamwidth becomes a real per-cell lever. The schema used to carry
     horizontal_beamwidth_deg / vertical_beamwidth_deg / gain_dbi labelled
     "FAST/ANALYTICAL TIER ONLY — the real engine's TR 38.901 element has
     65/65 compiled in with no attributes". That was true of the 3GPP element
     and false of the engine: ns-3 also ships cosine, parabolic and circular
     aperture elements, and the cosine one takes exactly those three numbers.
     Same shape of mistake as Phase 2c's "azimuth/tilt cannot work" verdict.

  2. The remaining LteEnbRrc attributes — L3 measurement filtering and the
     connection timers.

Run:  python3 test_antenna_rrc.py            # translation only
      python3 test_antenna_rrc.py --engine   # + real runs, ~12 min
"""
import sys

sys.path.insert(0, "/home/chancelor/nsg_ai_platform")
sys.path.insert(0, "/home/chancelor/nsg_ai_platform/nsg_ai")
sys.path.insert(0, "/home/chancelor/nsg_ai_platform/engines/nsoran")

from nsg_ai.main import nsoran_params                       # noqa: E402
from nsg_ai.schema import NSGSimulationConfig               # noqa: E402
from nsg_test_util import Checker, metric, run, thp         # noqa: E402

check = Checker()


def cfg(*, element=None, hbw=None, vbw=None, gain=None, **settings) -> dict:
    """Three cells in a row, UEs spread between them, all pointing east.

    Boresight matters here: with every panel aimed the same way, narrowing the
    beam has to change who is covered, which is the point of the test.
    """
    ant = {"azimuth_deg": 90.0, "tilt_deg": 3.0}
    if hbw is not None:
        ant["horizontal_beamwidth_deg"] = hbw
    if vbw is not None:
        ant["vertical_beamwidth_deg"] = vbw
    if gain is not None:
        ant["gain_dbi"] = gain
    s = dict(settings)
    if element is not None:
        s["antenna_element"] = element
    return {
        "name": "antenna + rrc",
        "base_stations": [
            {"id": f"c{i}", "name": f"C{i}",
             "position": {"x": x, "y": 0.0, "z": 25.0},
             "radio": {"band": {"label": "b", "frequency_mhz": 3500,
                                "bandwidth_mhz": 50, "numerology": 2},
                       "tx_power_dbm": 44.0, "antenna": dict(ant)}}
            for i, x in enumerate([-350.0, 0.0, 350.0], start=1)],
        "ue_groups": [
            {"id": f"u{i}", "name": f"U{i}", "count": 3,
             "spawn_position": {"x": x, "y": 120.0},
             "services": [{"slice_type": "eMBB", "qos_id": 9,
                           "target_throughput_gbps": 0.03}]}
            for i, x in enumerate([-250.0, 100.0], start=1)],
        "area": {"x_min": -900.0, "x_max": 900.0, "y_min": -900.0,
                 "y_max": 900.0, "resolution_m": 40.0},
        "settings": s,
    }


def translation() -> None:
    print("\n=== translation ===")
    p = nsoran_params(NSGSimulationConfig(**cfg(
        element="cosine", hbw=30.0, vbw=12.0, gain=17.0)))
    check(p["antenna_element"] == "cosine",
          f"element family reaches the engine: {p['antenna_element']}")
    first = p["gnb_antenna"].split(";")[0]
    check(first.endswith(",30.0,12.0,17.0"),
          f"per-cell beamwidth + gain ride the panel spec: {first}")

    for elem in ("isotropic", "3gpp", "parabolic", "circular"):
        p = nsoran_params(NSGSimulationConfig(**cfg(element=elem)))
        check(p["antenna_element"] == elem, f"element '{elem}' survives translation")

    # The RRC remainder.
    p = nsoran_params(NSGSimulationConfig(**cfg(
        rsrp_filter_coefficient=11, rsrq_filter_coefficient=2,
        connection_setup_timeout_ms=400.0, srs_periodicity=40,
        default_transmission_mode=2, report_all_ue_meas=False)))
    got = {k: p[k] for k in ("rsrp_filter_coeff", "rsrq_filter_coeff",
                             "conn_setup_timeout_ms", "srs_periodicity",
                             "default_tx_mode", "report_all_ue_meas")}
    check(got == {"rsrp_filter_coeff": 11, "rsrq_filter_coeff": 2,
                  "conn_setup_timeout_ms": 400.0, "srs_periodicity": 40,
                  "default_tx_mode": 2, "report_all_ue_meas": "0"},
          f"RRC remainder reaches the engine: {got}")

    # SrsPeriodicity is a TS 36.213 table value. ns-3 looks it up, so a number
    # that is merely plausible is not acceptable — the schema has to refuse it
    # rather than let the engine decide what to do with 37.
    try:
        NSGSimulationConfig(**cfg(srs_periodicity=37))
        check(False, "an off-table SRS periodicity is rejected")
    except Exception:
        check(True, "an off-table SRS periodicity (37) is rejected")


def _eng_beamwidth() -> None:
    """A narrow beam must not measure the same as a wide one.

    Both configs are identical apart from the cosine element's beamwidth, so
    anything that moves is the beamwidth. Narrow concentrates gain on boresight
    and starves everyone off-axis; wide is the reverse. Which way the aggregate
    goes depends on where the UEs happen to sit, so this asserts a DIFFERENCE
    rather than a direction — claiming a direction here would be claiming to
    know the geometry's answer in advance.
    """
    wide = thp(run(nsoran_params(NSGSimulationConfig(
        **cfg(element="cosine", hbw=120.0, vbw=120.0, gain=8.0)))))
    narrow = thp(run(nsoran_params(NSGSimulationConfig(
        **cfg(element="cosine", hbw=20.0, vbw=20.0, gain=8.0)))))
    check(abs(narrow - wide) / wide > 0.05,
          f"cosine beamwidth is a real lever: 120 deg {wide / 1000:.1f} -> "
          f"20 deg {narrow / 1000:.1f} Mbps "
          f"({(narrow - wide) / wide * 100:+.1f}%)")


def _eng_gain() -> None:
    """Element boresight gain, the other field that was written off.

    Raised on ONE cell, not all of them. Lifting every cell's gain together
    raises signal and interference by the same amount, so in an
    interference-limited network SINR is unchanged — measured, 10.3 vs 9.5 dB
    for a 20 dB swing, which is the physics working correctly and a test
    asserting the wrong thing. An asymmetric gain is a real change in who wins
    where.
    """
    def one_cell(gain):
        c = cfg(element="cosine", hbw=65.0, vbw=65.0, gain=0.0)
        # Low TX power on purpose. At the default 44 dBm this scenario already
        # runs at ~94% 64QAM — the link is at the top of the MCS table and
        # extra gain buys nothing, which measured as +2.7% and read as "the
        # lever does not work". A lever has to be measured where it has room.
        for b in c["base_stations"]:
            b["radio"]["tx_power_dbm"] = 12.0
        c["base_stations"][1]["radio"]["antenna"]["gain_dbi"] = gain
        return nsoran_params(NSGSimulationConfig(**c))

    flat, boosted = run(one_cell(0.0)), run(one_cell(20.0))
    d_thp = (thp(boosted) - thp(flat)) / thp(flat)
    bler_flat = float((flat.get("derived") or {}).get("bler_pct", 0.0))
    bler_boost = float((boosted.get("derived") or {}).get("bler_pct", 0.0))
    check(abs(d_thp) > 0.05,
          f"element gain on one cell changes the network: "
          f"{thp(flat) / 1000:.1f} -> {thp(boosted) / 1000:.1f} Mbps "
          f"({d_thp * 100:+.1f}%)")
    # Direction is deliberately not asserted on throughput. Measured, a 20 dB
    # boost on ONE cell made the network WORSE — that cell becomes the dominant
    # interferer for its neighbours' UEs, and the aggregate loses more than the
    # boosted cell gains. BLER rising alongside is the corroborating signal,
    # and three metrics moving together is what separates this from noise.
    check(bler_boost > bler_flat,
          f"and the asymmetry shows up as interference: BLER "
          f"{bler_flat:.1f}% -> {bler_boost:.1f}%")


def _eng_elements_run() -> None:
    """Every element in the family has to survive a real run. A pattern that
    aborts ns-3 is worse than one that is missing."""
    for elem in ("3gpp", "cosine", "parabolic", "circular"):
        d = run(nsoran_params(NSGSimulationConfig(**cfg(element=elem))))
        check(thp(d) > 0, f"element '{elem}' runs: {thp(d) / 1000:.1f} Mbps")


def _eng_rrc() -> None:
    """The RRC remainder has to be ACCEPTED by ns-3, which is not free: a
    rejected --ns3:: argument aborts before the scenario starts and the engine
    answers with no data at all, indistinguishable from a dead engine."""
    d = run(nsoran_params(NSGSimulationConfig(**cfg(
        rsrp_filter_coefficient=11, rsrq_filter_coefficient=2,
        connection_request_timeout_ms=40.0, connection_setup_timeout_ms=400.0,
        connection_rejected_timeout_ms=60.0, srs_periodicity=40,
        system_information_periodicity_ms=20.0, default_transmission_mode=2,
        report_all_ue_meas=False))))
    check(thp(d) > 0,
          f"every RRC attribute is accepted and the run completes: "
          f"{thp(d) / 1000:.1f} Mbps")


if __name__ == "__main__":
    translation()
    if "--engine" in sys.argv:
        print("\n=== engine ===")
        for name, fn in (("elements run", _eng_elements_run),
                         ("beamwidth", _eng_beamwidth),
                         ("element gain", _eng_gain),
                         ("rrc remainder", _eng_rrc)):
            check.guarded(name, fn)
    else:
        print("\n(skipping engine runs; pass --engine to include them)")
    sys.exit(check.report())
