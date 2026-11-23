"""The engine's OWN block error model, ported rather than approximated.

WHY THIS EXISTS. Tier B's block error rate read 3.11% where the engine
measured 10.11%, its 64QAM share 99.4% against 92.1%, its blocks-in-error
273 against 781 and its delivered bytes 2.09x — four of the numbers on the
headline accuracy claim, and ONE root cause behind all of them. Tier B chose
its MCS from a Shannon gap and then reported whatever block error rate that
margin implied; the engine chooses the highest MCS whose TBLER is still
under a literal 0.1 and then decodes with a mutual-information model. Those
are different algorithms, and `window_model.FADING_SIGMA_DB` carries the
record of five attempts to predict the spread that the heuristic needs. This
removes the need to predict it: the MI model computes the error rate from
the SINR and the transport-block size directly.

WHAT IS PORTED, from `src/mmwave/model/error-model/mmwave-lte-mi-error-model.cc`
in the ns-O-RAN tree — the model the shipped arguments select
(`--ns3::MmWaveSpectrumPhy::ErrorModelType=ns3::MmWaveLteMiErrorModel`):

    Mib()            per-RB SINR -> mutual information per bit, from the
                     modulation's own 797/994/752-point map
    MappingMiBler()  MI + code rate + code-block size -> BLER, via
                     0.5 * erfc((mi - b) / (sqrt(2) * c)) with b and c out of
                     bEcrTable / cEcrTable
    GetTbBitDecodificationStats()
                     TS 36.212 5.1.2 code-block segmentation, then the
                     per-block BLERs combined

WHAT IS NOT. The HARQ retransmission path: the engine combines the mutual
information of previous attempts and re-derives an effective code rate from
the history. Tier B has no per-block history — it computes a window, not a
sequence — so only the FIRST-TRANSMISSION branch is ported, which is the
branch the AMC itself uses (`CreateCqiFeedbackWbTdma` passes an empty
history). Retransmissions still appear, as they always have, in the block
error count.

The tables are in `mi_tables.py` and were extracted mechanically by
`tools/extract_mi_tables.py`; nothing here was copied by eye.
"""
from __future__ import annotations

import math

from mi_tables import (BlerCurvesEcrMap, McsEcrBlerTableMapping, McsEcrTable,
                       MI_map_16qam, MI_map_16qam_axis, MI_map_64qam,
                       MI_map_64qam_axis, MI_map_qpsk, MI_map_qpsk_axis,
                       bEcrTable, cEcrTable, cbMiSizeTable, cbSizeTable)

MI_QPSK_MAX_ID = 9
MI_16QAM_MAX_ID = 16
MI_64QAM_MAX_ID = 28          # 29, 30 and 31 are reserved
MAX_MCS = 28

# The maps are uniformly spaced, and the engine exploits that rather than
# searching: index = (sinr - axis[0]) * (N - 1) / (axis[N-1] - axis[0]) + 1,
# floored. The `+ 1` and the floor are the engine's, and they are kept —
# reproducing its quantisation is the point of porting instead of fitting.
_MAPS = {
    2: (MI_map_qpsk, MI_map_qpsk_axis),
    4: (MI_map_16qam, MI_map_16qam_axis),
    6: (MI_map_64qam, MI_map_64qam_axis),
}
_SCALE = {qm: (len(ax) - 1) / (ax[-1] - ax[0]) for qm, (_, ax) in _MAPS.items()}


def qm_for(mcs: int) -> int:
    """Modulation order the MI map is chosen by — the engine's own bands."""
    if mcs <= MI_QPSK_MAX_ID:
        return 2
    if mcs <= MI_16QAM_MAX_ID:
        return 4
    return 6


def mib(sinr_linear: float, mcs: int) -> float:
    """`MmWaveLteMiErrorModel::Mib` for a FLAT SINR across the allocation.

    The engine averages the per-RB mutual information over the RB map. Tier B
    carries one SINR per UE rather than a vector, so the average of a
    constant is that constant and the loop collapses. That is a real
    simplification and it is the one place this port is not exact: a
    frequency-selective channel has a lower effective MI than its mean SINR
    suggests, so this is optimistic by whatever the selectivity is worth.
    """
    table, axis = _MAPS[qm_for(mcs)]
    if sinr_linear > axis[-1]:
        return 1.0
    idx = int(max(0.0, math.floor((sinr_linear - axis[0]) * _SCALE[qm_for(mcs)] + 1)))
    return table[min(idx, len(table) - 1)]


def mapping_mi_bler(mi: float, ecr_id: int, cb_size: int) -> float:
    """`MmWaveLteMiErrorModel::MappingMiBler`. IEEE 802.16m EMD formula 55."""
    cb_index = 1
    while cb_index < 9 and cbMiSizeTable[cb_index] <= cb_size:
        cb_index += 1
    cb_index -= 1
    b = bEcrTable[cb_index][ecr_id]
    if b < 0.0:
        i = cb_index
        while i < 9 and b < 0:
            b = bEcrTable[i][ecr_id]
            i += 1
    c = cEcrTable[cb_index][ecr_id]
    if c < 0.0:
        i = cb_index
        while i < 9 and c < 0:
            c = cEcrTable[i][ecr_id]
            i += 1
    if c == 0:
        return 0.0 if mi >= b else 1.0
    return 0.5 * (1.0 - math.erf((mi - b) / (math.sqrt(2.0) * c)))


def _segment(size_bits: int) -> tuple[int, int, int, int]:
    """TS 36.212 5.1.2 code-block segmentation: (C, Cplus, Kplus, Kminus)."""
    Z = 6144
    B = size_bits
    if B <= Z:
        C, B1 = 1, B
    else:
        L = 24
        C = math.ceil(B / (Z - L))
        B1 = B + C * L
    target = B1 / C + 0.001
    # upper_bound over cbSizeTable excluding its last element, as the engine
    # does (`--cbSizeTable.end()`).
    k_idx = None
    for i, k in enumerate(cbSizeTable[:-1]):
        if k > target:
            k_idx = i
            break
    if k_idx is None:
        k_idx = len(cbSizeTable) - 1
    Kplus = cbSizeTable[k_idx]
    if C == 1:
        return 1, 1, Kplus, 0
    Kminus = cbSizeTable[k_idx] if k_idx == 0 else cbSizeTable[k_idx - 1]
    delta = Kplus - Kminus
    Cminus = math.floor(((C * Kplus) - B1) / delta) if delta else 0
    return C, C - Cminus, Kplus, Kminus


def tb_bler(sinr_linear: float, mcs: int, tb_bytes_: float) -> float:
    """`GetTbDecodificationStats` for a first transmission.

    `tb_bytes_` is the transport block the decision is about, which is NOT
    always the block that will be sent — the AMC asks about a 12-symbol block
    whatever the UE is allocated (`MmWaveAmc::m_numSymForCqi`).
    """
    if mcs < 0:
        return 1.0
    size_bits = max(8, int(round(tb_bytes_ * 8)))
    mi = mib(sinr_linear, mcs)
    ecr_id = McsEcrBlerTableMapping[min(mcs, MAX_MCS)]
    C, Cplus, Kplus, Kminus = _segment(size_bits)
    if C == 1:
        return min(1.0, max(0.0, mapping_mi_bler(mi, ecr_id, Kplus)))
    ok = (1.0 - mapping_mi_bler(mi, ecr_id, Kplus)) ** Cplus
    ok *= (1.0 - mapping_mi_bler(mi, ecr_id, Kminus)) ** (C - Cplus)
    return min(1.0, max(0.0, 1.0 - ok))


# `MmWaveAmc::m_numSymForCqi` — a compile-time constant citing TS 38.214
# 5.2.2.5. The AMC's decision is made for a block of THIS many symbols
# whatever the UE will actually be allocated, which on the uplink here is
# about 0.4 symbols: an MCS chosen for a block thirty times the one sent.
NUM_SYM_FOR_CQI = 12
BLER_TARGET = 0.1          # the literal in CreateCqiFeedbackWbTdma


def select_mcs(sinr_db: float, tb_bytes_fn, target: float = BLER_TARGET) -> int:
    """`CreateCqiFeedbackWbTdma`, ErrorModel branch — the engine's AMC.

        mcs = 0
        while mcs <= maxMcs:
            if tbler(CalculateTbSize(mcs, m_numSymForCqi)) > 0.1: break
            mcs++
        if mcs > 0: mcs--

    Returns -1 for the engine's "CQI out of range" case, where even MCS 0
    cannot hold the target — on the uplink that makes the scheduler skip the
    UE entirely, and on the downlink it reports CQI 0.

    `tb_bytes_fn(mcs)` gives the block size to ASK ABOUT at each index; the
    caller supplies it because the symbol count is the AMC's reference and
    not the allocation.
    """
    sinr_lin = 10 ** (sinr_db / 10.0)
    mcs = 0
    last = 1.0
    while mcs <= MAX_MCS:
        last = tb_bler(sinr_lin, mcs, tb_bytes_fn(mcs))
        if last > target:
            break
        mcs += 1
    if mcs > 0:
        return mcs - 1
    return -1 if last > target else 0
