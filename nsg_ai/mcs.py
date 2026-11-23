"""SINR → MCS → spectral efficiency, using the ENGINE'S OWN tables.

Tier B has to produce the counters Tier A produces, and three of the contract
metrics are about modulation and transport blocks: blocks sent, blocks in
error, blocks at 64QAM. A Shannon capacity — which is what the fast tier
computes today — has no MCS in it, so it cannot produce any of them. This adds
the missing step.

EVERY NUMBER HERE IS COPIED OUT OF THE ENGINE, not out of a standard. The
tables live in
`src/mmwave/model/error-model/mmwave-lte-mi-error-model.cc`, which is the error
model the shipped arguments select
(`--ns3::MmWaveAmc::ErrorModelType=ns3::MmWaveLteMiErrorModel`). Copying rather
than deriving is the point: a table from the spec that differs from the table
in the binary would make every disagreement a hunt.

The SINR→SE step is the engine's too — `MmWaveAmc::CreateCqiFeedbackWbTdma`:

    se  = log2(1 + SINR_linear / gamma),   gamma = -ln(5 * BER) / 1.5
    mcs = the largest MCS whose tabled SE does not exceed se

WHAT THIS DOES NOT REPRODUCE, and it matters. The shipped engine runs
`AmcModel=ErrorModel`, not `ShannonModel`, so its MCS comes from
`GetTbDecodificationStats` — the full EESM/mutual-information error model
searching for the highest MCS that holds a BLER target. That is not something a
per-window model can evaluate analytically. So this implements the engine's
OTHER branch, the one the same class ships, and the residual difference is a
calibration to be MEASURED against Tier A rather than assumed away.

READ AT THE SOURCE 2026-09-21, and the branch is now quoted rather than
described. `MmWaveAmc::CreateCqiFeedbackWbTdma`, ErrorModel branch:

    mcs = 0;
    while (mcs <= m_errorModel->GetMaxMcs()) {
        output = m_errorModel->GetTbDecodificationStats(
                     sinr, rbMap, CalculateTbSize(mcs, m_numSymForCqi), mcs, ...);
        if (output->m_tbler > 0.1) break;
        mcs++;
    }
    if (mcs > 0) mcs--;

Three things fall out of it that were not known before:

1. **THE TARGET IS 10% BLOCK ERROR**, written as a literal `0.1`. Tier B does
   not assume a target — it DERIVES 7.1% from an 8.2 dB gap and a 5.57 dB
   Rayleigh spread (`window_model.FADING_SIGMA_DB`) — and landing next to the
   engine's own hardcoded cutoff, from the other side, is a confirmation of
   that derivation rather than a coincidence to fit to.

2. **THE DECISION IS MADE FOR A 12-SYMBOL BLOCK**, always.
   `m_numSymForCqi = 12`, a compile-time constant citing TS 38.214 Sec.
   5.2.2.5, and it is the block size handed to the error model regardless of
   what the UE will actually be allocated. On the downlink a UE here gets
   about 1.6 symbols and on the uplink about 0.4, so the MCS is chosen for a
   block several times — on the uplink about thirty times — larger than the
   one that is sent.

3. **THE TWO DIRECTIONS DO NOT USE THE SAME PATH.** The downlink MCS is
   `m_amc->GetMcsFromCqi(cqi)` where `cqi` is what the UE REPORTED, so it
   round-trips MCS -> CQI -> MCS through sixteen levels and loses resolution.
   The uplink calls `CreateCqiFeedbackWbTdma` at the gNB on measured PUSCH
   SINR and takes the MCS straight out, with no quantisation. This module
   has one rule for both.
"""
from __future__ import annotations

import math

# SpectralEfficiencyForMcs[32] — mmwave-lte-mi-error-model.cc. The last three
# are the reserved MCS 29-31 and carry 0; they are kept so an index matches the
# engine's.
SE_FOR_MCS = (
    0.15, 0.19, 0.23, 0.31, 0.38, 0.49, 0.60, 0.74, 0.88, 1.03,
    1.18, 1.33, 1.48, 1.70, 1.91, 2.16, 2.41, 2.57, 2.73, 3.03,
    3.32, 3.61, 3.90, 4.21, 4.52, 4.82, 5.12, 5.33, 5.55, 0.0, 0.0, 0.0,
)
# ModulationSchemeForMcs[32] — bits per symbol. 2 = QPSK, 4 = 16QAM, 6 = 64QAM.
QM_FOR_MCS = (
    2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 4, 4, 4, 4, 4, 4, 4,
    6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 2, 4, 6,
)
# McsEcrTable[29] — the code rate behind each MCS.
ECR_FOR_MCS = (
    0.08, 0.10, 0.11, 0.15, 0.19, 0.24, 0.30, 0.37, 0.44, 0.51,
    0.30, 0.33, 0.37, 0.42, 0.48, 0.54, 0.60, 0.43, 0.45, 0.50,
    0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.89, 0.92,
)
MAX_MCS = 28                      # the last non-reserved entry
MODULATION = {2: "QPSK", 4: "16QAM", 6: "64QAM"}

# The BER the shipped arguments set (`--ns3::MmWaveAmc::Ber=1e-05`). The gap it
# implies is 8.2 dB, which is why a Shannon capacity overstates what the engine
# actually schedules.
DEFAULT_BER = 1e-5


def snr_gap(ber: float = DEFAULT_BER) -> float:
    """gamma = -ln(5 * BER) / 1.5, linear. `MmWaveAmc::CreateCqiFeedbackWbTdma`."""
    return -math.log(5.0 * ber) / 1.5


def se_from_sinr(sinr_db: float, ber: float = DEFAULT_BER) -> float:
    """Unquantised spectral efficiency, the engine's formula."""
    return math.log2(1.0 + (10 ** (sinr_db / 10.0)) / snr_gap(ber))


def mcs_from_se(se: float) -> int:
    """Largest MCS whose tabled SE fits — `MmWaveAmc::GetMcsFromSpectralEfficiency`.

    Returns -1 when even MCS 0 does not fit: the link cannot carry a transport
    block at all. A caller that maps that to "MCS 0" would invent throughput
    where the engine schedules none.
    """
    if se < SE_FOR_MCS[0]:
        return -1
    mcs = 0
    while mcs < MAX_MCS and SE_FOR_MCS[mcs + 1] <= se:
        mcs += 1
    return mcs


def link(sinr_db: float, ber: float = DEFAULT_BER) -> dict:
    """The whole step, as the fields a KPM report needs.

    `se` is the QUANTISED efficiency — what the scheduler can actually use —
    not the Shannon value. The difference is the point of this module: at 20 dB
    Shannon says 6.7 bits/s/Hz and the engine's own chain says 3.9, because of
    the 8.2 dB gap and then the drop to a tabled MCS.
    """
    raw = se_from_sinr(sinr_db, ber)
    mcs = mcs_from_se(raw)
    if mcs < 0:
        return {"sinr_db": sinr_db, "se_shannon_gap": round(raw, 3), "mcs": None,
                "qm": 0, "modulation": None, "se": 0.0, "ecr": 0.0, "in_outage": True}
    return {
        "sinr_db": sinr_db,
        "se_shannon_gap": round(raw, 3),
        "mcs": mcs,
        "qm": QM_FOR_MCS[mcs],
        "modulation": MODULATION[QM_FOR_MCS[mcs]],
        "se": SE_FOR_MCS[mcs],
        "ecr": ECR_FOR_MCS[mcs],
        "in_outage": False,
    }
