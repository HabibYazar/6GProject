"""The fast tier's RAN: one KPM window computed in closed form.

Tier A advances ns-3 one TTI at a time — 4000 steps for the second of airtime
that a single KPM report covers. This computes the same report's counters in
one step. That is where the speed comes from; the channel work everyone blames
is [[nsg-ceiling-stub-corrected]] worth only 1.34x.

For the counters to be COMPARABLE rather than merely plausible, they have to be
defined the way the engine defines them, not the way the specification does.
Every constant below is read out of the engine's own source and cited, because
a plausible-looking number from TS 38.214 that differs from the one in the
binary turns every disagreement into a hunt.

    slot            1 ms / 2^mu                 MmWavePhyMacCommon::SetNrNumerology
    symbols/slot    14                          same
    RB width        12 * 15kHz * 2^mu           same
    RB count        ceil(bandwidth / RB width)  MmWavePhyMacCommon::SetBandwidth
    useful SC/RB    12 - 1 (one reference)      SUBCARRIERS_PER_RB, REF_SUBCARRIERS_PER_RB
    TB bytes        usefulSC * nSym * nRb * Qm * Rcode / 8 - crc
                                                MmWaveLteMiErrorModel::GetPayloadSize
                                                + MmWaveAmc::CalculateTbSize (crc = 3 bytes)

TWO OF THESE ARE TRAPS, and both are the engine's, not ours:

1. **The numerology is always 2 or 3.** `MmWavePhyMacCommon::SetNrNumerology`
   asserts on anything else, and `engine_envelope.config_numerology` clamps
   accordingly: a config that says numerology 1 RUNS AT 2. Reading the config
   would give a 0.5 ms slot and half the transport blocks. The engine's own
   counters say 4000 per second, which is 0.25 ms, which is mu=2.

2. **`RRU.PrbUsedDl` is not PRBs.** `BuildRicIndicationMessageDu` reports
   `symbols_used / (slots * 14) * 139` — a share of the cell's OFDM symbols,
   scaled by a PRB count hardcoded for 100 MHz at 60 kHz ("TODO fix this for
   different numerologies", their comment). At 50 MHz the real RB count is 70,
   so the metric overstates by ~2x. Tier B reproduces the FORMULA, including
   the wrong 139, because the criterion compares against what the engine
   prints.

`DRB.UEThpDl` is deliberately NOT produced here. The engine computes it as
mean PDU size / mean RLC delay — bits over a LATENCY, not over the window — so
it is not the volume this model knows. [[nsg-delay-metric-is-noise]] measured
that denominator swinging 14x on identical inputs, and the Phase 0 criterion,
which never saw the source, independently classified the metric as reported-
only at a 27% band. `QosFlow.PdcpPduVolumeDL` is the volume figure, and the
model reproduces it to 2.5% with nothing fitted.
"""
from __future__ import annotations

import statistics
import math
from dataclasses import dataclass, field

import mi_error_model as mi
from mcs import ECR_FOR_MCS, QM_FOR_MCS, SE_FOR_MCS, link as mcs_link

# THE ENGINE'S OWN AMC AND ERROR MODEL, or the Shannon-gap approximation of
# them. Both are kept because the comparison between them is the evidence
# for using either, and a switch is how that comparison stays runnable.
#
#   False  `mcs.link` — highest MCS whose tabled SE fits under
#          log2(1 + SINR/gap). This is `MmWaveAmc`'s ShannonModel branch,
#          which the shipped arguments do NOT select.
#   True   `mi_error_model` — the ErrorModel branch the engine runs: walk
#          the MCS up until the mutual-information model puts the TBLER of
#          a 12-symbol block over 0.1, then step back one.
#
# Measured at every SINR, the engine's rule is FIVE TO SIX MCS STEPS more
# aggressive than the gap: 7 against 1 at 0 dB, 16 against 11 at 10, 22
# against 16 at 15, 28 against 22 at 20.
#
# IT IS OFF, AND THE MEASUREMENT THAT TURNED IT OFF IS WORTH MORE THAN THE
# SWITCH. Porting the engine's own AMC and error model — mechanically, no
# digit copied by eye — was expected to fix the block error rate, which read
# 3.11% against the engine's 10.11%. On the reference scenario it did:
# 3.11 -> 13.32, from 0.31x to 1.32x. Across the six breadth cases it made
# the model WORSE, mean |ratio - 1| going 0.452 -> 0.923, because it
# produces a roughly CONSTANT ~13% wherever the engine ranges from 4.4% to
# 28.5%. Swapping a flat 3% for a flat 13% is not a model of the variation.
#
# AND THE VALIDATION IT FAILS SAYS WHY. Fed the engine's OWN per-case SINR
# spread, measured from its L1M.RS-SINR histogram (`tools/sinr_spread.py`),
# the ported curve lands 1.04-6.55x off, geometric mean 2.42. The heuristic
# it would replace passes that same test at 0.95. The port cannot be right
# and fail a test the thing it replaces passes.
#
# THE REASON IS AN INDEPENDENCE ASSUMPTION, not the curve. The engine's AMC
# walks the MCS up until the TBLER of a 12-symbol block exceeds 0.1, so it
# selects an MCS that sits AT roughly 10% error at the SINR it was given.
# If the block then goes out into an INDEPENDENT draw from a 5.57 dB
# distribution, the expectation is necessarily well above 10% — the curve is
# convex the wrong way. The engine reports 10.11%, so its AMC input and its
# decode SINR are not independent: `UpdateUeSinrEstimatePeriod=25.6 ms`
# tracks the channel and the block follows shortly after, correlated. A
# window model has no "shortly after".
#
# So the block error rate gap is NOT the error curve, and that is the
# finding: it is the correlation between what the AMC sees and what the
# block meets, which is the same open item `FADING_SIGMA_DB` describes from
# the other side.
#
# ── AND THE CORRELATION IS MEASURED — `tools/amc_correlation.py` ──────────
# Model the AMC input and the decode SINR as jointly normal in dB at the
# engine's OWN measured spread and its OWN measured block size, and solve
# for the correlation that reproduces its OWN measured error rate:
#
#     mobility 0.992 · baseline 0.989 · slice_quota 0.984 · cell_off 0.887
#
# THE EFFECT IS REAL AND LARGE. At rho = 0 the same model predicts 23%
# where the engine measures 9-10%, so independence was decisively the wrong
# assumption and the ported model's 3x overshoot is explained.
#
# IT IS STILL NOT A CONSTANT TO SHIP. Held out — each case predicted from
# the mean rho of the OTHERS — three land at 1.10x, 1.29x and 1.09x and
# `cell_off` at 0.22x, a 4.5x miss. And the curve is pathological near 1:
# +/-0.03 in rho is 3.1x in the answer, so the narrow spread of the fitted
# values is the steepness of the curve, not agreement between scenarios.
#
# `buildings` cannot be fitted at all — 28.5% measured against 23.4% at
# FULL independence. No correlation reaches it, so something else widens
# that tail, most likely the spread itself: `sinr_spread` gives the two
# open histogram bins the centre of a 6 dB bin, which understates sigma
# exactly where the tails are longest.
#
# WHAT IS CLOSED: the error curve (ported, eliminated) and the correlation
# as a single shippable number (measured, eliminated). WHAT IS LEFT is the
# same thing it always was — the per-scenario SINR spread — but it is now
# the only candidate standing rather than one of three.
#
# The port stays, tested and switchable, because the next attempt at that
# correlation will need the engine's real curve underneath it rather than a
# Gaussian.
USE_ENGINE_AMC = False

SYMBOLS_PER_SLOT = 14
SUBCARRIERS_PER_RB = 12
REF_SUBCARRIERS_PER_RB = 1
USEFUL_SC = SUBCARRIERS_PER_RB - REF_SUBCARRIERS_PER_RB
CRC_BYTES = 3                     # MmWaveAmc::m_crcLen = 24 / 8
PRB_METRIC_SCALE = 139            # the engine's hardcoded scale — see trap 2

# `MmWaveFlexTtiMacScheduler::ScheduleDlTriggerReq`:
#     resvCtrl = GetDlCtrlSymbols() + GetUlCtrlSymbols()   # 1 + 1
#     symAvail = GetSymbPerSlot() - resvCtrl               # 12
DL_CTRL_SYMBOLS = 1
UL_CTRL_SYMBOLS = 1
DATA_SYMBOLS_PER_SLOT = SYMBOLS_PER_SLOT - DL_CTRL_SYMBOLS - UL_CTRL_SYMBOLS

# The scheduler divides symAvail evenly between active FLOWS, not UEs, "which
# are then evenly divided between DL and UL flows" (its own comment), and the
# even share is floored at one symbol. Our scenario installs both an OnOff
# downlink and an OnOff uplink on every UE, so every UE presents two flows.
#
# That floor is a HARD CEILING ON CONCURRENCY: once the flow count passes
# symAvail, the leftover flows get nothing that slot and the round-robin
# pointer (`m_nextRnti`) hands the turn to someone else next time. Twelve
# symbols over two flows each is SIX UEs per slot, and the engine's own data
# agrees — mean served UEs per cell climbs with load and then flattens between
# five and eight from eight UEs on, measured over 3600 cell-frames.
FLOWS_PER_UE = 2
MAX_UES_PER_SLOT = DATA_SYMBOLS_PER_SLOT // FLOWS_PER_UE

# ...but the two flows are not the same size. The even share is only the loop's
# STARTING point; the allocation that follows is driven by each flow's deficit,
# which is its buffer. The scenario builds the uplink OnOff at a QUARTER of the
# downlink rate — `DataRate((uint64_t)(svc.rate * 1e6 * 0.25))` — so the uplink
# flow asks for a quarter as much and the downlink keeps the rest.
#
# Splitting the symbols evenly between DL and UL instead makes every transport
# block too small, and the engine says so: measured bytes per block run
# 1.4-1.9x above an even split across every stored run, at every load.
UL_DEMAND_RATIO = 0.25
DL_SYMBOLS_PER_SLOT = DATA_SYMBOLS_PER_SLOT / (1.0 + UL_DEMAND_RATIO)
# ...and the rest of the slot IS the uplink. It was never a free remainder —
# the deficit split that gives the downlink 9.6 symbols gives the uplink 2.4,
# and the model simply threw them away and reported nothing above the MAC in
# that direction. The engine does not: `ul-reporting.patch` put real uplink
# traffic in the scenario at this same 25% and reads DRB.PdcpSduVolumeUl /
# DRB.PdcpSduBitRateUl out of the bearer stats calculator, which had been
# tracking it all along with nobody reading it.
UL_SYMBOLS_PER_SLOT = DATA_SYMBOLS_PER_SLOT - DL_SYMBOLS_PER_SLOT

# WHAT THE UPLINK MEASURES AGAINST — 2026-09-21, and REVISED the same day
# once the unit question was settled and the window was matched.
#
# THE ENGINE'S COUNTER IS IN KBIT AND ITS DOWNLINK COMPANION IS IN BYTES.
# Proved without reading the source: `QosFlow.PdcpPduVolumeDL` over
# `TB.TotNbrDl.1` is 520-1283 bytes per transport block across six stored
# cases, inside the engine's own MCS table, where kbit would put it at
# 65-160 kB against a largest-possible 5,097 bytes at 50 MHz.
#
# AT A MATCHED WINDOW THE GAP IS 3.9x, NOT THE 18x FIRST RECORDED. That
# figure compared a 30-frame Tier B against a 40-frame engine run, and this
# counter is window-sensitive like every other extensive one. At 30 frames
# on both sides, 8 realisations:
#
#     QosFlow.PdcpPduVolumeDL   0.95      <- the control, and it is exact
#     DRB.PdcpSduVolumeUl       3.89
#
# The downlink being right at the same window is what makes the uplink
# number a measurement of the uplink.
#
# AND IT IS NOT THE LINK BUDGET. `tools/uplink_probe.py` swept
# `ue_tx_power_dbm` over 20/30/40 dBm on the real engine. The uplink moved
# 0.88x end to end and NOT monotonically (2934, 1950, 2596 kbit/UE) against
# a repeat-measured 8% noise floor on that metric. The DOWNLINK in the same
# three runs moved 0.54x and monotonically against a 0.5% noise floor —
# through the UE-to-UE interference every node on one SpectrumChannel
# contributes — so the parameter demonstrably reached the radio and the
# uplink's flatness is the uplink's. Raising every UE together raises
# uplink signal and uplink interference equally and leaves SINR where it
# was.
#
# AND IT IS NOT A GRANT RATE EITHER. Six sweeps in `tools/uplink_probe.py`,
# each with the downlink beside it as a control, decompose the gap:
#
#   load    20x the offered uplink -> 1.07x delivered, downlink 4.50x.
#           A CEILING, not a proportional loss.
#   ues     12/36/72 UEs on six cells -> per-UE uplink 0.24x, against 0.33x
#           for a shared CELL CAPACITY and 1.00x for a per-UE GRANT RATE.
#           It is AIRTIME being divided, so a symbol share is the right
#           SHAPE and this model's is merely too large.
#   mcs_ul  PINNING the uplink MCS moves it 7.15x monotonically — 720, 994
#           and 5,148 kbit/UE at MCS 4, 12 and 24. The free-AMC run sits at
#           1,944, between the MCS 12 and 24 arms, so the engine's own
#           uplink AMC behaves like a spectral efficiency near 2.2 where
#           this model uses 3.90 at MCS 22. About 1.8x, reached WITHOUT
#           SINR since the transmit-power sweep is null.
#
# So the 5.0x is roughly 1.8x of conservative uplink AMC times 1.9x of
# airtime — the model is still 1.89x high against the engine PINNED at
# MCS 24, a higher index than its AMC ever chooses. The two named halves
# multiply to 3.4 and the measured total is 5.0, so they account for most
# of it and not all.
#
# NOTHING IS FITTED FROM ANY OF THAT. Either half closes with one constant
# — a UL MCS offset, or a smaller UL_SYMBOLS_PER_SLOT — and either would be
# absorbing a mechanism instead of modelling it. UL_SYMBOLS_PER_SLOT stays
# at the offered ratio the scenario documents
# (`DataRate(svc.rate * 1e6 * 0.25)`), the uplink MCS stays on the same AMC
# the downlink uses, and the metric stays REPORTED rather than CONTRACT.
#
# ── WHAT THE ENGINE'S SOURCE SAYS, read 2026-09-21 ──────────────────────────
# `mmwave-flex-tti-mac-scheduler.cc` and `mmwave-amc.cc`, in the engine
# distro at /home/builder/ns-O-RAN-flexric/mmwave-LENA-oran/src/mmwave/model.
# The uplink is a different code path from the downlink in five ways, and
# this model has none of them. They are recorded because each is a real
# mechanism with a name, not because any is implemented.
#
# 1. THE AMC DECIDES FOR A 12-SYMBOL BLOCK, ALWAYS. `m_numSymForCqi = 12` is
#    a compile-time constant and it is the TB size handed to the error model
#    whatever the UE will be allocated. On the uplink a UE here holds about
#    0.4 symbols, so the MCS is picked for a block about thirty times the
#    one that is sent. See `mcs.py`.
# 2. THE DOWNLINK ROUND-TRIPS THROUGH A 16-LEVEL CQI and the uplink does
#    not: DL is `GetMcsFromCqi(reported cqi)`, UL is the gNB's own
#    `CreateCqiFeedbackWbTdma` straight to an MCS.
# 3. A UE WITH NO UPLINK CQI GETS MCS 0. The uplink CQI exists only after a
#    real PUSCH transmission and EXPIRES after `CqiTimerThreshold` scheduler
#    passes (shipped: 100), so the uplink has a cold start and a decay the
#    downlink does not — the downlink's CQI is reported by the UE whether or
#    not it has been scheduled.
# 4. THE UPLINK NEEDS A BUFFER STATUS REPORT. Only UEs with
#    `m_ceBsrRxed[rnti] > 0` are considered; the BSR is quantised through
#    `BsrId2BufferSize` (the LTE 6-bit table) and DECREMENTED by each grant,
#    refreshed only when the next BSR arrives.
# 5. AN OUT-OF-RANGE UPLINK CQI SKIPS THE UE ENTIRELY —
#    `if (cqi == 0 && !m_fixedMcsUl) continue;` — unless the MCS is pinned,
#    in which case the UE is scheduled anyway. That asymmetry is why the
#    pinned-MCS sweep is not a clean interpolation: see `uplink_probe.py`.
#
# AGAINST VIAVI IT IS ALREADY CLOSE: uplink blocks 1.05, and their UL/DL
# block ratio is 0.16-0.34 against this model's 0.22-0.35, once their
# per-beam duplicate rows are collapsed and the MIMO rank divided out.
# Their own DRB.UEThpUl and DRB.UECqiUl are identically zero everywhere.
#
# ---- WHAT THE FIRST VERSION OF THIS NOTE SAID, AND WHY IT WAS WRONG ----
# It recorded 18x and "the data cannot separate two explanations": either
# the engine's uplink is genuinely weak, or `DRB.PdcpSduVolumeUl` is not in
# the unit it looks like. Both halves were resolvable and neither needed the
# engine's source.
#
#   * The UNIT fell out of the engine's own MCS table, above. It is kbit.
#   * The 18x fell out of matching the WINDOW. It was a 30-frame model
#     against a 40-frame run, and it is 3.9x when both are 30. The lesson is
#     the one this file already carries for every other extensive counter
#     and I did not apply it to the new one.
#   * "The engine holds at 6% of offered for ten simulated seconds" was also
#     a window artefact of the same kind: at 30 frames it delivers 20%.
#
# The `QosFlow.PdcpPduVolumeDL` name collision is real and still worth
# knowing — six frames peaking at 2,493 beside eighteen starting at 31,997,
# which `tier_criterion.name_collision` flags on its own — but it was not
# what made the uplink look wrong.

# A transport block fails when the channel has moved below what the MCS the
# scheduler already chose can carry. Both halves of that are derived:
#
# THE MARGIN. `mcs_from_se` picks the highest MCS whose tabled efficiency fits
# under log2(1 + SINR/gamma), and gamma = -ln(5*BER)/1.5 with the shipped
# BER of 1e-5 is 8.2 dB. So the chosen MCS sits about 8.2 dB above its own
# Shannon threshold, plus whatever the quantisation adds.
#
# THE SPREAD. ** THE 5.2-8.1 dB FIGURE BELOW IS WITHDRAWN — 2026-09-21. **
# It came from the engine's own SINR histogram, `L1M.RS-SINR.Bin*`, whose
# top bin is ">24 dB". The engine's PER-TRANSMISSION trace
# (`tools/rx_trace.py`, 55,919 downlink blocks) says the SINR runs from
# -21.6 to +78.1 dB with a MEAN of 44.4, so essentially the whole population
# lands in that one open bin and the "measurement" was reading the bin
# structure rather than the channel. The real numbers, decomposed:
#
#     between UEs (geometry)   14.99 dB
#     WITHIN a UE (fading)     12.68 dB, and 0.1 to 26.5 across the 36 UEs
#     what this constant says   5.57 dB
#
# So the derived Rayleigh value is 2.28x too small, and it AGREED with the
# histogram only because both were wrong. What follows is kept as written
# because the derivation is still the derivation and the constant is still
# what the model uses; the claim that it was CONFIRMED by measurement is
# what is withdrawn.
#
# The original text: the engine counts every transmission into a SINR
# histogram (whose edges mmwave-phy-trace.h documents as <-6, -6..0,
# ... >24 dB), and measured across five breadth runs its standard deviation is
# 5.2 to 8.1 dB. Rayleigh fading predicts exactly this shape: instantaneous
# SNR is exponentially distributed, and the standard deviation of 10*log10 of
# an exponential is (10/ln 10) * pi/sqrt(6) = 5.57 dB. Derived, and inside
# the measured range rather than fitted to it.
#
# Put together they give the operating point without assuming it:
# Phi(-8.2/5.57) = 7%, against the 6.7-10% the engine reports. The previous
# version of this model ASSUMED a 10% target and decayed from it; this one
# produces the target, and produces something different when the scenario
# changes the margin or the spread — which is the whole point, because the
# breadth runs show the engine at 28% with buildings.
FADING_SIGMA_DB = 10.0 / math.log(10) * math.pi / math.sqrt(6.0)

# ── AND IT IS NOT FADING AT ALL — MEASURED 2026-09-21 ────────────────────
# The engine's per-transmission trace (`tools/rx_trace.py`, 55,919 downlink
# blocks) settles what this spread IS, and it is none of the three things
# this constant and its five failed successors assumed.
#
#   consecutive samples, 0.25 ms apart   median |delta| 0.02 dB
#
# There is essentially NO fast fading at the shipped `channelUpdateMs=100`.
# A per-UE SINR trace is not a noisy line, it is a step function that sits
# on a level and jumps: sharply multimodal, with gaps of tens of dB and
# nothing in between. Three candidates, separated by the trace:
#
#   fast fading                          0.02 dB between samples
#   a neighbour transmitting or not      up to 16.8 dB
#   WHICH UE the neighbour is serving    up to 50.6 dB   <- the spread
#
# A NEIGHBOUR INTERFERES WHEN ITS BEAM POINTS AT YOU, and where it points
# is decided by which of its own UEs it is scheduling that slot. Cell 4
# serving one UE puts the victim at 42 dB; serving another puts it at 0.
# Cells are 45-99% busy, so on/off duty cannot produce the observed swing
# and `sinr_spread_db`'s rho(1-rho) term is the wrong mechanism even when
# it is not being zeroed by `load = 1.0` in `frame()`.
#
# THIS IS WHY THE SPREAD PREDICTOR DEFEATED FIVE ATTEMPTS. Every one of
# them modelled a FADING sigma, because the only instrument available was
# the L1M.RS-SINR histogram, whose top bin holds this entire population and
# which therefore showed a smooth 5-8 dB where the truth is a 50 dB step
# function. The instrument hid the structure, so the structure was never
# modelled.
#
# AND TIER B HAS EVERY INGREDIENT. It knows each cell's UE set and their
# positions, it knows the array and the element pattern, and its own
# scheduler already computes each UE's TURN SHARE — which is exactly the
# probability that its cell's beam points that way. The spread is then a
# MIXTURE over which UE each neighbour is serving, weighted by those
# shares, not a sigma to predict. What is missing is one closed form: the
# array factor of a uniform planar array steered at one UE, evaluated
# toward another. `_element_gain_toward` already does the element half.
#
# WHY THIS IS STILL RAYLEIGH AND NOT A FITTED NUMBER.
#
# The mechanism is validated: fed the engine's OWN per-case spread, the block
# error rate lands at 0.95 on the baseline and 0.95 on mobility. What Tier B
# cannot do is PREDICT that spread. Four derived candidates were implemented
# and all measured worse than the flat value, each reverted with its numbers
# kept (interferer fading, Rician LOS, a steering neighbour beam, and the
# neighbour's real duty cycle — see tools/spread_probe.py).
#
# Fitting one global sigma to the block error rate instead gives 7.5 dB and a
# geometric mean of 0.96 across six scenarios. It is not adopted, for a
# reason the numbers state plainly:
#
#   fitted on the block error rate        7.5 dB
#   measured directly from the engine's   6.08 dB mean, 2.9 to 8.1 across
#   own L1M.RS-SINR histogram             the same six scenarios
#
# If the mechanism were complete those two would agree. They do not, so a fit
# would be absorbing whatever is missing rather than measuring the channel —
# which is exactly what BEAMFORMING_LOSS_DB turned out to be doing. And no
# single value can work anyway: the engine's own spread varies by 2.8x across
# these scenarios, so the best global constant is still 2.65x out on the worst
# case while looking right on average.
#
# `cell_window(decay_db=...)` overrides it, which is how the sweep above was
# run and how a caller who knows the spread can supply it.

# The RLC transmit buffer, per UE, in bytes. `scenario-zero`'s `bufferSize`
# GlobalValue defaults to 10 and is applied as `bufferSize * 1024 * 1024` to
# LteRlcUm, LteRlcUmLowLat and LteRlcAm alike; our launcher passes
# `--bufferSize=${NSG_RLC_BUF_MB:-10}`, so it is a knob and not a constant.
#
# Without the cap a congested UE's queue grows without bound and
# DRB.BufferSize.Qos walks off to infinity. With it the counter behaves the
# way the engine's does — the reference measures a 2.9 MB mean against a
# 10,485,612-byte maximum, which is this ceiling to within a few hundred
# bytes.
RLC_BUFFER_BYTES = 10 * 1024 * 1024

# ZERO. This model has no fitted constants, and the story of how it briefly
# had one is worth keeping.
#
# It was 6.0 dB, and it looked well earned: holding the UMi channel fixed and
# sweeping the effective array gain against bytes-per-transport-block gave
# 0.46 at 0 dB, 0.84 at 9, 0.97 at 12 and 1.21 at the ideal 18.06, so an 8x8
# panel "behaved like 12 dB". There was even a physical story for it — a beam
# matched to one cluster of a 3GPP channel does not collect the others.
#
# The sweep was run against the count-weighted mean, which averages UEs
# measured over different windows and mixes served UEs with idle ones. Once
# the estimator was corrected and the same sweep was run against five real
# engine runs outside the original scenario family, the answer moved to zero:
#
#     loss dB   64QAM share B/A   bytes/blk   block errors B/A
#         0.0              1.03        1.13               0.94
#         2.0              0.93        1.10               1.00
#         6.0              0.65        1.08               1.10
#
# The constant was not absorbing a property of the antenna. It was absorbing
# a mistake in how the engine was being read. A fitted number that vanishes
# when the measurement is fixed was never physics, and the way to find out is
# to re-fit it every time the measurement changes.
BEAMFORMING_LOSS_DB = 0.0


def array_gain_db(rows: int, cols: int) -> float:
    """Coherent gain of a UniformPlanarArray steered at the UE it serves.

    The engine gives every gNB a planar array and points it at the served UE
    each slot, so the serving link gets the whole panel: 10*log10(rows*cols),
    18 dB on the default 8x8.

    It goes on the SERVING link only. An interferer's beam is aimed at its own
    UE, and an array moves power rather than making it, so its average gain in
    an unrelated direction is 0 dB. That asymmetry is the entire reason a dense
    network still reaches 64QAM — apply it to every link and it cancels out.

    Omitting it is not a rounding error: without it the six-cell reference sits
    near 0 dB SINR and puts 100% of its blocks on QPSK, where the engine
    measures 92% on 64QAM.

    `BEAMFORMING_LOSS_DB` comes off the ideal. 10*log10(N) is what a panel
    achieves against a single plane wave; the engine beamforms over a 3GPP
    channel matrix with several clusters, and a beam matched to one of them
    does not collect all of the others. This is the one MEASURED constant in
    the model, not a derived one — see the comment on it.
    """
    return (10 * math.log10(max(1, int(rows or 1) * int(cols or 1)))
            - BEAMFORMING_LOSS_DB)


# TR 38.901 Table 7.3-1, as `ThreeGppAntennaModel` implements it. These are
# ns-3's defaults for the model the engine is pointed at when
# `settings.antenna_element = '3gpp'`; the beamwidths and the boresight gain
# are config fields and arrive per cell, the two attenuation caps are not
# exposed and are the model's own.
ELEMENT_MAX_ATTENUATION_DB = 30.0   # m_maxAttenuation
ELEMENT_SLA_V_DB = 30.0             # m_slaV
# ...and these two are NOT config fields, whatever the schema offers.
# `schema.TIER_SCOPED` marks antenna.horizontal/vertical_beamwidth_deg and
# antenna.gain_dbi as analytical-tier only, in its own words: "The 3GPP
# element pattern compiles 65 deg in; the engine exposes no knob." Tier B's
# contract is parity with the engine, so it takes the compiled-in numbers and
# the config's are ignored here exactly as they are there. Reading them
# instead is not a harmless generalisation: the schema's default vertical
# beamwidth is 7.5 deg, which saturates SLA_V on any UE near its own mast and
# flattens every sector to the same -15 dB.
ELEMENT_HPBW_DEG = 65.0
ELEMENT_GAIN_DBI = 8.0


def _dirichlet(n: int, x: float) -> float:
    """|sum_{k=0}^{n-1} e^{jkx}| = |sin(nx/2) / sin(x/2)|, and n at x = 0."""
    h = 0.5 * x
    sh = math.sin(h)
    if abs(sh) < 1e-12:
        # Also the grating lobes: x a multiple of 2*pi puts every element
        # back in phase and the array is at full gain in that direction too.
        return float(n)
    return abs(math.sin(n * h) / sh)


def upa_gain_db(rows: int, cols: int, d_h: float, d_v: float,
                phi_deg: float, theta_deg: float,
                phi_s_deg: float, theta_s_deg: float) -> float:
    """A UPA STEERED AT ONE DIRECTION, EVALUATED TOWARD ANOTHER.

    `array_gain_db` answers the serving case — steered at the UE it is
    serving, so the whole panel, 10log10(rows*cols). This answers the
    INTERFERING case, which the model had no expression for at all and
    approximated as a flat 0 dB: an interferer's beam points at its own UE,
    and an array moves power rather than making it, so the AVERAGE gain in
    an unrelated direction is 0 dB.

    The average is not the quantity that matters. The engine's own
    per-transmission trace says a victim's SINR is a STEP FUNCTION, tens of
    dB between steps, and what steps it is WHICH UE the neighbour is
    serving — up to 50.6 dB, against 0.020 dB between consecutive samples.
    A flat average of that is the mean of a bimodal distribution, which is
    a value the link never takes.

    Exactly as ns-3 builds it (`PhasedArrayModel::GetBeamformingVector` and
    `UniformPlanarArray::GetElementLocation`): element (m, n) sits at
    `(0, d_h*n, d_v*m)` wavelengths in the panel's own frame, the steering
    vector's phase is `-2*pi*(sin(t)cos(p)*x + sin(t)sin(p)*y + cos(t)*z)`,
    and the beamforming vector is its conjugate over sqrt(N). The gain is
    then `|sum_i conj(a_i(steer)) a_i(toward)|^2 / N`, and because x is 0
    the two axes SEPARATE into a product of Dirichlet kernels:

        u = sin(theta) sin(phi)      (horizontal, along the columns)
        v = cos(theta)               (vertical, along the rows)
        G = D(cols, 2*pi*d_h*du)^2 * D(rows, 2*pi*d_v*dv)^2 / (rows*cols)

    At the steering direction that is `rows*cols` exactly, so this reduces
    to `array_gain_db` and does not need a separate case.

    THIS IS WHERE THE ELEMENT SPACING BECOMES REAL. `h_spacing_wl` and
    `v_spacing_wl` set the null positions and the grating lobes; they do
    not change the peak, which is why they were unreachable while the only
    array term was the peak.
    """
    n = max(1, int(rows or 1)) * max(1, int(cols or 1))
    t, p = math.radians(theta_deg), math.radians(phi_deg)
    ts, ps = math.radians(theta_s_deg), math.radians(phi_s_deg)
    du = math.sin(t) * math.sin(p) - math.sin(ts) * math.sin(ps)
    dv = math.cos(t) - math.cos(ts)
    h = _dirichlet(max(1, int(cols or 1)), 2 * math.pi * d_h * du)
    v = _dirichlet(max(1, int(rows or 1)), 2 * math.pi * d_v * dv)
    g = (h * h) * (v * v) / n
    return 10 * math.log10(max(g, 1e-12))


def element_gain_db(phi_deg: float, theta_deg: float,
                    h_beamwidth_deg: float = ELEMENT_HPBW_DEG,
                    v_beamwidth_deg: float = ELEMENT_HPBW_DEG,
                    boresight_gain_db: float = ELEMENT_GAIN_DBI) -> float:
    """One ARRAY ELEMENT's gain toward (phi, theta), in the panel's own frame.

    `ThreeGppAntennaModel::GetGainDb`, verbatim:

        vertGain  = -min(slaV, 12 * ((thetaDeg - 90) / vBeamwidth)^2)
        horizGain = -min(aMax, 12 * (phiDeg / hBeamwidth)^2)
        gain      = elementGain - min(aMax, -(vertGain + horizGain))

    `phi` is the azimuth off boresight in (-180, 180] and `theta` the
    inclination from zenith, so 90 is the horizon and boresight after downtilt.

    THIS IS WHY AZIMUTH AND TILT ARE REAL AT ALL. `array_gain_db` is the
    coherent gain of the lattice, and rotating a lattice of ISOTROPIC elements
    cancels out exactly — which is what the engine does by default and what
    made orientation look like a dead parameter for months. Swap the element
    for this one and the panel gates gain: 30 dB of front-to-back, and ~8 dB
    of boresight gain that makes the two element settings incomparable rather
    than better and worse.

    Tier B ignored the element entirely until now, so every config running
    `antenna_element = 3gpp` was modelled with the wrong antenna. It is also
    what a SECTORISED site needs: three cells on one mast are the same point
    in space and only the pattern tells them apart.
    """
    phi = (phi_deg + 180.0) % 360.0 - 180.0
    vert = -min(ELEMENT_SLA_V_DB,
                12.0 * ((theta_deg - 90.0) / max(1e-6, v_beamwidth_deg)) ** 2)
    horiz = -min(ELEMENT_MAX_ATTENUATION_DB,
                 12.0 * (phi / max(1e-6, h_beamwidth_deg)) ** 2)
    return boresight_gain_db - min(ELEMENT_MAX_ATTENUATION_DB, -(vert + horiz))


@dataclass
class Knobs:
    """The config levers a WINDOW MODEL can honour, read off the payload.

    These were all reaching ns-3 and none of them reached this tier, so a
    config that halved the RLC buffer, pinned the MCS, moved the AMC's BER
    target or restricted the scheduler to one direction changed the slow
    tier and left the fast one exactly as it was. Measured, 50 of the 165
    config fields were in that state; this is the subset a model with no
    clock and no channel matrix can actually express.

    What is NOT here is the other 67: per-TTI timers, the RRC state
    machine, backhaul transport, the channel solver. Those name mechanisms
    that only exist when you step time, and a default that quietly ignored
    them would be worse than the field being absent.
    """
    rlc_buffer_bytes: float = RLC_BUFFER_BYTES
    ber: float = 1e-5
    # 'shannon' or 'error_model'. The schema's own text says `amc_ber` is
    # read "when amc_model is 'shannon'"; this module applied it whatever the
    # model said, so a config asking for the error model still had its MCS
    # moved by a field that does not apply to it.
    amc_shannon: bool = True
    fixed_mcs_dl: int | None = None
    fixed_mcs_ul: int | None = None
    outage_db: float | None = None
    dl_only: bool = False
    ul_only: bool = False
    # Fixed-TTI quantisation. The scheduler rounds every per-flow symbol
    # count UP to a multiple of `sym_per_slot` — but ONLY inside
    # `if (m_fixedTti)`, four times in `AllocateSymbols`. So the two are one
    # lever, not two, and with the shipped default (flex TTI) `sym_per_slot`
    # does nothing on EITHER tier. Modelling one without the other would
    # invent an effect the engine does not have.
    fixed_tti: bool = False
    sym_per_slot: int = 6

    @classmethod
    def of(cls, params: dict | None) -> "Knobs":
        p = params or {}

        def _f(key, default):
            try:
                v = p.get(key)
                return default if v is None else float(v)
            except (TypeError, ValueError):
                return default

        def _i(key):
            v = p.get(key)
            if v is None or v == "":
                return None
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return None

        return cls(
            # MEGABYTES. The floor is 0.1 MB, not 1e3 — a byte-sized floor
            # applied to a megabyte field turned a 1 MB buffer into 1,048 MB
            # and the cap stopped binding at any setting. The measurement
            # caught it because the queue pinned at the same 1,048 MB for
            # 1, 10 and 40.
            rlc_buffer_bytes=max(0.1, _f("rlc_buffer_mb", 10.0)) * 1024 * 1024,
            # `--ns3::MmWaveAmc::Ber`. It sets the SNR gap the AMC works
            # against: gamma = -ln(5*BER)/1.5, 8.2 dB at the shipped 1e-5.
            ber=min(0.5, max(1e-9, _f("amc_ber", 1e-5)))
            if str(p.get("amc_model", "ShannonModel")) == "ShannonModel"
            else 1e-5,
            amc_shannon=str(p.get("amc_model", "ShannonModel")) == "ShannonModel",
            fixed_mcs_dl=_i("fixed_mcs_dl"),
            fixed_mcs_ul=_i("fixed_mcs_ul"),
            # A link below this is out, whatever the MCS table says. The
            # engine applies the network-side threshold and the UE's own;
            # the stricter of the two is what a UE actually survives.
            outage_db=max(_f("outage_db", -1e9), _f("ue_outage_db", -1e9))
            if (p.get("outage_db") is not None or p.get("ue_outage_db") is not None)
            else None,
            dl_only=str(p.get("sched_dl_only", "0")) in ("1", "True", "true"),
            ul_only=str(p.get("sched_ul_only", "0")) in ("1", "True", "true"),
            fixed_tti=str(p.get("fixed_tti", "0")) in ("1", "True", "true"),
            sym_per_slot=max(1, min(14, _i("sym_per_slot") or 6)),
        )


DEFAULT_KNOBS = Knobs()


def slot_period_s(numerology: int) -> float:
    return 1e-3 / (2 ** numerology)


def engine_numerology(config_numerology: int) -> int:
    """What the engine WILL run, not what the config asked for. See trap 1."""
    return 3 if int(config_numerology or 2) >= 3 else 2


def rb_count(bandwidth_mhz: float, numerology: int) -> int:
    rb_width_hz = SUBCARRIERS_PER_RB * 15_000 * (2 ** numerology)
    return max(1, math.ceil(bandwidth_mhz * 1e6 / rb_width_hz))


def _payload_bytes(mcs: int, n_sym: float, n_rb: int) -> float:
    """`MmWaveLteMiErrorModel::GetPayloadSize` — before the CRC comes off."""
    if mcs < 0:
        return 0.0
    return USEFUL_SC * n_sym * n_rb * QM_FOR_MCS[mcs] * ECR_FOR_MCS[mcs] / 8.0


def tb_bytes(mcs: int, n_sym: float, n_rb: int) -> float:
    """`MmWaveAmc::CalculateTbSize` for a fractional symbol allocation.

    The engine's version takes an integer symbol count because a scheduler
    hands out whole symbols; a window model holds a UE's AVERAGE share over
    4000 slots, which is fractional. Code-block segmentation is not modelled —
    it removes 3 bytes per code block, under 0.3% at these sizes.
    """
    if mcs < 0:
        return 0.0
    return max(0.0, _payload_bytes(mcs, n_sym, n_rb) - CRC_BYTES)


def required_sinr_db(mcs: int, ber: float = 1e-5) -> float:
    """The SINR at which the engine's AMC would just reach this MCS.

    The inverse of `se_from_sinr`: se = log2(1 + SNR/gap), so
    SNR = gap * (2^se - 1). The margin above it is what decides the error rate.
    """
    from mcs import snr_gap
    if mcs < 0:
        return float("-inf")
    return 10 * math.log10(snr_gap(ber) * (2 ** SE_FOR_MCS[mcs] - 1))


def shannon_threshold_db(mcs: int) -> float:
    """The SINR at which this MCS's efficiency is exactly achievable.

    `required_sinr_db` answers a different question — the SINR at which the
    AMC would SELECT this MCS, which carries the 8.2 dB gap. The gap is the
    margin; the threshold is where decoding actually fails.
    """
    if mcs < 0:
        return float("inf")
    return 10 * math.log10(2 ** SE_FOR_MCS[mcs] - 1)


def _phi(z: float) -> float:
    """Standard normal CDF, via erf."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


_GH_X, _GH_W = None, None


def _gauss_hermite(n: int = 9):
    """Nodes for averaging over the fading, computed once."""
    global _GH_X, _GH_W
    if _GH_X is None:
        import numpy as np
        _GH_X, _GH_W = np.polynomial.hermite.hermgauss(n)
    return _GH_X, _GH_W


_MCS_CACHE: dict = {}


def pick_mcs(sinr_db: float, n_rb: int,
             knobs: "Knobs | None" = None) -> int:
    """The MCS the engine's AMC would choose, or -1 for outage.

    THE DECISION IS MADE FOR A 12-SYMBOL BLOCK whatever the UE will be
    allocated — `MmWaveAmc::m_numSymForCqi`, a compile-time constant citing
    TS 38.214 5.2.2.5 — so this asks about that block and not the one that
    will be sent. On the uplink, where a UE holds about 0.4 symbols, that is
    an MCS chosen for a block thirty times the size.

    Cached on the SINR rounded to a tenth of a dB: the walk costs up to 29
    error-model evaluations and the answer is a step function, so a tenth of
    a dB is finer than the steps are.
    """
    ber = (knobs or DEFAULT_KNOBS).ber
    key = (round(sinr_db, 1), n_rb, ber)
    got = _MCS_CACHE.get(key)
    if got is None:
        if USE_ENGINE_AMC:
            got = mi.select_mcs(
                sinr_db, lambda k: tb_bytes(k, mi.NUM_SYM_FOR_CQI, n_rb))
        else:
            # `--ns3::MmWaveAmc::Ber` sets the gap the AMC works against;
            # this module hardcoded the shipped 1e-5 and ignored the field.
            l = mcs_link(sinr_db, ber)
            got = -1 if l["in_outage"] else l["mcs"]
        _MCS_CACHE[key] = got
    return got


def pick_mcs_engine(sinr_db: float, n_rb: int) -> int:
    """The engine's AMC walk, whatever `USE_ENGINE_AMC` is set to.

    `pick_mcs` honours the switch because the model has to; a tool measuring
    the engine's behaviour must not, or it would measure the switch.
    """
    key = ("engine", round(sinr_db, 1), n_rb)
    got = _MCS_CACHE.get(key)
    if got is None:
        got = mi.select_mcs(sinr_db,
                            lambda k: tb_bytes(k, mi.NUM_SYM_FOR_CQI, n_rb))
        _MCS_CACHE[key] = got
    return got


def engine_bler(sinr_db: float, mcs: int, tb_bytes_: float,
                sigma_db: float = FADING_SIGMA_DB) -> float:
    """The engine's error model, averaged over the fading Tier B derives.

    The engine decodes each block at the INSTANTANEOUS SINR and this
    computes a window, so what a window needs is the expectation over the
    fading distribution. Evaluating the MI model at the mean SINR instead
    gives essentially zero — the AMC picked the MCS so that a 12-symbol
    block just holds 10%, and the block actually sent is smaller — which is
    the same reason the old heuristic needed a spread in the first place.

    So the port replaces the SHAPE of the error curve, not the need for a
    spread: `FADING_SIGMA_DB` is still an input and still the open item its
    own note describes. What changes is that the curve is now the engine's
    erfc in mutual-information space, with the right code rate and the right
    code-block segmentation, instead of a Gaussian in dB about a Shannon
    threshold.
    """
    if mcs < 0:
        return 1.0
    x, w = _gauss_hermite()
    root2sig = math.sqrt(2.0) * max(sigma_db, 1e-6)
    tot = 0.0
    for xi, wi in zip(x, w):
        s = sinr_db + root2sig * xi
        tot += wi * mi.tb_bler(10 ** (s / 10.0), mcs, tb_bytes_)
    return min(1.0, max(0.0, tot / math.sqrt(math.pi)))


def bler_for(sinr_db: float, mcs: int,
             sigma_db: float = FADING_SIGMA_DB) -> float:
    """The chance the channel is below the chosen MCS when the block is sent.

    The scheduler picks an MCS from an averaged SINR and the block goes out
    into an instantaneous one. Modelling the instantaneous value as normal in
    dB around the average — which is what Rayleigh fading gives, and what the
    engine's own per-transmission histogram shows — makes the error rate the
    probability of landing under the threshold, rather than a number chosen
    to match the AMC's target.

    A link with no viable MCS is a total loss.
    """
    if mcs < 0:
        return 1.0
    margin = sinr_db - shannon_threshold_db(mcs)
    return min(1.0, max(0.0, _phi(-margin / max(sigma_db, 1e-6))))


@dataclass
class UeWindow:
    """One UE's counters for one KPM window, named as the engine names them."""
    imsi: int
    cell: int
    sinr_db: float
    mcs: int
    symbol_share: float               # fraction of the cell's symbols
    tb_total: int
    tb_error: int
    prb_used: float                   # the engine's 139-scaled figure
    # ...and the resource blocks actually occupied. `RRU.PrbUsedDl` is NOT a
    # PRB count (trap 2) and carrying only it makes any comparison against a
    # simulator that reports real PRBs a comparison of two different
    # quantities. This is the one to compare; that one is the one to match.
    prb_used_rb: float
    volume_bytes: float
    bler: float
    queued_bytes: float = 0.0         # still waiting when the window closed
    window_s: float = 1.0
    # ── the other direction ──────────────────────────────────────────────
    # `mcs_ul < 0` means no uplink was computed for this UE (no uplink SINR
    # was supplied) OR the link is in outage. `has_ul` tells those apart, so
    # a report can say "not modelled" instead of "zero", which is the
    # distinction the whole KPM health layer exists to preserve.
    has_ul: bool = False
    sinr_ul_db: float = 0.0
    mcs_ul: int = -1
    tb_ul: int = 0
    tb_ul_error: int = 0
    prb_used_ul: float = 0.0          # the engine's 139-scaled figure
    prb_used_ul_rb: float = 0.0
    symbol_share_ul: float = 0.0      # fraction of the cell's UL data symbols
    volume_ul_bytes: float = 0.0
    queued_ul_bytes: float = 0.0

    def measurements(self) -> dict[str, float]:
        qm = QM_FOR_MCS[self.mcs] if self.mcs >= 0 else 0
        initial = self.tb_total - self.tb_error
        ul = {} if not self.has_ul else {
            # Tier A reports exactly these two (plus a delay this model does
            # not produce): `mmwave-enb-net-device.cc` divides
            # `GetUlRxData(imsi, 3) * 8 / 1e3` by the E2 periodicity. Volume
            # is kbit over the window, bitrate is kbit/s, and they coincide
            # at a one-second window — which is why a check that they agree
            # proves nothing at the default period.
            "DRB.PdcpSduVolumeUl": self.volume_ul_bytes * 8 / 1e3,
            "DRB.PdcpSduBitRateUl": (self.volume_ul_bytes * 8 / 1e3
                                     / max(self.window_s, 1e-9)),
            # These three Tier A does NOT report and VIAVI's captures do, so
            # they exist to be compared outward rather than inward.
            "RRU.PrbUsedUl": self.prb_used_ul,
            "TB.TotNbrUl": float(self.tb_ul),
            "TB.ErrTotalNbrUl": float(self.tb_ul_error),
        }
        return {**ul,
            "RRU.PrbUsedDl": self.prb_used,
            "TB.TotNbrDl.1": float(self.tb_total),
            "TB.ErrTotalNbrDl.1": float(self.tb_error),
            "TB.TotNbrDlInitial": float(initial),
            "TB.TotNbrDlInitial.Qpsk": float(initial) if qm == 2 else 0.0,
            "TB.TotNbrDlInitial.16Qam": float(initial) if qm == 4 else 0.0,
            "TB.TotNbrDlInitial.64Qam": float(initial) if qm == 6 else 0.0,
            "QosFlow.PdcpPduVolumeDL": self.volume_bytes,
            # What did not fit. A RAN that never queues is a RAN with no load
            # problem to solve, which is the one thing a RIC is for.
            "DRB.BufferSize.Qos": self.queued_bytes,
            # Delivered rate over the window, in kbit/s — the volume figure,
            # NOT the engine's `DRB.UEThpDl`, which divides by a latency.
            "DRB.PdcpSduBitRateDl": self.volume_bytes * 8 / 1e3 / max(self.window_s, 1e-9),
        }


# ── RU POWER ────────────────────────────────────────────────────────────
#
# Read off `MmWaveRadioEnergyModelEnb` in the engine's own tree rather than
# assumed: energy is `duration * GetStateA(state) * supplyVoltage`, the
# scenario sets `BasicEnergySupplyVoltageV` to 5.0, and the per-state
# currents are the attribute defaults below. So a transmitting cell draws
# 742.2 A * 5 V = 3.7 kW and an idle one 431 W — the kW scale the slow tier
# reports is the model, not a bug.
#
# Tier B had NO energy model at all: `ric_sim` reported the RADIATED power
# (10 W at 40 dBm), so the fast tier's energy card and the slow tier's
# disagreed by two orders of magnitude and every `ru_*_current_a`, every
# sleep depth and `cell_off` itself did nothing on this tier.
SUPPLY_V = 5.0
RU_TX_A = 742.2
RU_RX_A = 138.9
RU_IDLE_A = 86.3                       # "DeepSleepA" upstream: active-idle
SLEEP_A = {"micro": 59.5, "light": 43.2, "deep": 28.5}


@dataclass
class RuPower:
    """The per-cell power profile, in Amperes at `SUPPLY_V`."""
    tx_a: float = RU_TX_A
    rx_a: float = RU_RX_A
    idle_a: float = RU_IDLE_A
    sleep_a: float | None = None       # None = the cell does not sleep
    idle_timer_s: float = 0.2

    @classmethod
    def of(cls, params: dict | None, cell: int) -> "RuPower":
        p = params or {}
        def _f(k, d):
            try:
                v = p.get(k)
                return d if v is None else float(v)
            except (TypeError, ValueError):
                return d
        # `gnb_sleep` is "depth,idleMs,wakeMs" per cell, the same string the
        # engine parses — empty when no cell asked to sleep.
        depth, idle_ms = 0, 200.0
        spec = str(p.get("gnb_sleep") or "")
        if spec:
            fields = spec.split(";")
            if cell < len(fields):
                parts = fields[cell].split(",")
                try:
                    depth = int(parts[0])
                    idle_ms = float(parts[1]) if len(parts) > 1 else 200.0
                except (TypeError, ValueError):
                    depth = 0
        # THE WIRE FORMAT, not the ns-3 enum. `_sleep_params` sends the
        # config's ordinal 1/2/3 and the engine's scenario translates that
        # into STATE_SLEEP_MICRO/LIGHT/DEEP = 5/6/7. Reading the enum here
        # meant no real config ever matched — and a hand test that passed
        # "7,..." straight in agreed with the bug.
        name = {1: "micro", 2: "light", 3: "deep"}.get(depth)
        sleep_a = None
        if name:
            sleep_a = _f(f"sleep_{name}_a", SLEEP_A[name])
        return cls(tx_a=_f("ru_tx_a", RU_TX_A),
                   rx_a=_f("ru_rx_a", RU_RX_A),
                   idle_a=_f("ru_idle_a", RU_IDLE_A),
                   sleep_a=sleep_a,
                   idle_timer_s=max(0.0, idle_ms) / 1e3)


def cell_power_w(cw: "CellWindow", ru: RuPower, window_s: float = 1.0,
                 off: bool = False, idle_for_s: float = 0.0) -> float:
    """Mean RU draw over the window, in Watts.

    The engine charges a STATE per slot, so the window's draw is the time
    spent in each. A slot is `DL_CTRL_SYMBOLS` of PDCCH the cell transmits
    whether or not it has data, `UL_CTRL_SYMBOLS` it listens through, and
    `DATA_SYMBOLS_PER_SLOT` shared out — the symbols this window actually
    allocated are already on each UE as `symbol_share`.

    SLEEP IS NOT A DISCOUNT ON A BUSY CELL. The idle gaps inside a loaded
    slot are microseconds and the idle timer is hundreds of milliseconds, so
    only a cell that served nothing all window accumulates a stretch long
    enough to enter a sleep state — which is exactly the "sleep only helps
    cells with no UEs" that was measured on Tier A and is here a consequence
    of the timer rather than an assumption.

    `idle_for_s` IS HOW LONG IT HAD ALREADY BEEN IDLE when the window
    opened, and without it the timer restarts every window. A cell with a
    1000 ms idle timer on a 1 s window then never sleeps at all: the
    "Site-C / N28 (South Coverage — Deep Sleep)" cell in the energy-saving
    scenario drew 684 W — the IDLE figure — for the whole run, on the one
    cell the scenario exists to put to sleep. A timer measures a stretch of
    idleness, and a stretch does not end because a reporting window did.
    """
    if off:
        return 0.0                     # STATE_OFF draws m_celloff = 0 A
    dl = sum(u.symbol_share for u in cw.ues) * DATA_SYMBOLS_PER_SLOT
    ul = sum(u.symbol_share_ul for u in cw.ues) * DATA_SYMBOLS_PER_SLOT
    tx_f = (DL_CTRL_SYMBOLS + min(dl, DL_SYMBOLS_PER_SLOT)) / SYMBOLS_PER_SLOT
    rx_f = (UL_CTRL_SYMBOLS + min(ul, UL_SYMBOLS_PER_SLOT)) / SYMBOLS_PER_SLOT
    idle_f = max(0.0, 1.0 - tx_f - rx_f)
    sleep_f = 0.0
    if ru.sleep_a is not None and dl <= 0 and ul <= 0 and window_s > 0:
        # The timer expires `idle_timer_s` after the idleness STARTED, which
        # may be several windows ago.
        remaining = max(0.0, ru.idle_timer_s - max(0.0, idle_for_s))
        sleep_f = max(0.0, (window_s - remaining) / window_s)
        sleep_f = min(sleep_f, idle_f)
    amps = (tx_f * ru.tx_a + rx_f * ru.rx_a
            + (idle_f - sleep_f) * ru.idle_a + sleep_f * ru.sleep_a
            if sleep_f else
            tx_f * ru.tx_a + rx_f * ru.rx_a + idle_f * ru.idle_a)
    return amps * SUPPLY_V


def backhaul_cap_bytes(params: dict | None, window_s: float) -> float:
    """What the S1-U link can carry in one window, in bytes.

    A cell's air interface is not the only thing between a UE and its data.
    The engine builds the S1-U as a real point-to-point link at
    `s1u_link_gbps` with `s1u_link_delay_ms`, and a cell scheduling more than
    the backhaul carries queues at the SGW instead of the RLC. Tier B had no
    transport at all, so every one of those fields was slow-only and a
    scenario testing a constrained fronthaul — an ordinary thing to test —
    ran as if the link were infinite.

    The delay is NOT modelled: it is a latency, and this tier reports
    volumes, not latencies.
    """
    try:
        gbps = float((params or {}).get("s1u_gbps") or 0.0)
    except (TypeError, ValueError):
        return float("inf")
    if gbps <= 0:
        return float("inf")
    return gbps * 1e9 / 8.0 * max(window_s, 1e-9)


@dataclass
class CellWindow:
    cell: int
    ues: list[UeWindow] = field(default_factory=list)


def cell_window(
    cell_id: int,
    ue_sinr_db: dict[int, float],
    ue_demand_bps: dict[int, float],
    bandwidth_mhz: float,
    numerology: int,
    window_s: float = 1.0,
    decay_db: float | None = FADING_SIGMA_DB,
    engine_clamp: bool = True,
    backlog_bytes: dict | None = None,
    rlc_buffer_bytes: float = RLC_BUFFER_BYTES,
    ue_slice: dict | None = None,
    slice_quota: dict | None = None,
    slice_floor: dict | None = None,
    slice_dedicated: dict | None = None,
    availability: dict | None = None,
    ue_sinr_ul_db: dict | None = None,
    ul_backlog_bytes: dict | None = None,
    knobs: "Knobs | None" = None,
) -> CellWindow:
    """One cell's KPM window: share the symbols, then count what came out.

    `slice_quota` caps each slice's share of the cell's TURNS, keyed the way
    `ue_slice` tags the UEs. That is where the engine applies it — the
    scheduler scales `m_maxDlSymbols` per slice before it shares anything out,
    so a capped slice loses AIRTIME. Scaling each UE's delivered bytes
    afterwards instead, which is what this model did until now, leaves the
    airtime with the capped slice and the bytes with nobody: measured, it put
    the block count at 2.98x the engine on the one breadth case with a policy.
    The engine's own comment calls an over-subscribed policy one where nothing
    is left over, so a slice under its cap does not hand the remainder on.

    `backlog_bytes` is what each UE still had waiting when the LAST window
    closed. Without it every window starts empty and the tier can never report
    a queue, never show congestion building, and never let a load-balancing
    xApp see the thing it is supposed to act on. With it the demand for this
    window is the backlog plus this window's arrivals, and what does not fit
    rolls forward.

    The mmWave scheduler is TDMA in symbols — a UE gets whole symbols across
    the full carrier, not a slice of the band — so the shared resource is
    `slots * 14` symbols and a UE's allocation is a symbol share. UEs that can
    be served with less than an equal share (their application does not ask for
    more) release the rest, and what they release is offered to the UEs that
    are still short, once. A UE with no viable MCS is in outage and takes
    nothing.
    """
    # `engine_clamp` is what makes this a REPRODUCTION of Tier A rather than a
    # model of NR. ns-3's mmWave stack asserts on any numerology but 2 or 3
    # (see trap 1), so reproducing its counters means running mu=2 whatever the
    # config says. A caller with a carrier Tier A cannot express — a per-cell
    # band, an FR1 15 or 30 kHz carrier — turns the clamp off and gets the
    # numerology it asked for, because there is no ns-3 run to agree with.
    knobs = knobs or DEFAULT_KNOBS
    if decay_db is None:
        decay_db = FADING_SIGMA_DB
    rlc_buffer_bytes = knobs.rlc_buffer_bytes
    mu = engine_numerology(numerology) if engine_clamp else max(0, int(numerology or 0))
    n_rb = rb_count(bandwidth_mhz, mu)
    slots = max(1, round(window_s / slot_period_s(mu)))

    viable = {}
    for imsi, sinr in ue_sinr_db.items():
        if knobs.ul_only:
            break
        m = knobs.fixed_mcs_dl if knobs.fixed_mcs_dl is not None else pick_mcs(
            sinr, n_rb, knobs)
        if m >= 0 and not (knobs.outage_db is not None and sinr < knobs.outage_db):
            viable[imsi] = m

    out = CellWindow(cell=cell_id)
    if not viable:
        # Nobody has a downlink, so nobody holds a slot, so nobody has an
        # uplink either — but the counters still have to EXIST, or a cell in
        # total outage would be indistinguishable from a cell whose uplink
        # was never modelled.
        dead = _uplink_window(None if knobs.dl_only else ue_sinr_ul_db,
                              {}, slots, n_rb, 1,
                              ue_demand_bps, window_s, decay_db,
                              ul_backlog_bytes or {}, rlc_buffer_bytes, knobs)
        for imsi, sinr in ue_sinr_db.items():
            held = (backlog_bytes or {}).get(imsi, 0.0)
            out.ues.append(UeWindow(imsi, cell_id, sinr, -1, 0.0, 0, 0, 0.0, 0.0,
                                    0.0, 1.0, held, window_s,
                                    **dead.get(imsi, {})))
        return out

    # Symbols each UE would need to clear its demand at its own MCS.
    need, want_bytes = {}, {}
    backlog = backlog_bytes or {}
    for imsi, mcs in viable.items():
        per_symbol = tb_bytes(mcs, 1.0, n_rb) * 8.0
        rate = ue_demand_bps.get(imsi, float("inf"))
        if rate == float("inf"):
            want_bits = float("inf")
            want_bytes[imsi] = float("inf")
        else:
            want_bits = rate * window_s + backlog.get(imsi, 0.0) * 8.0
            want_bytes[imsi] = want_bits / 8.0
        need[imsi] = (want_bits / per_symbol) if per_symbol > 0 else 0.0

    # THE SHARED RESOURCE IS TURNS, NOT SYMBOLS. The scheduler's even split is
    # floored at one symbol per flow, so once the flows outnumber the symbols
    # the surplus flows are not given a thinner slice — they are given nothing
    # and the round-robin pointer moves on. A cell therefore hands out
    # `slots * served_per_slot` turns per window, each worth `sym_per_flow`
    # symbols and carrying one transport block, and no UE can take more than
    # one turn in a slot.
    #
    # Sharing SYMBOLS instead of turns is a mistake that hides the ceiling
    # completely: symbols a demand-limited UE leaves behind get handed to a UE
    # that is already scheduled as often as it can be, and the model climbs
    # back to a full 4000 blocks. Measured, that bug put the reference at 4.04x
    # the engine and made the attachment skew look irrelevant.
    n_viable = len(viable)
    # THE SLOT SHARE IS SIZED BY EVERY VIABLE UE, not by how many are
    # reachable at once — MEASURED, and the other way round is worse.
    # Sizing it by the reachable count concentrates the airtime on fewer
    # UEs at bigger blocks, which is what the engine's own reference looks
    # like (three of cell 4's seven UEs get a block every slot) and which
    # takes delivered bytes to 1.92x the engine against 1.41x here, and
    # spectrum to 1.87x against 1.38x. Tier B's cells are already busier
    # than the engine's — its reference cells run 44.7-98.8% — so
    # concentrating them further over-delivers.
    #
    # The cost of leaving it is that the cell is UNDER-subscribed once
    # availability caps each UE, so a slice policy has nothing to bind on
    # at six UEs a cell. That is the coupling to resolve next, not a
    # reason to take the worse of two measured options.
    served_per_slot = min(n_viable, MAX_UES_PER_SLOT)
    # The downlink share, split among the UEs actually holding a turn.
    sym_per_flow = max(1.0, DL_SYMBOLS_PER_SLOT / served_per_slot)
    if knobs.fixed_tti:
        # `ceil(nSymPerFlow0 / m_symPerSlot) * m_symPerSlot`, the engine's own
        # line. A coarser TTI means fewer, fatter turns: at granularity 6 out
        # of 12 data symbols nobody gets a 2-symbol slice, so the cell serves
        # two UEs a slot instead of six. That is the whole point of the
        # setting and the fast tier ignored both halves of it.
        q = float(knobs.sym_per_slot)
        sym_per_flow = math.ceil(sym_per_flow / q) * q
        served_per_slot = max(1, min(served_per_slot,
                                     int(DL_SYMBOLS_PER_SLOT // sym_per_flow) or 1))
    turns_available = slots * served_per_slot

    # A UE CANNOT HOLD MORE SLOTS THAN IT IS REACHABLE IN. `availability`
    # is the share of the window its serving link is inside MaxLossDb —
    # the link flips LOS/NLOS every 100 ms and at typical geometry its
    # NLOS branch is often past the cut, so the UE is schedulable in its
    # LOS episodes and invisible in the others. Serving every UE for the
    # whole window is why this tier reported 36 UEs where the engine
    # reported 26.75, and why its minimum per-UE block count was three
    # quarters of its maximum where the engine's was zero.
    want_turns = {i: min(slots * max(0.0, min(1.0,
                                              (availability or {}).get(i, 1.0))),
                         need[i] / sym_per_flow)
                  for i in viable}

    # Group the UEs by slice, and give each group the airtime its policy
    # allows. With no policy there is one group holding everything, which is
    # the unsliced case unchanged.
    groups: dict[object, list] = {}
    for i in viable:
        groups.setdefault((ue_slice or {}).get(i), []).append(i)

    # `min_pct` RESERVES airtime before anything is shared, and only as much
    # of the floor as the slice can actually use — the engine's own comment.
    # What is left is `nsgBudget`, and it goes to the UEs with no floor of
    # their own. Over-subscribe the floors and there is nothing left over,
    # which is also its words. Modelling the cap alone left every unprotected
    # UE with the whole cell.
    # A FLOOR TRIMS THE OTHER SLICES, IT DOES NOT CAP ITS OWNER.
    #
    # This gave each protected slice a budget EQUAL to its floor, which
    # turns a guarantee into an allowance and — where every slice has one —
    # throws the rest of the cell away: two slices with 20% and 10% floors
    # and no unprotected traffic delivered 57 Mbps where the unsliced scene
    # delivers 159, and `max_pct` could not bind at any value because the
    # floor was already below it.
    #
    # The engine (`mmwave-flex-tti-mac-scheduler.cc`, "NSG slicing, second
    # pass") does the opposite: it reserves nothing and instead SCALES DOWN
    # what the unprotected UEs ask for, so the protected slice keeps
    # competing for everything and simply wins more of it. Its guard is
    # explicit — `if (nsgReserved > 0 && nsgUnprotDemand > nsgBudget)` —
    # so with no unprotected traffic the pass does nothing at all, and its
    # own comment records that getting this wrong moved throughput 69%.
    floors = {t: (slice_floor or {}).get(t, 0.0) for t in groups}
    deds = {t: (slice_dedicated or {}).get(t, 0.0) for t in groups}
    want_of = {t: sum(min(slots, want_turns[i]) for i in m) for t, m in groups.items()}
    cap_of = {}
    for t in groups:
        c = (slice_quota or {}).get(t)
        cap_of[t] = (turns_available if c is None
                     else turns_available * max(0.0, min(100.0, c)) / 100.0)
    protected = [t for t in groups if floors.get(t, 0.0) > 0 or deds.get(t, 0.0) > 0]
    unprotected = [t for t in groups if t not in protected]
    reserved = 0.0
    for t in protected:
        # min is work-conserving — only as much of the floor as the slice can
        # use. dedicated is not, and the two OVERLAP rather than stack: a 20%
        # dedicated slice with a 30% floor is guaranteed 30%, not 50%.
        mn = min(turns_available * floors.get(t, 0.0) / 100.0, want_of[t])
        reserved += mn
        ded = turns_available * deds.get(t, 0.0) / 100.0
        if ded > mn:
            reserved += ded - mn
    reserved = min(reserved, turns_available)   # over-subscribed: nothing left
    free_budget = turns_available - reserved
    unprot_demand = sum(want_of[t] for t in unprotected)
    scale = 1.0
    if reserved > 0 and unprot_demand > free_budget > 0:
        scale = free_budget / unprot_demand
    elif reserved > 0 and free_budget <= 0:
        scale = 0.0

    budget_of = {t: min(want_of[t] * (scale if t in unprotected else 1.0),
                        cap_of[t]) for t in groups}
    # The allocator's own ceiling: a cell cannot hand out more turns than it
    # has. Scaling EVERY group down to fit undoes the protection it just
    # applied — measured, a 60% floor trimmed the unprotected slices and
    # then handed the freed airtime back to nobody, because the protected
    # slice was scaled by the same factor. The shortfall comes off the
    # UNPROTECTED first, which is what a floor means.
    total_b = sum(budget_of.values())
    if total_b > turns_available > 0:
        over = total_b - turns_available
        pool = sum(budget_of[t] for t in unprotected)
        if pool >= over - 1e-9 and pool > 0:
            k = (pool - over) / pool
            budget_of = {t: (b * k if t in unprotected else b)
                         for t, b in budget_of.items()}
        else:
            k = turns_available / total_b
            budget_of = {t: b * k for t, b in budget_of.items()}

    # A CAPPED SLICE IS FILLED GREEDILY, NOT SHARED OUT.
    #
    # `mmwave-flex-tti-mac-scheduler.cc`, "NSG slicing: cap each slice's
    # total requested DL symbols": when a slice exceeds its ceiling the
    # engine hands the ceiling out UE BY UE from a rotating start —
    # `give = min(m_maxDlSymbols, nsgRemaining)` — and whoever the ceiling
    # runs out on gets ZERO that subframe. Its own comment says why it is
    # not proportional: "proportional floor-scaling can hand every UE 0
    # symbols when the ceiling is small".
    #
    # A backlogged UE asks for everything it needs to drain its buffer, so
    # the first UE takes the whole ceiling and the slice serves ONE UE a
    # subframe. The airtime is the same either way — it is the ceiling —
    # but it comes out as few big transport blocks instead of many small
    # ones, and the block COUNT is what the comparison reads. Measured on
    # the engine's own trace: under a policy its cells go from 98% busy at
    # 3.8 blocks per busy slot to 37-87% busy at about 1.2.
    turns: dict[int, float] = {}
    sym_of: dict[int, float] = {}
    for tag, members in groups.items():
        budget = budget_of[tag]
        cap = (slice_quota or {}).get(tag)
        capped = (cap is not None
                  and sum(want_turns[i] for i in members) > budget + 1e-9)
        if capped:
            # How many of its members the ceiling reaches in one subframe.
            ceil_sym = max(1e-9, (cap / 100.0) * DL_SYMBOLS_PER_SLOT)
            per_ue = min(DL_SYMBOLS_PER_SLOT,
                         max(sym_per_flow,
                             statistics.fmean(
                                 min(DL_SYMBOLS_PER_SLOT,
                                     need[i] / max(1.0, slots))
                                 for i in members)))
            m = max(1, int(ceil_sym // per_ue))
            share = slots * m / len(members)
            for i in members:
                turns[i] = min(want_turns[i], share)
                sym_of[i] = ceil_sym / m
            continue
        fair = budget / len(members)
        for i in members:
            turns[i] = min(want_turns[i], fair)
        spare = budget - sum(turns[i] for i in members)
        short = [i for i in members if want_turns[i] > turns[i]]
        if spare > 0 and short:
            extra = spare / len(short)
            for i in short:
                turns[i] = min(want_turns[i], turns[i] + extra, slots)
    grant = {i: turns[i] * sym_of.get(i, sym_per_flow) for i in viable}

    # ── the uplink, on the symbols the downlink split left ───────────────
    # Same turns, because it is the SAME round-robin: a UE holding a slot
    # holds it for both of its flows, and the deficit split gives the uplink
    # flow 2.4 of the 12 data symbols. What differs is the MCS, which comes
    # from the uplink SINR, and the demand, which is UL_DEMAND_RATIO of the
    # downlink's because that is the rate the scenario's uplink OnOff is
    # built at.
    ul = _uplink_window(None if knobs.dl_only else ue_sinr_ul_db,
                        turns, slots, n_rb, served_per_slot,
                        ue_demand_bps, window_s, decay_db,
                        ul_backlog_bytes or {}, rlc_buffer_bytes, knobs)

    for imsi, sinr in ue_sinr_db.items():
        mcs = viable.get(imsi, -1)
        sym = grant.get(imsi, 0.0)
        if mcs < 0 or sym <= 0:
            held = min(rlc_buffer_bytes, (backlog or {}).get(imsi, 0.0) + (
                0.0 if ue_demand_bps.get(imsi, 0.0) == float("inf")
                else ue_demand_bps.get(imsi, 0.0) * window_s / 8.0))
            out.ues.append(UeWindow(imsi, cell_id, sinr, -1, 0.0, 0, 0, 0.0, 0.0,
                                    0.0, 1.0, held, window_s,
                                    **ul.get(imsi, {})))
            continue
        # One transport block per slot is the ceiling, so a UE holding at
        # least one symbol per slot is scheduled in all of them and its blocks
        # grow instead of multiplying. Below that it gets a single symbol in
        # some slots and nothing in the rest, which is where a lightly loaded
        # cell reports a few hundred blocks instead of the full 4000.
        tb = max(1, min(slots, round(turns[imsi])))
        sym_per_tb = sym / tb
        bler = (engine_bler(sinr, mcs, tb_bytes(mcs, sym_per_tb, n_rb), decay_db)
                if USE_ENGINE_AMC else bler_for(sinr, mcs, decay_db))
        err = round(tb * bler)
        vol = tb_bytes(mcs, sym_per_tb, n_rb) * (tb - err)
        wanted = want_bytes.get(imsi, 0.0)
        # What does not fit waits, and what does not fit in the BUFFER is
        # dropped — an RLC queue is finite and the overflow never arrives.
        left = (0.0 if wanted == float("inf")
                else min(rlc_buffer_bytes, max(0.0, wanted - vol)))
        out.ues.append(UeWindow(
            imsi=imsi, cell=cell_id, sinr_db=sinr, mcs=mcs,
            symbol_share=sym / (slots * DATA_SYMBOLS_PER_SLOT),
            tb_total=tb, tb_error=err,
            prb_used=math.ceil(sym / (slots * SYMBOLS_PER_SLOT) * PRB_METRIC_SCALE),
            prb_used_rb=sym / (slots * DATA_SYMBOLS_PER_SLOT) * n_rb,
            volume_bytes=vol, bler=bler,
            queued_bytes=left, window_s=window_s,
            **ul.get(imsi, {}),
        ))
    return out


def _uplink_window(ue_sinr_ul_db: dict | None, turns: dict, slots: int,
                   n_rb: int, served_per_slot: int, ue_demand_bps: dict,
                   window_s: float, decay_db: float,
                   backlog: dict, rlc_buffer_bytes: float,
                   knobs: "Knobs | None" = None) -> dict:
    """Each UE's uplink counters, as kwargs for its `UeWindow`.

    Returns {} for every UE when no uplink SINR is supplied, so a caller that
    has not modelled the uplink reports NOTHING in that direction rather than
    zeros. A zero is a measurement and would be wrong; an absent counter is
    the truth, and `kpm_health` already distinguishes the two everywhere else.

    A UE only transmits in the slots it was granted, so `turns` is shared with
    the downlink rather than recomputed — the deficit split moves symbols
    between a UE's two flows, it does not give the uplink its own round-robin.
    """
    if not ue_sinr_ul_db:
        return {}
    knobs = knobs or DEFAULT_KNOBS
    # THE ONE-SYMBOL FLOOR IS NOT A LICENCE TO OVERSPEND THE BUDGET, and
    # copying it from the downlink was worth 2.5x of uplink throughput.
    #
    # The scheduler floors a scheduled flow at one symbol. On the downlink
    # that never bites — 9.6 symbols over six UEs is 1.6 each — so the
    # `max(1.0, ...)` in the downlink path is inert and looks harmless. On
    # the uplink the share is 2.4 over six, which is 0.4, and the floor
    # turned it into 1.0: six UEs holding one symbol each is SIX uplink
    # symbols a slot against a budget of 2.4, in a cell that only has twelve
    # data symbols to give in either direction.
    #
    # What the floor actually means is that FEWER FLOWS fit in a slot, not
    # that the slot grows. A flow gets one symbol and the rest of the cell's
    # uplink flows wait their turn, so each UE holds a turn only
    # `budget / served` of the time. That is a cap on TURNS, applied below,
    # and it leaves the symbols per turn at the floor where the floor binds.
    share = UL_SYMBOLS_PER_SLOT / max(1, served_per_slot)
    sym_per_flow = max(1.0, share)
    # Fraction of slots in which a given UE actually holds its uplink turn.
    turn_duty = min(1.0, share / sym_per_flow)
    out: dict[int, dict] = {}
    for imsi, sinr in ue_sinr_ul_db.items():
        mcs = (knobs.fixed_mcs_ul if knobs.fixed_mcs_ul is not None
               else pick_mcs(sinr, n_rb, knobs))
        if knobs.outage_db is not None and sinr < knobs.outage_db:
            mcs = -1
        rate = ue_demand_bps.get(imsi, float("inf"))
        want_bits = (float("inf") if rate == float("inf")
                     else rate * UL_DEMAND_RATIO * window_s
                     + backlog.get(imsi, 0.0) * 8.0)
        t = turns.get(imsi, 0.0) * turn_duty
        if mcs < 0 or t <= 0:
            held = min(rlc_buffer_bytes,
                       backlog.get(imsi, 0.0)
                       + (0.0 if want_bits == float("inf") else want_bits / 8.0))
            out[imsi] = {"has_ul": True, "sinr_ul_db": sinr, "mcs_ul": -1,
                         "queued_ul_bytes": held}
            continue
        per_symbol = tb_bytes(mcs, 1.0, n_rb) * 8.0
        need = (want_bits / per_symbol) if per_symbol > 0 else 0.0
        # The uplink cannot take more turns than the round-robin gave the UE,
        # and needs no more than its own (smaller) demand asks for.
        t = min(t, need / sym_per_flow) if need < float("inf") else t
        tb = max(1, min(slots, round(t)))
        sym = t * sym_per_flow
        bler = (engine_bler(sinr, mcs, tb_bytes(mcs, sym / tb, n_rb), decay_db)
                if USE_ENGINE_AMC else bler_for(sinr, mcs, decay_db))
        err = round(tb * bler)
        vol = tb_bytes(mcs, sym / tb, n_rb) * (tb - err)
        wanted = (0.0 if want_bits == float("inf") else want_bits / 8.0)
        out[imsi] = {
            "has_ul": True, "sinr_ul_db": sinr, "mcs_ul": mcs,
            "tb_ul": tb, "tb_ul_error": err,
            "prb_used_ul": math.ceil(sym / (slots * SYMBOLS_PER_SLOT)
                                     * PRB_METRIC_SCALE),
            "prb_used_ul_rb": sym / (slots * DATA_SYMBOLS_PER_SLOT) * n_rb,
            "symbol_share_ul": sym / (slots * DATA_SYMBOLS_PER_SLOT),
            "volume_ul_bytes": vol,
            "queued_ul_bytes": (0.0 if want_bits == float("inf")
                                else min(rlc_buffer_bytes, max(0.0, wanted - vol))),
        }
    return out
