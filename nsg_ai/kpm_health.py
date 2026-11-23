"""What the engine's own counters get wrong, checked where the product reads them.

The slow tier's SIMULATION is not in question — everything the fast tier
derived from its source reproduced its behaviour. Its TELEMETRY is: measuring
the engine six ways to build a tolerance band turned up five defects in what
it reports about itself, and three of them feed headline numbers the product
shows.

None of these needs an engine rebuild. They are all caught on the way in.

  DRB.BufferSize.Qos     underflows. At 5 Mbps per UE the engine reported
                         4,294,966,948 for a queue whose ceiling is 10 MB —
                         2^32 minus 348, so it computed MINUS 348 bytes and
                         wrapped. A wrapped counter is not a measurement and
                         is dropped, not averaged.

  RRU.PrbUsedDl          under-reports airtime. Bytes per transport block
                         over the symbols this metric implies exceeds what
                         the MCS table can carry, in five of six scenarios,
                         by 1.03 to 1.57x. It has form: prb-truncation-fix
                         caught it reading exactly 0.0 while UEs carried
                         8.9 Mbps.

  DRB.UEThpDl            is not a throughput. mmwave-enb-net-device computes
                         it as mean PDU size over mean RLC DELAY — bits over
                         a latency, not over time — which is why it reads
                         13 Mbps where the delivered volume says 36.
                         QosFlow.PdcpPduVolumeDL is the volume figure.

  QosFlow.PdcpPduVolumeDL is declared in BOTH the CU-UP and the DU report
                         headers, so one name carries two quantities sixteen
                         times apart.

  DRB.PdcpSduBitRateDl   appears in two frames of forty. Not enough to be a
                         measurement of anything.
"""
from __future__ import annotations

# A uint32 that went below zero. Nothing this platform measures legitimately
# reaches 2^31, and a wrap is not a large number — it is a different number.
UINT32_UNDERFLOW_FLOOR = 2 ** 31

CAVEATS = {
    "DRB.BufferSize.Qos":
        "underflows to ~4.29e9 at low load; wrapped samples are dropped",
    "RRU.PrbUsedDl":
        "under-reports airtime — it exceeds its own bytes-per-symbol ceiling",
    "DRB.UEThpDl":
        "bits over the RLC DELAY, not over time; use QosFlow.PdcpPduVolumeDL "
        "for a rate",
    "QosFlow.PdcpPduVolumeDL":
        "the engine declares this name in both its CU-UP and DU reports",
    "DRB.PdcpSduBitRateDl":
        "present in about one frame in twenty; too sparse to average",
}


def is_wrapped(value) -> bool:
    """A sample that is a wrap rather than a magnitude."""
    try:
        return float(value) >= UINT32_UNDERFLOW_FLOOR
    except (TypeError, ValueError):
        return False


def clean_samples(name: str, values: list) -> tuple[list, int]:
    """Usable samples, and how many were dropped as wrapped.

    Returns the count rather than logging it, because a value silently
    removed is as misleading as a wrong one kept — the caller puts it in the
    summary where whoever reads the number can see it happened.
    """
    kept = [v for v in values if not is_wrapped(v)]
    return kept, len(values) - len(kept)


def caveats_for(names) -> dict:
    """The caveats that apply to the counters this run actually produced."""
    return {n: CAVEATS[n] for n in names if n in CAVEATS}
