"""
NS-3 Adapter Bridge
Translates NSGSimulationConfig (modern Pydantic V2 schema) into the raw JSON
dictionary expected by ns3-dev-rem-gen (compiled C++ binary).

Data flow:
  NSGSimulationConfig
        │
        ▼  translate_nsg_to_ns3()
  {"buildingConfig": [...], "cellConfig": [...], "mapConfig": {...}}
        │
        ▼  run_simulation()
  write → /tmp/ns3_config.json
        │
        ▼  rsrp_heatmap(): evaluated in process, no binary
        │
        ▼  read output
  best-server RSRP per grid point, from the same propagation model
        │
        ▼
  [{"x": .., "y": .., "rsrp": ..}, ...]  →  returned as "heatmap" in run_simulation()

Live Traffic Mode (estimate_live_throughput):
  Separate, lightweight path — no subprocess, no NS-3 binary involved.
  Per tick: analytical log-distance RSRP per UE group's serving cell →
  Shannon-capacity throughput estimate → aggregated per NetworkSlice.
  See estimate_live_throughput() docstring for the full rationale; this
  is Option B from the Live Traffic Mode architecture review — replicating
  the legacy engine's per-cell subprocess + mmap IPC protocol was assessed
  as high-risk/high-cost with no source access, so live numbers are
  estimated directly from the same physical model the REM heatmap uses,
  not measured by a second simulation engine.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
from pathlib import Path

import numpy as np

from schema import (
    AntennaType,
    BaseStation,
    BuildingType,
    NSGSimulationConfig,
    PropagationScenario,
    SimulationArea,
    WallMaterial,
)


# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE PATHS
# ═══════════════════════════════════════════════════════════════════════════════

# THE TERA5G BINARY IS GONE. `_NS3_BIN` used to point at
# ai_rsg_kaynak_kod/Tera5G/ctsm/rem_gen/ns3-service-v3/bin/ns3-dev-rem-gen, and
# the coverage map was the last thing in this platform reaching into that tree.
# Deleted rather than left pointing at a path nobody calls: a dead constant to a
# license-patched appliance is the kind of thing that gets re-enabled by someone
# who assumes it was load-bearing. See `rsrp_heatmap` for what replaced it.


# ═══════════════════════════════════════════════════════════════════════════════
# NS-3 ENUM MAPPINGS
# Translate our clean enum values → the exact strings the C++ parser expects
# ═══════════════════════════════════════════════════════════════════════════════

_ANTENNA_TYPE: dict[AntennaType, str] = {
    AntennaType.COSINE:       "Cosine",
    AntennaType.MASSIVE_MIMO: "3GPP",
    AntennaType.ISOTROPIC:    "Isotropic",
}

# The binary's accepted scenario strings (confirmed from rem-gen-cfg.h's own
# NS_FATAL error message) are 3GPP TR 38.901 channel model names, not our
# 4-way schema enum verbatim. 'UMa', 'UMa-Buildings', and 'RMa' happen to
# match exactly, but bare 'UMi' does not exist on the C++ side — only
# 'UMi-StreetCanyon' and 'UMi-Buildings' do. Mapped explicitly (not
# .value passthrough) so a future enum addition fails loudly via KeyError
# instead of silently sending an unsupported string.
_SCENARIO: dict[PropagationScenario, str] = {
    PropagationScenario.UMA:           "UMa",
    PropagationScenario.UMA_BUILDINGS: "UMa-Buildings",
    PropagationScenario.UMI:           "UMi-StreetCanyon",
    PropagationScenario.RMA:           "RMa",
}

_BUILDING_TYPE: dict[BuildingType, str] = {
    BuildingType.RESIDENTIAL: "Residential",
    BuildingType.OFFICE:      "Office",
    # The binary only accepts 'Residential' | 'Office' | 'Commercial' (NS_FATAL
    # on anything else — confirmed from rem-gen-cfg.h's own error message).
    # 'Industrial' isn't a supported category; map it to the closest accepted
    # non-residential/non-office bucket.
    BuildingType.INDUSTRIAL:  "Commercial",
}

_WALL_MATERIAL: dict[WallMaterial, str] = {
    WallMaterial.WOOD:                  "Wood",
    WallMaterial.CONCRETE:              "ConcreteWithoutWindows",
    WallMaterial.CONCRETE_WITH_WINDOWS: "ConcreteWithWindows",
}


# ═══════════════════════════════════════════════════════════════════════════════
# INTERNAL TRANSLATION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _translate_antenna(bs: BaseStation, cell_index: int) -> dict:
    """
    Translate AntennaConfig → NS-3 antennaConfig dict for one cell.

    Cosine path:  adds height + beamwidth fields; no MIMO fields.
    MIMO path:    adds array geometry + beamforming fields; no beamwidth fields.

    Element spacing for mMIMO is set to 1λ at the operating frequency — this
    matches the values observed in ns3-service-v3 cached simulation outputs.
    """
    ant = bs.radio.antenna

    payload: dict = {
        "id":        cell_index,
        "type":      _ANTENNA_TYPE[ant.type],
        "gain":      int(ant.gain_dbi),
        "azimuth":   int(ant.azimuth_deg),
        "tilt":      int(ant.tilt_deg),
        "txPercent": 100,
    }

    if ant.type == AntennaType.COSINE:
        payload.update({
            "height":              int(bs.position.z),
            "horizontalBeamwidth": ant.horizontal_beamwidth_deg,
            "verticalBeamwidth":   ant.vertical_beamwidth_deg,
            "beamWidth":           ant.horizontal_beamwidth_deg,
        })

    elif ant.type == AntennaType.MASSIVE_MIMO:
        mimo = ant.massive_mimo  # presence guaranteed by schema model_validator
        freq_hz = bs.radio.band.frequency_mhz * 1_000_000
        # Physical element spacing in meters = 1λ at operating frequency
        spacing_m = round(3e8 / freq_hz, 10)
        payload.update({
            "groupId":          1,
            "nRows":            mimo.rows,
            "nCols":            mimo.cols,
            "horizontalSpacing": spacing_m,
            "verticalSpacing":   spacing_m,
            "polarization":     int(mimo.polarization_deg),
            "antennaMask":      mimo.antenna_mask,
        })

    # Isotropic needs no extra fields beyond the base payload

    return payload


def _content_hash(data: dict) -> str:
    """
    Deterministic 16-char hex digest of a dict.
    Used as remHash for NS-3's internal simulation caching/dedup logic.
    Computed from buildingConfig + cellConfig only, before mapConfig is built,
    to avoid a self-referential hash.
    """
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return "0x" + hashlib.sha256(canonical).hexdigest()[:16]


def _build_map_config(cfg: NSGSimulationConfig, rem_hash: str) -> dict:
    """
    Derive NS-3 mapConfig from SimulationArea + SimulationSettings.

    Resolution math (matches ns3-service-v3 observed pattern):
      resX      = floor(x_span / step)      — fine REM point count along X
      res2DX    = floor(x_span / (step*4))  — coarse 2D overview grid (4× step)
      resZ = res2DZ = 1                     — single horizontal plane at UE height
    """
    area     = cfg.area
    settings = cfg.settings
    step     = int(area.resolution_m)
    step_2d  = step * 4

    x_span   = area.x_max - area.x_min
    y_span   = area.y_max - area.y_min
    max_z    = max((bs.position.z for bs in cfg.base_stations), default=25.0)

    def _res(span: float, s: int) -> int:
        return max(1, int(span / s))

    return {
        "minX": int(area.x_min),
        "maxX": int(area.x_max),
        "minY": int(area.y_min),
        "maxY": int(area.y_max),
        "minZ": 2.0,           # UE receive height — pedestrian eye level
        "maxZ": int(max_z),
        # Fine REM grid
        "resX":  _res(x_span, step),
        "resY":  _res(y_span, step),
        "resZ":  1,
        "stepX": step,
        "stepY": step,
        "stepZ": 1,
        # Coarse 2D overview grid (used by the engine for quick previews)
        "res2DX":    _res(x_span, step_2d),
        "res2DY":    _res(y_span, step_2d),
        "res2DZ":    1,
        "res2DStepX": step_2d,
        "res2DStepY": step_2d,
        "res2DStepZ": 1,
        # Simulation control
        "scenario":          _SCENARIO[settings.scenario],
        "remHash":           rem_hash,
        "remXYZFile":        "/tmp/remPoints.npy",
        "shadowingEnabled":  settings.shadowing_enabled,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════════

def translate_nsg_to_ns3(cfg: NSGSimulationConfig) -> dict:
    """
    Adapter: convert NSGSimulationConfig → the raw dict ns3-dev-rem-gen expects.

    Mapping summary:
      cfg.buildings     →  buildingConfig[]
      cfg.base_stations →  cellConfig[]        (one entry per sector)
      cfg.area
      cfg.settings      →  mapConfig

    Note on multi-cell runs:
      The C++ binary accepts an array in cellConfig, so all sectors can be
      submitted in one invocation for merged coverage maps. If per-beam REMs
      are needed (mMIMO with multiple digital beams), call this function once
      per beam with a digital steering angle varied, then merge
      the resulting .npy outputs in post-processing.
    """
    building_config = [
        {
            "minX": b.bounds.x_min,
            "maxX": b.bounds.x_max,
            "minY": b.bounds.y_min,
            "maxY": b.bounds.y_max,
            "minZ": b.bounds.z_min,
            "maxZ": b.bounds.z_max,
            "nFloors":       b.floors,
            "nRoomsX":       b.rooms_x,
            "nRoomsY":       b.rooms_y,
            "buildingType":  _BUILDING_TYPE[b.building_type],
            "extWallsType":  _WALL_MATERIAL[b.wall_material],
        }
        for b in cfg.buildings
    ]

    cell_config = [
        {
            "x":       int(bs.position.x),
            "y":       int(bs.position.y),
            "z":       int(bs.position.z),
            "txPower": bs.radio.tx_power_dbm,
            "cellId":  idx + 1,   # 1-based cell IDs match NS-3 convention
            "remId":   idx,       # 0-based REM index used for output file naming
            "name":    bs.name,
            "antennaConfig": _translate_antenna(bs, idx),
            "bandwidthConfig": {
                "frequency":  bs.radio.band.frequency_mhz * 1_000_000,
                "bandwidth":  bs.radio.band.bandwidth_mhz * 1_000_000,
                "numerology": bs.radio.band.numerology,
            },
        }
        for idx, bs in enumerate(cfg.base_stations)
    ]

    # Hash is computed before mapConfig so it doesn't include remHash itself
    rem_hash   = _content_hash({"buildingConfig": building_config, "cellConfig": cell_config})
    map_config = _build_map_config(cfg, rem_hash)

    return {
        "buildingConfig": building_config,
        "cellConfig":     cell_config,
        "mapConfig":      map_config,
    }


_MIN_RECEIVED_POWER = -156.0  # dBm — confirmed from the original TeraVM rem_gen.py
                               # (RemGen.MIN_RECEIVED_POWER), recovered by decompiling
                               # rem_gen.pyc. Used both as the array's init-fill value
                               # and the practical noise floor; any value at or below
                               # this is "no signal", not a real measurement. Some other
                               # out-of-range sentinels (e.g. -9999) also appear in raw
                               # output and are safely caught by the same threshold.


def parse_rem_points(npy_path: str, area: SimulationArea) -> list[dict]:
    """
    Parse the NS-3 Radio Environment Map output into JSON-friendly points.

    FORMAT (confirmed by direct binary testing + decompiling the original TeraVM
    rem_gen.pyc, since this binary's actual output didn't match the originally
    assumed (N, 4) format):

      The file is a dense 2D grid of raw int16 RSRP values, shape (n_x, n_y) —
      X is axis 0, Y is axis 1. This matches simulation-dash.js's Plotly heatmap
      trace, which loads this same data with `transpose: true` to get conventional
      row=Y rendering, confirming the raw layout has X first.

      n_x and n_y are NOT necessarily equal to the resX/resY requested in
      mapConfig: the binary silently floors each axis to a minimum of 10
      samples (confirmed across 5 independent runs with varied asymmetric
      resX/resY — e.g. resX=5 always produced 10 rows, resX=50 produced 50).
      Coordinates are therefore reconstructed from the actual array shape, not
      the requested resolution, via linspace across the SimulationArea bounds
      (matching dx = (maxX-minX)/(resX-1) from the original product's own
      rendering code).

    Returns [] if the file is missing, or if the shape doesn't match either
    the legacy (N, 4) point-list format or this 2D grid format — a missing
    heatmap is recoverable; a heatmap with silently wrong x/y is not.
    """
    path = Path(npy_path)
    if not path.exists():
        return []

    points = np.load(path)

    # Legacy (N, 4) point-list format: [x, y, z, rsrp] rows, float dtype.
    # Not produced by this binary, but kept for forward/cross-version compat.
    # Exact shape[1]==4 match (not >=) plus a dtype check, since a 2D RSRP
    # grid can coincidentally have shape[1] >= 4 (e.g. (10, 10)) and would
    # otherwise be silently misread as 10 rows of [x, y, z, rsrp, ...].
    if points.ndim == 2 and points.shape[1] == 4 and np.issubdtype(points.dtype, np.floating):
        return [
            {"x": float(row[0]), "y": float(row[1]), "rsrp": float(row[3])}
            for row in points
        ]

    # 2D dense grid format — the format this binary actually produces.
    if points.ndim == 2:
        n_x, n_y = points.shape
        xs = np.linspace(area.x_min, area.x_max, n_x)
        ys = np.linspace(area.y_min, area.y_max, n_y)
        return [
            {"x": float(xs[i]), "y": float(ys[j]), "rsrp": float(points[i, j])}
            for i in range(n_x)
            for j in range(n_y)
            if points[i, j] > _MIN_RECEIVED_POWER
        ]

    print(
        f"[parse_rem_points] Unsupported REM output shape {points.shape} "
        f"(dtype={points.dtype}) from {npy_path}. Returning no heatmap data."
    )
    return []


# ── Engine-safety clamps ──────────────────────────────────────────────────────
# ns3-dev-rem-gen SIGABRTs / SIGSEGVs on degenerate input (negative bandwidth, zero
# frequency, inverted or zero-size area, resolution 0). The schema does NOT range-check
# numbers, and AI / React-Flow / manual-tab edits can produce such values, so sanitise
# here — the one choke point before the binary — so the engine can never be crashed.
_REM_MAX_GRID_POINTS = 300_000  # cap so a huge fine-grained area can't run for minutes


# Kept buildings must be at least this far apart, so no position can ever sit on
# a shared face and read as "inside" two Buildings at once.
_BLD_GAP_M = 0.5


def deoverlap_buildings(buildings: list) -> tuple[list, int]:
    """Drop buildings whose XY boxes overlap (or nearly touch) an already-kept one.

    ns-3's buildings module NS_FATALs the whole run ("MobilityBuildingInfo already
    inside another building!") the moment any position falls inside two Building
    boxes at once — and an OSM city import produces exactly that: adjacent real
    footprints turned into overlapping axis-aligned boxes (the user's 84-building
    Istanbul import killed BOTH the REM binary and the live ns-O-RAN startup).
    Keep the LARGEST footprints first (they dominate the blocking the user drew)
    and drop anything that overlaps a kept box; also drops degenerate boxes
    (non-finite / inverted / thinner than 1 m). Shared by the REM sanitiser and
    the ns-O-RAN param builder so both engines only ever see valid geometry.
    """
    def fp(b):
        bb = b.bounds
        return (bb.x_max - bb.x_min) * (bb.y_max - bb.y_min)

    valid = []
    for b in buildings or []:
        bb = b.bounds
        vals = (bb.x_min, bb.x_max, bb.y_min, bb.y_max, bb.z_min, bb.z_max)
        if (all(math.isfinite(v) for v in vals)
                and bb.x_max - bb.x_min >= 1.0 and bb.y_max - bb.y_min >= 1.0):
            valid.append(b)
    kept: list = []
    for b in sorted(valid, key=fp, reverse=True):
        bb = b.bounds
        clear = all(
            bb.x_min >= k.bounds.x_max + _BLD_GAP_M
            or k.bounds.x_min >= bb.x_max + _BLD_GAP_M
            or bb.y_min >= k.bounds.y_max + _BLD_GAP_M
            or k.bounds.y_min >= bb.y_max + _BLD_GAP_M
            for k in kept
        )
        if clear:
            kept.append(b)
    return kept, len(buildings or []) - len(kept)


def _sanitize_config(cfg: NSGSimulationConfig) -> tuple[NSGSimulationConfig, list[str]]:
    """Clamp/repair anything that would crash or hang the REM binary. Returns a fixed
    copy plus a list of human-readable notes about what was changed."""
    c = cfg.model_copy(deep=True)
    w: list[str] = []
    if c.buildings:
        c.buildings, dropped = deoverlap_buildings(c.buildings)
        if dropped:
            w.append(f"{dropped} overlapping/degenerate building(s) dropped "
                     f"(ns-3 aborts when building boxes overlap)")
    # NaN/inf (e.g. from clearing a numeric field in the Config tab, parseFloat("") → NaN)
    # slip past comparisons, so treat non-finite as out-of-range → the low bound.
    clamp = lambda v, lo, hi: lo if not math.isfinite(v) else min(hi, max(lo, v))

    for bs in c.base_stations:
        band = bs.radio.band
        if not (5 <= band.bandwidth_mhz <= 400):
            w.append(f"{bs.id}: bandwidth_mhz {band.bandwidth_mhz} clamped"); band.bandwidth_mhz = clamp(band.bandwidth_mhz, 5, 400)
        if not (700 <= band.frequency_mhz <= 100_000):
            w.append(f"{bs.id}: frequency_mhz {band.frequency_mhz} clamped"); band.frequency_mhz = clamp(band.frequency_mhz, 700, 100_000)
        if not (0 <= band.numerology <= 4):
            band.numerology = int(clamp(band.numerology, 0, 4))
        if not (-40 <= bs.radio.tx_power_dbm <= 60):
            w.append(f"{bs.id}: tx_power_dbm {bs.radio.tx_power_dbm} clamped"); bs.radio.tx_power_dbm = clamp(bs.radio.tx_power_dbm, -40, 60)
        for axis in ("x", "y", "z"):
            if not math.isfinite(getattr(bs.position, axis)):
                setattr(bs.position, axis, 0.0); w.append(f"{bs.id}: position.{axis} not finite → 0")
        ant = bs.radio.antenna
        for fld, dflt in (("azimuth_deg", 0.0), ("tilt_deg", 0.0), ("gain_dbi", 15.0),
                          ("horizontal_beamwidth_deg", 65.0), ("vertical_beamwidth_deg", 10.0)):
            if not math.isfinite(getattr(ant, fld)):
                setattr(ant, fld, dflt); w.append(f"{bs.id}: antenna.{fld} not finite → {dflt}")

    a = c.area
    if not all(math.isfinite(v) for v in (a.x_min, a.x_max, a.y_min, a.y_max)):
        a.x_min, a.x_max, a.y_min, a.y_max = -400.0, 400.0, -400.0, 400.0; w.append("area bounds not finite → reset to ±400 m")
    if a.x_min > a.x_max:
        a.x_min, a.x_max = a.x_max, a.x_min; w.append("area x_min/x_max swapped")
    if a.y_min > a.y_max:
        a.y_min, a.y_max = a.y_max, a.y_min; w.append("area y_min/y_max swapped")
    if a.x_max - a.x_min < 20:
        mid = (a.x_min + a.x_max) / 2; a.x_min, a.x_max = mid - 10, mid + 10; w.append("area width < 20 m expanded")
    if a.y_max - a.y_min < 20:
        mid = (a.y_min + a.y_max) / 2; a.y_min, a.y_max = mid - 10, mid + 10; w.append("area height < 20 m expanded")
    if not math.isfinite(a.resolution_m) or a.resolution_m < 1:
        a.resolution_m = 10.0; w.append("resolution_m invalid set to 10 m")
    while ((a.x_max - a.x_min) / a.resolution_m) * ((a.y_max - a.y_min) / a.resolution_m) > _REM_MAX_GRID_POINTS:
        a.resolution_m *= 2; w.append(f"grid over {_REM_MAX_GRID_POINTS} pts: resolution_m raised to {a.resolution_m:g} m")

    return c, w



# ═══════════════════════════════════════════════════════════════════════════════
# RSRP COVERAGE GRID — computed here, from the model this file already uses
#
# This replaced a subprocess call into a compiled ns-3 binary that lived in the
# Tera5G tree. The binary itself was ordinary ns-3 (libns3.39-nr / -lte /
# -spectrum), but it came from a LICENSE-PATCHED VIAVI appliance, and carrying
# that dependency for one heatmap was a legal risk out of all proportion to the
# feature. The whole of what it produced is a best-server RSRP grid.
#
# Nothing is approximated to get rid of it. The path loss, antenna pattern and
# building attenuation below are the SAME functions the live estimator uses for
# serving-cell selection and SINR — see `_received_power_dbm`. Computing the map
# from them makes the picture and the numbers printed beside it consistent by
# construction, which two separate implementations never guaranteed.
# ═══════════════════════════════════════════════════════════════════════════════

# A grid is O(cells x points), and the payload crosses the wire. 20k points is a
# 140x140 sweep — finer than a coverage picture can show — and the resolution is
# widened rather than the area truncated, so the map still covers what was asked
# for.
_MAX_GRID_POINTS = 20_000


def rsrp_heatmap(cfg: NSGSimulationConfig) -> list[dict]:
    """Best-server RSRP over the scenario area, as [{x, y, rsrp}, ...]."""
    area = cfg.area
    if not cfg.base_stations:
        return []
    step = max(1.0, float(area.resolution_m or 25.0))
    width, height = area.x_max - area.x_min, area.y_max - area.y_min
    if width <= 0 or height <= 0:
        return []
    while (width / step + 1) * (height / step + 1) > _MAX_GRID_POINTS:
        step *= 1.5

    out: list[dict] = []
    y = area.y_min
    while y <= area.y_max:
        x = area.x_min
        while x <= area.x_max:
            best = None
            for bs in cfg.base_stations:
                rx = _received_power_dbm(bs, x, y, cfg.buildings)
                rsrp = rx - _re_spreading_db(bs.radio.band)
                if best is None or rsrp > best:
                    best = rsrp
            out.append({"x": round(x, 1), "y": round(y, 1), "rsrp": round(best, 1)})
            x += step
        y += step
    return out


def run_simulation(
    cfg: NSGSimulationConfig,
    config_path: str = "/tmp/ns3_config.json",
) -> dict:
    """
    Produce the RSRP coverage map for a scenario.

    This used to spawn `ns3-dev-rem-gen`, a compiled ns-3 binary from the Tera5G
    tree, and parse the .npy grid it wrote. The binary was real ns-3 — 22
    libns3.39 libraries, including nr, lte and spectrum — but it shipped inside a
    license-patched VIAVI appliance, and keeping that dependency alive for a
    single heatmap was a legal risk far out of proportion to the feature. This
    computes the same grid from the propagation model already in this file.

    The config is still written to `config_path`. It is no longer input to
    anything, but it is the record of what produced a given map, and the rem hash
    is what the UI caches on.
    """
    payload = translate_nsg_to_ns3(cfg)
    Path(config_path).write_text(json.dumps(payload, indent=2))

    heatmap = rsrp_heatmap(cfg)
    return {
        "status":      "completed",
        "rem_hash":    payload["mapConfig"]["remHash"],
        "config_path": config_path,
        # Kept in the contract, and honest about being empty: nothing writes a
        # .npy any more, and reporting a path that does not exist would send the
        # next reader looking for a file.
        "output_npy":  "",
        "stdout":      (f"computed in process: {len(heatmap)} grid points over "
                        f"{len(cfg.base_stations)} cells "
                        f"(no external binary)"),
        "heatmap":     heatmap,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# LIVE TRAFFIC MODE — analytical throughput estimator (Option B)
#
# No subprocess, no NS-3 binary, no mmap IPC. Reuses the same physical model
# class as the REM heatmap (log-distance path loss + cosine antenna pattern),
# evaluated as a single point per UE group instead of a full grid sweep, so a
# tick can run in microseconds — fast enough for a ~1 Hz WebSocket stream.
#
# This trades NS-3-level fidelity (no real packet scheduling, no per-UE
# contention, no actual MAC-layer queuing) for something that is fast,
# fully ours, debuggable, and directionally realistic: stronger RSRP and
# wider bandwidth produce higher Shannon capacity; QoS targets cap delivered
# throughput; mobility pattern affects how many UEs in a group are presumed
# actively transmitting at any instant.
# ═══════════════════════════════════════════════════════════════════════════════

_NOISE_FIGURE_DB          = 7.0   # typical UE receiver noise figure
_MAX_SPECTRAL_EFFICIENCY  = 7.8   # bits/s/Hz — practical 5G NR 256-QAM ceiling, not raw Shannon
_PHY_EFFICIENCY           = 0.75  # overhead for control channels, reference signals, guard bands
_SINR_FLOOR_DB            = -10.0
_SINR_CEIL_DB             = 30.0

# Fraction of UEs in a group presumed actively transmitting at any instant —
# a coarse proxy for duty cycle, since this estimator has no real packet/
# session model. Static IoT-style groups are assumed mostly-on; mobile
# pedestrian/vehicle groups are bursty.
_ACTIVITY_FACTOR: dict[str, float] = {
    "static":      0.85,
    "pedestrian":  0.55,
    "vehicle":     0.45,
    "drone":       0.65,
    "random_walk": 0.50,
}


def _noise_floor_dbm(bandwidth_hz: float,
                     noise_figure_db: float | None = None) -> float:
    """Thermal noise floor: -174 dBm/Hz + 10*log10(B) + receiver noise figure.

    The figure is the RECEIVER's, so the downlink takes the UE's and the
    uplink takes the gNB's — `settings.ue_noise_figure_db` and
    `settings.gnb_noise_figure_db`, both of which reach ns-3 as
    MmWaveUePhy/MmWaveEnbPhy::NoiseFigure. The default here stays the
    estimator's 7 dB so every existing caller is unchanged; the fast tier
    passes the config's, which is 5 dB unless the user says otherwise and is
    what the engine actually runs.
    """
    nf = _NOISE_FIGURE_DB if noise_figure_db is None else float(noise_figure_db)
    return -174.0 + 10 * math.log10(bandwidth_hz) + nf


# Extra loss (dB) per exterior wall the line-of-sight crosses, by material. A
# blocked building costs this twice (entry + exit).
_WALL_LOSS_DB = {
    WallMaterial.WOOD: 4.0,
    WallMaterial.CONCRETE_WITH_WINDOWS: 12.0,
    WallMaterial.CONCRETE: 20.0,
}


def _los_crosses_building(x0: float, y0: float, z0: float,
                          x1: float, y1: float, z1: float, b) -> bool:
    """Does the 3D line-of-sight (x0,y0,z0)->(x1,y1,z1) pass THROUGH building box b?

    Liang-Barsky clip of the segment against the footprint, then check the ray's
    height at the crossing midpoint against the box: a ray passing over the roof
    (or under a raised box) is not blocked.
    """
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - b.bounds.x_min), (dx, b.bounds.x_max - x0),
                 (-dy, y0 - b.bounds.y_min), (dy, b.bounds.y_max - y0)):
        if p == 0.0:
            if q < 0.0:
                return False          # parallel to this slab and outside it
            continue
        r = q / p
        if p < 0.0:
            if r > t1:
                return False
            t0 = max(t0, r)
        else:
            if r < t0:
                return False
            t1 = min(t1, r)
    if t0 > t1:
        return False
    z_mid = z0 + (z1 - z0) * ((t0 + t1) / 2.0)
    return b.bounds.z_min <= z_mid <= b.bounds.z_max


def _building_loss_db(bs: BaseStation, x: float, y: float, buildings) -> float:
    """Blocking loss (dB) from the scenario's buildings between a cell and (x,y).
    Lets the RIC test's KPIs respect the SAME obstacles the user drew on the map
    (the REM heatmap already did). Capped so a dense block attenuates hard but
    stays numerically sane."""
    if not buildings:
        return 0.0
    total = 0.0
    for b in buildings:
        if _los_crosses_building(bs.position.x, bs.position.y, bs.position.z, x, y, 1.5, b):
            total += 2.0 * _WALL_LOSS_DB.get(b.wall_material, 12.0)
    return min(total, 60.0)


def _received_power_dbm(bs: BaseStation, x: float, y: float, buildings=None) -> float:
    """
    TOTAL received signal power (dBm) at (x, y) from one cell — the full carrier
    power arriving after path loss and antenna pattern.

    This is NOT RSRP. RSRP is the per-resource-element reference-signal power,
    which is this total spread across the channel's subcarriers (~35 dB lower
    for a 100 MHz carrier — see _re_spreading_db). Reporting the total as RSRP
    was the bug that produced implausible ~-16 dBm "RSRP" values.

    Path loss: 3GPP-UMa-LOS-like log-distance (PL = 28 + 22*log10(d) +
    20*log10(f_GHz)). Optimistic (no NLOS/shadowing/building terms) — the REM
    heatmap remains the full-fidelity coverage source; this is the cheap
    per-tick estimate. Antenna directionality: cosine attenuation off boresight,
    clamped to a 20 dB front-to-back floor; isotropic gets no penalty.
    """
    dx = x - bs.position.x
    dy = y - bs.position.y
    horiz_dist = math.hypot(dx, dy)
    height_diff = max(1.0, bs.position.z - 1.5)  # UE height assumed 1.5 m
    distance_m = max(1.0, math.hypot(horiz_dist, height_diff))

    freq_ghz = max(0.1, bs.radio.band.frequency_mhz / 1000.0)
    path_loss_db = 28.0 + 22 * math.log10(distance_m) + 20 * math.log10(freq_ghz)

    ant = bs.radio.antenna
    directional_offset_db = 0.0
    if ant.type != AntennaType.ISOTROPIC and horiz_dist > 1.0:
        bearing_deg = math.degrees(math.atan2(dx, dy)) % 360.0  # 0 = North, clockwise
        angle_h = abs(((bearing_deg - ant.azimuth_deg + 180.0) % 360.0) - 180.0)
        hbw = ant.horizontal_beamwidth_deg or 65.0
        att_h = min(20.0, 12.0 * (angle_h / hbw) ** 2)
        # Vertical pattern: elevation down to the UE vs the antenna's electrical
        # downtilt. UEs close to the mast sit in the pattern null — the classic
        # downtilt effect a horizontal-only model misses (and the reason tilt_deg /
        # vertical_beamwidth_deg previously had no effect on RIC-test KPIs).
        elev_deg = math.degrees(math.atan2(height_diff, horiz_dist))
        vbw = ant.vertical_beamwidth_deg or 7.5
        att_v = min(20.0, 12.0 * ((elev_deg - ant.tilt_deg) / vbw) ** 2)
        directional_offset_db = -min(25.0, att_h + att_v)

    return (bs.radio.tx_power_dbm + ant.gain_dbi + directional_offset_db
            - path_loss_db - _building_loss_db(bs, x, y, buildings))


# Back-compat: older code referenced _estimate_rsrp for what is really total
# received power. Kept as an alias so external imports don't break.
_estimate_rsrp = _received_power_dbm


def _re_spreading_db(band) -> float:
    """
    dB by which total carrier power spreads across resource elements to give
    per-RE (RSRP) power. N_RE ≈ bandwidth / subcarrier-spacing, where
    SCS = 15 kHz * 2^numerology (5G NR). For 100 MHz / numerology 1 this is
    ~35 dB — exactly why real RSRP sits ~35 dB below total received power.
    """
    scs_hz = 15_000 * (2 ** band.numerology)
    n_re = max(1.0, band.bandwidth_mhz * 1e6 / scs_hz)
    return 10 * math.log10(n_re)


def _sinr_db(serving_rx_dbm: float, interferer_rx_dbms: list[float], bandwidth_mhz: float) -> float:
    """
    SINR from total received powers: signal / (thermal noise + co-channel
    interference), computed in linear mW over the full channel bandwidth.

    Modelling interference (not just noise) is what keeps SINR realistic — a
    noise-only SINR in a multi-cell network is absurdly high (60+ dB) and makes
    every UE hit its throughput ceiling (the "QoS always 100" symptom). With
    interference, cell-centre UEs get high SINR and cell-edge/overlap UEs get
    low SINR, as in a real network. Clamped to [floor, ceil].
    """
    bw_hz = max(1.0, bandwidth_mhz) * 1e6
    signal_mw = 10 ** (serving_rx_dbm / 10)
    interference_mw = sum(10 ** (r / 10) for r in interferer_rx_dbms)
    noise_mw = 10 ** (_noise_floor_dbm(bw_hz) / 10)
    sinr = signal_mw / (noise_mw + interference_mw)
    return max(_SINR_FLOOR_DB, min(_SINR_CEIL_DB, 10 * math.log10(sinr)))


def _spectral_efficiency(sinr_db: float) -> float:
    """SINR → usable spectral efficiency (bits/s/Hz), Shannon capped at a
    practical 5G NR ceiling (256-QAM)."""
    return min(_MAX_SPECTRAL_EFFICIENCY, math.log2(1 + 10 ** (sinr_db / 10)))


def _cell_prb_count(band) -> int:
    """Approximate number of downlink PRBs for a carrier. A PRB is 12
    subcarriers; SCS = 15 kHz * 2^numerology. The 0.9 factor accounts for
    guard bands. 100 MHz / numerology 1 → ~250 PRBs (3GPP lists 273)."""
    scs_hz = 15_000 * (2 ** band.numerology)
    prb_bw_hz = 12 * scs_hz
    return max(1, int(band.bandwidth_mhz * 1e6 * 0.9 / prb_bw_hz))


def _prb_throughput_mbps(se: float, band) -> float:
    """Throughput carried by one PRB at spectral efficiency `se` (Mbps)."""
    scs_hz = 15_000 * (2 ** band.numerology)
    prb_bw_hz = 12 * scs_hz
    return se * prb_bw_hz * _PHY_EFFICIENCY / 1e6


def spectral_overlap(victim: tuple, interferer: tuple) -> float:
    """How much of the interferer's power lands inside the victim's band.

    Both carriers are a centre and a width, so the overlap is the intersection
    of two intervals, and what reaches the victim's receiver is the fraction of
    the INTERFERER'S power that falls in it — its power is spread over its own
    width, not the victim's. Co-channel gives 1.0 and the formula collapses to
    what the single-band model always did; disjoint bands give 0.0, which is
    the whole reason an operator buys a second band.

    A narrow interferer sitting entirely inside a wide victim gives 1.0 too:
    all of its power arrives. It is concentrated rather than spread, which
    matters to a per-subcarrier receiver and not to the wideband SINR this
    model computes.
    """
    fv, bv = victim[0], max(1e-9, victim[1])
    fi, bi = interferer[0], max(1e-9, interferer[1])
    ov = min(fv + bv / 2, fi + bi / 2) - max(fv - bv / 2, fi - bi / 2)
    return max(0.0, min(1.0, ov / bi))


def link_metrics(
    serving_bs: BaseStation,
    serving_rx_dbm: float,
    all_cell_rx: list[tuple[BaseStation, float]],
) -> tuple[float, float, float]:
    """
    Given a UE's serving cell + every candidate cell's received power at that
    UE, return (rsrp_dbm, sinr_db, spectral_efficiency).

    Interferers = other cells on the SAME frequency as the serving cell
    (co-channel); different bands don't interfere. Shared by the live-traffic
    estimator and the RIC-test sim so both stay physically consistent.

    Returns spectral efficiency (bits/s/Hz), not raw throughput, because the
    caller must share the serving cell's finite PRBs across all its UEs before
    a per-UE throughput is meaningful — a single UE does not get the whole cell.
    """
    # Interference is weighted by the SPECTRUM SHARED, not gated on the centre
    # frequencies being equal. Exact equality is right for the common case and
    # wrong twice over otherwise: two carriers 20 MHz apart with 40 MHz each
    # overlap by half and were counted as no interference at all, and a narrow
    # interferer inside a wide victim was discarded even though all of its
    # power arrives. The fast tier and this estimator now use one function.
    band = (serving_bs.radio.band.frequency_mhz,
            serving_bs.radio.band.bandwidth_mhz)
    interferers = []
    for bs, rx in all_cell_rx:
        if bs.id == serving_bs.id:
            continue
        frac = spectral_overlap(band, (bs.radio.band.frequency_mhz,
                                       bs.radio.band.bandwidth_mhz))
        if frac > 0:
            interferers.append(rx + 10 * math.log10(frac))
    sinr = _sinr_db(serving_rx_dbm, interferers, serving_bs.radio.band.bandwidth_mhz)
    rsrp = serving_rx_dbm - _re_spreading_db(serving_bs.radio.band)
    return rsrp, sinr, _spectral_efficiency(sinr)


def estimate_live_throughput(cfg: NSGSimulationConfig, tick: int) -> dict[str, float]:
    """
    Estimate live network KPIs for one tick: per-slice throughput plus the
    same system-level metrics the (now-unused) InfluxDB /live-metrics path
    used to report — power, PRB usage, RSRP, active UEs, QoS score. All of
    them fall out of data this function already computes per UE group, so
    they're accumulated in the same loop rather than requiring a second pass.

    For each UE group: find its best-serving cell, compute per-UE Shannon
    capacity from the interference-aware SINR (signal vs thermal noise +
    co-channel interference from other cells) and bandwidth, cap delivered
    throughput at the group's QoS target (a UE doesn't draw more than its app
    demands even if the link could support it), scale by an estimated count of
    actively-transmitting UEs, and sum into the matching NetworkSlice bucket.

    Derived system metrics (all approximations, not NS-3-measured):
      tput        Mbps  — sum of all four slice totals
      power       W     — sum of configured tx_power_dbm across all sectors,
                           converted from dBm. Static per config; this
                           estimator has no PA-efficiency-vs-load model.
      prb         %     — demand/capacity ratio: how much of each UE's
                           available Shannon capacity is actually being used
                           to carry its (QoS-capped) delivered traffic,
                           averaged across active UEs. A proxy for resource
                           utilization, not an actual PRB scheduler.
      qos         /100  — average of min(1, delivered/target) across active
                           UEs × 100. 100 = every UE is getting its full QoS
                           target; lower = demand is being throttled by RF
                           capacity.
      conn_ues    count — sum of (group.count × activity factor) across groups.
      rsrp        dBm   — average best-server RSRP across UE groups.
      energy_eff  b/J   — tput_bps / power_watts.
      handover    —     — NOT modeled: this estimator has no time-stepped
                           mobility/position simulation, so there's nothing to
                           hand over between ticks. Omitted rather than faked.

    A small amount of per-tick jitter is layered onto the slice totals purely
    for a "live" feel on the chart — cosmetic only, doesn't affect the other
    derived metrics.

    Returns: {"time", "eMBB", "uRLLC", "mMTC", "voice", "tput", "power",
              "prb", "qos", "conn_ues", "rsrp", "energy_eff"}
    """
    totals: dict[str, float] = {"eMBB": 0.0, "uRLLC": 0.0, "mMTC": 0.0, "voice": 0.0}

    total_active_ues  = 0.0
    rsrp_weighted_sum = 0.0

    # ── Pass 1: per UE group → serving cell, SE, and PRB demand to meet target ──
    # A group's PRB demand = (target × active_ues) / (throughput per PRB at its SE).
    # Higher SINR (better SE) means fewer PRBs needed for the same target.
    demand_per_cell: dict[str, float] = {bs.id: 0.0 for bs in cfg.base_stations}
    group_state = []  # (ue, serving_bs, active_ues, prb_needed_total, se)

    for ue in cfg.ue_groups:
        all_rx = [(bs, _received_power_dbm(bs, ue.spawn_position.x,
                                           ue.spawn_position.y, cfg.buildings))
                  for bs in cfg.base_stations]
        if not all_rx:
            continue
        serving_bs, serving_rx = max(all_rx, key=lambda t: t[1])
        rsrp, _sinr, se = link_metrics(serving_bs, serving_rx, all_rx)

        activity = _ACTIVITY_FACTOR.get(ue.mobility.pattern.value, 0.6)
        active_ues = ue.count * activity
        total_active_ues  += active_ues
        rsrp_weighted_sum += rsrp * active_ues

        prb_tp = _prb_throughput_mbps(se, serving_bs.radio.band)
        target_mbps = sum(svc.target_throughput_gbps * 1000.0 for svc in ue.services)
        prb_needed = (target_mbps * active_ues / prb_tp) if prb_tp > 0 else 0.0
        demand_per_cell[serving_bs.id] += prb_needed
        group_state.append((ue, serving_bs, active_ues, prb_needed, se))

    # ── Pass 2: throttle each cell's UEs to its PRB budget, then deliver ────────
    # throttle = available / demand (≤1). Congested cells (demand > PRBs) throttle
    # every UE proportionally → QoS drops; lightly-loaded cells deliver full target.
    throttle: dict[str, float] = {}
    for bs in cfg.base_stations:
        avail = _cell_prb_count(bs.radio.band)
        demand = demand_per_cell[bs.id]
        throttle[bs.id] = min(1.0, avail / demand) if demand > 0 else 1.0

    satisfaction_sum = 0.0
    for ue, serving_bs, active_ues, _prb_needed, _se in group_state:
        t = throttle[serving_bs.id]
        for svc in ue.services:
            target_mbps = svc.target_throughput_gbps * 1000.0
            delivered_per_ue = target_mbps * t

            # Slice PRB cap (QoS assurance / RIC control) scales that slice further.
            # (In the estimator there are no live overrides, but a config could
            #  carry per-slice policy in future; left as a no-op multiplier here.)

            totals[svc.slice_type.value] += delivered_per_ue * active_ues
            satisfaction_sum += t * active_ues  # delivered/target == throttle

    # Total configured transmit power across every sector (dBm -> W), independent of load.
    total_power_w = sum(10 ** (bs.radio.tx_power_dbm / 10) / 1000.0 for bs in cfg.base_stations)

    total_tput_mbps = sum(totals.values())
    avg_rsrp = (rsrp_weighted_sum / total_active_ues) if total_active_ues > 0 else 0.0
    # Network PRB usage = load-weighted mean of per-cell min(100%, demand/avail).
    total_demand = sum(demand_per_cell.values())
    total_avail  = sum(_cell_prb_count(bs.radio.band) for bs in cfg.base_stations if demand_per_cell[bs.id] > 0)
    prb_usage_pct = min(100.0, total_demand / total_avail * 100) if total_avail > 0 else 0.0
    qos_score  = (satisfaction_sum / total_active_ues * 100) if total_active_ues > 0 else 0.0
    energy_eff = (total_tput_mbps * 1e6) / total_power_w if total_power_w > 0 else 0.0  # bits/s ÷ W = bits/J

    # Light jitter so the live chart visibly moves tick-to-tick — cosmetic only,
    # applied to the slice totals; the derived system metrics below are not jittered.
    jittered = {
        slice_name: round(max(0.0, value + random.gauss(0, value * 0.06)), 2) if value > 0 else 0.0
        for slice_name, value in totals.items()
    }

    return {
        "time": tick,
        **jittered,
        "tput":       round(sum(jittered.values()), 2),
        "power":      round(total_power_w, 1),
        "prb":        round(min(100.0, prb_usage_pct), 1),
        "qos":        round(min(100.0, qos_score), 1),
        "conn_ues":   round(total_active_ues),
        "rsrp":       round(avg_rsrp, 1),
        "energy_eff": round(energy_eff, 2),
    }
