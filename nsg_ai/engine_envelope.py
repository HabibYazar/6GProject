"""
NSG ns-O-RAN engine operating envelope — THE RULEBOOK (single source of truth).

The ns-O-RAN engine (ns-3.42 mmWave + FlexRIC) is research-grade code: rock-solid
inside a validated parameter envelope, but it degrades (PRB reads 0) or crashes
outside it. Instead of fighting every fork limit, we CONSTRAIN scenario generation
to the proven-good envelope:
  • the AI scenario generator (llm_agent) is given RULEBOOK_PROMPT (soft guidance),
  • the backend (main.py /ric/validate-nsoran) clamps derived params (hard net).

Change a number here and BOTH the clamp and the AI prompt follow — so when a fork
fix later widens a limit, it's a one-line edit. Re-run the stress harness
(engines/nsoran, /root/nsg_stress.sh in the nsoran distro) and update these when
the engine's limits move.

Envelope validated 2026-07-09 (stress test; see engines/nsoran/README.md).
"""
from __future__ import annotations

import math

# ── mmWave gNBs (cells) ───────────────────────────────────────────────────────
# <3: DU metrics (PRB/SINR/thp) not decoded + abort-prone.
# The old cap was 7, on a 2026-07-09 verdict that 9+ E2 nodes made PRB and
# throughput "read 0 — hard fork limit". Re-measured 2026-08-28 and that is
# FALSE: 16 cells / 32 UEs ran 120 frames with every cell reporting ~20 PRB and
# ~5.5 Mbps and zero crash signatures. "Reads 0" was a measurement symptom
# diagnosed before we understood how the KPM frame is assembled. 16 is what has
# actually been measured — raise it further only with another run, and note that
# scale costs wall time (120 frames: 283 s at 9 cells, 523 s at 16).
# Lowered from 3 on 2026-09-10. The old floor was never a property of the
# simulation — it was two crashes, and both are now fixed in the engine
# (nsg-engine:1.8):
#   * FlexRIC aborted in kpm_dec_meas_data_asn on an E2SM-KPM measurement whose
#     measRecord list was EMPTY, which the encoder could produce whenever a
#     neighbour list held only the negative-id entries that exist to register a
#     NAME. Killing the RIC closes the SCTP socket, so ns-3 went on encoding into
#     a dead peer and the run produced nothing while looking alive.
#   * MmWaveBearerStatsConnector::ConnectSrb0Traces asserted on a one-shot
#     (cellId, rnti) map miss, which recycled RNTIs make reachable.
# Measured after the fixes, all producing real PRB/SINR/throughput and 34-36 KPM
# metric names against 37-39 at three cells: 1 cell with 1 and 8 UEs, 2 cells
# with 1, 2, 3 and 6 UEs, 3 cells with 1 and 4. So the DU metrics DO decode below
# three cells — the 2026-07-09 note saying otherwise was reading the crash.
GNB_MIN = 1
GNB_MAX = 16

# ── UEs ───────────────────────────────────────────────────────────────────────
# 0 UEs → ns-3 crash. Cells must be LOADED (≥ a couple UEs each) or PRB reads ~0.
# UE_MAX is an ABSOLUTE ceiling (32 measured good); the binding constraint is
# UEs-per-CELL, not the total — see clamp_ue.
UE_MIN = 1
# Raised from 32 on 2026-09-01 after the density work: 150 UEs on 3 gNBs ran a
# clean session (E2 registered, KPM produced, normal teardown) in 0.97 GB, and
# 210 UEs fits in 1.69 GB where it used to be OOM-killed at 23.8 GB. Memory is no
# longer the binding constraint; SPEED is — see the horizon table below.
# 150 UEs does not crash, but the product path returns `partial` — the run covers
# ~0.1 simulated seconds and reports no DRB.UEThpDl or L3servingSINR at all, i.e.
# a configuration the platform accepts and then cannot measure. An envelope should
# promise a RESULT, not merely the absence of a crash.
#
# RE-MEASURED 2026-09-13 after the real cause was found, and it was never the
# engine. The batch path's KPM collection window was a hardcoded `sleep(5)` in
# the xApp: indications arrive once per reporting period of SIMULATED time, and a
# heavy scenario advances slowly, so five wall seconds bought 0.1 simulated
# seconds at 72 UEs — zero complete E2 rounds, only the two startup indications,
# and every classifier downstream read that as a density limit. Sized to the
# scenario instead (NSG_KPM_COLLECT_SECS, nsg-engine:1.12):
#
#     72 UEs   3 of 3 ok   (82 names,  11 indications)
#     90 UEs       ok      (94 names,  20)
#    120 UEs       ok      (106 names, 20)
#    150 UEs   3 of 3 ok   (156-182 names, 17-20)
#
# The RC closed loop survives all of it (rc_control.closed_loop true), which is
# why the fix extends the one-shot path rather than switching batch to the
# bridge xApp, which skips the control phase.
#
# 150 is where this stopped being measured, not where it broke. The earlier
# ceilings of 75, 72, 66 and 60 were all the five-second window.
UE_MAX = 150
UE_PER_CELL_MIN = 2
# The old value was 4, chosen because "too many UEs on ONE cell corrupts the
# mmWave stack's heap (malloc(): invalid size -> abort), ~12 aborts". That abort
# was NOT a density limit: it was `calloc(1, 8192)` in e2sim's KPM indication
# encoder against a memcpy of message_length, which crosses 8192 bytes at roughly
# ten UEs on a cell. Fixed 2026-09-01, and 30/40/50 UEs per gNB then ran clean
# sessions in a row. 30 is set here because it is measured with margin, not
# because anything breaks above it.
# The engine still attaches by RSRP, so a cell inside a cluster draws more UEs
# than geometry predicts — this stays the trigger the backend uses to rebuild a
# risky scenario as a balanced scatter.
#
# RE-TESTED 2026-09-14 against the last recorded density defect, "at 25-30 UEs
# per cell the run dies at t=+0.000000000s with NS_ASSERT cond="m_ptr"". It DOES
# NOT REPRODUCE. On 3 cells at 50 MHz: 25, 30, 40 and 50 UEs per cell all ran
# clean with real spans and 11 indications each. That matches what the E2
# registration race turned out to be — the null pointer was the last link in a
# chain that started with an empty RIC, not a density limit — so the defect is
# closed by measurement rather than by a fix.
# 30 stays as the value here because it is measured with margin, not because
# anything breaks above it.
UE_PER_CELL_MAX = 30

# ── What a run OBSERVES: the simulated horizon, measured on 1.12 ──────────────
# There used to be a `UE_PRACTICAL_MAX = 60` here, and a fitted "span law" under
# it. Both are gone, because both were measured through the xApp's hardcoded
# five-second KPM collection window and both described that window rather than
# the engine. Re-measured 2026-09-13 on nsg-engine:1.12, through the PRODUCT path
# (a full platform config, 50 MHz, sim_time 60), reading `energy.span_s`.
# `tools/horizon_sweep.py` is what measured it and `tools/horizon.1_12.json` is
# the data — re-run it rather than re-deriving this table by argument:
#
#      cells   UEs   UEs/cell   span_s   wall_s   KPM names
#          3     6        2       0.9      39         38
#          3    18        6       0.4      58         45
#          3    45       15       0.3     102         65
#          3    75       25       0.3     176         81
#          6    12        2       0.6      48         41
#          6    30        5       0.4      79         52
#          6    60       10       0.2     153         69
#          6    90       15       0.2     178         94
#          6   150       25       0.1     216        182
#         12    24        2       0.3      69         47
#         12    60        5       0.3     157         69
#         12   120       10       0.2     246        100
#
# Three things this says, none of which the old constant said.
#
# 1. THERE IS NO UE THRESHOLD. `UE_PRACTICAL_MAX` promised that below it a run
#    covers enough simulated time to watch something evolve. It does not: the
#    LARGEST horizon anywhere in the envelope is 0.9 simulated seconds, at the
#    smallest scenario measured. Sub-second is the whole product path, so the
#    constant drew a line where the behaviour has no step.
#
# 2. THE OLD LAW IS DISPROVED, not merely unverified. It claimed `span_s x UEs`
#    is a constant per cell count. At 3 cells that product runs 5.4, 7.2, 13.5,
#    22.5 as UEs go 6 -> 75 — it rises four-fold. Nothing is being conserved.
#    The reason is that 1.12 sizes the collection window FROM the UE count, so
#    a bigger scenario buys back part of the wall time it costs.
#
# 3. WHAT A RUN RETURNS GOES THE OTHER WAY from what it costs: KPM names climb
#    38 -> 182 with the crowd, so a dense scenario is a richer measurement, not a
#    poorer one. Never thin a crowd to "see more time".
#
#    This point USED to read "UE count is not what costs the horizon — cells
#    are", from the table above: the horizon falls 0.9 -> 0.6 -> 0.3 as cells go
#    3 -> 6 -> 12. That table scales UEs WITH cells (2 per cell), so it cannot
#    separate the two, and the conclusion was wrong. Measured 2026-09-14 with the
#    UE count HELD AT 12 and the same 60 s window:
#
#        cells    1      3      6     12
#        span_s   1.6    1.1    1.0    1.0
#
#    Twelve times the cells costs 1.6x, and it SATURATES after about three. The
#    reason is pruning: `SpectrumChannel::MaxLossDb` drops a transmitter-receiver
#    pair before the channel matrix is built for it, so a cell beyond that radius
#    is free. UEs are what cost, because a UE inside the radius is a pair that
#    must be computed. Large topologies are cheap; large crowds are not.
#
# The design rule that replaces the constant: at the AUTOMATIC window a batch run
# is a per-configuration PHY/MAC SNAPSHOT at every size the envelope allows, and
# no UE count changes that.
#
# ── But the horizon turned out to be a BUDGET, not a wall ─────────────────────
# Measured straight after the table above, and it inverts the prescription. The
# collection window is the horizon, the relationship is LINEAR with no diminishing
# return, and an explicit window may go far past the auto sizing (1.13 allows
# 900 s). Bare 3 cells / 6 UEs, windows of 10 / 30 / 60 / 120 s:
#
#     span_s   4.1   5.8   9.2   13.9        (~0.10 simulated s per wall s, flat)
#
# On the product path the same trade is ~4x smaller but just as linear: the
# 3-cell/6-UE config went 1.1 s of span at the automatic window to 3.6 s at 120 s,
# with indications 11 -> 44. And that is enough to change what a scenario can
# MEAN — a `cell_outage` at `start_s=2.0` fired for the first time: the target
# cell drew 4841 J against 9840 J unfaulted, while its two neighbours picked up
# the load. At the automatic window the identical request had come back with the
# cells within a rounding error of the no-fault run, and PASSED.
#
# So a scenario that turns on elapsed simulated time is not barred from the batch
# path — it has to PAY for it, in wall clock, through `settings.kpm_collect_s`
# (roughly 40x the `start_s` you need, on a small topology). The live path is
# still the right home for anything open-ended. `energy.span_s` comes back with
# every run; campaign.py already compares it against what the scenario asked for.

# ── Why speed work keeps ending here: the pruning radius ──────────────────────
# `SpectrumChannel::MaxLossDb=110` (run_scenario.sh) drops a transmitter-receiver
# pair BEFORE the 3GPP channel matrix is built for it, and that is not a tuning
# detail — it is most of the engine's speed. Measured 2026-09-14 at 12 cells /
# 12 UEs, same window:
#
#     MaxLossDb 110 (default)   span 2.3 s   (repeat: 2.3 s)
#     MaxLossDb 1e9 (off)       span 0.3 s
#
# 7.7x. And it CANNOT be pushed further. The same topology at tighter thresholds:
#
#     110   span 2.3    84 KPM names   per-cell energy varied (6265/1531/6254/...)
#     100   span 80     24 names       ten of twelve cells identical at 1997 J
#      95   span  3.2    0 names       no DU metrics at all
#      90   span  3.7    0 names
#
# At 100 dB the network has already collapsed — ten cells carrying nothing, which
# is why the sim "speeds up" to an 80-second span. The recorded boundary ("below
# 90 dB it wrecks results") was measured at 3 cells and is too generous: at 12
# cells the margin below 110 is gone.
#
# This is what kills rank-based (top-K interference) pruning as a speed idea
# before any C++ is written. Top-K would cut the links MaxLossDb has already cut,
# which is also why adding cells is nearly free (see the horizon note above).
# Whatever is left to win is in the pairs that SURVIVE pruning, so it is a
# per-pair cost or a parallelism question, not a pruning question.

# ── Bandwidth (MHz) ───────────────────────────────────────────────────────────
# Very wide (100 MHz) under-loads cells → sparse PRB≈0. Match bandwidth to load.
BW_MHZ_MIN = 10
BW_MHZ_MAX = 50

# Bandwidth and per-cell UE count are NOT independent. The flex-TTI scheduler
# splits a transport block across a UE's RLC flows with integer division
# (mmwave-flex-tti-mac-scheduler.cc:1807/1820); squeeze enough UEs onto one cell
# and that average rounds to ZERO, which trips
#   NS_ASSERT(ueSchedInfo.m_rlcPduInfo[i].m_size > 0)
# and aborts the run. The assert is doing real work: the line under it computes
# `m_size - m_subHdrSize` on an unsigned, so a zero there would underflow the RLC
# buffer accounting instead of merely being wrong.
#
# Measured 2026-09-01, 3 cells:
#     10 MHz /  3 UEs per cell = 3.33 MHz/UE   ran clean
#     20 MHz / 10 UEs per cell = 2.00 MHz/UE   ran clean
#     10 MHz /  6 UEs per cell = 1.67 MHz/UE   scheduler assert
#     10 MHz / 10 UEs per cell = 1.00 MHz/UE   scheduler assert
# The boundary sits between 1.67 and 2.00, so require 2.0 and stay on the
# measured side of it.
MIN_MHZ_PER_UE = 2.0  # kept for reference; NOT the model — see below

# A flat MHz-per-UE ratio does not fit. 2.0 MHz/UE holds at 20 MHz (10 UEs/cell
# runs clean) but ABORTS at 10 MHz (5 UEs/cell), so narrow carriers pay a fixed
# cost the ratio hides — the transport block itself is small enough that the
# per-flow split rounds to zero sooner. Measured points only, no interpolation
# dressed up as a law:
#     10 MHz ->  3 UEs/cell clean;  5 and 6 abort
#     20 MHz -> 10 UEs/cell clean
#     50 MHz -> 25 UEs/cell clean (21 also clean at 7 cells)
# Re-measured 2026-09-02 THROUGH THE PRODUCT PATH (:8090/run), which installs the
# UDP traffic applications that a bare scenario run does not. Real offered load
# splits the transport block further, so the ceiling is lower than the
# bare-binary measurement suggested:
#     10 MHz,  3 cells, 3 UEs/cell -> ok      but
#     10 MHz, 16 cells, 3 UEs/cell -> ABORT   (16 cells, 32 UEs at 2/cell is ok)
# Cell count or total UEs also matters and this data cannot tell which, so 10 MHz
# is pinned to the value that held at BOTH cell counts rather than modelling a
# mechanism we have not isolated.
_BW_UE_MEASURED = [(10.0, 2), (20.0, 10), (50.0, 25)]


# THE ABORT ITSELF IS FIXED (nsg-engine:1.16, engines/nsoran/flex-tti-zero-pdu.patch).
# The scheduler now serves as many RLC flows as the transport block can actually
# carry and defers the rest a slot, instead of handing one a zero-byte share and
# asserting. Both recorded crash points run clean: 3 cells / 18 UEs and 3 cells /
# 30 UEs, each at 10 MHz.
#
# The table below is KEPT anyway, and deliberately. It was measured as a crash
# boundary, but it is also roughly where a cell stops being able to serve its
# crowd — below it the run now survives and reports poor service instead of
# dying, which is better behaviour and not a reason to send scenarios there. What
# changed is the CONSEQUENCE of crossing it, so the numbers below now describe a
# quality edge rather than a cliff, and re-deriving them as one is open work.
#
# Fixing the abort was never about relaxing this. The guard is computed from the
# topology AS DRAWN, and the load that used to trip the split arrives at run
# time — UEs attach by RSRP, and a cell outage moves a whole cell's crowd onto
# its neighbours. No static guard can see that, which is why the abort had to go
# for a long run to be trustworthy at all.


def max_ue_per_cell_for_bw(bw_mhz: float, numerology: int = 2) -> int:
    """How many UEs one cell can carry at this bandwidth before service falls
    apart. Measured as the point where the run used to CRASH; since 1.16 it
    survives and degrades instead, so treat this as the edge, not a cliff.

    Interpolates between measured points and stays on the low side of them: an
    earlier version used a flat 2.0 MHz/UE, which allowed exactly the 10 MHz /
    5 UEs-per-cell point that the envelope sweep then crashed on.

    NUMEROLOGY matters for the same reason bandwidth does. The table was measured
    at µ2 (60 kHz, 0.25 ms slots). µ3 halves the slot, so the transport block per
    slot roughly halves and the per-flow split reaches zero sooner — measured
    once: µ3 at 20 MHz with 10 UEs/cell hits the same
    NS_ASSERT(m_rlcPduInfo[i].m_size > 0) that µ2 survives. The 0.5 factor below
    is DERIVED from that one point, not measured across the range; if µ3 becomes
    a scenario people actually use, measure it properly the way µ2 was.
    """
    if not bw_mhz or bw_mhz <= 0:
        return UE_PER_CELL_MIN
    shrink = 0.5 if int(numerology or 2) >= 3 else 1.0
    lo = _BW_UE_MEASURED[0]
    if bw_mhz <= lo[0]:
        return max(UE_PER_CELL_MIN, int(lo[1] * shrink))
    for (b0, u0), (b1, u1) in zip(_BW_UE_MEASURED, _BW_UE_MEASURED[1:]):
        if bw_mhz <= b1:
            frac = (bw_mhz - b0) / (b1 - b0)
            return max(UE_PER_CELL_MIN, int((u0 + frac * (u1 - u0)) * shrink))
    hi = _BW_UE_MEASURED[-1]
    base = int(hi[1] * bw_mhz / hi[0] * shrink)
    return max(UE_PER_CELL_MIN, base)

# ── Positions ─────────────────────────────────────────────────────────────────
# Cells must be at DISTINCT positions; co-located cells → PRB unreliable (0).
MIN_CELL_SEPARATION_M = 50

# ── Frequency (MHz) ───────────────────────────────────────────────────────────
# Any REAL band validated (FR1 sub-6 GHz .. FR2 mmWave). We clamp only to reject
# garbage from bad edits (0 / negative / NaN / absurd), which would crash the ns-3
# mmWave stack the way a zero carrier crashes the REM binary.
FREQ_MHZ_MIN = 600       # ~n71 low band
FREQ_MHZ_MAX = 60_000    # ~FR2 top (52.6 GHz), rounded up
FREQ_MHZ_DEFAULT = 3500  # fall back to 3.5 GHz mid-band on garbage input


def _to_finite_int(n, default: int) -> int:
    """int(n) that never raises — NaN/inf (e.g. from a UE count cleared in the
    Config tab → parseFloat('') → NaN) would otherwise ValueError on int()."""
    try:
        return int(n) if math.isfinite(n) else default
    except (TypeError, ValueError):
        return default


def clamp_gnb(n: int) -> int:
    """Cap the mmWave gNB (cell) count at the measured max. The lower bound
    GNB_MIN is DESIGN guidance for the AI generator (RULEBOOK_PROMPT); the backend
    can't invent cells the user didn't design, so it only caps the max and keeps
    >= 1."""
    return max(1, min(GNB_MAX, _to_finite_int(n, 1)))


def clamp_ue(n: int, n_gnb: int | None = None,
              bw_mhz: float | None = None, numerology: int = 2) -> int:
    """Cap total UEs. 0 crashes ns-3, so keep >= 1.

    What actually breaks the mmWave stack is UEs on ONE cell, not the total: the
    heap corrupts (`malloc(): invalid size` -> abort) somewhere past a dozen on a
    single cell, and it is probabilistic. A flat total cap is therefore the wrong
    shape — it was needlessly tight on wide designs (32 UEs across 16 cells is
    2 each and runs clean, measured) and too loose on narrow ones (16 UEs across
    3 cells is over 5 each, into the risk band).

    So when the cell count is known the ceiling is derived from it. Pass n_gnb
    wherever it is available; without it this falls back to the absolute cap,
    which is the old behaviour.

    Every caller that feeds `ue_alloc` MUST pass the same n_gnb — IMSI is the
    slot index, so a disagreement here silently misaligns the per-UE slice and
    rate maps against the engine's attach order.
    """
    ceiling = UE_MAX
    if n_gnb is not None:
        cells = max(1, _to_finite_int(n_gnb, 1))
        # The span cap that used to sit here is GONE for good. It was fitted to
        # tools/envelope_sweep.py, and every span_s in that data came through the
        # five-second KPM collection window — so the law described the window.
        # The clean re-measurement on 1.12 (see the horizon table above) then
        # DISPROVED it: `span_s x UEs` rises four-fold across a single cell
        # count instead of holding. There is nothing here to restore.
        per_cell = UE_PER_CELL_MAX
        if bw_mhz is not None:
            # Bandwidth caps the per-cell count independently: below ~2 MHz per
            # UE the scheduler's transport-block split rounds to zero and the run
            # aborts. See MIN_MHZ_PER_UE for the measurements.
            per_cell = min(per_cell, max_ue_per_cell_for_bw(bw_mhz, numerology))
        # NARROW, do not reassign — so that a future per-cell or per-bandwidth
        # term added above this line still binds.
        ceiling = min(ceiling, cells * per_cell)
    return max(UE_MIN, min(ceiling, _to_finite_int(n, UE_MIN)))


def config_numerology(config) -> int:
    """The numerology `clamp_ue` should see. Same reason `config_bw_mhz` exists:
    every caller must derive it identically or batch and live clamp to different
    totals and the IMSI slots misalign."""
    bss = list(getattr(config, "base_stations", None) or [])
    band = getattr(bss[0].radio, "band", None) if bss else None
    mu = int(getattr(band, "numerology", 2) or 2) if band else 2
    return 3 if mu >= 3 else 2


def config_bw_mhz(config) -> float:
    """The bandwidth `clamp_ue` should see for a config.

    Exists so every caller derives it identically. clamp_ue's result decides how
    many engine slots exist, and IMSI is the slot index — if batch and live
    computed the bandwidth differently they would clamp to different totals and
    silently misalign every per-UE slice and rate map against the attach order.
    """
    bss = list(getattr(config, "base_stations", None) or [])
    band = getattr(bss[0].radio, "band", None) if bss else None
    return clamp_bw_mhz(band.bandwidth_mhz) if band else 20.0


def ue_alloc(ues: list, n_ue: int) -> list:
    """Split the n_ue engine slots across UE groups by count share. Shared by
    the position, slice/rate, speed AND live slice-tag builders so slot i
    belongs to the same group everywhere (lives here so both main and
    nsoran_live can import it without a circular import)."""
    counts = [max(0, int(g.count)) if math.isfinite(g.count) else 0 for g in ues]
    total = sum(counts) or 1
    alloc = [max(1, round(n_ue * c / total)) for c in counts]
    while sum(alloc) > n_ue:
        alloc[alloc.index(max(alloc))] -= 1
    while sum(alloc) < n_ue:
        alloc[alloc.index(min(alloc))] += 1
    return alloc


def clamp_bw_mhz(bw_mhz: float) -> float:
    """Clamp the ns-O-RAN run's channel bandwidth (MHz) into the range where PRB
    stays meaningful — very wide bandwidth under-loads cells → sparse PRB. Shapes
    only this engine's payload; does not mutate the user's config."""
    bw = float(bw_mhz)
    if not math.isfinite(bw):
        bw = BW_MHZ_MIN
    return max(BW_MHZ_MIN, min(BW_MHZ_MAX, bw))


def clamp_freq_mhz(freq_mhz: float) -> float:
    """Clamp centre frequency (MHz) to a real cellular band. Garbage from bad edits
    (0 / negative / NaN / absurd) falls back to 3.5 GHz mid-band so the ns-3 mmWave
    stack never receives an invalid carrier."""
    f = float(freq_mhz)
    if not math.isfinite(f) or f <= 0:
        return float(FREQ_MHZ_DEFAULT)
    return max(FREQ_MHZ_MIN, min(FREQ_MHZ_MAX, f))


# Human-readable rulebook injected into the AI scenario generator's system prompt.
# The worked examples of the span cap that used to be computed here are gone with
# the cap itself; the horizon is now quoted from the measured table instead.

# Split into HARD limits and QUALITY guidance on purpose. Presenting both with one
# voice cost us a demo: asked for "2 cells, 1 user walking from one to the other",
# the generator read "at least 2 UEs per cell", multiplied by 2 cells, and returned
# FOUR users. It was obeying the rulebook over an explicit instruction — the right
# instinct applied to a rule that was never a safety rule. A 1-UE handover scenario
# is a legitimate design: PRB and throughput read near zero, and the handover event
# is still completely real.
#
# So the rulebook now says three things instead of one: what CRASHES the engine
# (never cross), what DEGRADES the measurement (cross it knowingly, and say so),
# and what to do when the user's request conflicts with the second kind.
RULEBOOK_PROMPT = f"""\
ns-O-RAN real-E2AP validation engine — the envelope, in two tiers.

HARD LIMITS (crossing these crashes or blanks the run — never cross them):
- UEs: {UE_MIN}-{UE_MAX} total. The per-cell limit is set by BANDWIDTH, not by a
  flat number: below roughly 2 MHz per UE the scheduler's transport-block split
  rounds to zero and the run aborts, so 10 MHz carries ~2 UEs/cell, 20 MHz ~10,
  50 MHz ~25. Ask for bandwidth that matches the crowd, or thin the crowd.
  (`UE_PER_CELL_MAX` is {UE_PER_CELL_MAX} in the code but can never bind — the
  bandwidth rule is lower at every permitted bandwidth. Ignore it.)
- A heavy scenario needs a LONGER KPM COLLECTION WINDOW, and the engine sizes it
  from the UE count automatically. Nothing to set — but it is why a 150-UE run
  takes minutes of wall clock rather than seconds, so say so in `description`
  when you design one.
- Cells (gNBs): {GNB_MIN} to {GNB_MAX}. {GNB_MAX} cells was measured clean at 32 UEs;
  the large UE counts above were measured on 3 cells. A scenario at BOTH extremes
  at once is untested — prefer one or the other. ONE and TWO cells now run and
  report real metrics (fixed 2026-09-10, see GNB_MIN) — so build the number of
  sites the user asked for. Two sites means two sites.
- BANDWIDTH AND CROWD ARE LINKED, and narrow carriers are stricter than a simple
  ratio suggests. Below the limit the scheduler's transport-block split rounds to
  zero and the run ABORTS. Measured ceilings per cell: 10 MHz -> 3 UEs,
  20 MHz -> 10, 50 MHz -> 25. A crowded scenario needs the bandwidth to match, or
  fewer UEs per cell.
- Cell positions must be DISTINCT (min {MIN_CELL_SEPARATION_M} m apart); co-located cells
  give unreliable PRB.
- Do not place a UE spawn point on top of a cell (a zero-distance link is log10(0)).
- SPREAD the UEs across the cells. The engine attaches by RSRP, so a cell sitting
  in the middle of a cluster draws far more than an even split suggests.
- A BATCH RUN IS A SNAPSHOT UNLESS YOU BUY TIME, and there is no UE count that
  turns it into a story. At the automatic window the simulated horizon runs 0.9 s
  at 3 cells / 6 UEs down to 0.1 s at 6 cells / 150 UEs — under a second
  everywhere — and making the scenario SMALLER does not fix that; it buys about
  half a second.
- SO IF THE SCENARIO TURNS ON ELAPSED TIME, SET `settings.kpm_collect_s`. That is
  the collection window in WALL seconds and it is the run's horizon: the trade is
  linear (a 3-cell/6-UE config covers 1.1 s at the automatic window and 3.6 s at
  kpm_collect_s=120), and it is what makes a fault at start_s=2.0 actually fire
  instead of returning a run identical to one with no fault. Rule of thumb: about
  40x the start_s you need, on a small topology; more cells need more. It costs
  WALL CLOCK one-for-one, so ask for it only when the scenario needs it, keep the
  topology small when you do, and say in `description` that the run is long.
- CELLS ARE CHEAP, CROWDS ARE NOT. Measured with the UE count held fixed, going
  from 1 cell to 12 costs only 1.6x of horizon and saturates after about three —
  a far cell is pruned out of the channel computation before it costs anything.
  So build the number of sites the scenario asks for without worrying about it.
  What costs is UEs, because a UE near a cell is a link that must be computed.
- AND DO NOT THIN A CROWD TO "SEE MORE TIME". It does not work that way, and the
  measurement gets POORER: KPM names climb from 38 to 182 as the crowd grows.
- A MOVING scenario must be SCALED TO THE HORIZON, and you must COMPUTE that
  horizon, never estimate it in prose. The horizon is set by the KPM WINDOW
  BUDGET, not by settings.duration_s (which does not reach the batch engine at
  all) and not by the UE count:

      simulated_seconds  ~=  frames / 13
      travel_metres      ~=  speed_mps * frames / 13

  Measured 2026-09-09 across four runs — 120 windows gave 10.2 s at 1 UE and
  10.1 s at 9 UE, 40 windows gave 3.0 s, 21 windows gave 1.2 s. More UEs cost
  WALL time, not simulated time, so the window budget is the only input.

  ASSUME THE DEFAULT BUDGET unless the user names one: a campaign run is 40
  windows, i.e. **~3 simulated seconds**. That is the number to design against.

      pedestrian 1.4 m/s,  40 windows ->   4 m of travel
      pedestrian 1.4 m/s, 120 windows ->  13 m
      vehicle     20 m/s,  40 windows ->  61 m
      vehicle     20 m/s, 120 windows -> 185 m

  So a 40 m walking path is not a scenario the default budget can run: measured
  2026-09-09, exactly that design covered 4.2 m of its 40 m and reported ZERO
  handovers, while its own `description` claimed "the simulated horizon is well
  above 20 s" — a seven-fold error stated as fact. Do the division and put the
  RESULT in the description ("40 windows ~ 3 s ~ 4 m of travel"); never assert a
  horizon you did not compute.

  Make the CROSSING fit that distance, and reach for the levers IN THIS ORDER:

    1. SHORTEN THE PATH. This is almost always the right lever and it is the one
       that gets skipped. Put the UE a couple of metres on the serving side of
       the boundary and walk it a few metres past — a 4 m path at 1.4 m/s fits
       the default budget exactly. The path does not have to span the sites; it
       only has to span the CROSSOVER.
    2. RAISE THE BUDGET: set `settings.kpm_collect_s`. The 40-window default is
       not a law — it is the auto window for a small scenario (10 s), and the
       budget scales with it at roughly 4 windows per collection second, so
       kpm_collect_s=60 gives ~240 windows. If the walk genuinely needs to be
       long (the user asked for a 30-second drive-through), BUY THE ROOM instead
       of shrinking their scenario into something they did not ask for.
       Measured 2026-09-16: generations kept producing 60-120 window walks and
       left kpm_collect_s at the default, so the scenario could not be observed
       in the run it asked for. This lever was missing from this list.
    3. Only then raise the speed — and ONLY if the user did not state one. A
       speed the user asked for is their requirement, not a free variable.

  Moving the cells closer together is NOT on that list for a handover scenario.
  It fits the budget by destroying the thing under test: below ~200 m apart the
  UE crosses the whole boundary in a couple of metres and there is no handover
  left to observe. Cells may move closer only in scenarios that are not about
  handover.

  If you do raise the speed, the LABELS must follow it: `mobility.pattern` and
  the group's `name` have to match the new speed. Measured failure 2026-09-09:
  asked for "4 users at 5 km/h", the generator correctly computed that 40 windows
  give 4 m, then raised the speed to 20 m/s while leaving
  `pattern: "pedestrian"` and the name "Pedestrians crossing A to B" — a
  pedestrian at 72 km/h, shown that way in the properties panel. Trading one
  visible inconsistency for another is not a fix. Shortening the path would have
  kept the user's 5 km/h AND fired the handover. If the design genuinely needs more, say so in `description`
  with the window count it requires (metres / speed_mps * 13, rounded up) so the
  user can raise the budget instead of reading a silent zero. Verified: cells
  600 m apart with the UE starting 80 m from the weaker one handed over at 9.4 s
  within a 120-window run.

  FINAL CHECK, before you return, on the NUMBERS YOU ARE ACTUALLY RETURNING.
  BOTH of these have to hold, and both are arithmetic — not a judgement call:

      (1) (crossover - path_start) / speed_mps * 13  <=  40   (reached in time)

      (2) path_metres              / speed_mps * 13  >=  40   (still walking)

      (3) path_start  <  crossover  <  path_end               (it crosses)

  (1) and (2) together mean the crossover sits EARLY in the walk and the far end
  runs off past where the budget stops — a path sized to finish inside the budget
  leaves the UE standing on the boundary flipping cells, which is the failure the
  path-end note above describes.

  For (3), COMPUTE the crossover, do not eyeball it. Between two cells separated
  by `sep` metres whose powers differ by `delta` dB (stronger minus weaker), the
  RSRP boundary sits this far from the WEAKER one:

      d_weak = sep / (1 + 10 ** (delta / 35))

  THAT FORMULA IS ONE-DIMENSIONAL: it gives a distance ALONG THE LINE JOINING
  THE TWO CELLS, and it means nothing anywhere else. So the walk has to lie on
  that line — same y if the cells share a y, and starting/ending between them.
  Measured 2026-09-11: a scenario computed the crossover at x=40 for cells at
  (0,0) and (100,0), then put the UEs on y=200. From the middle of that path
  both cells are 206 m away — equidistant — so the boundary it had just computed
  was not on the path at all, and eight UEs produced one handover, to a THIRD
  cell 300 m off. Put the UEs on the line, then apply the formula.

  so 40 dBm at x=0 and 46 dBm at x=100 put it at 100 / (1 + 10**(6/35)) = 40 m,
  i.e. x = 40 — not the midpoint, and not "near x=25" either. Measured failure
  2026-09-09: a config computed the horizon correctly, placed a 4 m path at
  x=10..14, and asserted in its description that the crossover was "near x=25,
  well inside the 4 m path". Both halves were wrong — 25 is not the crossover,
  and it is not inside 10..14. The run was clean and reported zero handovers.

  And mind where the path ENDS. With `waypoint_mode: "once"` the UE stops on the
  last waypoint and stays there for the rest of the run, so a path that ends one
  metre past the crossover parks it ON the boundary — where the two cells differ
  by a fraction of a dB and shadowing decides the ordering tick by tick. Measured
  2026-09-10: a 37->41 m path across a crossover at x=40 handed over as designed
  and then flipped back and forth four more times while stationary.

  So the path must be LONGER than the budget, not equal to it. Two conditions,
  and the first one alone is what check (1) above tests:

      (crossover - path_start) / speed_mps * 13  <=  frames    reach it in time
      path_metres            / speed_mps * 13   >=  frames    still walking at the end

  Put the crossover EARLY in the walk — the first third — and let the far end run
  off past where the budget stops. A UE that finishes its path stands still on
  `waypoint_mode: "once"`, and a stationary UE anywhere near the boundary flips
  cells for the rest of the run: the geometry has stopped changing but the
  channel is re-drawn every `channel_update_ms`, so the two links keep crossing
  with nothing left to break the tie.

  Measured 2026-09-10, and the pattern held in every run: while the UE was
  MOVING the behaviour was clean — with 6 dB hysteresis it held its serving cell
  from x=37 all the way to x=49, nine metres past a crossover at x=40, then
  handed over once. Every extra handover in every run happened AFTER the UE
  reached the end of its path and stopped. `"pingpong"` is not the fix either:
  on a short path it walks the UE back and forth inside the ambiguous zone and
  never leaves it — measured, four handovers over the same 4 m, half of them not
  tracking the geometry at all.

  TUNE THE TRIGGER, OR IT PING-PONGS. This is the single biggest thing between a
  handover scenario that reads as working and one that reads as broken, and the
  defaults are the eager end of the range. `ho_hysteresis_db: 0` gives the A3
  event no guard band at all, and `handover_mode: "DynamicTtt"` shortens the
  time-to-trigger toward `ho_dyn_ttt_min_ms` (25 ms) exactly when the two cells
  are close — so at the boundary, where the whole scenario puts the UE, the
  trigger is at its most eager precisely when it should be at its least.

  For a scenario whose point is A handover, use:

      handover_mode: "FixedTtt",  ho_ttt_ms: 255,  ho_hysteresis_db: 6

  Measured 2026-09-10 on the same scenario and seed, changing only these:
    * 0 dB + DynamicTtt  -> 6 handovers, four of them with the UE standing still
    * 3 dB + DynamicTtt  -> 1 to 3, still flipping back and forth
    * 6 dB + FixedTtt 255 -> exactly 1, twice in a row, and it held its serving
      cell from x=37 to x=49 — nine metres PAST a crossover at x=40 — before
      switching once.
  A long fixed TTT is what actually suppresses it: the A3 condition has to hold
  continuously for a quarter of a second, which momentary fluctuation cannot
  sustain. 255 ms is the CEILING, not a choice — the engine declares
  FixedTttValue as a uint8, so 3GPP's longer steps cannot be asked for. The runs
  that gave one clean handover were asking for 480 and silently getting 255.
  That is also how real networks are tuned, so this is not a demo trick.

  Two things this is NOT, both tested: it is not shadowing (turning
  `shadowing_enabled` off made it WORSE, 9 handovers), and it is not the cell
  count. Use the eager defaults only when the user is asking to STUDY ping-pong,
  and then say in `description` that that is what the scenario shows.

  A path that fits the budget but misses the boundary is the same silent failure
  as one that misses the budget. If the two constraints cannot both be met, move
  the CELLS closer together — that shrinks `sep`, which pulls the crossover to
  where a short path can reach it.

  If it does not hold, SHORTEN THE PATH and bring the cells closer so the
  crossover stays inside it, then check again — do not merely warn about it. And
  `description` must describe the config you RETURN, not the reasoning that led
  there. Two measured failures on 2026-09-09, both from the same habit: one
  description concluded "cells 20 m apart, a 4 m path" and shipped cells 100 m
  apart with an 80 m path; another correctly stated "the UEs will travel ~4 m of
  that 40 m path" and shipped the 40 m path anyway. Working the answer out and
  then shipping the draft is, to the user, the same as not working it out.
- START ON THE SERVING CELL'S SIDE OF THE CROSSOVER, and do not overshoot into the
  target. The handover point is NOT the midpoint: the stronger cell pushes it
  toward the weaker one, roughly by the ratio the powers imply, so with 40 dBm at
  x=0 and 46 dBm at x=600 the crossover sits near x=250, not x=300. A UE started
  at x=480 is already inside the strong cell's dominance — measured: it attached
  to the target at t=0, walked the whole path and reported ZERO handovers, which
  looks exactly like a broken handover but is a scenario with nothing to hand over.
  Put the UE clearly on the WEAKER cell's side (well under the crossover), and end
  the path clearly past it. Both ends stay tens of metres clear of any cell.
- Never end a waypoint path ON a cell. The spawn point is nudged clear of base
  stations automatically; waypoints are NOT, and a UE that arrives at d=0 from a
  cell is a log10(0) in the 3GPP pathloss — the same blank-run failure the spawn
  guard exists to prevent.

MEASUREMENT QUALITY (these shape how trustworthy the NUMBERS are, and none of them
crash anything):
- 3+ cells is the design point to prefer when the user has no opinion, for two
  measured reasons — neither of which forbids fewer. First, neighbour metrics are
  per-neighbour, so a run reports 34-36 distinct KPM names at one or two cells
  against 37-39 at three. That is the only remaining reason.

  The PHY abort that used to make two-cell runs risky is FIXED (2026-09-12). It
  was never a density or topology rule: two UEs could accept the same random
  access response and end up sharing one RNTI on the handover target cell, then
  answer the same uplink grant, so the gNB received two non-orthogonal wanted
  signals (`NS_ASSERT Sum((*rxPsd) * (*m_rxSignal)) == 0.0`,
  mmwave-interference.cc:108). See engines/nsoran/rar-rnti-hijack-guard.patch;
  it ships in engine image 1.9. Do NOT warn the user about it any more, and do
  not reach for `sched_direction: dl_only` or `harq_enabled: false` to avoid it.

  THIS IS NOT A LICENCE TO ADD CELLS THE USER DID NOT ASK FOR. If they say two
  sites, build TWO. The old engine really did need a third and the rulebook said
  so; that is fixed, and adding one anyway now changes the topology the user is
  trying to study — a third cell is a third interferer and a third handover
  target. Three cells is the default only when the user expressed no preference.
- ~{UE_PER_CELL_MIN}-{UE_PER_CELL_MAX} UEs per cell: fewer and PRB reads ~0 (nothing to schedule);
  many more on ONE cell corrupts the mmWave heap.
- Bandwidth {BW_MHZ_MIN}-{BW_MHZ_MAX} MHz matched to the offered load; very wide bandwidth
  under-loads cells and PRB goes sparse.
- Frequency/band: any real band (FR1 sub-6 GHz or FR2 mmWave).
- ANTENNA ORIENTATION IS OFF BY DEFAULT. `antenna.azimuth_deg` and `tilt_deg` do
  NOTHING unless `settings.antenna_element` is `'3gpp'`: the array's rotation is
  applied, but the default isotropic element has no directionality, so rotating
  it cancels out exactly. So there are only two honest choices:
    * the design DEPENDS on where cells point (sectors, "A faces east", a
      coverage story) -> set `settings.antenna_element: "3gpp"` and say in
      `description` that orientation is active; or
    * it does not -> leave `azimuth_deg` at 0 rather than writing a decorative
      value.
  Writing azimuths with the isotropic element is the worst of both: the canvas
  draws the sector cones, the description tells a directional story, and the
  simulation runs omnidirectionally. Measured 2026-09-09 on a handover design
  whose two cells were given 90 deg and 270 deg to face each other — neither
  angle reached the physics.
- THE PANEL SIZE IS A SPEED LEVER, and the only one there is. The 3GPP steering
  vector is computed per antenna element, and that trigonometry is the engine's
  hottest code, so `antenna.massive_mimo.rows`/`cols` set the cost. Measured
  2026-09-13 at 3 cells / 30 UEs: 8x8 (the default, 64 elements) took 28.1 wall
  seconds per simulated second, 4x4 took 19.6 — 1.43x more network time per run.
  It curves off there; 2x2 only reached 1.52x. What it costs is array gain, and
  it lands on the best links: at 4x4 handovers and MEAN throughput were
  unchanged while PEAK throughput fell about 35%.
  So choose by what the scenario is ASKING: 4x4 when the question is timing,
  mobility or a handover sequence that needs simulated seconds; 8x8 when it is
  link budget, peak rate or coverage. Say which you chose in `description`.
- DRAWING BUILDINGS CHANGES EVERY LINK, not just the ones behind them. With no
  buildings the engine decides LOS/NLOS statistically; with any building it
  switches the whole network to real geometry. So one building in a far corner
  still moves every number — measured, an off-path building took mean throughput
  from 3377 to 3193 kbps, and a scene where statistical NLOS becomes
  deterministic LOS gets FASTER, not slower. Add buildings when blocking is the
  POINT of the scenario, not as scenery.

WHEN THE USER'S REQUEST CONFLICTS WITH MEASUREMENT QUALITY:
Follow the USER. If they ask for 1 UE, build 1 UE — do not quietly round it up to
satisfy the load guidance. Then say what it costs, in one sentence at the end of
`description`, e.g. "1 UE: the handover event is real, PRB and
throughput will read near zero." A scenario that answers the question the user
asked, with its limitation stated, beats a scenario that measures well and tests
something else. HARD LIMITS are the exception: never cross those, and if a request
requires it, build the closest legal scenario and say which limit you hit.

GOLDEN RULE for a scenario with no stated preference: keep cells LOADED but not
overloaded — {GNB_MIN}-{GNB_MAX} cells, ~{UE_PER_CELL_MIN}-{UE_PER_CELL_MAX} UEs/cell, bandwidth matched to demand.
"""
