"""Tier B: a KPM frame stream with no ns-3 under it.

The fast tier's job is to answer the same question Tier A answers — what do
this scenario's counters look like — without stepping a discrete-event
simulator 4000 times per reported second. `window_model` does the per-window
physics; this supplies it with a scenario and turns its output into frames
shaped exactly like the ones the real engine's bridge emits, so the same
harvest, the same criterion and the same UI read both tiers.

**It is built on the ENGINE'S OWN PARAMETERS, not on the config.**
`main.nsoran_params()` is what decides where the cells and UEs actually stand:
it clamps counts to the validated envelope, recentres the whole scene, pushes
UEs out of buildings and off base stations, and only then hands ns-3 a list of
positions. Reading the config instead would compare two different scenarios
and call the difference model error. Taking the params means the geometry is
identical by construction — the same clamps, the same evictions, the same
coordinates the engine is given.

WHAT THIS TIER DOES NOT HAVE, stated here rather than discovered later:

  - **No mobility.** Positions are the ones the scenario starts with, so
    handovers are zero by construction. Tier A measures ten on the reference.
    A moving Tier B is the next phase, not a missing constant.
  - **No RLC or HARQ state.** Retransmissions show up as block errors, which
    are counted, but nothing queues across windows, so `DRB.BufferSize.Qos`
    is the offered backlog rather than an evolving one.
  - **Path loss is the estimator's** 3GPP-UMa-LOS log-distance form from
    `ns3_bridge._received_power_dbm`: no shadowing, no NLOS, no building
    terms. Tier A runs the full 3GPP spatial channel. This is the largest
    remaining physical difference between the tiers and it is deliberate —
    it is also where the 4000x came from.
"""
from __future__ import annotations

import hashlib
import math
import statistics

import window_model as wm
from ns3_bridge import _noise_floor_dbm, _re_spreading_db, spectral_overlap

UE_HEIGHT_DEFAULT_M = 1.5

# ── 3GPP UMi Street Canyon, which is NOT what most runs use ─────────────────
# THE CLAIM THAT USED TO BE HERE — "the launcher defaults to
# --propScenario=umi" — IS FALSE, and it was false for the whole of Phase 2.
# `schema.SimulationSettings.scenario` defaults to UMA_BUILDINGS, which
# `main._PROP_SCENARIO` maps to `uma`; the `"umi"` in that lookup is the
# fallback for a MISSING field, not the default. So every run this tier has
# ever been compared against ran UMa, and this function computed UMi.
#
# It surfaced from reading the command line an actual run was launched with
# — `--propScenario=uma` sits in it plainly — and not from reading any of
# the code, which is the same lesson [[nsg-numerology-fr1]] records.
#
# This path is kept and unchanged, because a config CAN select UMi and then
# it is right. `uma_path_loss_db` below is what the default takes.
#
# The engine runs ThreeGppUmiStreetCanyonPropagationLossModel over a
# stochastic ThreeGppUmiStreetCanyonChannelConditionModel, with shadowing
# on. Tier B used
# a LOS-only log-distance formula, and the consequence was not a few dB: with
# every UE in line of sight NOBODY falls below the MCS table and the model
# serves all 36 where the engine serves 55%, which was the whole of the
# six-UE-per-cell disagreement.
#
# Copied from the engine's source, not from TR 38.901, and they differ:
#
#   P_LOS    = 18/d2D + exp(-d2D/36) * (1 - 18/d2D),  1 below 18 m
#   PL_LOS   = 32.4 + 21*log10(d3D) + 20*log10(fc_GHz)
#   PL_NLOS  = max(PL_LOS,
#                  22.4 + 35.3*log10(d3D) + 21.3*log10(fc_GHz) - 0.3*(hUt-1.5))
#   sigma    = 4 dB in LOS, 7.82 dB in NLOS
#
# THE HEIGHT ASSIGNMENT IS THE ENGINE'S, NOT THE SPECIFICATION'S.
# `GetBsUtHeightsUmiStreetCanyon` reads `hBs = (heightA == 10) ? heightA :
# heightB` — it identifies the base station by being exactly 10 m tall. Our
# cells stand at 25 m, so neither node matches and it returns hBs = 1.5,
# hUt = 25: the UE and the mast swapped. ns-3 warns that the UT height is out
# of range and carries on. The visible effect is the -0.3*(hUt - 1.5) term,
# which takes 7 dB OFF the NLOS loss instead of the 0 it would take at a
# 1.5 m UE. Reproducing the engine means reproducing that, so this does.
UMI_SIGMA_LOS_DB = 4.0
UMI_SIGMA_NLOS_DB = 7.82


def _parse_buildings(spec: str) -> list[tuple]:
    """'xmin,xmax,ymin,ymax,zmin,zmax,wall,...' per ';'-separated record.

    Only the box matters here — the wall material and the room grid feed the
    engine's O2I loss, which this tier does not model because the platform
    evicts every UE from every building before the run
    (`main._push_out_of_buildings`), so every link is outdoor-to-outdoor.
    """
    out = []
    for item in (spec or "").split(";"):
        f = [v for v in item.split(",") if v.strip()]
        if len(f) < 6:
            continue
        try:
            x0, x1, y0, y1, z0, z1 = (float(v) for v in f[:6])
        except ValueError:
            continue
        out.append((min(x0, x1), max(x0, x1), min(y0, y1),
                    max(y0, y1), min(z0, z1), max(z0, z1)))
    return out


def _segment_hits_box(a: tuple, b: tuple, box: tuple) -> bool:
    """Slab method: does the segment a-b pass through this axis-aligned box?"""
    x0, x1, y0, y1, z0, z1 = box
    lo, hi = 0.0, 1.0
    for i, (mn, mx) in enumerate(((x0, x1), (y0, y1), (z0, z1))):
        p, q = a[i], b[i] - a[i]
        if abs(q) < 1e-12:
            if p < mn or p > mx:
                return False
            continue
        t0, t1 = (mn - p) / q, (mx - p) / q
        if t0 > t1:
            t0, t1 = t1, t0
        lo, hi = max(lo, t0), min(hi, t1)
        if lo > hi:
            return False
    return True


def line_of_sight(a: tuple, b: tuple, boxes: list) -> bool:
    """`BuildingsChannelConditionModel::IsLineOfSightBlocked`, inverted.

    When the config carries buildings the engine STOPS drawing the LOS coin:
    `scenario-zero` swaps the channel-condition model for
    `BuildingsChannelConditionModel`, which intersects the segment with the
    real geometry and has no probability in it at all. A model that kept
    rolling the stochastic UMi probability would put UEs in line of sight
    through a wall and behind an obstacle at random, which is the opposite of
    what the user drew.
    """
    return not any(_segment_hits_box(a, b, box) for box in boxes)


_MASK64 = (1 << 64) - 1
_TAG_CACHE: dict[str, int] = {}


def _tag(k: str) -> int:
    """FNV-1a 64 of a key tag, because `hash()` of a str is NOT stable.

    THIS WAS A REAL BUG AND IT HID FOR THE WHOLE OF PHASE 2. `_draw` mixed
    `hash(k)` for every element of the key, and the elements include the
    literal tags "los", "sh1" and "sh2". Python randomises `str.__hash__` per
    interpreter unless PYTHONHASHSEED is set, so every process drew a
    DIFFERENT shadowing and line-of-sight realisation from the same seed:

        $ for i in 1 2 3; do python -c '...; print(_draw("los", 7, 0, 1))'
        0.59439   0.586969   0.237114

    The in-process reproducibility test passed because it never crossed a
    process boundary, and the promise in the docstring below — that a run
    reproduces itself — was true only within one. Every tool that computed
    Tier B in a separate run from the one it compared against was comparing
    two different realisations of the channel and calling the difference model
    error. The shadowing sigma is 4 to 7.8 dB, so that is up to 15 dB of
    spurious disagreement per link.

    Ints are already stable and skip this entirely; the tags are three bytes
    and cached, so the cost does not show in the profile.
    """
    v = _TAG_CACHE.get(k)
    if v is None:
        v = 0xCBF29CE484222325
        for c in k.encode():
            v = ((v ^ c) * 0x100000001B3) & _MASK64
        _TAG_CACHE[k] = v
    return v


def _draw(*key) -> float:
    """A stable uniform [0,1) for one link.

    Shadowing and the LOS coin are random in the engine and fixed here, keyed
    on the link. Tier B cannot reproduce WHICH UE the engine shadows — nothing
    reproduces a different RNG stream — only the distribution. Making the draw
    deterministic at least means a Tier B run reproduces ITSELF, which a
    criterion built on repeats depends on — see `_tag` for how long that was
    only half true.

    SplitMix64, not blake2b. The first version hashed `repr(key)` with a
    cryptographic digest, which is the obvious way to get a stable number and
    cost 29% of the whole window in the profile — three draws per link per
    frame, 51,840 of them. Nothing here needs a hash to be hard to invert,
    only well distributed and reproducible, and SplitMix64 is both.
    """
    z = 0x9E3779B97F4A7C15
    for k in key:
        n = (k & _MASK64) if isinstance(k, int) else _tag(str(k))
        z = (z + n * 0x9E3779B97F4A7C15) & _MASK64
        z ^= z >> 30
        z = (z * 0xBF58476D1CE4E5B9) & _MASK64
        z ^= z >> 27
        z = (z * 0x94D049BB133111EB) & _MASK64
        z ^= z >> 31
    return z / 2 ** 64


def _draw_stream(n: int, *key) -> list[float]:
    """`n` uniforms for ONE link, from one seeded state.

    `_draw` re-mixes the whole key on every call, which is the right shape
    when a link needs three numbers and the wrong one when it needs fifty:
    the condition mixture asks for `n_samples` draws per link per cell, and
    at 36 UEs and 6 cells that is twenty thousand key-mixings a window —
    measured, the frame went from 2 ms to 46. Advancing a state is one
    multiply-xor chain instead of one per key element, and the sequence is
    just as reproducible because the state is still keyed.
    """
    z = int(_draw(*key) * 2 ** 64) & _MASK64
    out = []
    for _ in range(n):
        z = (z + 0x9E3779B97F4A7C15) & _MASK64
        x = z
        x ^= x >> 30
        x = (x * 0xBF58476D1CE4E5B9) & _MASK64
        x ^= x >> 27
        x = (x * 0x94D049BB133111EB) & _MASK64
        x ^= x >> 31
        out.append(x / 2 ** 64)
    return out


def _gauss(u1: float, u2: float) -> float:
    """Box-Muller, so the shadowing is normal and not uniform."""
    u1 = min(max(u1, 1e-12), 1 - 1e-12)
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def umi_path_loss_db(d2d: float, d3d: float, freq_ghz: float,
                     h_ut: float, key, los: bool | None = None,
                     shadowing: bool = True) -> float:
    """One link's path loss: the LOS condition, its formula, then shadowing.

    `los` overrides the coin when the geometry already decided — see
    `line_of_sight`.
    """
    d2d, d3d = max(1.0, d2d), max(1.0, d3d)
    if los is None:
        p_los = 1.0 if d2d <= 18.0 else (
            18.0 / d2d + math.exp(-d2d / 36.0) * (1.0 - 18.0 / d2d))
        los = _draw("los", *key) < p_los
    pl_los = 32.4 + 21.0 * math.log10(d3d) + 20.0 * math.log10(max(0.1, freq_ghz))
    if los:
        pl, sigma = pl_los, UMI_SIGMA_LOS_DB
    else:
        pl_nlos = (22.4 + 35.3 * math.log10(d3d)
                   + 21.3 * math.log10(max(0.1, freq_ghz)) - 0.3 * (h_ut - 1.5))
        pl, sigma = max(pl_los, pl_nlos), UMI_SIGMA_NLOS_DB
    if not shadowing:
        return pl
    return pl + sigma * _gauss(_draw("sh1", *key), _draw("sh2", *key))


# ── 3GPP UMa, which is what the reference scenario actually runs ─────────────
# FOUND BY READING THE LAUNCHED COMMAND LINE, not the source: the engine is
# started with `--propScenario=uma`, and `main._PROP_SCENARIO` maps both UMa
# and UMa-Buildings to it. The criterion's own scenario is UMA_BUILDINGS. So
# this model computed UMi Street Canyon for the scenario every agreement
# number was measured on, and the note above — "the launcher defaults to
# --propScenario=umi" — was true of the DEFAULT and not of the run.
#
# TR 38.901 Table 7.4.1-1, as ns-3's ThreeGppUmaPropagationLossModel
# implements it:
#
#   P_LOS   = 18/d2D + exp(-d2D/63) * (1 - 18/d2D),  1 below 18 m
#             (the C'(hUt) term is zero for hUt <= 13 m, which every UE here is)
#   d_BP    = 4 * (hBs - 1) * (hUt - 1) * fc_Hz / c        hE = 1 m below 13 m
#   PL_LOS  = 28.0 + 22*log10(d3D) + 20*log10(fc_GHz)              d2D <= d_BP
#           = 28.0 + 40*log10(d3D) + 20*log10(fc_GHz)
#             - 9*log10(d_BP^2 + (hBs - hUt)^2)                    d2D >  d_BP
#   PL_NLOS = max(PL_LOS,
#                 13.54 + 39.08*log10(d3D) + 20*log10(fc_GHz) - 0.6*(hUt - 1.5))
#   sigma   = 4 dB in LOS, 6 dB in NLOS
#
# AND THE HEIGHT SWAP DOES NOT HAPPEN HERE. The UMi note above explains that
# `GetBsUtHeightsUmiStreetCanyon` identifies the base station by being exactly
# 10 m tall, so our 25 m masts fall through and the UE and the mast swap. UMa's
# reference base-station height IS 25 m, which is exactly what our cells are,
# so the helper identifies them correctly however it is written — by the
# equality or by "the taller node is the BS", the answer is the same for this
# geometry and no guess is needed.
#
# WHAT ADOPTING IT DID TO THE SCORE, honestly: it is a WASH, and it is kept
# anyway. On the freshest engine run of the reference scenario (30 frames,
# 8 realisations) it moves QosFlow.PdcpPduVolumeDL from 0.86 to 0.97 and
# RRU.PrbUsedDl from 0.88 to 1.07. Across the six older 40-frame breadth
# captures the mean |ratio - 1| goes 0.475 -> 0.523, i.e. slightly worse.
#
# The tie is broken by which one is TRUE, not by which one scores: the
# engine is launched with `--propScenario=uma` and computing UMi for it is
# wrong however the arithmetic lands. Choosing the better-scoring error is
# how a model acquires a constant that later has to be withdrawn.
#
# One number came out of that comparison worth keeping on its own: the two
# engine captures of the SAME baseline scenario report 3,221,331 and
# 5,694,297 bytes per UE — 1.77x apart, against a 0.5% repeat noise floor
# measured inside a single sweep. Different frame counts and different
# capture dates, so any ratio quoted from one capture carries that much
# uncertainty before the model is even involved.
UMA_SIGMA_LOS_DB = 4.0
UMA_SIGMA_NLOS_DB = 6.0
UMA_HE_M = 1.0                 # effective environment height below hUt 13 m
C_M_S = 3e8


def uma_path_loss_db(d2d: float, d3d: float, freq_ghz: float, h_bs: float,
                     h_ut: float, key, los: bool | None = None,
                     shadowing: bool = True) -> float:
    """One link's UMa path loss: the LOS condition, its formula, then shadowing."""
    d2d, d3d = max(1.0, d2d), max(1.0, d3d)
    if los is None:
        p_los = 1.0 if d2d <= 18.0 else (
            18.0 / d2d + math.exp(-d2d / 63.0) * (1.0 - 18.0 / d2d))
        los = _draw("los", *key) < p_los
    fc = max(0.1, freq_ghz)
    d_bp = (4.0 * max(0.1, h_bs - UMA_HE_M) * max(0.1, h_ut - UMA_HE_M)
            * fc * 1e9 / C_M_S)
    pl1 = 28.0 + 22.0 * math.log10(d3d) + 20.0 * math.log10(fc)
    if d2d <= d_bp:
        pl_los = pl1
    else:
        pl_los = (28.0 + 40.0 * math.log10(d3d) + 20.0 * math.log10(fc)
                  - 9.0 * math.log10(d_bp ** 2 + (h_bs - h_ut) ** 2))
    if los:
        pl, sigma = pl_los, UMA_SIGMA_LOS_DB
    else:
        pl_nlos = (13.54 + 39.08 * math.log10(d3d) + 20.0 * math.log10(fc)
                   - 0.6 * (h_ut - 1.5))
        pl, sigma = max(pl_los, pl_nlos), UMA_SIGMA_NLOS_DB
    if not shadowing:
        # `settings.shadowing_enabled` reaches ns-3 as `--shadowingEnabled`
        # and the slow tier has always honoured it. This tier added the
        # shadowing term unconditionally, so a config that switched it OFF
        # got it anyway — and the two tiers then disagreed by 4 to 7.8 dB
        # per link for a reason no comparison would have named.
        return pl
    return pl + sigma * _gauss(_draw("sh1", *key), _draw("sh2", *key))


def _parse_points(spec: str) -> list[tuple[float, float, float]]:
    out = []
    for item in (spec or "").split(";"):
        if not item.strip():
            continue
        parts = [float(v) for v in item.split(",")]
        while len(parts) < 3:
            parts.append(0.0)
        out.append((parts[0], parts[1], parts[2]))
    return out


# `SpectrumChannel::MaxLossDb`, as `run_scenario.sh` ships it. Above this
# much PROPAGATION loss ns-3 does not deliver the transmission at all — no
# signal and, more to the point, no interference. The launcher's own comment
# records the sweep: bit-identical down to 110 dB for ~2x the speed, and
# breaking badly at 90.
MAX_LOSS_DB = 110.0


def los_probability(d2d: float, scenario: str = "umi") -> float:
    """TR 38.901 Table 7.4.2-1, the same P_LOS both branches draw against."""
    if d2d <= 18.0:
        return 1.0
    if scenario == "uma":
        return 18.0 / d2d + math.exp(-d2d / 63.0) * (1.0 - 18.0 / d2d)
    return 18.0 / d2d + math.exp(-d2d / 36.0) * (1.0 - 18.0 / d2d)


# HOW OFTEN THE ENGINE RE-DRAWS LOS/NLOS. `channelCondUpdateMs` is a scenario
# global defaulting to 100 ms, and the launcher never overrides it, so
# `ThreeGppChannelConditionModel::UpdatePeriod` is 100 ms in every run this
# platform has ever made. ns-3's own default is 0 — never update — which is
# what Tier B modelled: one coin per link, frozen for the whole run.
#
# It is not a detail. At the reference geometry a link's two branches are
# 88.5 dB (LOS) and 112.3 dB (NLOS), so a link that re-draws is a 24 dB
# STEP FUNCTION, and P_LOS at 173 m is 0.16, which puts its standard
# deviation at 24*sqrt(0.16*0.84) = 8.8 dB. Measured over the reference's
# 36 UEs this one mechanism gives a per-UE SINR spread of 10.50 dB against
# the engine's own per-transmission trace at 12.68 — where flat Rayleigh
# gives 5.57 and five previous closed forms all scored worse than that.
COND_UPDATE_MS = 100.0


def _rx_dbm(tx_dbm: float, gnb: tuple, ue: tuple, freq_ghz: float, key,
            boxes: list | None = None, scenario: str = "umi",
            shadowing: bool = True,
            force_los: bool | None = None) -> float:
    d2d = math.hypot(ue[0] - gnb[0], ue[1] - gnb[1])
    dz = max(1.0, gnb[2] - (ue[2] or UE_HEIGHT_DEFAULT_M))
    d3d = math.hypot(d2d, dz)
    los = force_los
    if boxes:
        # Buildings decide it outright: the engine switches to
        # BuildingsChannelConditionModel and the geometry is the answer.
        los = line_of_sight((ue[0], ue[1], ue[2] or UE_HEIGHT_DEFAULT_M),
                            gnb, boxes)
    if scenario == "uma":
        return tx_dbm - uma_path_loss_db(
            d2d, d3d, freq_ghz, gnb[2], ue[2] or UE_HEIGHT_DEFAULT_M, key, los,
            shadowing)
    # h_ut as the ENGINE resolves it for UMi — see the note above. Our masts
    # are not 10 m, so the helper hands the model the mast's height as the
    # UE's. UMa's reference height IS 25 m and the swap does not happen there.
    h_ut = gnb[2] if gnb[2] != 10.0 else (ue[2] or UE_HEIGHT_DEFAULT_M)
    return tx_dbm - umi_path_loss_db(d2d, d3d, freq_ghz, h_ut, key, los,
                                     shadowing)


# ── Per-cell carriers ────────────────────────────────────────────────────────
# Tier A runs ONE carrier. `scenario-zero` builds a single MmWaveHelper from a
# single `--frequency` and `--bandwidth`, so `nsoran_params` has always taken
# `base_stations[0].radio.band` for the whole scene and quietly flattened any
# config whose cells differ. Tier B computes a window per cell in closed form,
# where a per-cell carrier costs nothing, so it reads `gnb_bands` when the
# params carry one and falls back to the scene band when they do not.
#
# This is the first thing the fast tier can do that the slow tier cannot, and
# it is not a flourish: every VIAVI reference scenario on disk is two-band
# (N77 3600 MHz / 40 MHz over N28 728 MHz / 10 MHz), which is sixteen of the
# twenty captured runs and all of the eighteen-cell ones.


def cell_bands(params: dict, n_cells: int) -> list[tuple[float, float, int]]:
    """(centre MHz, width MHz, numerology) per cell, gnb_positions-aligned.

    The scene band repeats when `gnb_bands` is empty, which is every
    single-band config, so nothing about those changes.
    """
    scene = (float(params.get("freq_hz", 3.5e9)) / 1e6,
             float(params.get("bw_hz", 50e6)) / 1e6,
             int(params.get("numerology", 2) or 2))
    out: list[tuple[float, float, int]] = []
    for item in (params.get("gnb_bands") or "").split(";"):
        f = [v for v in item.split(",") if v.strip()]
        if len(f) < 2:
            continue
        try:
            out.append((float(f[0]), float(f[1]),
                        int(float(f[2])) if len(f) > 2 else scene[2]))
        except ValueError:
            continue
    while len(out) < n_cells:
        out.append(out[-1] if out else scene)
    return out[:n_cells]


def panel_spec(params: dict, cell: int) -> tuple:
    """(azimuth_deg, tilt_deg, rows, cols, hbw, vbw, boresight_gain) for a cell.

    `main._gnb_antenna_specs` emits
    'azimuth,tilt,rows,cols,polslant,hspace,vspace,hbw,vbw,gain' per cell and
    leaves the whole string empty when every cell is on the engine's default
    panel, so the fallbacks here are that default.
    """
    spec = (params.get("gnb_antenna") or "").split(";")
    az = tilt = 0.0
    rows = cols = 8
    hbw = vbw = 65.0
    gain = 8.0
    # ns-3's UniformPlanarArray defaults, in wavelengths. They were parsed
    # out of the spec string's positions and thrown away, because the only
    # array term was the PEAK gain and spacing does not move a peak.
    d_h = d_v = 0.5
    if cell < len(spec) and spec[cell]:
        f = spec[cell].split(",")
        def _f(i, d):
            try:
                return float(f[i])
            except (IndexError, ValueError):
                return d
        az, tilt = _f(0, 0.0), _f(1, 0.0)
        rows, cols = int(_f(2, 8)), int(_f(3, 8))
        d_h, d_v = _f(5, 0.5), _f(6, 0.5)
        hbw, vbw, gain = _f(7, 65.0), _f(8, 65.0), _f(9, 8.0)
    return az, tilt, rows, cols, hbw, vbw, gain, d_h, d_v


# THE UE HAS AN ARRAY TOO. `MmWaveHelper` gives every UE a 2x2
# UniformPlanarArray (`m_uePhasedArrayModelFactory`, NumRows/NumColumns = 2)
# and points it at its serving cell, which is 6.02 dB on the serving link
# that this tier did not model at all. Measured against the engine's own PHY
# trace it is half of a 16 dB gap; the other half is the link pruning above.
UE_ARRAY_ROWS = 2
UE_ARRAY_COLS = 2


def ue_array_spec(params: dict) -> tuple[int, int, float, float]:
    """(rows, cols, d_h, d_v) of the UE's own panel, in wavelengths."""
    def _i(k, d):
        try:
            return max(1, int(params.get(k) or d))
        except (TypeError, ValueError):
            return d
    def _f(k, d):
        try:
            return float(params.get(k) or d)
        except (TypeError, ValueError):
            return d
    return (_i("ue_ant_rows", UE_ARRAY_ROWS), _i("ue_ant_cols", UE_ARRAY_COLS),
            _f("ue_ant_h_sp", 0.5), _f("ue_ant_v_sp", 0.5))


def array_gain_db(params: dict, cell: int) -> float:
    """Beamforming gain of the gNB panel, toward the UE it is serving.

    The engine gives every gNB a `UniformPlanarArray` and steers it at the
    served UE each slot, so the serving link gets the coherent gain of the
    whole panel: 10*log10(rows*cols), 18 dB on the default 8x8.

    LEAVING THIS OUT IS NOT A SMALL ERROR. Without it every UE in a six-cell
    scene sits near 0 dB and the model puts 100% of blocks on QPSK, where the
    engine measures 92% on 64QAM. An 18 dB array is the difference.

    It is the LATTICE's gain only. The ELEMENT's pattern is `element_gain_db`
    and applies to every link, served or not — see `_element_gain_toward`.
    """
    rows, cols = panel_spec(params, cell)[2:4]
    return wm.array_gain_db(rows, cols)


def _element_gain_toward(params: dict, cell: int, gnb: tuple,
                         ue: tuple) -> float:
    """The element's gain from this cell toward this UE.

    Azimuth arrives in the CONFIG'S COMPASS CONVENTION — 0 = North, clockwise
    — and the scenario converts it to ns-3's phi, which is measured from +x
    (East) counter-clockwise: `phi = 90 - compass`. Getting that backwards
    points every sector at its mirror image, which on a three-sector site is
    indistinguishable from a wiring bug until the throughput comes out wrong.

    Positive `tilt_deg` is DOWNtilt, so it subtracts from the inclination: a
    UE at the boresight depression angle lands at theta = 90, the pattern's
    peak. The test that proved the lever passes -80 for "80 degrees up".
    """
    phi, theta = _local_angles(params, cell, gnb, ue)
    # The beamwidths and the boresight gain are the model's compiled-in ones,
    # not the panel spec's — see the note on `window_model.ELEMENT_HPBW_DEG`.
    return wm.element_gain_db(phi, min(180.0, max(0.0, theta)))


def _local_angles(params: dict, cell: int, gnb: tuple,
                  ue: tuple) -> tuple[float, float]:
    """(phi, theta) of a point in this panel's own frame, in degrees.

    Shared by the ELEMENT pattern and the ARRAY factor, because the two must
    agree about where the panel is looking or the sector's own UEs land in
    its sidelobes.
    """
    az, tilt = panel_spec(params, cell)[:2]
    dx, dy = ue[0] - gnb[0], ue[1] - gnb[1]
    dz = gnb[2] - (ue[2] or UE_HEIGHT_DEFAULT_M)
    phi = math.degrees(math.atan2(dy, dx)) - (90.0 - az)
    d2d = math.hypot(dx, dy)
    # theta from zenith: 90 at the horizon, more as the UE drops below it.
    theta = 90.0 + math.degrees(math.atan2(dz, max(1e-6, d2d))) - tilt
    return phi, min(180.0, max(0.0, theta))


def _band_priority(bands: list) -> dict[int, int]:
    """Reselection priority per cell: the higher the carrier, the higher it ranks.

    TS 38.304 reselects on an ABSOLUTE PRIORITY per frequency, configured by
    the operator, not on signal strength across bands — otherwise every UE
    lands on the low band, which propagates better, and the capacity layer
    sits empty. Operators configure it the other way round for exactly that
    reason: the wide high band carries the traffic and the low band is the
    coverage floor underneath it.

    Ranking by centre frequency is that convention, and it is a convention
    rather than a measurement — a config that wants the opposite has no way to
    say so yet. It reproduces what the reference captures show: VIAVI's
    two-band runs put 4654 UE-reports on N77 against 979 on N28, 83% on the
    high band with the low band underneath.
    """
    return {j: round(b[0]) for j, b in enumerate(bands)}


def seed_of(params: dict, seed: int | None) -> int:
    """The realisation to draw. An explicit `seed` wins, because the
    comparison tools sweep it deliberately; otherwise it is the CONFIG'S
    `random_seed`, which this tier used to ignore entirely — every scenario
    drew realisation 0 however the user set the seed, while the slow tier
    honoured it as `--rngRun`."""
    if seed is not None:
        return int(seed)
    try:
        value = params.get("rng_run")
        if value is None:
            value = params.get("random_seed")
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def link_state(params: dict, positions: list | None = None,
               seed: int | None = None,
               serving: dict[int, int] | None = None,
               rrc_state: dict | None = None) -> dict:
    """Serving cell, RSRP and both directions' SINR for every UE.

    Interference is every other cell IN PROPORTION TO THE SPECTRUM IT SHARES.
    With one carrier that is every other cell at full weight, which is what
    this did before and what Tier A can express; with `gnb_bands` the weight
    is `spectral_overlap`, so a cell on a disjoint band is not an interferer
    at all.

    The array gain goes on the SERVING link only. An interfering cell's beam is
    pointed at its own UE, not at this one, and a phased array does not create
    power — it moves it — so the average gain in an unrelated direction is 0 dB.
    That asymmetry is the whole reason a dense network still reaches 64QAM, and
    modelling the array as a flat gain on every link would cancel it out
    exactly and change nothing.

    Serving selection uses the gain too, which matters when panels differ: the
    strongest RAW signal is not the strongest link if one cell has a bigger
    array than another. Across BANDS it is priority first and strength second
    — see `_band_priority`.

    THE UPLINK IS NOT THE DOWNLINK WITH A SMALLER NUMBER. Three things change
    and they pull in opposite directions:

      * the transmitter is the UE at `ue_tx_dbm` (the engine's default is
        30 dBm, generous for a handset) instead of the cell at 40-46, so the
        budget is 10-16 dB tighter;
      * the receiver is the gNB with `gnb_nf_db`, not the UE with `ue_nf_db`;
      * and the INTERFERERS ARE UEs, not cells. A neighbour cell's uplink
        interference at this cell is whichever of its UEs is transmitting,
        which is a different place every slot and much closer to the ground
        than a mast. Averaging its members' received power over the cell is
        the expectation under the round-robin the scheduler actually runs.

    Path loss is drawn ONCE per link and reused in both directions, because
    it is reciprocal and because two draws would put a UE in line of sight
    one way and behind a wall the other.

    `serving` is the cell each UE is currently ON. It costs one extra SINR
    per UE and it is what makes a handover POLICY possible: without it the
    single-carrier path computes the winner's SINR and nothing else, so
    `apply_ho_policy` had no number for the cell it was being asked to
    keep the UE on.
    """
    seed = seed_of(params, seed)
    gnbs = _parse_points(params.get("gnb_positions", ""))
    ues = positions if positions is not None else _parse_points(
        params.get("ue_positions", ""))
    powers = [float(p) for p in (params.get("gnb_powers") or "").split(";") if p]
    while len(powers) < len(gnbs):
        powers.append(powers[-1] if powers else 40.0)
    bands = cell_bands(params, len(gnbs))
    multiband = len({b[:2] for b in bands}) > 1
    gains = [array_gain_db(params, j) for j in range(len(gnbs))]
    boxes = _parse_buildings(params.get("buildings", ""))
    ue_nf = params.get("ue_nf_db")
    gnb_nf = params.get("gnb_nf_db")
    ue_tx_dbm = float(params.get("ue_tx_dbm", 30.0))
    # A per-cell noise rise, in dB — what a `noise_rise` anomaly injects and
    # what an interference source outside the model looks like from inside it.
    rise = [float(x) for x in str(params.get("noise_rise_db") or "").split(",")
            if x.strip()]
    def _rise(j):
        return rise[j] if j < len(rise) else 0.0
    noise = [10 ** ((_noise_floor_dbm(b[1] * 1e6, ue_nf) + _rise(j)) / 10)
             for j, b in enumerate(bands)]
    noise_ul = [10 ** ((_noise_floor_dbm(b[1] * 1e6, gnb_nf) + _rise(j)) / 10)
                for j, b in enumerate(bands)]
    # The overlap matrix is geometry-free, so it is built once per call rather
    # than once per UE. Co-channel scenes get a matrix of ones and the maths
    # below reduces to exactly what the single-band model computed.
    ov = [[spectral_overlap(bands[j], bands[k]) for k in range(len(gnbs))]
          for j in range(len(gnbs))]
    prio = _band_priority(bands)
    # The element pattern is per LINK, not per cell, so it costs an atan2 per
    # UE per cell and is only computed when the engine would apply one. The
    # default is `isotropic`, whose ns-3 model has a flat 0 dB gain, so the
    # whole term vanishes for every scenario the criterion was measured on.
    directional = str(params.get("antenna_element") or "isotropic") == "3gpp"
    # WHICH CHANNEL THE ENGINE IS ACTUALLY RUNNING. `prop_scenario` reaches
    # ns-3 as `--propScenario`, and the criterion's own scenario is
    # UMA_BUILDINGS, so every agreement number this tier has was measured
    # against UMa while this function computed UMi. Anything but `uma` keeps
    # the UMi Street Canyon path unchanged; `rma` is not implemented and
    # falls back to UMi rather than silently claiming to be rural.
    scenario = "uma" if str(params.get("prop_scenario") or "") == "uma" else "umi"
    # `shadowing` arrives as "1"/"0" from `nsoran_params`, the same string the
    # engine gets as `--shadowingEnabled`.
    shadowing = str(params.get("shadowing", "1")) not in ("0", "False", "false")
    try:
        max_loss = float(params.get("max_loss_db") or MAX_LOSS_DB)
    except (TypeError, ValueError):
        max_loss = MAX_LOSS_DB
    ue_spec = ue_array_spec(params)

    out = {}
    ul_mw: list[list[float]] = []      # [ue][cell] uplink power arriving, mW
    ul_heard: list[set] = []           # [ue] which cells are within range
    ul_los: list[list[float]] = []
    ul_nlos: list[list[float]] = []
    ul_plos: list[list[float]] = []
    for i, ue in enumerate(ues, start=1):
        # BOTH BRANCHES OF EVERY LINK, not one frozen coin.
        #
        # The engine re-draws each link's LOS/NLOS condition every
        # `COND_UPDATE_MS`, so within one KPM window a link visits both
        # states — and the two are 24 dB apart at this geometry. Tier B
        # drew the coin once and kept it, which made a UE either LOS or
        # NLOS for the whole run: the population mean came out right and
        # every UE's VARIANCE came out as fading alone, which is the
        # 5.57 dB that five spread predictors were built on top of.
        #
        # `_rx_dbm` still decides the condition when buildings are present,
        # because there the engine uses BuildingsChannelConditionModel and
        # the geometry IS the condition — nothing re-draws.
        rx_los, rx_nlos, p_los = [], [], []
        for j, g in enumerate(gnbs):
            d2d = math.hypot(ue[0] - g[0], ue[1] - g[1])
            fixed = None
            if boxes:
                fixed = line_of_sight((ue[0], ue[1], ue[2] or UE_HEIGHT_DEFAULT_M),
                                      g, boxes)
            a = _rx_dbm(powers[j], g, ue, bands[j][0] / 1e3, (seed, j, i),
                        boxes, scenario, shadowing, force_los=True)
            b = _rx_dbm(powers[j], g, ue, bands[j][0] / 1e3, (seed, j, i),
                        boxes, scenario, shadowing, force_los=False)
            if directional:
                # On EVERY link, serving or interfering, and on BOTH
                # branches — a sector pointed away is 30 dB quieter
                # whichever way the condition coin lands.
                eg = _element_gain_toward(params, j, g, ue)
                a += eg
                b += eg
            rx_los.append(a)
            rx_nlos.append(b)
            p_los.append(1.0 if fixed is True else 0.0 if fixed is False
                         else los_probability(d2d, scenario))
        # The level the window reports is the mixture's, in dB — a UE that
        # is LOS a sixth of the time is not an NLOS UE.
        rx = [p * a + (1.0 - p) * b
              for a, b, p in zip(rx_los, rx_nlos, p_los)]
        if not rx:
            continue
        # WHAT THE ENGINE CANNOT HEAR. `SpectrumChannel::MaxLossDb` drops a
        # link outright above 110 dB of PROPAGATION loss — before any
        # antenna gain, which is where ns-3 applies the cut — and the
        # launcher has shipped that since the pruning sweep. Tier B applied
        # none of it and summed interference from every cell in the scene.
        #
        # Measured on the reference: under the cut a UE hears 0.81 cells on
        # average, at most two. Summing six instead both RAISES the mean
        # interference (Tier B reads 28-38 dB where the engine's trace reads
        # 44.4) and SMOOTHS it, which is most of why the per-UE spread came
        # out unimodal at 5 dB against a measured 12.7.
        #
        # ON THE INTERFERENCE, NOT ON THE ATTACHMENT. ns-3 applies it to
        # both, but the engine's 36 UEs are all attached and all reported,
        # so in ITS shadowing draw every serving link came in under the
        # cut. Tier B's draw is a different sample of the same
        # distribution and the reference scenario sits right on the edge —
        # several UEs land within 1 dB of 110 — so a symmetric cut drops
        # 13 of 36 UEs, and that alone swamps everything: measured, it
        # takes delivered volume from 1.06 to 2.36 against the engine,
        # because the survivors inherit the dropped UEs' airtime. Dropping
        # a UE over 0.2 dB of our own shadowing sample reports the sample,
        # not the behaviour.
        audible = [j for j in range(len(rx)) if powers[j] - rx[j] <= max_loss]
        rx_mw = [10 ** (r / 10) for r in rx]
        heard = set(audible)
        # The UE's own panel. It is steered at the cell serving this UE, so
        # the serving link gets the whole 2x2 (6.02 dB) and an interferer
        # gets whatever that same beam is worth in ITS direction — the same
        # asymmetry as the gNB's array, from the other end of the link.
        u_rows, u_cols, u_dh, u_dv = ue_spec
        ue_peak = wm.array_gain_db(u_rows, u_cols)
        # Where each cell sits as seen FROM the UE, so the UE's beam toward
        # its serving cell can be evaluated toward the others. Bearings in a
        # common frame: the UE panel has no configured orientation, and only
        # the difference between two directions matters to the array factor.
        ue_dir = []
        for gn in gnbs:
            ddx, ddy = gn[0] - ue[0], gn[1] - ue[1]
            ue_dir.append((math.degrees(math.atan2(ddy, ddx)),
                           90.0 - math.degrees(math.atan2(
                               gn[2] - (ue[2] or UE_HEIGHT_DEFAULT_M),
                               max(1e-6, math.hypot(ddx, ddy))))))
        if not multiband:
            # One carrier: the serving choice cannot depend on the SINR of the
            # cells it excludes, so only the winner's needs computing — plus
            # the cell the UE is already on, which the handover policy has to
            # be able to price.
            best = max(range(len(rx)), key=lambda j: rx[j] + gains[j])
            cand = [best]
            j0 = (serving or {}).get(i)
            if j0 is not None and 0 <= j0 < len(rx) and j0 != best:
                cand.append(j0)
        else:
            cand = range(len(rx))
        sinr_of, interf_of, rsrp_of = {}, {}, {}
        for j in cand:
            sig = 10 ** ((rx[j] + gains[j] + ue_peak) / 10)
            others = []
            for k in heard:
                if k == j or ov[j][k] <= 0:
                    continue
                # The UE is looking at cell j; this is what its panel gives
                # toward cell k.
                ug = wm.upa_gain_db(u_rows, u_cols, u_dh, u_dv,
                                    ue_dir[k][0], ue_dir[k][1],
                                    ue_dir[j][0], ue_dir[j][1])
                others.append((k, rx_mw[k] * ov[j][k] * 10 ** (ug / 10)))
            tot = sum(p for _, p in others)
            sinr_of[j] = 10 * math.log10(sig / (noise[j] + tot))
            interf_of[j] = others
            rsrp_of[j] = (rx[j] + gains[j] + ue_peak
                          - _re_spreading_db_hz(bands[j][1] * 1e6, bands[j][2]))
        if multiband:
            best = _select_cell(rx, gains, sinr_of, prio)
        # Reciprocity: the same link, the other transmitter. `rx[j]` is
        # `powers[j] - pathloss + element`, so swapping the transmit power
        # gives the uplink arrival without drawing the channel again.
        ul_mw.append([10 ** ((r - powers[j] + ue_tx_dbm) / 10)
                      for j, r in enumerate(rx)])
        # BOTH BRANCHES UPLINK TOO. The link is the same one, so it carries
        # the same condition — and giving the downlink a condition mixture
        # while the uplink kept the frozen mean opened a gap the engine
        # does not have: its own trace puts UL-DL at -7.5 dB median with 10
        # of 36 UEs BETTER on the uplink, and a one-sided model read -11.7
        # with none.
        ul_los.append([10 ** ((a - powers[j] + ue_tx_dbm) / 10)
                       for j, a in enumerate(rx_los)])
        ul_nlos.append([10 ** ((b - powers[j] + ue_tx_dbm) / 10)
                        for j, b in enumerate(rx_nlos)])
        ul_plos.append(list(p_los))
        # ...and which of those links are in range. The cut is on the
        # PROPAGATION loss, which is reciprocal, so a cell this UE cannot
        # hear cannot hear this UE either. It applies to the INTERFERENCE
        # only, the same call as the downlink: zeroing the serving entry
        # too leaves a UE transmitting nothing into a log10.
        ul_heard.append(set(audible))
        out[i] = {"cell": best,
                  "rsrp_dbm": rsrp_of[best],
                  "sinr_db": sinr_of[best],
                  # Every candidate priced, so `apply_ho_policy` can move a UE
                  # BACK to its serving cell and have the frame downstream see
                  # that cell's interference and noise, not the winner's.
                  "_cand": {j: (rsrp_of[j], sinr_of[j], interf_of[j], noise[j])
                            for j in sinr_of},
                  # Every link's two branches and its LOS probability, so
                  # the mixture can re-draw the condition the way the
                  # engine does instead of inheriting one frozen coin.
                  "_branches": (list(rx_los), list(rx_nlos), list(p_los)),
                  "_ue_dir": list(ue_dir),
                  "_interferers": interf_of[best],
                  "_noise_mw": noise[best],
                  "_pos": ue}
    # ── THE MIXTURE ─────────────────────────────────────────────────────
    # Everything above answers "what is this link worth ON AVERAGE". The
    # engine's per-transmission trace says the average is a value the link
    # never takes: its SINR is a step function that jumps tens of dB, and
    # two mechanisms make it jump — the LOS/NLOS condition re-drawing every
    # 100 ms, and which UE each neighbour's beam is pointing at. Both are
    # sampled here, because both need every UE's serving cell to be known
    # and that is only true once the loop above has finished.
    _condition_mixture(params, out, gnbs, powers, gains, ov, noise,
                       max_loss, ue_spec, seed, rrc_state=rrc_state)
    _add_uplink(out, ul_mw, gains, ov, noise_ul,
                ue_peak=wm.array_gain_db(ue_spec[0], ue_spec[1]),
                ul_heard=ul_heard, br=(ul_los, ul_nlos, ul_plos), seed=seed,
                n_samples=max(4, min(64, round(
                    window_s(params) / (COND_UPDATE_MS / 1e3)))))
    return out


def _condition_mixture(params: dict, out: dict, gnbs: list, powers: list,
                       gains: list, ov: list, noise: list, max_loss: float,
                       ue_spec: tuple, seed: int,
                       n_samples: int | None = None,
                       rrc_state: dict | None = None) -> None:
    """Re-draw every link's condition and every neighbour's beam, in place.

    Sets `sinr_db` to the mixture's mean and adds `sinr_sd_db`, which is the
    spread that five closed forms tried and failed to predict as a fading
    sigma. It is not one: measured on the engine's own trace, consecutive
    transmissions 0.25 ms apart differ by 0.020 dB while the same UE swings
    tens of dB between them.

    Three things re-draw per sample and all three are the engine's:

      * THE CONDITION. 100 ms, and the two branches are 24 dB apart, so a
        link alternates between 88 dB and 112 dB of loss.
      * WHAT IS AUDIBLE. `MaxLossDb` is applied per sample, not to the
        average, because a neighbour is within range during its LOS
        episodes and gone during its NLOS ones. That is the "a neighbour
        transmitting or not" term, worth up to 16.8 dB on the trace.
      * WHERE THE BEAM POINTS. A neighbour serves one of its own UEs and
        its array gain toward this one follows — up to 50.6 dB on the
        trace, and the largest of the three.

    `n_samples` IS the window's condition epochs — `window_s / 100 ms`, ten
    at the defaults — and that is not a speed compromise. The window
    averages over exactly that many draws, so sampling more estimates a
    distribution the window never realises: measured, n=10 puts the median
    per-UE spread at 15.64 dB against the engine's 15.22 while n=96 gives
    16.80, and costs a third as much.
    """
    if not out:
        return
    if n_samples is None:
        n_samples = max(4, min(64, round(
            window_s(params) / (COND_UPDATE_MS / 1e3))))
    # HANDOVERS INSIDE THE WINDOW, and the definition is the whole of it.
    #
    # Counting every change in the strongest-cell RANKING gave 1743 over
    # twenty windows against the engine's single digits, and neither
    # time-to-trigger (137) nor the L3 filter on top of it (493, because
    # two filtered levels cross more often than two raw ones) rescued it.
    # The event is narrower: a UE hands over when ITS SERVING CELL GOES
    # OUT OF RANGE while another is in it. `MaxLossDb` decides that, the
    # condition re-draw supplies the events, and the L3 filter is what
    # keeps a single 100 ms dropout from being a handover.
    #
    # Measured against the engine's 14.4 a second: unfiltered 42, at the
    # shipped `rsrp_filter_coeff` of 4 nineteen, at 8 eight. Nothing
    # fitted — the lever is at its own value.
    _ho = ho_policy(params)
    a3_lin = 10 ** (_ho["a3_offset"] / 10)
    l3a = _ho["l3_alpha"]
    admit = _ho["admit"]
    members: dict[int, list[int]] = {}
    for imsi, l in out.items():
        members.setdefault(l["cell"], []).append(imsi)
    u_rows, u_cols, u_dh, u_dv = ue_spec
    ue_peak = wm.array_gain_db(u_rows, u_cols)
    n_cell = len(gnbs)
    # (cell j steered at its UE k) toward victim i — built lazily, because
    # only the pairs that actually come up are ever asked for.
    ang: dict[tuple[int, int], tuple[float, float]] = {}
    af: dict[tuple[int, int, int], float] = {}
    pos_of = {imsi: l for imsi, l in out.items()}

    def _ang(j, imsi):
        got = ang.get((j, imsi))
        if got is None:
            got = ang[(j, imsi)] = _local_angles(
                params, j, gnbs[j], pos_of[imsi]["_pos"])
        return got

    def _af(j, k, i):
        key = (j, k, i)
        got = af.get(key)
        if got is None:
            rows, cols, d_h, d_v = (panel_spec(params, j)[2:4]
                                    + panel_spec(params, j)[7:9])
            a_i, a_k = _ang(j, i), _ang(j, k)
            got = af[key] = wm.upa_gain_db(rows, cols, d_h, d_v,
                                           a_i[0], a_i[1], a_k[0], a_k[1])
        return got

    for i, l in out.items():
        br = l.get("_branches")
        if not br:
            continue
        rx_los, rx_nlos, p_los = br
        ue_dir = l.get("_ue_dir") or []
        c = l["cell"]
        # EVERYTHING THAT DOES NOT DEPEND ON THE SAMPLE, ONCE. The inner
        # loop runs n_cell * n_samples times per UE and the only things in
        # it that change are the condition coin and which UE a neighbour
        # is serving; the UE's own array gain toward each cell and both
        # branches in linear power do not. Hoisting them took the frame
        # from 46 ms to single digits.
        lin_los = [10 ** (x / 10) for x in rx_los]
        lin_nlos = [10 ** (x / 10) for x in rx_nlos]
        cut_los = [powers[j] - rx_los[j] <= max_loss for j in range(n_cell)]
        cut_nlos = [powers[j] - rx_nlos[j] <= max_loss for j in range(n_cell)]
        ue_g = []
        for j in range(n_cell):
            if j == c or j >= len(ue_dir) or c >= len(ue_dir):
                ue_g.append(1.0)
                continue
            ue_g.append(10 ** (wm.upa_gain_db(
                u_rows, u_cols, u_dh, u_dv, ue_dir[j][0], ue_dir[j][1],
                ue_dir[c][0], ue_dir[c][1]) / 10))
        others_j = [j for j in range(n_cell)
                    if j != c and ov[c][j] > 0 and (members.get(j) or [])]
        sig_gain = 10 ** ((gains[c] + ue_peak) / 10)
        ncw = noise[c]
        # One stream per link instead of one key-mixing per sample.
        cond_u = [_draw_stream(n_samples, seed, "cond", i, j)
                  for j in range(n_cell)]
        beam_u = {j: _draw_stream(n_samples, seed, "beam", i, j)
                  for j in others_j}
        vals = []
        reachable = 0
        # THE FILTER HAS MEMORY ACROSS WINDOWS. An RRC does not restart
        # its L3 average every second, and restarting it here made the
        # first epoch of every window unfiltered: 26 handovers a second
        # against 19 when the state carries, and the engine's 14.4.
        st = (rrc_state or {}).get(i) or {}
        flips = 0
        cur = st.get("cell", c)
        flt = st.get("flt")
        if cur >= n_cell:
            cur = c
        if flt is None:
            # WARM, not cold. Seeding the filter from the first epoch's
            # draw leaves that epoch unfiltered, and it is the one most
            # likely to look like a handover: the first window counted 26
            # events against 19 in every window after it. The filter's own
            # steady state is the condition mixture's mean, which needs no
            # history to know.
            flt = [(p_los[j] * lin_los[j] + (1.0 - p_los[j]) * lin_nlos[j])
                   * 10 ** (gains[j] / 10) for j in range(n_cell)]
        g_of = [10 ** (gains[j] / 10) for j in range(n_cell)]
        per_cell_i: dict[int, float] = {}
        for t in range(n_samples):
            los_of = [cond_u[j][t] < p_los[j] for j in range(n_cell)]
            aud_of = [j for j in range(n_cell)
                      if (cut_los[j] if los_of[j] else cut_nlos[j])]
            # THE RRC's OWN VIEW: `Fn = (1-a)Fn-1 + aMn`, a = 2^(-k/4).
            raw_l = [(lin_los[j] if los_of[j] else lin_nlos[j]) * g_of[j]
                     for j in range(n_cell)]
            flt = (list(raw_l) if flt is None
                   else [(1.0 - l3a) * o + l3a * r
                         for o, r in zip(flt, raw_l)])
            if admit and aud_of and cur not in aud_of:
                b = max(aud_of, key=lambda j: flt[j])
                if b != cur and flt[b] > flt[cur] * a3_lin:
                    flips += 1
                    cur = b
            # THE CUT IS ON THE INTERFERENCE, NOT ON THE ATTACHMENT — the
            # same call as above, now per epoch. Skipping the epochs where
            # the serving link is out of range CONDITIONS the mean on the
            # link being good, and measured that put the per-UE mean at
            # 54.3 dB against the engine's 45.5 while the spread stayed
            # right: the excursions were the correct size and the baseline
            # they hung from was not.
            # WHETHER THIS UE IS REACHABLE AT ALL THIS EPOCH. Its own
            # serving link flips LOS/NLOS with everything else, and at
            # this geometry the NLOS branch is often past MaxLossDb — so
            # the UE is served during its LOS episodes and not otherwise.
            # That is what the engine's reports look like: 0 to 29 UEs a
            # frame of 36 configured, median 19, and only 29 distinct over
            # forty frames. Tier B served all 36 every window.
            if cut_los[c] if los_of[c] else cut_nlos[c]:
                reachable += 1
            sig = (lin_los[c] if los_of[c] else lin_nlos[c]) * sig_gain
            tot = 0.0
            for j in others_j:
                if not (cut_los[j] if los_of[j] else cut_nlos[j]):
                    continue          # out of range in this epoch
                mem = members[j]
                r = beam_u[j][t]
                g_beam = _af(j, mem[int(r * len(mem)) % len(mem)], i)
                contrib = ((lin_los[j] if los_of[j] else lin_nlos[j])
                           * ov[c][j] * ue_g[j] * 10 ** (g_beam / 10))
                per_cell_i[j] = per_cell_i.get(j, 0.0) + contrib
                tot += contrib
            vals.append(10 * math.log10(sig / max(ncw + tot, 1e-30)))
        if not vals:
            continue
        l["sinr_db"] = statistics.fmean(vals)
        l["sinr_sd_db"] = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        # The share of the window this UE can be scheduled in at all. It
        # caps its turns: a UE reachable a third of the time cannot hold
        # more than a third of the slots however much it wants.
        l["served_frac"] = reachable / max(1, len(vals))
        l["ho_in_window"] = flips
        if rrc_state is not None:
            rrc_state[i] = {"cell": cur, "flt": flt}
        # The interferer list the rest of the tier reads, PER CELL and as
        # the mixture's mean. Collapsing it to one entry keyed by whatever
        # the inner loop left `j` at is how a list that looks populated
        # names the wrong cell.
        l["_interferers"] = [(j, v / len(vals))
                             for j, v in sorted(per_cell_i.items()) if v > 0]
        # ...and the serving entry of `_cand`, which the handover policy
        # prices the UE's current cell from. Leaving it at the pre-mixture
        # value makes the policy compare a mixture SINR against a frozen
        # one, which is a comparison of two different quantities.
        cand = l.get("_cand")
        if cand and c in cand:
            r0, _, i0, n0 = cand[c]
            cand[c] = (r0, l["sinr_db"], l["_interferers"], noise[c])
            l["_noise_mw"] = noise[c]


def _add_uplink(out: dict, ul_mw: list, gains: list, ov: list,
                noise_ul: list, ue_peak: float = 0.0,
                ul_heard: list | None = None,
                br: tuple | None = None, seed: int = 0,
                n_samples: int = 10) -> None:
    """Fill in `sinr_ul_db`, once every UE's serving cell is known.

    The victim is a CELL and the interferers are the UEs of other cells, so
    this cannot be done in the same pass as the downlink — a UE's uplink
    interference depends on where the other UEs ended up attaching.

    A neighbour cell contributes the MEAN received power of its own members,
    which is the expectation over which of them holds the slot: the scheduler
    is round-robin and one UE transmits at a time, so over a window each
    member is equally likely to be the interferer. Taking the strongest
    member instead would model the worst slot as though it were every slot.

    Cost is O(cells x UEs) for the accumulation and O(UEs x cells) for the
    sum, not O(UEs^2) — the per-cell means are shared by every victim.

    `ue_peak` is the UE's OWN array, and it belongs on the uplink signal by
    reciprocity: the same 2x2 panel that collects the downlink is pointed
    at the same cell when the UE transmits. Adding it to the downlink and
    not here opened a 6 dB gap in the wrong direction and took the measured
    UL-DL difference from -6.9 dB to -19.5.
    """
    if not out:
        return
    members: dict[int, list[int]] = {}
    index = {imsi: n for n, imsi in enumerate(out)}
    for imsi, l in out.items():
        members.setdefault(l["cell"], []).append(index[imsi])
    heard_of = ul_heard if ul_heard is not None else [
        set(range(len(noise_ul))) for _ in out]
    # mean_at[s][c] — mean uplink power reaching cell s from cell c's members.
    mean_at: dict[int, dict[int, float]] = {}
    for s in range(len(noise_ul)):
        row = {}
        for c, mem in members.items():
            if c == s or ov[s][c] <= 0:
                continue
            row[c] = (sum(ul_mw[n][s] if (ul_heard is None
                                           or s in ul_heard[n]) else 0.0
                          for n in mem) / len(mem)) * ov[s][c]
        mean_at[s] = row
    if br is None:
        for imsi, l in out.items():
            s = l["cell"]
            sig = ul_mw[index[imsi]][s] * 10 ** ((gains[s] + ue_peak) / 10)
            interf = sum(mean_at[s].values())
            l["sinr_ul_db"] = 10 * math.log10(
                max(sig, 1e-30) / (noise_ul[s] + interf))
        return
    # THE SAME MIXTURE, THE OTHER WAY UP. Each link re-draws its condition
    # every epoch; which member of a neighbour cell is transmitting rotates
    # with the round robin. Both were averages here and the downlink's were
    # not, which is the whole of the asymmetry.
    ul_los, ul_nlos, ul_plos = br
    g = 10 ** (ue_peak / 10)
    for imsi, l in out.items():
        s = l["cell"]
        n = index[imsi]
        sig_g = 10 ** (gains[s] / 10) * g
        rivals = [(c, mem) for c, mem in members.items()
                  if c != s and ov[s][c] > 0 and mem]
        u_sig = _draw_stream(n_samples, seed, "ulc", imsi, s)
        u_who = {c: _draw_stream(n_samples, seed, "ulw", imsi, c)
                 for c, _ in rivals}
        u_cnd = {c: _draw_stream(n_samples, seed, "uli", imsi, c)
                 for c, _ in rivals}
        vals = []
        for t in range(n_samples):
            los = u_sig[t] < ul_plos[n][s]
            sig = (ul_los[n][s] if los else ul_nlos[n][s]) * sig_g
            tot = 0.0
            for c, mem in rivals:
                m = mem[int(u_who[c][t] * len(mem)) % len(mem)]
                if s not in heard_of[m]:
                    continue
                ml = u_cnd[c][t] < ul_plos[m][s]
                tot += (ul_los[m][s] if ml else ul_nlos[m][s]) * ov[s][c]
            vals.append(10 * math.log10(
                max(sig, 1e-30) / (noise_ul[s] + tot)))
        l["sinr_ul_db"] = statistics.fmean(vals)
        l["sinr_ul_sd_db"] = statistics.pstdev(vals) if len(vals) > 1 else 0.0


def faults_firing(params: dict, sim_t: float, win: float = 1.0) -> list[str]:
    """Which scheduled faults overlap this window, as readable labels.

    `apply_faults` answers what the RAN looks like; this answers WHICH
    fault made it look that way, which is what the horizon audit needs:
    `anomalies_configured` against `anomalies_fired` is the check that
    catches a fault scheduled past the end of the run, and a tier that
    reports no fired faults fails it on every run that has one.
    """
    names = {0: "radio_degradation", 1: "cell_outage",
             2: "noise_rise", 3: "handover_block"}
    out = []
    for f in str(params.get("gnb_faults") or "").split(";"):
        parts = f.split(",")
        if len(parts) < 5:
            continue
        try:
            cell, start, dur, kind = (int(parts[0]), float(parts[1]),
                                      float(parts[2]), int(parts[3]))
        except (TypeError, ValueError):
            continue
        if start < sim_t and start + dur > sim_t - win:
            out.append(f"{names.get(kind, kind)} cell={cell} "
                       f"start={start:g}s dur={dur:g}s")
    return out


def apply_faults(params: dict, sim_t: float,
                 win: float = 1.0) -> tuple[dict, bool]:
    """The config's scheduled anomalies, at this instant.

    `gnb_faults` is "cell,start_s,duration_s,type,value" per fault — the
    same string the engine parses, so a scenario cannot mean one thing on
    one tier and another on the other. The four types:

        0  tx power steps to `value` dBm (a degradation)
        1  the cell goes out entirely
        2  the cell's noise floor rises by `value` dB
        3  handovers are blocked (the anchor RRC's, so network-wide)

    A fault counts for a window when it OVERLAPS it, not when it happens to
    straddle the instant the window closes. The point test looked right and
    silently dropped every fault shorter than a window — including the whole
    of the slow tier's own fixture set, whose anomalies last 0.2 s against a
    1 s window here. What a window model genuinely cannot express is the
    sub-window EDGE: a fault covering 30% of a window is on for all of it,
    and its effect is overstated by the rest. Rounding it to nothing is not
    the more conservative choice, it is the one that reports no fault at all.

    Returns the adjusted payload and whether handovers are blocked.
    """
    spec = str(params.get("gnb_faults") or "")
    if not spec:
        return params, False
    powers = [float(x) for x in str(params.get("gnb_powers") or "").split(";") if x]
    off = {int(c) for c in str(params.get("cells_off") or "").split(",")
           if c.strip().lstrip("-").isdigit()}
    rise: dict[int, float] = {}
    blocked = False
    touched = False
    for f in spec.split(";"):
        parts = f.split(",")
        if len(parts) < 5:
            continue
        try:
            cell, start, dur, kind, val = (int(parts[0]), float(parts[1]),
                                           float(parts[2]), int(parts[3]),
                                           float(parts[4]))
        except (TypeError, ValueError):
            continue
        if not (start < sim_t and start + dur > sim_t - win):
            continue
        touched = True
        if kind == 3:
            blocked = True
        elif kind == 1:
            off.add(cell)
        elif kind == 2:
            rise[cell] = rise.get(cell, 0.0) + val
        elif kind == 0 and 0 <= cell < len(powers):
            powers[cell] = val
    if not touched:
        return params, False
    q = dict(params)
    q["gnb_powers"] = ";".join(f"{p:.1f}" for p in powers)
    q["cells_off"] = ",".join(str(c) for c in sorted(off))
    if rise:
        n = max(len(powers), max(rise) + 1)
        q["noise_rise_db"] = ",".join(f"{rise.get(j, 0.0):.1f}" for j in range(n))
    # A cell that is OUT is not a quiet cell — same -200 dBm convention the
    # RIC console uses for a cell an xApp switched off.
    if off:
        q["gnb_powers"] = ";".join(
            f"{(-200.0 if j in off else p):.1f}" for j, p in enumerate(powers))
    return q, blocked


def call_duty(params: dict, sim_t: float, win: float) -> dict[int, float]:
    """Each UE's ON fraction of this window, from its own call model.

    `ue_slices` carries 'qci,rate[,on_s,off_s,dist,proto]' per UE slot and
    this tier read the QCI and nothing else — so `avg_call_duration_s`,
    `avg_idle_duration_s` and `call_distribution` were slow-only although
    their values were already sitting in the payload it parses.

    Phase is per UE, so six UEs on one call model are not all on the phone
    at the same instant — which is the difference between a cell carrying
    its mean load and a cell carrying all of it at once. Where the cycle is
    short against the window this comes out as a duty factor; where it is
    long the UE is simply on or off for whole windows, and both fall out of
    the same calculation rather than needing a rule for each.
    """
    out: dict[int, float] = {}
    for i, item in enumerate((params.get("ue_slices") or "").split(";"), start=1):
        f = item.split(",")
        if len(f) < 4:
            continue
        try:
            on, off = float(f[2]), float(f[3])
        except (TypeError, ValueError):
            continue
        period = on + off
        if period <= 0 or off <= 0:
            continue
        phase = (_tag(f"call{i}") % 10000) / 10000.0 * period
        t0 = sim_t - win + phase
        # How much of [t0, t0+win) lands in an ON stretch of the cycle.
        whole, rem = divmod(win, period)
        onv = whole * on
        a = t0 % period
        b = a + rem
        onv += max(0.0, min(on, b) - min(on, a))
        if b > period:
            onv += max(0.0, min(on, b - period))
        out[i] = max(0.0, min(1.0, onv / win))
    return out


def ho_policy(params: dict) -> dict:
    """The handover thresholds, off the payload.

    The tier picked the strongest cell every window, so a UE on a boundary
    changed cell whenever the shadowing draw tipped — the engine does not,
    and the fields that stop it were all reaching ns-3 and none reaching
    here:

        ho_sinr_db    A3 offset: how far a neighbour must BEAT the serving
                      cell before it is a candidate (LteEnbRrc::HoSinrDifference)
        ho_hyst_db    hysteresis on top of it (HoHysteresisDb)
        ho_a3_entry   whether the A3 entry condition applies at all
        ho_a2_db      A2: the serving level below which the UE looks for
                      one at all, -1000 meaning never
        qrxlevmin     the minimum level a cell may be camped on

    A window model has no time-to-trigger — that is one of the per-TTI
    mechanisms it structurally cannot express — so `ho_ttt_ms` stays with
    the slow tier. The LEVEL conditions it can express exactly.
    """
    def _f(k, d):
        try:
            v = params.get(k)
            return d if v is None else float(v)
        except (TypeError, ValueError):
            return d
    mode = str(params.get("ho_mode") or "DynamicTtt")
    return {
        "a3_offset": _f("ho_sinr_db", 0.0) + _f("ho_hyst_db", 0.0),
        "a3_entry": str(params.get("ho_a3_entry", "0")) in ("1", "True", "true"),
        "a2_db": _f("ho_a2_db", -1000.0),
        # Whether a handover can happen AT ALL. `NoAuto` leaves it to an
        # xApp and `admit_handover=0` has the target cell refuse — two
        # different reasons for the same outcome here, and the pair is how
        # you tell a mobility problem from a coverage one.
        "admit": (str(params.get("admit_handover", "1")) in ("1", "True", "true")
                  and mode != "NoAuto"),
        # `Threshold` skips the time-to-trigger the other modes impose. A
        # window model has no TTT either way, so what it can express is that
        # Threshold is the LOOSEST mode: no hysteresis on top of the margin.
        "threshold_mode": mode == "Threshold",
        # The L3 filter. `rsrp_filter_coeff` is the 3GPP k, and the filter
        # weight is 1/2^(k/4) — 0.5 at the default 4. It is why an RRC does
        # not chase a single bad sample, and without it this tier decided on
        # raw instantaneous RSRP.
        "l3_alpha": 2.0 ** (-_f("rsrp_filter_coeff", 4.0) / 4.0),
        # How often the RRC looks. Longer than a window and it does not look
        # in every one.
        "check_s": max(0.0, _f("ho_check_ms", 100.0)) / 1e3,
        # 3GPP-ENCODED. `nsoran_params` sends `q_rx_lev_min_dbm / 2`, the
        # form ns-3 wants, so -70 on the wire is -140 dBm at the antenna.
        "q_rx_lev_min_dbm": _f("qrxlevmin", -1e9) * 2.0,
    }


def _select_cell(rx: list, gains: list, sinr_of: dict, prio: dict) -> int:
    """The highest-priority band this UE can actually be served on.

    The gate is the MCS table's own edge — `mcs.link` returns `in_outage` when
    even MCS 0 does not fit — so no threshold is invented here. A UE that
    cannot decode anything on the capacity layer falls to the next band down,
    and one that can decode nowhere takes the strongest cell it has, which is
    what the single-band model always did.
    """
    from mcs import link as mcs_link
    order = sorted({prio[j] for j in sinr_of}, reverse=True)
    for band in order:
        here = [j for j in sinr_of if prio[j] == band]
        ok = [j for j in here if not mcs_link(sinr_of[j])["in_outage"]]
        if ok:
            return max(ok, key=lambda j: rx[j] + gains[j])
    return max(sinr_of, key=lambda j: rx[j] + gains[j])


def sinr_spread_db(signal_mw: float, noise_mw: float,
                   interferer_mw: list[float], loads: list[float]) -> float:
    """How far this UE's SINR moves between transmissions, in dB.

    Two terms, both derived, added in quadrature because they are
    independent:

    FADING. Rayleigh gives an exponentially distributed instantaneous power,
    and the standard deviation of 10*log10 of an exponential is
    (10/ln 10) * pi/sqrt(6) = 5.57 dB. `window_model.FADING_SIGMA_DB`.

    INTERFERENCE. A neighbour only interferes in the slots it is transmitting
    in, and Tier B knows how busy each neighbour is because it just scheduled
    them. Treating each as on with probability rho gives
    Var(I) = sum(I_k^2 * rho_k * (1 - rho_k)), and a small perturbation of
    the denominator moves the SINR by (10/ln 10) * sd(I) / (N + E[I]) dB.

    This is the same effect [[nsg-bursty-interference-works]] measured from
    the other side: same mean interference, flat 0.55% block errors against
    bursty 9.72%. Burstiness is not a detail of the interference, it is what
    the interference DOES.
    """
    if not interferer_mw:
        return wm.FADING_SIGMA_DB
    mean_i = sum(i * r for i, r in zip(interferer_mw, loads))
    var_i = sum((i ** 2) * r * (1.0 - r) for i, r in zip(interferer_mw, loads))
    denom = max(noise_mw + mean_i, 1e-30)
    sigma_i = (10.0 / math.log(10)) * math.sqrt(var_i) / denom
    return math.hypot(wm.FADING_SIGMA_DB, min(sigma_i, 20.0))


def _re_spreading_db_hz(bw_hz: float, numerology: int = 2) -> float:
    """`ns3_bridge._re_spreading_db` takes a band object; this takes the width
    and the numerology the params actually carry.

    Total carrier power spread over its subcarriers, so it depends on the
    SUBCARRIER SPACING and not only the width. The numerology was hardcoded
    to 2 here, which was harmless while every cell ran the engine's compiled-in
    60 kHz and wrong the moment cells could carry their own: a 40 MHz carrier
    at 30 kHz has twice the subcarriers of one at 60 kHz, so its RSRP is 3 dB
    lower than this used to report.
    """
    class _B:
        bandwidth_mhz = bw_hz / 1e6
    _B.numerology = max(0, int(numerology or 0))
    return _re_spreading_db(_B())


def slice_policy(params: dict) -> tuple[dict, dict, dict, dict]:
    """(imsi -> QCI, QCI -> max_pct) from the payload the engine is handed.

    `ue_slices` is 'qci,rate[,on,off,dist,proto]' per UE slot, UE-aligned;
    `slice_quotas` is 'qci:max:min:dedicated' per policy. The engine
    partitions on the QCI, not on the slice label the UI shows — a config can
    call two groups eMBB and uRLLC and hand the scheduler the same number.
    """
    tags: dict[int, int] = {}
    for i, item in enumerate((params.get("ue_slices") or "").split(";"), start=1):
        f = item.split(",")
        if f and f[0].strip():
            try:
                tags[i] = int(float(f[0]))
            except ValueError:
                pass
    quota: dict[int, float] = {}
    floor: dict[int, float] = {}
    ded: dict[int, float] = {}
    for item in (params.get("slice_quotas") or "").split(";"):
        f = item.split(":")
        if len(f) >= 2:
            try:
                quota[int(float(f[0]))] = float(f[1])
                if len(f) >= 3 and float(f[2]) > 0:
                    floor[int(float(f[0]))] = float(f[2])
                # 'dedicated' is airtime the slice holds whether or not it
                # has traffic — the one share that is NOT work-conserving,
                # and the field was parsed off the wire and dropped.
                if len(f) >= 4 and float(f[3]) > 0:
                    ded[int(float(f[0]))] = float(f[3])
            except ValueError:
                pass
    return tags, quota, floor, ded


def window_s(params: dict) -> float:
    """How much airtime one report covers — `params['ind_period_s']`.

    NOT a constant. It is `settings.report_interval_ms`, it reaches ns-3 as
    `--indicationPeriodicity`, and it sets how many slots a counter is summed
    over: at 0.2 s a fully scheduled UE reports 800 transport blocks, at 1.0 s
    it reports 4000. Hardcoding a second here made every block count come out
    five times the engine's, which looked like a broken scheduler and was a
    broken constant.
    """
    return float(params.get("ind_period_s") or 0.5)


def frame(params: dict, tick: int, sim_t: float,
          demand_bps: dict[int, float] | None = None,
          decay_db: float | None = None,
          positions: list | None = None,
          backlog: dict | None = None,
          handovers: int = 0,
          links: dict | None = None,
          seed: int | None = None,
          ul_backlog: dict | None = None,
          idle_for: dict | None = None) -> dict:
    """One KPM window, in the shape the real engine's bridge emits.

    `backlog` is mutated in place with what is still queued when the window
    closes, so a caller stepping frames keeps the congestion between them.
    """
    # `frames()` already resolved these to count handovers; recomputing them
    # here ran the whole channel twice per frame.
    links = links if links is not None else link_state(params, positions, seed)
    bands = cell_bands(params, len(_parse_points(params.get("gnb_positions", ""))))
    # With no `gnb_bands` the numerology is the scene's, which `nsoran_params`
    # has already clamped to what ns-3 will run, and the window model clamps it
    # again on the way in so the two tiers agree. With one, the carrier is
    # Tier A's to express and the clamp would silently move a 30 kHz cell to
    # 60 kHz and double its block count.
    engine_clamp = not (params.get("gnb_bands") or "").strip()
    # The config levers a window model can honour — the RLC buffer, the
    # AMC's BER target, a pinned MCS, the outage threshold, a one-direction
    # scheduler. All of them reached ns-3 and none reached this tier.
    knobs = wm.Knobs.of(params)
    win = window_s(params)
    duty = call_duty(params, sim_t, win)
    ue_tag, quota, floor, sliced = slice_policy(params)

    per_cell: dict[int, dict[int, float]] = {}
    per_cell_ul: dict[int, dict[int, float]] = {}
    for imsi, l in links.items():
        per_cell.setdefault(l["cell"], {})[imsi] = l["sinr_db"]
        if "sinr_ul_db" in l:
            per_cell_ul.setdefault(l["cell"], {})[imsi] = l["sinr_ul_db"]

    # Each cell's duty in TIME, which is what makes its interference bursty.
    # Not its share of UE capacity: a backlogged cell with two UEs transmits
    # in every slot just as a cell with twelve does — it serves fewer UEs per
    # slot, not fewer slots — and is exactly as steady an interferer. Reading
    # the UE count as the duty said the opposite and predicted 7.1 dB of
    # spread for the 10 MHz case where the engine measures 2.9.
    #
    # A cell blinks only when it runs out of DATA, so the duty is the demand
    # it was offered against the airtime it has.
    load = {c: (1.0 if sinrs else 0.0) for c, sinrs in per_cell.items()}
    spread: dict[int, float] = {}
    for imsi, l in links.items():
        # Each interferer arrives with the cell it came from, so its duty
        # cycle is looked up rather than positionally guessed. The old version
        # zipped a list of powers against a list of loads built by a different
        # filter and padded the shortfall with 0.5 — with one band that was
        # merely fragile, and with cells on different carriers the two lists
        # are different lengths by construction.
        others = l.get("_interferers") or []
        # THE MIXTURE'S OWN SPREAD, when the link carries one. `link_state`
        # re-draws each link's LOS/NLOS condition and each neighbour's beam
        # and reports what the SINR actually did; `sinr_spread_db` predicts
        # a sigma from a fading model, which is the thing five attempts
        # failed at and the engine's trace says is not the mechanism.
        got = l.get("sinr_sd_db")
        spread[imsi] = (got if got is not None else
                        sinr_spread_db(0.0, l.get("_noise_mw", 1e-12),
                                       [p for _, p in others],
                                       [load.get(c, 0.5) for c, _ in others]))

    samples: dict[str, list[float]] = {}
    ue_rows, cell_rows, cell_power = [], [], []
    # Cells switched OFF, as indices into the payload's cell order. A cell at
    # -200 dBm serves nobody, but "serves nobody" is an IDLE cell drawing
    # 431 W — charging it that would have made an energy-saving action look
    # free of benefit, the mirror of the ghost draw the slow tier had.
    off = {int(c) for c in str(params.get("cells_off") or "").split(",")
           if c.strip().lstrip("-").isdigit()}
    for cell, sinrs in sorted(per_cell.items()):
        # ...and only while the UE's call model says it is transmitting. An
        # unlimited demand stays unlimited: a UE with no rate cap is the
        # "offer everything" probe, and duty-cycling infinity is infinity.
        demand = {}
        for i in sinrs:
            d = (demand_bps or {}).get(i, float("inf"))
            f = duty.get(i)
            demand[i] = d if (f is None or d == float("inf")) else d * f
        _, cell_bw_mhz, cell_mu = (bands[cell] if cell < len(bands)
                                    else (3500.0, 50.0, 2))
        cw = wm.cell_window(cell, sinrs, demand, cell_bw_mhz, cell_mu,
                            window_s=win, engine_clamp=engine_clamp,
                            # The model computes its own spread per UE. An
                            # explicit decay_db OVERRIDES it, which is how the
                            # spread can be swept against the engine's own
                            # histogram — until now the argument existed and
                            # did nothing, because the computed value was
                            # passed regardless of what the caller asked for.
                            decay_db=(decay_db if decay_db is not None else
                                      (statistics.fmean([spread[i] for i in sinrs])
                                       if sinrs else wm.FADING_SIGMA_DB)),
                            backlog_bytes={i: (backlog or {}).get(i, 0.0)
                                           for i in sinrs},
                            ue_slice={i: ue_tag.get(i) for i in sinrs},
                            slice_quota=quota, slice_floor=floor,
                            slice_dedicated=sliced,
                            availability={i: links[i].get("served_frac", 1.0)
                                          for i in sinrs},
                            ue_sinr_ul_db=per_cell_ul.get(cell),
                            ul_backlog_bytes={i: (ul_backlog or {}).get(i, 0.0)
                                              for i in sinrs},
                            knobs=knobs)
        # WHAT THE BACKHAUL CARRIES. The air interface decides the share; the
        # S1-U decides the ceiling, and a cell scheduling past it delivers
        # less and queues the rest. Scaled proportionally because the
        # bottleneck is the link, not any one bearer.
        cap = wm.backhaul_cap_bytes(params, win)
        served = sum(u.volume_bytes for u in cw.ues)
        if served > cap > 0:
            k = cap / served
            for u in cw.ues:
                held = u.volume_bytes * (1.0 - k)
                u.volume_bytes *= k
                u.queued_bytes = min(knobs.rlc_buffer_bytes,
                                     u.queued_bytes + held)
        if backlog is not None:
            for u in cw.ues:
                backlog[u.imsi] = u.queued_bytes
        if ul_backlog is not None:
            for u in cw.ues:
                ul_backlog[u.imsi] = u.queued_ul_bytes
        cell_prb = cell_thp = 0.0
        for u in cw.ues:
            for name, v in u.measurements().items():
                samples.setdefault(name, []).append(v)
            cell_prb += u.prb_used
            cell_thp += u.volume_bytes * 8 / 1e3 / win
            ue_rows.append({"imsi": u.imsi, "cell": cell + wm_cell_base(),
                            "sinr": round(u.sinr_db, 1), "prb": u.prb_used,
                            "thp_kbps": round(u.volume_bytes * 8 / 1e3 / win, 1),
                            "mcs": u.mcs, "sim_t": sim_t,
                            # The engine's 139-scaled `prb` is above; this is
                            # the real block count, and the only one that can
                            # be compared with a simulator that reports PRBs.
                            "prb_rb": round(u.prb_used_rb, 1),
                            # Tier B has always computed RSRP and never emitted
                            # it, which is why a comparison against a capture
                            # carrying RSRP had nothing to put beside it.
                            "rsrp_dbm": round(links[u.imsi]["rsrp_dbm"], 1),
                            "band_mhz": round(bands[cell][0], 1)
                            if cell < len(bands) else None,
                            # Absent, not zero, when the uplink is not
                            # modelled — see `_uplink_window`.
                            **({"sinr_ul": round(u.sinr_ul_db, 1),
                                "mcs_ul": u.mcs_ul, "tb_ul": u.tb_ul,
                                "tb_ul_err": u.tb_ul_error,
                                "prb_ul": u.prb_used_ul,
                                "prb_ul_rb": round(u.prb_used_ul_rb, 1),
                                "thp_ul_kbps": round(
                                    u.volume_ul_bytes * 8 / 1e3 / win, 1)}
                               if u.has_ul else {}),
                            # The transport-block counters per UE. They were
                            # only ever aggregated into `measurements`, which
                            # is enough for a comparison and not enough for an
                            # E2 report: E2SM-KPM style 4 reports against a UE.
                            "tb": u.tb_total, "tb_err": u.tb_error,
                            "queued": round(u.queued_bytes, 1)})
        # RU draw, from the engine's own per-state currents. `off_cells` is
        # what an energy-saving xApp switched off; those draw nothing, which
        # is the whole point of the action and was invisible here before.
        pw = wm.cell_power_w(cw, wm.RuPower.of(params, cell), win,
                             off=cell in off,
                             idle_for_s=(idle_for or {}).get(cell, 0.0))
        cell_power.append(pw)
        cell_rows.append({"cell": cell + wm_cell_base(), "ues": len(cw.ues),
                          "prb": round(cell_prb, 1),
                          "thp_kbps": round(cell_thp, 1),
                          "power_w": round(pw, 1),
                          "energy_j": round(pw * win, 1)})

    # Cells with NO UEs. `per_cell` is built from the links, so an empty cell
    # — one switched off, one knocked out by a fault, one nobody happens to be
    # near — had no row at all and simply DISAPPEARED from the console mid-run.
    # It is also the only cell a sleep state can help, so leaving it out left
    # the whole energy-saving lever invisible in the counter that measures it.
    n_cells = int(params.get("n_gnb") or 0) or len(bands)
    empty = wm.CellWindow(cell=0)
    for cell in range(n_cells):
        if cell in per_cell:
            continue
        pw = wm.cell_power_w(empty, wm.RuPower.of(params, cell), win,
                             off=cell in off,
                             idle_for_s=(idle_for or {}).get(cell, 0.0))
        cell_power.append(pw)
        cell_rows.append({"cell": cell + wm_cell_base(), "ues": 0,
                          "prb": 0.0, "thp_kbps": 0.0,
                          "power_w": round(pw, 1),
                          "energy_j": round(pw * win, 1)})
    cell_rows.sort(key=lambda r: r["cell"])
    # How long each cell has now been idle, carried to the next window so a
    # sleep timer longer than one window can still expire.
    if idle_for is not None:
        for row in cell_rows:
            c = row["cell"] - wm_cell_base()
            # IDLE MEANS NO SYMBOLS, not no UEs — the same test
            # `cell_power_w` applies. A cell whose UEs are attached but
            # sending nothing draws the idle current and its sleep timer
            # runs; counting attachments instead kept the timer pinned at
            # zero and no cell ever slept.
            busy = row["ues"] > 0 and row["prb"] > 0
            idle_for[c] = 0.0 if busy else idle_for.get(c, 0.0) + win

    # PEE is a CELL counter, not a UE one, so it is folded in here rather
    # than through `UeWindow.measurements`. Tier A reports exactly these two.
    for pw in cell_power:
        samples.setdefault("PEE.AvgPower", []).append(round(pw, 1))
        samples.setdefault("PEE.Energy", []).append(round(pw * win, 1))
    measurements = {
        name: {"count": len(v), "min": round(min(v), 2), "max": round(max(v), 2),
               "mean": round(sum(v) / len(v), 2)}
        for name, v in sorted(samples.items())
    }
    return {
        "type": "frame", "tick": tick, "sim_t": round(sim_t, 2),
        "tier": "B",
        "measurements": measurements,
        "ue_ids": sorted(links),
        "ues": ue_rows,
        "cells": cell_rows,
        "handovers": handovers,
        "kpm_fresh": True,
        "kpm_fresh_cells": len(cell_rows),
    }


def wm_cell_base() -> int:
    """Cell ids in the engine's frames start at 2 — see partition_live.CELL_BASE."""
    from partition_live import CELL_BASE
    return CELL_BASE


def _l3_filter(links: dict, state: dict, alpha: float) -> None:
    """3GPP L3 filtering of every candidate's SINR, in place.

    `Fn = (1 - a) * Fn-1 + a * Mn` with `a = 1/2^(k/4)` — TS 36.331 5.5.3.2.
    Only the HANDOVER decision reads the filtered value; the throughput
    maths keeps the instantaneous one, because the filter is a property of
    the measurement report and not of the radio.
    """
    if alpha >= 1.0:
        return
    for imsi, l in links.items():
        prev_f = state.setdefault(imsi, {})
        cand = l.get("_cand") or {}
        for j, (rsrp, sinr, interf, noise) in list(cand.items()):
            old = prev_f.get(j)
            f = sinr if old is None else (1.0 - alpha) * old + alpha * sinr
            prev_f[j] = f
            cand[j] = (rsrp, f, interf, noise)
        # IT DOES NOT RE-PICK THE SERVING CELL. This used to take the
        # argmax of the filtered level and write it into `l["cell"]`, which
        # silently replaced `_select_cell` — and `_select_cell` is where
        # reselection PRIORITY lives. On a two-band scene a plain argmax
        # moves every UE onto the low band, because 700 MHz propagates
        # better: measured on the energy-saving scenario, 133 of 150 UEs
        # landed on the N28 coverage cell that the same run's live console
        # correctly showed empty. The reference scenario is single-band, so
        # nothing caught it.
        #
        # Deciding belongs to `apply_ho_policy`, which reads these filtered
        # values. Filtering them is this function's whole job.


def apply_ho_policy(links: dict, prev: dict, ho: dict) -> None:
    """Keep a UE on its serving cell unless the A2/A3 conditions say move.

    `link_state` answers "which cell is best NOW", which is the right
    question for a UE with no history and the wrong one for a UE that
    already has a serving cell: an RRC only hands over when a neighbour
    beats the serving cell by an offset plus hysteresis, and in the mmWave
    stack only once the serving cell has dropped below A2. Without that the
    tier handed over on every shadowing wobble, and `ho_sinr_margin_db` —
    a field an xApp tunes — did nothing here.

    Mutates `links` in place, and restores the WHOLE link record, because
    the serving cell decides interference, noise and RSRP downstream, not
    just a label.
    """
    if not prev:
        return
    if not ho["admit"]:
        # Nobody is going anywhere. The UE keeps its cell and whatever that
        # cell is now worth, which is the point: a stranded UE's metrics are
        # the evidence.
        for imsi, l in links.items():
            was = prev.get(imsi)
            c = l.get("_cand", {}).get(was) if was is not None else None
            if c is not None and was != l["cell"]:
                l["cell"], l["rsrp_dbm"], l["sinr_db"] = was, c[0], c[1]
                l["_interferers"], l["_noise_mw"] = c[2], c[3]
        return
    for imsi, l in links.items():
        was = prev.get(imsi)
        if was is None or was == l["cell"]:
            continue
        c = l.get("_cand", {}).get(was)
        if c is None:
            continue                      # the old cell is gone; take the new
        rsrp0, sinr0, interf0, noise0 = c
        if rsrp0 < ho["q_rx_lev_min_dbm"]:
            continue                      # below the camp floor — it must leave
        stay = False
        if ho["a2_db"] > -999.0 and sinr0 > ho["a2_db"]:
            stay = True                   # A2 not met: the UE is not measuring
        elif (ho["a3_entry"] and not ho["threshold_mode"]
              and l["sinr_db"] - sinr0 < ho["a3_offset"]):
            stay = True                   # the neighbour does not beat it enough
        if stay:
            l["cell"] = was
            l["rsrp_dbm"] = rsrp0
            l["sinr_db"] = sinr0
            l["_interferers"] = interf0
            l["_noise_mw"] = noise0


# THE BOX THE ENGINE WALKS IN. `scenario-zero` gives every UE a
# `RandomWalk2dOutdoorMobilityModel` with `Bounds = Rectangle(0, maxXAxis,
# 0, maxYAxis)` and both axes hardcoded to 4000, with the scene centred at
# (2000, 2000) — which is the frame `nsoran_params` recentres into, so the
# payload's coordinates are already in it.
#
# Tier B walked a straight bearing forever. Over the two simulated seconds
# a batch run used to reach, that is indistinguishable from anything else;
# once the fast tier could run 600 windows it stopped being: measured on
# the energy-saving scenario at t=600 s the UEs spanned 16 km against a
# 1.4 km scene, and the median distance to the nearest cell went from
# 62 m to 762 m. They were not roaming, they were leaving.
WALK_BOUNDS = (0.0, 4000.0, 0.0, 4000.0)

# AND IT TURNS EVERY 30 METRES. `RandomWalk2dOutdoorMobilityModel` defaults
# to `Mode = MODE_DISTANCE` with `Distance = 30.0`, and the scenario
# overrides neither — so a UE draws a new direction every 30 m of travel.
# That makes the walk DIFFUSIVE, which is what the scenario's own comment
# means by "the random walk changes direction every leg and nets ~nothing":
# net displacement grows with the square root of time, not with time. A
# straight bearing crosses far more cell boundaries than the engine does,
# and over 600 s it leaves the scene entirely.
WALK_LEG_M = 30.0


def _reflect(x: float, v: float, lo: float, hi: float) -> tuple[float, float]:
    """One axis of a walk that bounces off the box, as ns-3's does.

    Folded rather than clamped: a UE that would overshoot by 30 m ends up
    30 m back inside with its direction reversed, so the speed is
    conserved and the population stays spread instead of piling up on the
    edge. The loop handles a step longer than the box, which a fast UE and
    a long window can produce.
    """
    span = hi - lo
    if span <= 0:
        return x, v
    for _ in range(8):
        if x < lo:
            x, v = lo + (lo - x), -v
        elif x > hi:
            x, v = hi - (x - hi), -v
        else:
            break
    return min(hi, max(lo, x)), v


def _waypoint_paths(params: dict) -> dict[int, list[tuple]]:
    """Per-UE routes off `ue_waypoints`, keyed by the UE's slot.

    `;` per UE aligned with `ue_positions`, `|` per waypoint, each
    `x,y,z,t` with t the arrival time in seconds. The MODE is already
    baked in by `_ue_waypoint_specs` — `once` emits the legs, `pingpong`
    and `repeat` expand the cycle — so nothing here has to know about it.

    The engine's own rule, from the scenario source: "A UE with a path
    gets a WaypointMobilityModel and TRAVERSES its route; a UE with an
    empty slot keeps the diffusive RandomWalk2dOutdoor." Tier B read
    neither, so a handover demo whose whole point is a vehicle crossing a
    boundary on a defined route wandered at random instead.
    """
    out: dict[int, list[tuple]] = {}
    for i, item in enumerate((params.get("ue_waypoints") or "").split(";")):
        if not item.strip():
            continue
        legs = []
        for w in item.split("|"):
            f = w.split(",")
            if len(f) < 4:
                continue
            try:
                legs.append((float(f[0]), float(f[1]), float(f[2]), float(f[3])))
            except ValueError:
                continue
        if len(legs) >= 2:
            out[i] = legs
    return out


def _on_path(legs: list[tuple], t: float) -> tuple[float, float, float]:
    """Where a UE is on its route at time `t`, between waypoints.

    Past the last waypoint it STAYS THERE, which is what a `once` path
    means and what ns-3's WaypointMobilityModel does when it runs out.
    """
    if t <= legs[0][3]:
        return legs[0][:3]
    for a, b in zip(legs, legs[1:]):
        if t <= b[3]:
            span = b[3] - a[3]
            f = 0.0 if span <= 0 else (t - a[3]) / span
            return (a[0] + (b[0] - a[0]) * f,
                    a[1] + (b[1] - a[1]) * f,
                    a[2] + (b[2] - a[2]) * f)
    return legs[-1][:3]


def _speeds(params: dict, n_ue: int) -> list[float]:
    """Per-UE speed in m/s, `ue_speeds` being ';'-separated and UE-aligned."""
    raw = [v for v in (params.get("ue_speeds") or "").split(";") if v.strip()]
    out = []
    for i in range(n_ue):
        try:
            out.append(float(raw[i]))
        except (IndexError, ValueError):
            out.append(0.0)
    return out


def stream(params: dict, n: int | None = None,
           demand_bps: dict[int, float] | None = None,
           decay_db: float | None = None,
           seed: int | None = None):
    """Consecutive windows, with the state that makes them differ.

    `n` windows, or forever when it is None — a live session and a batch run
    are the same computation and share this one implementation, so the RAN an
    xApp connects to cannot drift from the one a campaign measures.

    Two things carry across: the RLC queue, so congestion builds instead of
    resetting, and the UE positions, so serving cells change and handovers
    happen. Neither existed before and their absence was visible — the queue
    counter was a constant and the handover counter was a structural zero.

    `seed` picks a shadowing and LOS realisation. Tier B is deterministic
    given one, and its output MOVES between them — the shadowing sigma is 4
    to 7.8 dB, so a single draw is a sample and not an answer. Tier A's
    seed-to-seed spread has been measured since Phase 0; this is the same
    question asked of Tier B, and nothing had asked it.

    The walk is deterministic on a per-UE bearing. The engine runs
    `RandomWalk2dOutdoorMobilityModel` where no waypoints are given, so this
    reproduces the RATE at which UEs cross boundaries, not which UE crosses
    when. A handover count is a rate; nothing about a random walk is
    reproducible one for one, in either direction.
    """
    seed = seed_of(params, seed)
    win = window_s(params)
    ho = ho_policy(params)
    pos = _parse_points(params.get("ue_positions", ""))
    speeds = _speeds(params, len(pos))
    # The bearing is per UE and now lives in a velocity that reflection can
    # turn around. The golden angle still spreads the initial directions.
    vel = [(math.cos(i * 2.399963) * speeds[i],
            math.sin(i * 2.399963) * speeds[i]) for i in range(len(pos))]
    legs = [0.0] * len(pos)        # metres into the current leg
    turn = [0] * len(pos)          # how many turns each UE has taken
    paths = _waypoint_paths(params)   # UEs that have a route instead
    backlog: dict[int, float] = {}
    ul_backlog: dict[int, float] = {}
    prev: dict[int, int] = {}
    l3: dict[int, dict[int, float]] = {}
    rrc: dict[int, dict] = {}       # the RRC's filter state, across windows
    idle_for: dict[int, float] = {}  # per cell, how long it has been idle
    total_ho = 0
    t = -1
    while n is None or t + 1 < n:
        t += 1
        sim_t = (t + 1) * win
        # The config's scheduled anomalies, evaluated at this window.
        wp, ho_blocked = apply_faults(params, sim_t, win)
        links = link_state(wp, pos, seed, serving=prev, rrc_state=rrc)
        # THE FILTERED LEVEL, not the instantaneous one. The RRC decides on
        # an EWMA of the measurements; feeding it the raw draw made the tier
        # react to a single bad sample the way a real one never does.
        _l3_filter(links, l3, ho["l3_alpha"])
        if ho["check_s"] > win and (t % max(1, round(ho["check_s"] / win))):
            # The RRC did not look in this window.
            for imsi, l in links.items():
                was = prev.get(imsi)
                c = l.get("_cand", {}).get(was) if was is not None else None
                if c is not None and was != l["cell"]:
                    l["cell"], l["rsrp_dbm"], l["sinr_db"] = was, c[0], c[1]
                    l["_interferers"], l["_noise_mw"] = c[2], c[3]
        else:
            apply_ho_policy(links, prev, ho)
        if ho_blocked:
            # A handover BLOCK is the UE staying put, not the UE vanishing:
            # it keeps its old cell and whatever that cell is now worth.
            for imsi, l in links.items():
                was = prev.get(imsi)
                c = l.get("_cand", {}).get(was) if was is not None else None
                if c is not None and was != l["cell"]:
                    l["cell"], l["rsrp_dbm"], l["sinr_db"] = was, c[0], c[1]
                    l["_interferers"], l["_noise_mw"] = c[2], c[3]
        for imsi, l in links.items():
            if imsi in prev and prev[imsi] != l["cell"]:
                total_ho += 1
            # ...and the ones INSIDE the window. At a matched horizon Tier
            # B's walk covers two seconds and crosses no boundary, so every
            # handover the engine reports in that span is one of these.
            total_ho += int(l.get("ho_in_window") or 0)
            prev[imsi] = l["cell"]
        yield frame(wp, t, sim_t, demand_bps, decay_db,
                    positions=pos, backlog=backlog, handovers=total_ho,
                    links=links, seed=seed, ul_backlog=ul_backlog,
                    idle_for=idle_for)
        # Turn every WALK_LEG_M, then bounce off the box the engine bounces
        # off. Sub-stepped by leg so a window longer than one leg still
        # turns the right number of times.
        nxt = []
        for i, (x, y, z) in enumerate(pos):
            vx, vy = vel[i]
            sp = speeds[i]
            left = win
            while left > 1e-9 and sp > 0:
                step = min(left, max(1e-6, (WALK_LEG_M - legs[i]) / sp))
                x, vx = _reflect(x + vx * step, vx,
                                 WALK_BOUNDS[0], WALK_BOUNDS[1])
                y, vy = _reflect(y + vy * step, vy,
                                 WALK_BOUNDS[2], WALK_BOUNDS[3])
                legs[i] += sp * step
                left -= step
                if legs[i] >= WALK_LEG_M - 1e-9:
                    legs[i] = 0.0
                    turn[i] += 1
                    a = _draw(seed, "walk", i, turn[i]) * 2.0 * math.pi
                    vx, vy = math.cos(a) * sp, math.sin(a) * sp
            vel[i] = (vx, vy)
            nxt.append((x, y, z))
        for i, route in paths.items():
            if i < len(nxt):
                nxt[i] = _on_path(route, sim_t)
        pos = nxt


def frames(params: dict, n: int,
           demand_bps: dict[int, float] | None = None,
           decay_db: float | None = None,
           seed: int | None = None) -> list[dict]:
    """`n` windows as a list. `stream` is the same thing without an end, and
    the two share one implementation so a session and a batch cannot drift."""
    return list(stream(params, n, demand_bps, decay_db, seed))
