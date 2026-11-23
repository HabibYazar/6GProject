#!/usr/bin/env python3
"""Probe undriven engine attributes: which of them actually move a run?

The audit's backlog is a POOL, not a target. It counts every attribute the
engine registers, and most of those are not product knobs — object pointers,
LTE-anchor namesakes of things already driven on the mmWave data path, or
models this scenario never instantiates. Adding them all would hand the user a
wall of controls that mostly control nothing, which is worse than not having
them.

So each candidate is run through the engine with a non-default value and
compared against a baseline. Three outcomes:

  MOVES     the run changed -> worth a schema field
  INERT     accepted and nothing changed -> the attribute is registered but
            nothing in this scenario consumes it
  BREAKS    the run died -> record the reason, do not expose

Run:  python3 probe_backlog.py            # the curated candidate list
      python3 probe_backlog.py --all      # everything, slow
"""
import sys

sys.path.insert(0, "/home/chancelor/nsg_ai_platform")
sys.path.insert(0, "/home/chancelor/nsg_ai_platform/nsg_ai")
sys.path.insert(0, "/home/chancelor/nsg_ai_platform/engines/nsoran")

from nsg_ai.main import nsoran_params                       # noqa: E402
from nsg_ai.schema import NSGSimulationConfig               # noqa: E402
from nsg_test_util import run                               # noqa: E402

# Curated from the 47 undriven attributes on MmWaveHelper + LteEnbNetDevice.
# Everything left out is left out for a stated reason, in the report below.
CANDIDATES = [
    # Carrier aggregation — a real 5G feature the platform cannot express at
    # all today. MEASURED: the flag alone aborts the run with
    #   NS_ASSERT failed, cond="m_componentCarrierPhyParams.size ..."
    # because the helper expects a component-carrier PHY map that this scenario
    # never builds. So CA is not a parameter we are missing; it is a FEATURE
    # the scenario would have to grow. Kept in the list as the record of that.
    ("carrier aggregation (2 CC)",
     "--ns3::MmWaveHelper::UseCa=true "
     "--ns3::MmWaveHelper::NumberOfComponentCarriers=2"),
    # Swap the propagation model class outright, rather than picking a scenario
    # inside the 3GPP one (which we already drive as prop_scenario).
    ("pathloss model -> Friis",
     "--ns3::MmWaveHelper::PathlossModel=ns3::FriisPropagationLossModel"),
    # Automatic neighbour relations.
    ("ANR enabled", "--ns3::MmWaveHelper::AnrEnabled=true"),
    # The LTE anchor's own bandwidth. All user traffic rides mmWave, so the
    # expectation is that this does nothing measurable — worth confirming
    # rather than assuming, since it is the single most plausible-looking
    # attribute on LteEnbNetDevice.
    ("LTE anchor DL bandwidth 100 RB",
     "--ns3::LteEnbNetDevice::DlBandwidth=100"),
    # The LTE-side E2 reporting namesakes. We already drive the mmWave ones.
    ("LTE anchor CU-CP report off",
     "--ns3::LteEnbNetDevice::EnableCuCpReport=false"),
]


def cfg() -> dict:
    return {
        "name": "backlog probe",
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
                           "target_throughput_gbps": 0.03}]}
            for i, x in enumerate([-350.0, 0.0, 350.0], start=1)],
        "area": {"x_min": -900.0, "x_max": 900.0, "y_min": -900.0,
                 "y_max": 900.0, "resolution_m": 40.0},
    }


def fingerprint(d: dict) -> tuple:
    """What a run looked like. PRB and BLER alongside throughput, because a
    parameter can reshape the radio without moving the aggregate."""
    m = d.get("measurements") or {}
    t = m.get("DRB.UEThpDl", {})
    return (round(float(t.get("mean", 0.0)) * int(t.get("count", 0)), 1),
            round(float(m.get("RRU.PrbUsedDl", {}).get("mean", 0.0)), 2),
            round(float((d.get("derived") or {}).get("bler_pct", 0.0)), 2))


BASELINE_RUNS = 3


def main() -> int:
    params = nsoran_params(NSGSimulationConfig(**cfg()))

    # The baseline is a RANGE, not a sample.
    #
    # The first version of this probe took one baseline run and compared each
    # candidate against it. All four candidates came back "MOVES" — and all
    # four landed within 1% of EACH OTHER while the lone baseline sat 3 BLER
    # points away. The baseline was the outlier. One of those candidates was
    # "turn off an E2 report on the LTE anchor", which cannot plausibly change
    # mmWave throughput at all; that is what tipped it off.
    #
    # A candidate now has to fall outside the observed baseline spread on some
    # metric, which is a claim about signal rather than about one roll.
    print(f"baseline x{BASELINE_RUNS}...", flush=True)
    base = [fingerprint(run(params)) for _ in range(BASELINE_RUNS)]
    lo = tuple(min(b[i] for b in base) for i in range(3))
    hi = tuple(max(b[i] for b in base) for i in range(3))
    for b in base:
        print(f"    total={b[0] / 1000:6.1f} Mbps  prb={b[1]:5.2f}  bler={b[2]:5.2f}%")
    print(f"  spread: total {lo[0] / 1000:.1f}-{hi[0] / 1000:.1f} Mbps  "
          f"prb {lo[1]}-{hi[1]}  bler {lo[2]}-{hi[2]}%\n", flush=True)

    for label, args in CANDIDATES:
        p = dict(params)
        p["extra_args"] = args
        try:
            got = fingerprint(run(p))
        except Exception as exc:
            print(f"  BREAKS  {label}\n          {str(exc)[:120]}", flush=True)
            continue
        # Outside the baseline band, with a little margin so a value grazing
        # the edge of three samples does not count.
        margin = 1.10
        out = [got[i] < lo[i] / margin or got[i] > hi[i] * margin for i in range(3)]
        verdict = "MOVES " if any(out) else "INERT "
        which = ",".join(n for n, o in zip(("thp", "prb", "bler"), out) if o)
        print(f"  {verdict} {label}" + (f"  [{which}]" if which else ""), flush=True)
        print(f"          total={got[0] / 1000:.1f} Mbps  prb={got[1]}  "
              f"bler={got[2]}%", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
