"""
NSG AI Platform — Simulation Configuration Schema
Pydantic V2. Designed for LLM generation and Next.js frontend consumption.

Legacy API models (RouteResponse, ScenarioModifications) are preserved at the
top so existing FastAPI routes in main.py continue working without modification.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Dict, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ═══════════════════════════════════════════════════════════════════════════════
# LEGACY API MODELS  —  used by existing FastAPI routes, do not remove
# ═══════════════════════════════════════════════════════════════════════════════

class RouteResponse(BaseModel):
    config_path: str = Field(description="The exact config_path from the template menu.")


class ScenarioModifications(BaseModel):
    modifications: dict = Field(description="Dictionary of YAML paths and their new values.")
    ai_message: str = Field(
        description="A brief, professional summary of exactly what was changed."
    )

    @field_validator("modifications")
    @classmethod
    def enforce_hard_limits(cls, mods: dict) -> dict:
        for key, value in mods.items():
            if "rsrp_threshold" in key and not (-160 <= value <= -40):
                raise ValueError(f"RSRP threshold must be between -160 and -40. Got {value}")
            if ("targetTput" in key or "gbrTput" in key) and value > 1.0:
                raise ValueError(f"Throughput cannot exceed 1.0 per UE. Got {value}")
            if "delay" in key and not re.match(r"^[0-9]+(\.[0-9]+)?[smhd]$", str(value)):
                raise ValueError(f"Delay must match format e.g. '10s' or '5m'. Got {value}")
        return mods


# ═══════════════════════════════════════════════════════════════════════════════
# ENUMERATIONS
# ═══════════════════════════════════════════════════════════════════════════════

class AntennaType(str, Enum):
    COSINE       = "cosine"        # Simple 3-sector pattern (default)
    MASSIVE_MIMO = "massive_mimo"  # 5G mMIMO — 3GPP TR 38.901 model
    ISOTROPIC    = "isotropic"     # Omni-directional reference antenna


class BeamformingMode(str, Enum):
    ANALOG  = "analog"   # Phase-shifter only — single beam, lower cost
    DIGITAL = "digital"  # One RF chain per element — full flexibility
    HYBRID  = "hybrid"   # Combined analog + digital


class MobilityPattern(str, Enum):
    STATIC     = "static"
    PEDESTRIAN = "pedestrian"   # ~3 m/s, Manhattan-grid walk
    VEHICLE    = "vehicle"      # ~10–30 m/s, road-following
    DRONE      = "drone"        # 3D waypoint-based flight path
    RANDOM_WALK = "random_walk"


class NetworkSlice(str, Enum):
    EMBB  = "eMBB"   # Enhanced Mobile Broadband
    URLLC = "uRLLC"  # Ultra-Reliable Low-Latency
    VOICE = "voice"  # Conversational Voice (VoNR / 5QI 1)
    MMTC  = "mMTC"   # Massive Machine-Type Communications


class BuildingType(str, Enum):
    RESIDENTIAL = "residential"
    OFFICE      = "office"
    INDUSTRIAL  = "industrial"


class WallMaterial(str, Enum):
    WOOD                    = "wood"
    CONCRETE                = "concrete"
    CONCRETE_WITH_WINDOWS   = "concrete_with_windows"


class PropagationScenario(str, Enum):
    UMA          = "UMa"            # Urban Macro — open field
    UMA_BUILDINGS = "UMa-Buildings" # Urban Macro — with building obstacles
    UMI          = "UMi"            # Urban Micro / street-level
    RMA          = "RMa"            # Rural Macro


# ═══════════════════════════════════════════════════════════════════════════════
# GEOMETRY PRIMITIVES
# ═══════════════════════════════════════════════════════════════════════════════

class Position3D(BaseModel):
    x: float = Field(..., description="East offset from simulation origin in meters")
    y: float = Field(..., description="North offset from simulation origin in meters")
    z: float = Field(0.0, description="Height above ground level in meters")


class Position2D(BaseModel):
    x: float = Field(..., description="East offset from simulation origin in meters")
    y: float = Field(..., description="North offset from simulation origin in meters")


class BoundingBox3D(BaseModel):
    x_min: float = Field(..., description="West edge of building footprint in meters")
    x_max: float = Field(..., description="East edge of building footprint in meters")
    y_min: float = Field(..., description="South edge of building footprint in meters")
    y_max: float = Field(..., description="North edge of building footprint in meters")
    z_min: float = Field(0.0, description="Base of the building (usually 0)")
    z_max: float = Field(..., description="Roof height in meters")


# ═══════════════════════════════════════════════════════════════════════════════
# RADIO: ANTENNA & FREQUENCY BAND
# ═══════════════════════════════════════════════════════════════════════════════

class MassiveMIMOConfig(BaseModel):
    """
    mMIMO-specific parameters. Required when AntennaConfig.type == 'massive_mimo'.
    Models a planar antenna array per 3GPP TR 38.901 / NS-3 NR module.
    """
    rows: int = Field(
        8, ge=1, le=16,
        description="Vertical antenna elements in the gNB panel. Default 8 to "
                    "MATCH the engine, which uses an 8x8 panel whenever a config "
                    "carries no massive_mimo block — this field used to default "
                    "to 4, so adding the block at all silently halved the array.\n"
                    "\n"
                    "This is also the ONLY config lever measured to move "
                    "simulation SPEED (2026-09-13). The 3GPP steering vector is "
                    "computed per element, and that trigonometry is the engine's "
                    "hottest code, so element count sets the cost: 3 gNB / 30 UE "
                    "took 28.1 s of wall clock per simulated second at 8x8 and "
                    "19.6 s at 4x4 — 1.43x more network time per run. It curves "
                    "off there; 2x2 only reached 1.52x.\n"
                    "\n"
                    "The price is array gain, and it lands on the best links: at "
                    "4x4 handover behaviour and MEAN throughput were unchanged "
                    "while PEAK throughput fell about 35%. Use 4x4 when the "
                    "question is timing, mobility or handover; keep 8x8 when it "
                    "is link budget or peak rate "
                    "(UniformPlanarArray::NumRows)."
    )
    cols: int = Field(
        8, ge=1, le=16,
        description="Horizontal antenna elements in the gNB panel. Same default "
                    "and the same speed/gain trade as `rows` — cost tracks "
                    "rows x cols, so 8x8 is 64 elements against 4x4's 16 "
                    "(UniformPlanarArray::NumColumns)."
    )
    h_spacing_wl: float = Field(
        0.5, ge=0.1, le=4.0,
        description="Horizontal element spacing in wavelengths. 0.5 is the 3GPP "
                    "default; tighter couples the elements and broadens the "
                    "beam, wider sharpens it and raises grating lobes "
                    "(UniformPlanarArray::AntennaHorizontalSpacing)."
    )
    v_spacing_wl: float = Field(
        0.5, ge=0.1, le=4.0,
        description="Vertical element spacing in wavelengths "
                    "(UniformPlanarArray::AntennaVerticalSpacing)."
    )
    polarization_deg: float = Field(
        0.0,
        description="Polarisation slant of the panel's elements in degrees "
                    "(UniformPlanarArray::PolSlantAngle). ±45 models a "
                    "cross-polarised panel. Real engine lever — but the UE "
                    "arrays stay at 0°, so a slant here introduces a genuine "
                    "polarisation mismatch loss rather than a free option. "
                    "Default 0 keeps gNB and UE aligned."
    )

    @property
    def antenna_mask(self) -> str:
        """Bit string enabling all rows × cols antenna elements."""
        return "1" * (self.rows * self.cols)


class AntennaConfig(BaseModel):
    """
    Antenna radiation pattern and pointing configuration for one cell sector.
    For 'cosine': uses horizontal/vertical beamwidths.
    For 'massive_mimo': requires the nested MassiveMIMOConfig.
    """
    type: AntennaType = Field(
        AntennaType.COSINE,
        description="Radiation pattern model. FAST/ANALYTICAL TIER ONLY — on the "
                    "real engine the element pattern is chosen network-wide by "
                    "settings.antenna_element (isotropic or 3GPP TR 38.901)."
    )
    azimuth_deg: float = Field(
        0.0,
        description="Mechanical boresight azimuth. 0 = North, increases clockwise (0–359)."
    )
    tilt_deg: float = Field(
        3.0, ge=0.0, le=20.0,
        description="Downtilt in degrees, aimed toward the ground. Bounded 0–20 "
                    "because that is the range a real macro site has: a few "
                    "degrees of mechanical tilt plus up to about ten of "
                    "electrical, and beyond ~20 the main lobe is in the dirt at "
                    "the foot of the mast. The schema used to accept −90 to +90, "
                    "which let a config ask for something no deployed antenna "
                    "does. Only bites when settings.antenna_element is '3gpp' — "
                    "the default isotropic element has no pattern to tilt."
    )
    gain_dbi: float = Field(
        15.0,
        description="Peak antenna gain in dBi (typical: 0–30). Under the "
                    "'cosine' and 'circular' elements this is the ELEMENT's "
                    "boresight gain and reaches the engine; the array's own "
                    "gain from massive_mimo.rows × cols (8×8 ≈ 18 dBi) stacks "
                    "on top of it. Under '3gpp' and 'isotropic' the element has "
                    "no settable gain, so array size is the only lever."
    )
    horizontal_beamwidth_deg: float = Field(
        65.0,
        description="3 dB horizontal beamwidth in degrees (1–360). REAL on the "
                    "engine when settings.antenna_element is 'cosine' or "
                    "'parabolic' — those elements take a beamwidth. It is "
                    "correctly ignored under '3gpp' (TR 38.901 fixes the "
                    "pattern) and 'isotropic' (no direction at all). Under "
                    "'circular' this field carries the aperture RADIUS in "
                    "centimetres instead, because a dish's beamwidth follows "
                    "from radius and frequency rather than being set."
    )
    vertical_beamwidth_deg: float = Field(
        7.5,
        description="3 dB vertical beamwidth in degrees (1–360). REAL under the "
                    "'cosine' element, which takes H and V independently. "
                    "'parabolic' has a single beamwidth and reads the "
                    "horizontal figure only."
    )
    massive_mimo: Optional[MassiveMIMOConfig] = Field(
        None,
        description="mMIMO array parameters. Required when type is 'massive_mimo'."
    )

    @model_validator(mode="after")
    def mimo_params_required_when_type_is_mimo(self) -> "AntennaConfig":
        if self.type == AntennaType.MASSIVE_MIMO and self.massive_mimo is None:
            self.massive_mimo = MassiveMIMOConfig()
        return self


class FrequencyBand(BaseModel):
    """5G NR / LTE frequency band and channel configuration."""
    label: str = Field(
        ...,
        description="Human-readable band label, e.g. 'NR-N77' or 'LTE-B3'"
    )
    frequency_mhz: float = Field(
        ..., ge=100, le=100_000,
        description="Carrier center frequency in MHz (e.g. 700, 1800, 2100, 3600, 28000)"
    )
    bandwidth_mhz: float = Field(
        ..., ge=1, le=400,
        description="Channel bandwidth in MHz (e.g. 20, 50, 100 for NR; up to 400 for mmWave)"
    )
    numerology: int = Field(
        2, ge=0, le=6,
        description=(
            "5G NR subcarrier spacing (µ): 0 = 15 kHz, 1 = 30 kHz, 2 = 60 kHz, "
            "3 = 120 kHz. Real engine lever — but the ns-O-RAN engine defines "
            "only µ2 and µ3, so anything outside that is CLAMPED into 2–3 for "
            "the real-engine run (the same documented clamping the platform "
            "applies to cell count, bandwidth and UE speed). It used to be "
            "accepted and never passed at all."
        )
    )


class SleepProfile(BaseModel):
    """
    RU sleep state machine for one cell — the energy-saving lever.

    A cell that is `active: false` is switched OFF (0 W, and nothing brings it
    back). Sleep is the state in between: with no UEs to serve the radio
    descends into a sleep depth, drawing far less while staying wakeable. The
    deeper the sleep, the bigger the saving and the longer it takes to serve
    traffic again. Runs on the real ns-O-RAN engine.
    """
    mode: Literal["none", "micro", "light", "deep"] = Field(
        "none",
        description="Sleep depth this cell may descend to when idle. "
                    "none = always on (default); micro = symbol-level, wakes in "
                    "microseconds, saves ~30%; light = slot-level, ~1 ms wake, "
                    "saves ~50%; deep = ~10 ms wake, saves ~67%."
    )
    idle_timer_ms: float = Field(
        200.0, ge=1.0, le=60000.0,
        description="Shutdown timer: how long the cell must carry no traffic "
                    "before it descends into the sleep state."
    )
    wake_delay_ms: Optional[float] = Field(
        None, ge=0.0, le=10000.0,
        description="Wake-up latency before the cell can serve again. null = "
                    "the physical default for the depth (micro 0.071, light 1, "
                    "deep 10 ms). During this window the cell is off the air."
    )


class RadioConfig(BaseModel):
    band: FrequencyBand = Field(..., description="Frequency band and channel configuration")
    tx_power_dbm: float = Field(
        43.0, ge=-60, le=70,
        description="Total radiated transmit power per cell in dBm (typical macro: 43–46, small cell: 23–33)"
    )
    antenna: AntennaConfig = Field(default_factory=AntennaConfig)
    sleep: SleepProfile = Field(
        default_factory=SleepProfile,
        description="RU sleep-state (energy saving) profile for this cell."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# BASE STATION
# ═══════════════════════════════════════════════════════════════════════════════

class BaseStation(BaseModel):
    """
    A single gNB (5G) or eNB (4G) cell sector.
    Each entry maps to exactly one cellConfig element in the NS-3 JSON payload.
    """
    id: str = Field(
        ...,
        description="Unique machine-readable identifier, e.g. 'gnb-site-a-sector-1'"
    )
    name: str = Field(
        ...,
        description="Display label shown in the topology view, e.g. 'Site-A / N77 / Cell-1'"
    )
    position: Position3D = Field(
        ...,
        description=(
            "Location of the antenna: x/y are horizontal offsets from the "
            "simulation origin in meters; z is antenna height above ground."
        )
    )
    radio: RadioConfig


# ═══════════════════════════════════════════════════════════════════════════════
# USER EQUIPMENT GROUPS
# ═══════════════════════════════════════════════════════════════════════════════

# The 5QIs this engine can actually carry, with the label ns-3's own QoS table
# gives each one. Anything outside this set is not a stricter schema rule for
# its own sake: EpsBearer's priority/delay-budget/error-rate lookup ends in
# NS_FATAL_ERROR, so an unknown 5QI aborts the run at the first bearer setup.
#
# The values above 9 are the reason this list exists. The platform used to
# accept any integer and the engine clamped everything to the LTE 1-9 range,
# so mission-critical, V2X, AR and industrial-automation classes were silently
# rewritten to "video, best effort" — a scenario that looked configured and
# wasn't.
QOS_5QI_CATALOGUE: dict[int, str] = {
    1: "Conversational voice (GBR)",
    2: "Conversational video, live (GBR)",
    3: "Real-time gaming, V2X, process automation (GBR)",
    4: "Non-conversational video, buffered (GBR)",
    5: "IMS signalling (non-GBR)",
    6: "Video buffered, TCP — operator (non-GBR)",
    7: "Voice, video live, interactive gaming (non-GBR)",
    8: "Video buffered, TCP — premium (non-GBR)",
    9: "Video buffered, TCP — default (non-GBR)",
    65: "Mission-critical push-to-talk voice (GBR)",
    66: "Non-mission-critical push-to-talk voice (GBR)",
    67: "Mission-critical video (GBR)",
    69: "Mission-critical delay-sensitive signalling (non-GBR)",
    70: "Mission-critical data (non-GBR)",
    75: "V2X messages (GBR)",
    79: "V2X messages (non-GBR)",
    80: "Low-latency eMBB, augmented reality (non-GBR)",
    82: "Discrete automation, small packets <=255 B (GBR)",
    83: "Discrete automation, big packets <=1358 B (GBR)",
    84: "Intelligent transport systems (GBR)",
    85: "Electricity distribution, high voltage (GBR)",
}


class TrafficProtocol(str, Enum):
    UDP = "udp"   # open loop — offered load is what the radio sees
    TCP = "tcp"   # closed loop — congestion control reacts to the radio


class CallDistribution(str, Enum):
    """Shape of the on/off call process. All keep the SAME mean, so offered
    load is comparable and only the burstiness changes."""
    EXPONENTIAL = "exponential"  # memoryless, the classic Poisson call model
    CONSTANT    = "constant"     # a metronome — the repeatable control case
    UNIFORM     = "uniform"      # mild spread over [0, 2x mean]
    PARETO      = "pareto"       # heavy-tailed: real web/video burst behaviour


class ServiceRequirement(BaseModel):
    """One QoS bearer / traffic flow carried by UEs in the group."""
    slice_type: NetworkSlice = Field(..., description="Network slice category")
    qos_id: int = Field(
        ...,
        # The validator below is the enforcement; this is the ADVERTISEMENT.
        # Without it the JSON Schema says "integer" and the config UI renders a
        # free number box for a field with exactly 21 legal values — the one
        # field most in need of a dropdown. x_enum_labels is ours, and the
        # editor uses it to show "82 — Discrete automation" instead of "82".
        json_schema_extra={
            "enum": sorted(QOS_5QI_CATALOGUE),
            "x_enum_labels": {str(k): f"{k} — {v}"
                              for k, v in sorted(QOS_5QI_CATALOGUE.items())},
        },
        description="5G QoS Identifier (5QI) per 3GPP TS 23.501. This engine "
                    "carries the full standardised set — 1-9 plus the 5G-only "
                    "classes 65/66/67/69/70 (mission critical), 75/79 (V2X), "
                    "80 (low-latency eMBB / AR) and 82-85 (industrial "
                    "automation). Each carries its own priority, packet delay "
                    "budget and error loss rate."
    )

    @field_validator("qos_id")
    @classmethod
    def _known_5qi(cls, v: int) -> int:
        # Rejected rather than coerced. 5QI is categorical, not a range: a
        # value outside the set is not "too big", it is meaningless. The
        # engine's own fallback rewrites it to 9, which is how mission-critical
        # and V2X services used to run as best-effort video while the config
        # still said otherwise — a scenario that looks configured and is not is
        # the exact failure this project keeps paying for.
        if v not in QOS_5QI_CATALOGUE:
            raise ValueError(
                f"{v} is not a standardised 5QI this engine can carry. "
                f"Valid: {sorted(QOS_5QI_CATALOGUE)}")
        return v
    protocol: TrafficProtocol = Field(
        TrafficProtocol.UDP,
        description="Transport for this flow. UDP is open loop: the offered "
                    "rate arrives at the radio whatever the radio does. TCP is "
                    "closed loop — its congestion control reacts to the losses "
                    "and delay the RAN produces, so the same scenario yields a "
                    "different (usually lower and less steady) throughput. Use "
                    "TCP when the point is how the network behaves under a real "
                    "application, UDP when the point is the radio itself."
    )
    call_distribution: CallDistribution = Field(
        CallDistribution.EXPONENTIAL,
        description="Shape of the on/off call process. Only meaningful with "
                    "avg_call_duration_s. The mean is preserved across all "
                    "four, so switching changes the BURSTINESS, not the load: "
                    "'pareto' produces rare very long bursts that push peak "
                    "load far above the average, which is what a RIC has to "
                    "survive; 'constant' removes the randomness entirely."
    )
    target_throughput_gbps: float = Field(
        0.01,
        description="Per-UE downlink throughput target in Gbps (e.g. 0.1 for eMBB, 0.005 for IoT)"
    )
    # Call/session dynamics. Without these every UE is a constant-rate faucet,
    # which no real network looks like — real traffic breathes, and a RIC test
    # is only interesting when load fluctuates (sleep cells in the lull, wake
    # them in the burst). Modelled as exponential on/off (a Poisson-style call
    # process, like the classic "average call duration / time between calls").
    avg_call_duration_s: float | None = Field(
        None,
        description="Mean length of an active transmission burst in seconds "
                    "(exponentially distributed). Omit/None = continuous traffic."
    )
    avg_idle_duration_s: float | None = Field(
        None,
        description="Mean silence between bursts in seconds (exponentially "
                    "distributed). Only meaningful with avg_call_duration_s."
    )


class WaypointStep(BaseModel):
    """One step in a drone/UAV flight path."""
    position: Position3D
    speed_mps: float = Field(
        5.0,
        description="Speed used to travel from the previous waypoint to this one (m/s)"
    )


class MobilityConfig(BaseModel):
    pattern: MobilityPattern = Field(
        MobilityPattern.PEDESTRIAN,
        description="Movement archetype. On the REAL engine the levers that "
                    "actually move a UE are speed_mps and waypoints — this "
                    "field only sets a traffic activity factor in the fast "
                    "analytical tier. Set waypoints for a path that crosses "
                    "cell boundaries."
    )
    speed_mps: float = Field(
        3.0,
        description="Average UE speed in m/s (e.g. 1.4 pedestrian, 14 vehicle, 0 static). Ignored for 'drone'."
    )
    waypoints: Optional[list[WaypointStep]] = Field(
        None,
        description=(
            "Ordered 3D waypoints the group TRAVERSES, at the per-leg speed. "
            "Real ns-O-RAN engine lever (WaypointMobilityModel): unlike the "
            "default random walk — which diffuses and nets almost no "
            "displacement — a waypoint path actually crosses cell boundaries, "
            "so it is the mobility lever that produces handovers. Each UE in "
            "the group flies the path translated to its own start position, so "
            "the group moves as a spread-out convoy rather than a single point."
        )
    )
    waypoint_mode: Literal["once", "repeat", "pingpong"] = Field(
        "pingpong",
        description=(
            "What happens at the end of the waypoint path: 'once' = stop there "
            "(a UE parked for the rest of the run), 'repeat' = teleport back to "
            "the start and fly it again, 'pingpong' = retrace it in reverse. "
            "Default 'pingpong' — a path walked once usually ends before the "
            "run does, which silently turns a mobility test into a static one."
        )
    )


class UEGroup(BaseModel):
    """
    A population of user devices sharing the same mobility and traffic profile.
    UE groups drive the demand-side of the simulation but are not passed to
    ns3-dev-rem-gen directly — they are used by higher-level traffic generators.
    """
    id: str = Field(
        ...,
        description="Unique group identifier, e.g. 'ue-downtown-pedestrians'"
    )
    name: str = Field(
        ...,
        description="Human-readable label shown in the frontend, e.g. 'Downtown Pedestrians'"
    )
    count: int = Field(..., ge=1, description="Number of UEs in this group (e.g. 10–10000)")
    spawn_position: Position2D = Field(
        ...,
        description="Center point from which UEs are initially distributed"
    )
    height_m: float = Field(
        1.5, ge=0.5, le=150.0,
        description="UE antenna height above ground in metres. Real engine "
                    "lever — it was hardcoded to 3 m for every node, so a "
                    "handset and a rooftop relay looked identical to the "
                    "channel model. Drones should set this (or use waypoints, "
                    "whose z overrides it in flight)."
    )
    mobility: MobilityConfig = Field(default_factory=MobilityConfig)
    services: list[ServiceRequirement] = Field(
        ..., min_length=1,
        description="Traffic flows generated by this group. At least one required."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ENVIRONMENT / BUILDINGS
# ═══════════════════════════════════════════════════════════════════════════════

class Building(BaseModel):
    """
    A physical RF obstacle.
    Each entry maps to one buildingConfig element in the NS-3 JSON payload.
    The ITU-R indoor penetration model uses nFloors / nRoomsX / nRoomsY to
    compute additional path loss for UEs inside the building.
    """
    id: str = Field(..., description="Unique building identifier, e.g. 'bldg-office-north'")
    bounds: BoundingBox3D = Field(..., description="3D spatial extents of the building")
    building_type: BuildingType = Field(BuildingType.OFFICE)
    wall_material: WallMaterial = Field(WallMaterial.CONCRETE_WITH_WINDOWS)
    floors: int = Field(4, description="Number of floors (e.g. 1–100)")
    rooms_x: int = Field(
        4,
        description="Room columns per floor — affects the ITU indoor path-loss model (typical: 2–10)"
    )
    rooms_y: int = Field(4, description="Room rows per floor (typical: 2–10)")


# ═══════════════════════════════════════════════════════════════════════════════
# SIMULATION CONTROL
# ═══════════════════════════════════════════════════════════════════════════════

class SimulationArea(BaseModel):
    """
    Axis-aligned bounding box for the Radio Environment Map (REM).
    The NS-3 binary samples RSRP at a regular grid across this rectangle.
    Smaller resolution_m → finer coverage map → higher compute cost.
    """
    x_min: float = Field(-1000.0, description="West boundary in meters from origin")
    x_max: float = Field( 1000.0, description="East boundary in meters from origin")
    y_min: float = Field(-1000.0, description="South boundary in meters from origin")
    y_max: float = Field( 1000.0, description="North boundary in meters from origin")
    resolution_m: float = Field(
        37.0, gt=0,
        description="Grid sampling step in meters — smaller = finer map = higher compute cost (e.g. 5–100)"
    )

    @model_validator(mode="after")
    def _area_not_degenerate(self) -> "SimulationArea":
        if self.x_max <= self.x_min or self.y_max <= self.y_min:
            raise ValueError(
                f"Simulation area is inverted or zero-size: x[{self.x_min}, {self.x_max}] "
                f"y[{self.y_min}, {self.y_max}]. Need x_max > x_min and y_max > y_min."
            )
        return self


class SimulationSettings(BaseModel):
    duration_s: int = Field(
        180,
        description="Target simulated duration in seconds. Used IN FULL by the "
                    "fast analytical tier, and NOT AT ALL by the ns-O-RAN "
                    "engine — it does not reach it, so raising it buys no extra "
                    "simulated time on a batch or campaign run. What bounds "
                    "those is the KPM WINDOW BUDGET: on a LIVE/campaign run, "
                    "about 13 windows per simulated second, so 40 windows cover "
                    "roughly 3 s and 120 cover about 10 s (measured 2026-09-09: "
                    "120 windows gave 10.2 s at 1 UE and 10.1 s at 9 UE — more "
                    "UEs cost wall time, not simulated time). On a BATCH run the "
                    "equivalent lever is settings.kpm_collect_s. Raise one of "
                    "those, not this, to see more of a scenario that unfolds "
                    "over time."
    )
    report_interval_ms: int = Field(
        1000, ge=100, le=5000,
        description="KPI export granularity in ms — the real engine's KPM "
                    "indication period. Lower means more samples per run but "
                    "not more simulated time; sampling is bounded by wall "
                    "clock, not by this."
    )
    kpm_collect_s: Optional[int] = Field(
        None, ge=10, le=14400,
        description="How many WALL seconds the batch engine collects KPM for — "
                    "and therefore how much SIMULATED time the run covers, "
                    "because the two are linear with no diminishing return "
                    "(measured 2026-09-13 at 3 cells / 6 UEs: windows of 10 / "
                    "30 / 60 / 120 s gave spans of 4.1 / 5.8 / 9.2 / 13.9 "
                    "simulated seconds, a flat ~0.10 s per wall second). This "
                    "is the ONLY way a batch run sees past a sub-second "
                    "horizon: on the product path the same config went 1.1 s of "
                    "span at the automatic window to 3.6 s at 120 s, which is "
                    "what made a cell outage at start_s=2.0 actually fire "
                    "(serving cell 9840 J -> 4841 J) instead of coming back "
                    "byte-identical to a run with no fault. null = let the "
                    "engine size it from the UE count (10-180 s), which is "
                    "right for everything that does not turn on elapsed time. "
                    "Setting it BUYS simulated seconds with wall clock, one for "
                    "one: 900 s here means a run of roughly a quarter of an hour "
                    "and the ceiling of 14400 means four. Past about an hour "
                    "prefer the live path — a batch run is one blocking request "
                    "with a single salvage point at the end, so a very long one "
                    "stakes everything it collected on that connection."
    )
    scenario: PropagationScenario = Field(
        PropagationScenario.UMA_BUILDINGS,
        description="3GPP propagation environment. Selects the path-loss model "
                    "AND the matching LOS/NLOS channel-condition model in the "
                    "real engine (UMa / UMi street canyon / RMa). Drawing "
                    "buildings overrides the condition model with real geometry, "
                    "which is what 'UMa-Buildings' means."
    )
    shadowing_enabled: bool = Field(
        True,
        description="Enable log-normal large-scale shadowing "
                    "(ThreeGppPropagationLossModel::ShadowingEnabled). Real "
                    "engine lever. NOTE: the engine hardcoded this OFF until "
                    "2026-07-26, so runs recorded before then were "
                    "shadowing-free regardless of this setting."
    )
    # MRO (mobility robustness) lever: how much better (dB) a neighbour cell's
    # SINR must be before the RAN hands the UE over. Low = aggressive/ping-pong
    # handovers, high = sticky UEs that cling to a degrading cell.
    ho_sinr_margin_db: float = Field(
        3.0,
        description="Handover SINR margin in dB (1 = aggressive/ping-pong, "
                    "3 = balanced default, 10+ = sticky). Drives real handover "
                    "behaviour in the ns-O-RAN engine — the MRO test lever."
    )
    # ── 3GPP handover trigger controls (real ns-O-RAN LteEnbRrc levers) ──────────
    handover_mode: Literal["FixedTtt", "DynamicTtt", "Threshold", "NoAuto"] = Field(
        "DynamicTtt",
        description="Secondary-cell handover trigger mode (real engine lever, "
                    "LteEnbRrc::SecondaryCellHandoverMode): Threshold = hand over "
                    "as soon as a neighbour beats the serving cell by the SINR "
                    "margin; FixedTtt = require the condition to hold for "
                    "ho_ttt_ms first (anti-ping-pong / time-to-trigger); "
                    "DynamicTtt = TTT scales with UE speed; NoAuto = disable "
                    "automatic handover."
    )
    ho_ttt_ms: int = Field(
        110, ge=1, le=255,
        description="Time-to-trigger in ms for FixedTtt mode — how long the "
                    "handover condition must hold before the RAN commits. "
                    "Higher = fewer ping-pong handovers; 255 (the maximum) gave "
                    "one clean handover where 110 with DynamicTtt gave six. "
                    "CAPPED AT 255 BY THE ENGINE, not by us: LteEnbRrc's "
                    "FixedTttValue is declared MakeUintegerChecker<uint8_t>, so "
                    "3GPP's longer enumerated steps (480, 640, 1024 ms) cannot "
                    "be expressed — asking for 480 silently ran at 255 until "
                    "this bound was added."
    )
    # ── Phase 2b: 3GPP measurement-event handover layer ─────────────────────────
    # ns-O-RAN upstream disabled the RRC's periodic handover re-evaluation, so
    # automatic handover was decided once at t=0 and never again — which is why
    # no speed/margin/TTT setting could ever produce a motion-driven handover.
    ho_check_period_ms: int = Field(
        100, ge=0, le=60000,
        description="How often (ms) the RAN re-evaluates handover for each UE. "
                    "0 = evaluate only once at the start (the engine's inherited "
                    "behaviour — mobility can then never trigger a handover). "
                    "100 is a realistic RRC measurement cadence; lower costs "
                    "simulation speed. Real engine lever "
                    "(LteEnbRrc::HoCheckPeriodMs)."
    )
    ho_a3_entry: bool = Field(
        True,
        description="Require the 3GPP A3 entry condition — a neighbour must beat "
                    "the serving cell by ho_sinr_margin_db + ho_hysteresis_db — "
                    "before a handover is scheduled. False = any better "
                    "neighbour schedules one and only the TTT damps it "
                    "(LteEnbRrc::HoA3Entry)."
    )
    ho_hysteresis_db: float = Field(
        0.0, ge=0.0, le=30.0,
        description="A3 hysteresis in dB, added to ho_sinr_margin_db in the "
                    "entry condition. Raise it to suppress ping-pong between two "
                    "similarly strong cells (LteEnbRrc::HoHysteresisDb)."
    )
    ho_a2_threshold_db: Optional[float] = Field(
        None,
        description="A2 event: when the serving cell's SINR falls below this "
                    "(dB), hand over to the best neighbour immediately, "
                    "bypassing the time-to-trigger. Models 'the serving cell "
                    "collapsed, don't wait'. Must be above the -5 dB outage "
                    "floor (below it the UE falls back to LTE instead). "
                    "null = disabled (LteEnbRrc::HoA2ThresholdDb)."
    )
    ho_joining_timeout_ms: float = Field(
        45.0, ge=1.0, le=5000.0,
        description="TOO-LATE side of handover robustness: once the TARGET cell "
                    "has accepted a handover, how long it waits for the UE to "
                    "finish reconfiguring before it destroys the context and the "
                    "handover counts as failed. The window has to cover the X2 "
                    "ACK, the Handover Command and non-contention random access, "
                    "so shortening it below the transport delay turns working "
                    "handovers into failures — which is exactly the MRO fault "
                    "worth rehearsing (LteEnbRrc::HandoverJoiningTimeoutDuration)."
    )
    ho_leaving_timeout_ms: float = Field(
        45.0, ge=1.0, le=5000.0,
        description="TOO-EARLY side of the same trade-off: once the SOURCE cell "
                    "has issued the Handover Command, how long it holds the UE "
                    "context waiting for re-establishment or an X2 release. Too "
                    "short and a UE that would have come back is dropped; too "
                    "long and a dead context occupies the cell "
                    "(LteEnbRrc::HandoverLeavingTimeoutDuration)."
    )
    # ── RRC: measurement filtering, connection timers, signalling ──────────────
    # The rest of LteEnbRrc. The handover EVENTS were wired in 2b; these are the
    # knobs around them — how measurements are smoothed before an event is
    # evaluated, and how long the RRC waits at each step of setting a UE up.
    rsrp_filter_coefficient: int = Field(
        4, ge=0, le=19,
        description="L3 filter coefficient k for RSRP, per 3GPP TS 36.331: the "
                    "measurement is smoothed as a=1/2^(k/4), so 0 is no "
                    "filtering (every report is raw and handovers chase fast "
                    "fading) and large k is heavy smoothing (stable, but slow "
                    "to notice a real change). The companion to hysteresis and "
                    "time-to-trigger — those delay the DECISION, this smooths "
                    "the INPUT (LteEnbRrc::RsrpFilterCoefficient)."
    )
    rsrq_filter_coefficient: int = Field(
        4, ge=0, le=19,
        description="Same L3 filter for RSRQ (LteEnbRrc::RsrqFilterCoefficient)."
    )
    connection_request_timeout_ms: float = Field(
        15.0, ge=1.0, le=1000.0,
        description="After random access, how long the cell waits for RRC "
                    "CONNECTION REQUEST before dropping the UE context. Too "
                    "short under load and UEs fail to attach at all "
                    "(LteEnbRrc::ConnectionRequestTimeoutDuration)."
    )
    connection_setup_timeout_ms: float = Field(
        150.0, ge=1.0, le=5000.0,
        description="How long the cell waits for RRC CONNECTION SETUP COMPLETE "
                    "before giving up on the UE "
                    "(LteEnbRrc::ConnectionSetupTimeoutDuration)."
    )
    connection_rejected_timeout_ms: float = Field(
        30.0, ge=1.0, le=5000.0,
        description="How long the UE context survives after an RRC CONNECTION "
                    "REJECT (LteEnbRrc::ConnectionRejectedTimeoutDuration)."
    )
    srs_periodicity: Literal[0, 2, 5, 10, 20, 40, 80, 160, 320] = Field(
        320,
        description="Sounding Reference Signal periodicity in ms, per 3GPP TS "
                    "36.213 Table 8.2-1. NOT a free number — ns-3 looks the "
                    "value up in that table and only these are defined. Lower "
                    "means fresher uplink channel estimates but fewer SRS "
                    "resources, so it also caps how many UEs a cell can hold "
                    "(LteEnbRrc::SrsPeriodicity)."
    )
    system_information_periodicity_ms: float = Field(
        5.0, ge=1.0, le=1000.0,
        description="How often system information is broadcast "
                    "(LteEnbRrc::SystemInformationPeriodicity)."
    )
    default_transmission_mode: int = Field(
        0, ge=0, le=6,
        description="LTE transmission mode a UE starts in: 0 SISO, 1 MIMO "
                    "spatial multiplexing, 2 transmit diversity, and so on "
                    "(LteEnbRrc::DefaultTransmissionMode)."
    )
    # NOT EXPOSED: LteEnbRrc::AdmitRrcConnectionRequest. Setting it false is a
    # network-wide admission block, which is a scenario worth having — and it
    # kills the run. This scenario attaches every UE at t=0 with
    # AttachToClosestEnb, so refusing every connection leaves the attach
    # unfinishable and ns-3 dumps core during startup ("timeout: the monitored
    # command dumped core"). Measured, not assumed. A field the engine accepts
    # and then breaks on is worse than a missing one.
    admit_handover_request: bool = Field(
        True,
        description="Whether cells accept incoming handovers. Off strands UEs "
                    "on their serving cell however bad it gets, which is how "
                    "you tell a mobility problem from a coverage one. Also "
                    "network-wide (LteEnbRrc::AdmitHandoverRequest)."
    )
    report_all_ue_meas: bool = Field(
        True,
        description="Report measurements for every UE rather than only those "
                    "that trigger an event. Off cuts signalling volume and is "
                    "closer to a real deployment; on is what a test bench "
                    "usually wants (LteEnbRrc::ReportAllUeMeas)."
    )

    # ── RU sleep-state controller (network-wide side of the per-cell profiles) ──
    sleep_wake_load_pct: float = Field(
        60.0, ge=0.0, le=100.0,
        description="Wake-on-demand threshold: when the cells still serving UEs "
                    "are transmitting this share of the time, a sleeping cell is "
                    "brought back (paying its wake-up delay). Lower = wake "
                    "sooner and save less."
    )
    sleep_wake_hold_ms: float = Field(
        500.0, ge=0.0, le=60000.0,
        description="Anti-ping-pong: minimum time a just-woken cell stays up "
                    "before it may sleep again."
    )
    sleep_idle_duty_pct: float = Field(
        0.0, ge=0.0, le=100.0,
        description="Aggressive energy saving: also sleep cells that still have "
                    "UEs attached but transmit less than this share of the time "
                    "(their UEs are pushed to neighbours). 0 = only sleep cells "
                    "with no UEs."
    )
    sleep_power_a: Optional[Dict[str, float]] = Field(
        None,
        description="Override the per-depth RU draw in Amperes, e.g. "
                    "{'micro': 59.5, 'light': 43.2, 'deep': 28.5}. null = the "
                    "engine's built-in power model."
    )
    # ── Phase 2c: antenna element pattern ───────────────────────────────────────
    antenna_element: Literal["isotropic", "3gpp", "cosine",
                             "parabolic", "circular"] = Field(
        "isotropic",
        description="Radiating element of each cell's phased array — the choice "
                    "that decides whether orientation and beamwidth mean "
                    "anything at all.\n"
                    "• 'isotropic' (the engine's inherited default) has no "
                    "directionality, so the per-cell antenna.azimuth_deg / "
                    "tilt_deg CANNOT affect anything — rotating isotropic "
                    "elements cancels out exactly.\n"
                    "• '3gpp' is the TR 38.901 element (65° beamwidth, 30 dB "
                    "front-to-back). Panel orientation becomes real and "
                    "sectorisation meaningful, but the pattern itself is fixed "
                    "by the standard — there is nothing to tune.\n"
                    "• 'cosine' takes antenna.horizontal_beamwidth_deg, "
                    "vertical_beamwidth_deg and gain_dbi PER CELL. This is the "
                    "one to pick when beamwidth is the subject: narrow beams "
                    "for capacity, wide for coverage.\n"
                    "• 'parabolic' has a single beamwidth (the horizontal "
                    "figure) — a dish or a narrow point-to-point link.\n"
                    "• 'circular' is an aperture whose beamwidth FOLLOWS from "
                    "radius and frequency; horizontal_beamwidth_deg carries the "
                    "radius in centimetres there.\n"
                    "Elements differ in boresight gain, so results are not "
                    "comparable across settings — compare within one."
    )
    # ── Mobility robustness / cell selection (real LteEnbRrc attributes) ────────
    outage_threshold_db: float = Field(
        -5.0, ge=-30.0, le=10.0,
        description="Serving SINR (dB) below which a UE is declared in outage "
                    "and falls back to the LTE anchor instead of any mmWave "
                    "cell. Raise it for a network that gives up on a bad link "
                    "sooner; lower it to make UEs cling. Sets the floor that "
                    "ho_a2_threshold_db must sit above (LteEnbRrc::OutageThreshold)."
    )
    q_rx_lev_min_dbm: int = Field(
        -140, ge=-140, le=-44,
        description="Minimum RSRP a UE must receive from a cell before it may "
                    "camp on it — 3GPP TS 36.133 cell (re)selection. Raising it "
                    "shrinks each cell's usable footprint at initial access "
                    "(LteEnbRrc::QRxLevMin, in dBm)."
    )
    # DynamicTtt curve. handover_mode='DynamicTtt' is the default, and until now
    # only the FixedTtt value was exposed — so the mode the engine actually runs
    # had none of its parameters reachable.
    ho_dyn_ttt_min_ms: int = Field(
        25, ge=1, le=255,
        description="DynamicTtt: shortest time-to-trigger, used when a "
                    "neighbour is overwhelmingly better (LteEnbRrc::MinDynTttValue)."
    )
    ho_dyn_ttt_max_ms: int = Field(
        150, ge=1, le=255,
        description="DynamicTtt: longest time-to-trigger, used when a neighbour "
                    "is only marginally better (LteEnbRrc::MaxDynTttValue)."
    )
    ho_dyn_diff_min_db: float = Field(
        3.0, ge=0.0, le=30.0,
        description="DynamicTtt: SINR difference at which the TTT curve is at "
                    "its longest (LteEnbRrc::MinDiffValue)."
    )
    ho_dyn_diff_max_db: float = Field(
        20.0, ge=0.0, le=60.0,
        description="DynamicTtt: SINR difference at which the TTT curve bottoms "
                    "out at ho_dyn_ttt_min_ms (LteEnbRrc::MaxDiffValue)."
    )
    # ── RU power model (the rest of the VIAVI energy profile) ───────────────────
    ru_tx_current_a: Optional[float] = Field(
        None, gt=0.0, le=2000.0,
        description="RU current draw while transmitting, in Amperes. null = the "
                    "engine's built-in value. Together with ru_rx_current_a and "
                    "ru_idle_current_a this is the per-cell power profile the "
                    "sleep states step down from "
                    "(MmWaveRadioEnergyModelEnb::TxCurrentA)."
    )
    ru_rx_current_a: Optional[float] = Field(
        None, gt=0.0, le=2000.0,
        description="RU current draw while receiving, in Amperes. null = the "
                    "engine's built-in value (MmWaveRadioEnergyModelEnb::RxCurrentA)."
    )
    ru_idle_current_a: Optional[float] = Field(
        None, gt=0.0, le=2000.0,
        description="RU current draw when up but idle, in Amperes — the baseline "
                    "the micro/light/deep sleep depths reduce. null = the "
                    "engine's built-in value (MmWaveRadioEnergyModelEnb::DeepSleepA, "
                    "a legacy attribute name for active-idle)."
    )

    # ── Link-level error model ──────────────────────────────────────────────────
    error_model: Literal["lte_mi", "nr_cc_table1", "nr_cc_table2",
                         "nr_ir_table1", "nr_ir_table2"] = Field(
        "lte_mi",
        description="How the PHY decides whether a transport block decodes. "
                    "'lte_mi' is the engine's default and is the LTE "
                    "mutual-information model — an LTE model on a 5G engine. "
                    "The others are 3GPP NR EESM: 'cc' = Chase Combining "
                    "(a retransmission repeats the same bits), 'ir' = "
                    "Incremental Redundancy (it sends new parity, which is what "
                    "NR actually does and is strictly stronger); 'table1' caps "
                    "at 64QAM, 'table2' allows 256QAM. Drives BOTH "
                    "MmWaveSpectrumPhy::ErrorModelType and MmWaveAmc::"
                    "ErrorModelType — ns-3 requires the two to match, since AMC "
                    "and the receiver must share one MCS table."
    )
    # ── Transport network (backhaul) ────────────────────────────────────────────
    # The links between cells and the core. X2 carries handover signalling
    # between gNBs, so its delay is part of how long a handover really takes —
    # the Phase-2b work measured handovers over an effectively perfect backhaul.
    x2_link_delay_ms: float = Field(
        1.0, ge=0.0, le=200.0,
        description="One-way delay on the X2 link between base stations, in ms. "
                    "X2 carries handover signalling, so this is part of the real "
                    "handover latency budget — the engine's 1 ms default is a "
                    "co-located ideal (MmWavePointToPointEpcHelper::X2LinkDelay)."
    )
    x2_link_gbps: float = Field(
        100.0, gt=0.0, le=400.0,
        description="X2 link capacity in Gbps (…::X2LinkDataRate)."
    )
    s1u_link_delay_ms: float = Field(
        1.0, ge=0.0, le=200.0,
        description="One-way delay on the S1-U user-plane link to the core, in "
                    "ms. Adds directly to end-to-end latency for every packet "
                    "(…::S1uLinkDelay)."
    )
    s1u_link_gbps: float = Field(
        10.0, gt=0.0, le=400.0,
        description="S1-U link capacity in Gbps. Lower it to model a "
                    "backhaul-limited site, where the bottleneck is the "
                    "transport network rather than the radio (…::S1uLinkDataRate)."
    )
    s1u_link_mtu: int = Field(
        2000, ge=576, le=65535,
        description="S1-U MTU in bytes; below the payload size it forces "
                    "fragmentation (…::S1uLinkMtu)."
    )
    s1ap_link_delay_ms: float = Field(
        15.0, ge=0.0, le=500.0,
        description="One-way delay on S1-AP, the control-plane link to the core, "
                    "in ms. Paid during attach and NAS signalling rather than on "
                    "user data (…::S1apLinkDelay)."
    )
    s1ap_link_gbps: float = Field(
        10.0, gt=0.0, le=400.0,
        description="S1-AP control-plane link capacity in Gbps (…::S1apLinkDataRate)."
    )
    x2_link_mtu: int = Field(
        10000, ge=576, le=65535,
        description="X2 MTU in bytes — bounds how much handover context fits in "
                    "one packet between gNBs (…::X2LinkMtu)."
    )
    # ── PHY / scheduler detail ──────────────────────────────────────────────────
    gnb_transient_ns: int = Field(
        320000, ge=0, le=10000000,
        description="Transmitter transient (settling) time in ns before a gNB's "
                    "signal is usable (MmWaveEnbPhy::Transient)."
    )
    gnb_noise_and_filter: bool = Field(
        False,
        description="Apply the receive filter model on top of thermal noise at "
                    "the gNB (MmWaveEnbPhy::NoiseAndFilter)."
    )
    ue_mac_sinr_period_us: float = Field(
        1.6, gt=0.0, le=10000.0,
        description="How often the UE MAC refreshes its SINR estimate, in µs "
                    "(MmWaveUeMac::UpdateUeSinrEstimatePeriod)."
    )
    fixed_tti: bool = Field(
        False,
        description="Force a fixed TTI instead of the flexible one the "
                    "scheduler normally sizes per slot. Removes one source of "
                    "variability when you want a controlled experiment "
                    "(MmWaveFlexTtiMacScheduler::FixedTti)."
    )
    # ── Indoor/outdoor channel condition ────────────────────────────────────────
    # NO internal_wall_loss / shadow_sigma_* fields. Those are attributes of
    # ns3::BuildingsPropagationLossModel, which this scenario NEVER INSTANTIATES
    # — it uses the ThreeGpp*PropagationLossModel family, which computes its own
    # O2I loss internally. Measured 2026-07-26: 5 dB vs 20 dB of internal wall
    # loss produced byte-identical PRB. Same class as ICIC: real ns-3
    # attributes on a model that is not in this engine's path.
    o2i_threshold: float = Field(
        0.0, ge=0.0, le=1.0,
        description="Probability that a UE is treated as indoors for the "
                    "outdoor-to-indoor channel condition. 0 = everyone outdoors "
                    "(the engine's default), 1 = everyone indoors "
                    "(ThreeGppChannelConditionModel::O2iThreshold)."
    )
    o2i_low_loss_threshold: float = Field(
        1.0, ge=0.0, le=1.0,
        description="Of the indoor UEs, the share that get the LOW-loss "
                    "building model rather than the high-loss one. 1 = all "
                    "low-loss; lower it to model older, lossier buildings "
                    "(…::O2iLowLossThreshold)."
    )
    # ── PHY/MAC timing budget ───────────────────────────────────────────────────
    harq_dl_timeout: int = Field(
        20, ge=1, le=200,
        description="How many HARQ processes' worth of time a downlink "
                    "retransmission may wait before being abandoned "
                    "(MmWavePhyMacCommon::HarqDlTimeout)."
    )
    tb_decode_latency_us: int = Field(
        100, ge=1, le=10000,
        description="Time the PHY needs to decode a transport block, in µs — "
                    "part of the end-to-end latency floor "
                    "(MmWavePhyMacCommon::TbDecodeLatency)."
    )
    # NO ul_sched_delay_slots field. MmWavePhyMacCommon::UlSchedDelay=3 crashes
    # the engine (measured 2026-07-26, isolated from the other two timing knobs
    # which are fine). NSG_UL_SCHED_DELAY still reaches the engine API.
    # ── Beamforming solver cost ─────────────────────────────────────────────────
    svd_max_iterations: int = Field(
        30, ge=1, le=1000,
        description="Iteration cap for the SVD beamforming solver. Fewer means "
                    "a rougher beam and a faster simulation; only relevant when "
                    "beamforming_model is 'svd' (MmWaveSvdBeamforming::MaxIterations)."
    )
    svd_tolerance: float = Field(
        1e-8, gt=0.0, le=1.0,
        description="Convergence tolerance for the SVD beamforming solver "
                    "(…::Tolerance)."
    )
    svd_use_cache: bool = Field(
        True,
        description="Reuse a computed beamforming vector until the channel "
                    "changes. Disabling it recomputes every time — more exact, "
                    "much slower (…::UseCache)."
    )

    # ── Beamforming, control plane, and what KPM the engine collects ────────────
    beamforming_model: Literal["svd", "dft", "codebook"] = Field(
        "svd",
        description="How the beam is computed. 'svd' is the engine default and "
                    "an OPTIMISTIC upper bound — it solves for the best possible "
                    "beam from perfect channel knowledge. 'dft' steers from a "
                    "DFT grid, 'codebook' is restricted to a finite codebook, "
                    "which is what real hardware does. Move off svd when you "
                    "want a beam a product could actually form "
                    "(MmWaveHelper::BeamformingModel)."
    )
    use_ideal_rrc: bool = Field(
        True,
        description="Skip real RRC signalling and its delays. True (the "
                    "engine's hardcoded behaviour until now) means attach and "
                    "handover complete instantly and the control plane costs "
                    "nothing — convenient, and optimistic about how fast a "
                    "handover really is (MmWaveHelper::UseIdealRrc)."
    )
    use_pdsch_for_cqi: bool = Field(
        False,
        description="Derive CQI from the data channel (PDSCH) instead of a "
                    "reference signal — closer to how a real UE measures on a "
                    "loaded cell (MmWaveHelper::UsePdschForCqiGeneration)."
    )
    rrc_t300_ms: int = Field(
        100, ge=10, le=2000,
        description="3GPP T300: how long a UE waits for its RRC connection "
                    "setup before declaring the attempt failed (LteUeRrc::T300)."
    )
    # Which KPM families the engine collects. These decide the MEASUREMENT
    # surface, so switching one off removes metrics from every downstream view.
    kpm_report_du: bool = Field(
        True,
        description="Collect DU-level KPM — PRB usage, per-UE throughput, "
                    "serving SINR. This is what the throughput and PRB views "
                    "read; turning it off blanks them."
    )
    kpm_report_cu_up: bool = Field(
        True,
        description="Collect CU-UP KPM — PDCP volume, bitrate and delay."
    )
    kpm_report_cu_cp: bool = Field(
        True,
        description="Collect CU-CP KPM — connected-UE counts and control-plane "
                    "state."
    )
    # NO kpm_reduced_values field. reducedPmValues=true fails to ASN.1-encode
    # the indication ("Error during the encoding of the RIC Indication Message",
    # kpm-indication.cc:459) and aborts the run at t=0.13 s — measured
    # 2026-07-26. The run scripts always passed 0, so the path was never
    # exercised upstream. Same rule as the dead schedulers and the data error
    # model: a config must not be able to produce a crashed run. NSG_KPM_REDUCED
    # still reaches the engine API for research.

    # ── Channel realism: blockage, O2I penetration, channel ageing ──────────────
    # Blockage is the defining mmWave impairment — a hand or a passing body costs
    # tens of dB at 28 GHz — and none of it was reachable.
    blockage_enabled: bool = Field(
        False,
        description="Enable the 3GPP TR 38.901 blockage model: bodies and "
                    "objects that intermittently cut the beam. This is the "
                    "characteristic mmWave impairment and it is OFF by default "
                    "in the engine, so a run without it is optimistic about "
                    "link stability (ThreeGppChannelModel::Blockage)."
    )
    blocker_speed_mps: float = Field(
        1.0, gt=0.0, le=30.0,
        description="Speed of the moving blockers, in m/s. Faster blockers mean "
                    "shorter, more frequent outages "
                    "(ThreeGppChannelModel::BlockerSpeed)."
    )
    non_self_blocking_count: int = Field(
        4, ge=0, le=20,
        description="How many external blockers (people, vehicles) can obstruct "
                    "a link. 0 leaves only self-blocking "
                    "(ThreeGppChannelModel::NumNonselfBlocking)."
    )
    portrait_mode: bool = Field(
        True,
        description="Self-blocking geometry: true models a handset held upright "
                    "in front of the user, false a landscape grip. Changes which "
                    "angles the user's own body shadows "
                    "(ThreeGppChannelModel::PortraitMode)."
    )
    scatterer_speed_mps: float = Field(
        0.0, ge=0.0, le=50.0,
        description="Speed of environmental scatterers, in m/s — Doppler spread "
                    "from a moving environment even when the UE is still "
                    "(ThreeGppChannelModel::vScatt)."
    )
    building_penetration_loss: bool = Field(
        True,
        description="Apply outdoor-to-indoor penetration loss when a link "
                    "crosses a building wall. Pairs with the buildings you draw "
                    "(ThreeGppPropagationLossModel::BuildingPenetrationLossesEnabled)."
    )
    channel_update_ms: float = Field(
        100.0, ge=1.0, le=10000.0,
        description="How often the channel matrix is regenerated, in ms. "
                    "Shorter tracks motion and Doppler more closely and costs "
                    "simulation time; longer lets a UE travel a long way on a "
                    "stale channel (ThreeGppChannelModel::UpdatePeriod)."
    )
    # ── RLC: buffering and retransmission ───────────────────────────────────────
    rlc_am_enabled: bool = Field(
        False,
        description="Use RLC Acknowledged Mode — per-PDU retransmission with "
                    "status reporting — instead of Unacknowledged Mode. AM "
                    "trades latency for reliability; UM is the engine's default "
                    "and what a latency-sensitive slice wants "
                    "(MmWaveHelper::RlcAmEnabled)."
    )
    rlc_buffer_mb: int = Field(
        10, ge=1, le=1000,
        description="RLC transmit buffer per bearer, in MB. Too small and bursts "
                    "are dropped at the RLC rather than carried; very large "
                    "buffers instead show up as bufferbloat latency."
    )
    rlc_reordering_timer_ms: float = Field(
        1.0, gt=0.0, le=1000.0,
        description="How long RLC waits for a missing PDU before giving up on "
                    "reordering (LteRlcAm/LteRlcUm::ReorderingTimer)."
    )
    rlc_poll_retransmit_ms: float = Field(
        2.0, gt=0.0, le=1000.0,
        description="RLC AM only: how long to wait for a status report before "
                    "retransmitting (LteRlcAm::PollRetransmitTimer)."
    )
    rlc_status_prohibit_ms: float = Field(
        1.0, gt=0.0, le=1000.0,
        description="RLC AM only: minimum gap between status reports, which "
                    "bounds feedback overhead (LteRlcAm::StatusProhibitTimer)."
    )

    # ── PHY / MAC on the mmWave DATA PATH ───────────────────────────────────────
    # Deliberately the mmWave classes, not their LTE namesakes: in this engine
    # every UE is dual-connected and ALL user traffic rides the mmWave gNBs, so
    # LteUePhy/LteEnbPhy knobs would only touch the idle LTE anchor. Same reason
    # there is no ICIC/FFR field — the fork disables FFR on the mmWave path
    # outright ("not used in mmwave" in mmwave-helper.cc), leaving it live only
    # on the single anchor cell that carries no data.
    ue_noise_figure_db: float = Field(
        5.0, ge=0.0, le=20.0,
        description="UE receiver noise figure in dB — device quality. Higher "
                    "means a worse receiver and less downlink throughput at the "
                    "same SINR (MmWaveUePhy::NoiseFigure)."
    )
    gnb_noise_figure_db: float = Field(
        5.0, ge=0.0, le=20.0,
        description="gNB receiver noise figure in dB. Affects the UPLINK, where "
                    "the base station is the receiver (MmWaveEnbPhy::NoiseFigure)."
    )
    ue_tx_power_dbm: float = Field(
        30.0, ge=-20.0, le=40.0,
        description="UE transmit power in dBm — the uplink budget. Handsets are "
                    "typically 23 dBm; the engine's default of 30 is generous "
                    "(MmWaveUePhy::TxPower)."
    )
    ue_outage_threshold_db: float = Field(
        -5.0, ge=-30.0, le=10.0,
        description="SINR below which the UE's own PHY declares the link out. "
                    "Pairs with settings.outage_threshold_db, which is the "
                    "network-side view (MmWaveUePhy::OutageThreshold)."
    )
    ue_n310: int = Field(
        2, ge=1, le=20,
        description="Consecutive out-of-sync indications before the UE declares "
                    "radio link failure — 3GPP N310. Lower = a UE that gives up "
                    "on a flapping link sooner (MmWaveUePhy::n310)."
    )
    cqi_report_period_ms: int = Field(
        10, ge=1, le=100,
        description="How often the UE reports channel quality. Slower feedback "
                    "means the scheduler acts on staler channel information, "
                    "which costs throughput on a moving UE "
                    "(MmWaveUePhy::CqiReportPeriod)."
    )
    cqi_timer_threshold_ms: int = Field(
        100, ge=1, le=1000,
        description="How long a CQI report stays usable before the scheduler "
                    "discards it as stale "
                    "(MmWaveFlexTtiMacScheduler::CqiTimerThreshold)."
    )
    sinr_estimate_period_us: int = Field(
        1600, ge=100, le=100000,
        description="How often the gNB re-estimates SINR. This is the input the "
                    "handover evaluation loop reads, so it bounds how fresh a "
                    "mobility decision can be — pair it with ho_check_period_ms "
                    "(MmWaveEnbPhy::UpdateSinrEstimatePeriod)."
    )
    ue_sinr_estimate_period_us: float = Field(
        25.6, ge=1.0, le=10000.0,
        description="How often the gNB REPORTS its per-UE SINR estimate down to "
                    "the UE PHY. The name says µs and that is wrong: the engine "
                    "attribute is documented in MILLISECONDS, so the 25.6 "
                    "default is 25.6 ms and a value of 1000 asks for one second, "
                    "not one millisecond. The field name is kept so saved "
                    "scenarios still load.\n"
                    "\n"
                    "It does NOT control how often the expensive estimate is "
                    "COMPUTED — that is sinr_estimate_period_us, which is the "
                    "period the loop reschedules itself on. Upstream gates the "
                    "report on `m_ueUpdateSinrPeriod / m_updateSinrPeriod`, "
                    "dividing a millisecond value by a microsecond one, so with "
                    "the defaults the gate evaluates 0.016 and never holds — the "
                    "report fires every round regardless. Changing this moved "
                    "neither speed nor results in measurement "
                    "(MmWaveEnbPhy::UpdateUeSinrEstimatePeriod)."
    )
    fixed_mcs_dl: Optional[int] = Field(
        None, ge=0, le=28,
        description="Pin the downlink MCS to this index instead of letting AMC "
                    "choose. Removes link adaptation from the experiment, which "
                    "is how you isolate everything else. null = AMC decides "
                    "(MmWaveFlexTtiMacScheduler::FixedMcsDl + McsDefaultDl)."
    )
    fixed_mcs_ul: Optional[int] = Field(
        None, ge=0, le=28,
        description="Pin the uplink MCS. null = AMC decides "
                    "(MmWaveFlexTtiMacScheduler::FixedMcsUl + McsDefaultUl)."
    )
    sched_direction: Literal["both", "dl_only", "ul_only"] = Field(
        "both",
        description="Restrict the scheduler to one direction. This engine's MAC "
                    "is flexible-TTI — it derives the DL/UL symbol split from "
                    "demand every slot rather than from a configured TDD frame "
                    "pattern — so this is the real DL/UL control it offers, and "
                    "the honest stand-in for a frame-format field "
                    "(MmWaveFlexTtiMacScheduler::DlSchedOnly / UlSchedOnly).\n"
                    "\n"
                    "This USED to double as the workaround for the PHY abort "
                    "(NS_ASSERT Sum((*rxPsd) * (*m_rxSignal)) == 0.0, "
                    "mmwave-interference.cc:108) that ended 2-cell runs early. "
                    "It is not needed for that any more: the abort was a "
                    "random-access defect — two UEs could accept the same RAR "
                    "and share one RNTI, then answer the same uplink grant — "
                    "fixed in the engine on 2026-09-12 "
                    "(engines/nsoran/rar-rnti-hijack-guard.patch, image 1.9). "
                    "Choose this field for what you want to MEASURE, not to "
                    "dodge a crash. What 'dl_only' costs is uplink DATA "
                    "scheduling, not all uplink visibility — PDCP-level UL "
                    "metrics (DRB.PdcpSduDelayUl / VolumeUl / BitRateUl) still "
                    "report."
    )
    sym_per_slot: int = Field(
        6, ge=1, le=14,
        description="Symbols per scheduled TTI — the scheduler's allocation "
                    "granularity (MmWaveFlexTtiMacScheduler::SymPerSlot)."
    )
    ra_preambles: int = Field(
        50, ge=1, le=64,
        description="Random-access preambles available per cell. Too few and "
                    "simultaneous joins collide (MmWaveEnbMac::NumberOfRaPreambles)."
    )
    ra_preamble_trans_max: int = Field(
        50, ge=1, le=200,
        description="Maximum preamble transmission attempts before a UE gives up "
                    "on random access (MmWaveEnbMac::PreambleTransMax)."
    )
    ra_response_window: int = Field(
        3, ge=1, le=20,
        description="Random-access response window, in subframes "
                    "(MmWaveEnbMac::RaResponseWindowSize)."
    )
    # NO data_error_model field. MmWaveSpectrumPhy::DataErrorModelEnabled=false
    # null-dereferences at t=0.1 s (measured 2026-07-26) — something downstream
    # dereferences the error model unconditionally. Exposing it would let a
    # config produce a crashed run, the same reason there is no scheduler field.
    # The engine API still accepts NSG_DATA_ERR for research.

    # ── Phase 2c: radio stack choices ───────────────────────────────────────────
    # Real ns-3 attributes on MmWaveHelper / MmWaveAmc / MmWavePhyMacCommon.
    # There is deliberately NO TDD frame-format field: this engine's scheduler is
    # flexible-TTI, so the DL/UL symbol split is derived from demand every slot
    # rather than configured. See engines/nsoran/radio-2c.patch.
    # NO scheduler field. The fork ships three alternatives to
    # MmWaveFlexTtiMacScheduler (Pf / MaxRate / MaxWeight) and MEASURED
    # 2026-07-26 they are dead code: Pf and MaxRate run clean, report E2 and
    # allocate NOTHING (0 PRB, 0 throughput), MaxWeight aborts ns-3. Exposing
    # the choice would let a config produce a healthy-looking zero-throughput
    # run. The engine API still accepts NSG_SCHEDULER for research use — see
    # engines/nsoran/radio-2c.patch.
    amc_model: Literal["error_model", "shannon"] = Field(
        "error_model",
        description="How the MCS is chosen. 'error_model' (default) picks the "
                    "highest MCS meeting a target BLER from real 3GPP MCS "
                    "tables; 'shannon' uses a capacity bound at amc_target_ber "
                    "— optimistic, and useful for an upper-bound run "
                    "(MmWaveAmc::AmcModel)."
    )
    amc_target_ber: float = Field(
        0.00001, gt=0.0, le=0.5,
        description="Target BER the Shannon AMC model designs for. Only used "
                    "when amc_model is 'shannon' (MmWaveAmc::Ber)."
    )
    harq_enabled: bool = Field(
        True,
        description="Enable HARQ retransmissions. Turning it off isolates how "
                    "much throughput the retransmission machinery is actually "
                    "buying (MmWaveHelper/MmWaveFlexTtiMacScheduler::HarqEnabled)."
    )
    harq_processes: int = Field(
        100, ge=1, le=200,
        description="Concurrent stop-and-wait HARQ processes per UE. The engine "
                    "hardcoded 100; fewer means a UE stalls waiting for an ACK "
                    "before it may transmit again (MmWavePhyMacCommon::"
                    "NumHarqProcess)."
    )
    random_seed: Optional[int] = Field(
        None,
        description="Fix an integer seed for fully reproducible runs — drives "
                    "the engine's RNG run number, so mobility, positions and "
                    "channel all repeat. null = the engine's default run 1. "
                    "Vary it to generate a multi-seed dataset from one config."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SCHEMA UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def _strip_numeric_constraints(schema: dict) -> dict:
    """
    Recursively remove JSON Schema numeric constraint keywords that Anthropic's
    structured output API does not support.

    Unsupported (per API error): exclusiveMinimum, exclusiveMaximum, maximum.
    We also strip minimum to keep the schema symmetric and avoid future surprises.
    Pydantic still enforces all constraints at Python validation time; the LLM
    reads the 'description' fields to understand acceptable value ranges.
    """
    _REMOVE = frozenset({"exclusiveMinimum", "exclusiveMaximum", "minimum", "maximum"})

    def _clean(obj: object) -> object:
        if isinstance(obj, dict):
            return {k: _clean(v) for k, v in obj.items() if k not in _REMOVE}
        if isinstance(obj, list):
            return [_clean(x) for x in obj]
        return obj

    return _clean(schema)  # type: ignore[return-value]


# The parameters that decide what a run MEANS, out of the ~600 the schema
# defines. Everything else is a refinement; these are the ones where taking the
# default silently turns a scenario into a different scenario — the coverage
# footprint (power/frequency/bandwidth/antenna), the demand (UE count, speed,
# slice, throughput), the environment (propagation scenario, walls, fading) and
# the run itself (duration, seed, resolution).
#
# The config UI reads this to flag them: an AI-generated scenario fills in a
# default for every parameter the prompt never mentioned, and a user who cannot
# see WHICH values were assumed has no way to know what to correct. Names are
# leaf field names — they are unique enough across this schema, and matching by
# name keeps the list one flat thing to curate instead of a path grammar.
KEY_PARAMETERS: frozenset[str] = frozenset({
    # Radio / coverage
    "tx_power_dbm", "frequency_mhz", "bandwidth_mhz", "numerology",
    "azimuth_deg", "tilt_deg", "gain_dbi", "rows", "cols",
    # Demand
    "count", "speed_mps", "pattern", "slice_type", "qos_id",
    "target_throughput_gbps",
    # Environment
    "scenario", "wall_material", "building_type", "shadowing_enabled",
    "blockage_enabled",
    # Run control. kpm_collect_s is here because it is the ONLY lever that buys
    # a batch run more SIMULATED time, and a scenario that turns on elapsed time
    # is unbuildable without it.
    "duration_s", "resolution_m", "random_seed", "kpm_collect_s",
    # Energy / mobility levers that are the whole point of the scenarios that
    # use them, and are "none"/off by default.
    "mode", "ho_sinr_margin_db", "handover_mode",
})


def _mark_key_parameters(schema: dict) -> dict:
    """Stamp `x_key: true` on every property named in KEY_PARAMETERS.

    Ours, not JSON Schema's — the `x_` prefix marks the same kind of UI hint as
    x_enum_labels / x_options_from. Applied to the generated schema rather than
    to each Field(...) so the list stays readable as a list: it is a judgement
    about which parameters matter, and that judgement is easier to review in one
    place than scattered across 600 field definitions.
    """
    def _walk(obj: object) -> None:
        if isinstance(obj, dict):
            props = obj.get("properties")
            if isinstance(props, dict):
                for name, node in props.items():
                    if name in KEY_PARAMETERS and isinstance(node, dict):
                        node["x_key"] = True
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for x in obj:
                _walk(x)

    _walk(schema)
    return schema


# Fields that are real on the fast analytical tier and DO NOT reach the real
# ns-O-RAN engine — each for a reason the engine imposes, not an oversight.
#
# The distinction matters more now that the editor tells the user which
# parameters decide the outcome: five of these are KEY_PARAMETERS, so without
# this list the UI says "this matters, you should set it" about a value the real
# engine will ignore. That is a worse lie than saying nothing.
#
# Paths, not leaf names — `type` alone would also hit Anomaly.type, and
# `resolution_m` needs to be scoped to `area`. `[]` descends into an array's
# items. This is the single source of truth: tools/param_audit.py imports it to
# separate "tier-scoped by decision" from INERT, which is the bug class.
# ── WHICH TIER READS A FIELD, MEASURED ──────────────────────────────────────
# `tools/tier_scoped_audit.py` perturbs each of these and diffs what the FAST
# TIER produces and what the COVERAGE MAP produces. That is a different
# question from `param_audit`, which diffs the ENGINE PAYLOAD: a value can
# reach a dict nobody consumes and look perfectly wired.
#
# THE LABEL USED TO SAY "analytical" AND THERE IS NO ANALYTICAL TIER ANY MORE.
# It meant the Shannon sandbox behind the RIC console, which is gone — the
# console runs the fast tier and the coverage map is the only analytical
# thing left. So each entry now says WHICH consumer reads it, and five fields
# that the audit found nothing reads at all were deleted rather than relabelled:
# settings.fast_fading_enabled, massive_mimo.beamforming,
# massive_mimo.digital_azimuth_deg and digital_tilt_deg.
TIER_SCOPED: dict[str, str] = {
    # ── the two the fast tier structurally cannot express ────────────────
    # Everything else in its neighbourhood IS read by both tiers now — the
    # A3 offset, hysteresis, A2 and qRxLevMin all bind on the fast tier —
    # so a user editing this group needs to be told which two do not, and
    # why it is the mechanism and not an omission.
    "settings.ho_ttt_ms":
        "SLOW TIER only — and the reason changed once the fast tier began "
        "re-drawing each link's LOS/NLOS condition every 100 ms. The "
        "interval DOES exist now, so time-to-trigger is expressible as a "
        "number of condition epochs, and a UE's best cell really does "
        "flip on that grid. Counting those flips as handovers was "
        "measured and overshoots: 1743 over 20 windows raw against the "
        "engine's single digits, 137 with time-to-trigger, and 493 with "
        "the L3 filter on top — the filter makes it WORSE, because two "
        "filtered levels converge and cross more often than two raw ones. "
        "The between-window count undershoots by 2-6x, which is the "
        "better error until the engine's handover decisions can be traced. "
        "The LEVEL conditions beside it (ho_sinr_margin_db, "
        "ho_hysteresis_db, ho_a3_entry, ho_a2_threshold_db, "
        "q_rx_lev_min_dbm) all reach both tiers.",
    "slices[].min_pct":
        "FAST TIER reads it — asserted in tools/test_fast_tier.py, where a "
        "60% floor moves the protected class 444 -> 451 Mbps and trims the "
        "rest 383 -> 377. The consumer chip beside this may still say slow: "
        "a floor is WORK-CONSERVING, so it does nothing until a cell is "
        "congested, and the audit's probe scene cannot congest one enough. "
        "The engine behaves identically — its trim pass is guarded on the "
        "unprotected demand exceeding what is left.",
    "slices[].dedicated_pct":
        "FAST TIER reads it. Unlike min_pct it is held whether or not there "
        "is traffic to use it, and the two OVERLAP rather than stack — a "
        "20% dedicated slice with a 30% floor is guaranteed 30%, not 50%. "
        "Same probe limitation as slices[].min_pct.",
    "settings.amc_model":
        "SLOW TIER only, measured — and not for want of the mechanism. The "
        "engine's own AMC and error model ARE ported (nsg_ai/mi_tables.py, "
        "5,800 numbers extracted from its source) and switchable, but Tier "
        "B's own MCS rule was CALIBRATED against an engine already running "
        "AmcModel=ErrorModel, so both of its paths target the same engine "
        "setting and neither is the other. Switching to the literal port "
        "moves the mean |ratio-1| across six breadth cases from 0.452 to "
        "0.923. `amc_target_ber` is wired and gated on this field, which is "
        "what the field's own description says it should be.",
    "settings.error_model":
        "SLOW TIER only. Which 3GPP decode curve the PHY uses — see "
        "settings.amc_model for why Tier B cannot select between them.",
    "settings.channel_update_ms":
        "SLOW TIER only, measured. It is `ThreeGppChannelModel::UpdatePeriod`, "
        "the SMALL-SCALE channel redraw — not shadowing, which decorrelates "
        "over distance as a UE moves. Tier B integrates fast fading over the "
        "whole window statistically, and the shipped 100 ms is a tenth of a "
        "window, so there is nothing left for the period to change. Wiring "
        "it to the shadowing draw instead was tried and reverted: it is the "
        "wrong quantity and it reshuffled every stored realisation.",
    "settings.s1u_link_delay_ms":
        "SLOW TIER only. `s1u_link_gbps` IS read here — it caps what a cell "
        "can deliver in a window — but a delay is a latency and this tier "
        "reports volumes over a window, not the time any byte took. Same "
        "for the X2 and S1AP delays and every link MTU.",
    "settings.sinr_estimate_period_us":
        "SLOW TIER only, structurally. Microsecond periods inside a window "
        "the tier computes in one step. The whole family is out for this "
        "reason: ue_sinr_estimate_period_us, ue_mac_sinr_period_us, "
        "tb_decode_latency_us, gnb_transient_ns.",
    "settings.rrc_t300_ms":
        "SLOW TIER only, structurally. Tier B has no attach procedure — a "
        "UE is attached or it is not. The whole family is out for this "
        "reason: the RA parameters, the connection timeouts, ue_n310 and "
        "system_information_periodicity_ms.",
    "settings.svd_use_cache":
        "SLOW TIER only. An implementation detail of the engine's SVD "
        "beamforming solver, not a property of the radio. Tier B applies "
        "the panel's coherent gain in closed form and has no solver to "
        "tune — same for svd_tolerance, svd_max_iterations and "
        "beamforming_model.",
    "settings.harq_enabled":
        "SLOW TIER only, structurally. The fast tier's block error rate is "
        "what is left AFTER retransmission — it was calibrated against an "
        "engine running HARQ — so there is no pre-HARQ figure here to turn "
        "retransmission off against. Modelling the switch would mean "
        "modelling the per-TTI retransmission it exists to disable.",
    "ue_groups[].mobility.pattern":
        "FAST TIER reads it (measured): it sets the traffic activity factor "
        "in the RIC console's RAN. The real engine moves UEs by speed_mps and "
        "waypoints and ignores the pattern.",
    "ue_groups[].services[].slice_type":
        "FAST TIER reads it (measured): it labels the per-slice KPM rows. The "
        "engine is driven by qos_id and rate, so the engine payload does not "
        "carry it.",
    "base_stations[].radio.antenna.type":
        "A LABEL, not a lever. Measured, changing it moves nothing — the array "
        "element is network-wide (settings.antenna_element) and the panel's "
        "rows/cols are read whatever this says. It is kept because it selects "
        "whether a massive_mimo block exists at all, and rows/cols ARE read by "
        "both tiers.",
    "base_stations[].radio.antenna.gain_dbi":
        "COVERAGE MAP only (measured). The 3GPP element compiles its own "
        "boresight gain in; both tiers use that and ignore this.",
    "base_stations[].radio.antenna.horizontal_beamwidth_deg":
        "COVERAGE MAP only (measured). The 3GPP element pattern compiles 65 "
        "degrees in and exposes no knob.",
    "base_stations[].radio.antenna.vertical_beamwidth_deg":
        "COVERAGE MAP only (measured). Same compiled-in pattern as the "
        "horizontal beamwidth.",
    "area.x_min": "COVERAGE MAP only (measured). The area box frames the REM "
                  "grid; both tiers place nodes from their own coordinates.",
    "area.x_max": "See area.x_min.",
    "area.y_min": "See area.x_min.",
    "area.y_max": "See area.x_min.",
    "area.resolution_m":
        "COVERAGE MAP only (measured). It is the REM grid step and nothing "
        "else reads it.",
    "settings.scenario":
        "PARTLY on the fast tier (measured). UMa and UMi are both implemented "
        "there — UMa-Buildings maps to UMa — but RMa is NOT: it falls back to "
        "UMi, so a rural config gets an urban street canyon with no warning. "
        "The slow tier passes all three to ns-3 as --propScenario.",
    "settings.duration_s":
        "SLOW TIER only (measured). It is how long ns-3 runs. The fast tier's "
        "horizon is the number of windows asked for, and a live session runs "
        "until it is stopped.",
    "settings.kpm_collect_s":
        "SLOW TIER only (measured). It is the engine's own KPM collection "
        "window — the lever that trades wall clock for simulated horizon. The "
        "fast tier computes a whole window in one step and has no such trade.",
}


def _resolve_path(schema: dict, path: str) -> dict | None:
    """Walk a dotted path (with `[]` for array items) to its schema node."""
    def _deref(node: dict) -> dict:
        # Optional fields arrive as anyOf: [realType, {type: 'null'}] — follow
        # the real branch, or a nullable sub-object (massive_mimo) is a dead end.
        for branch in node.get("anyOf", []):
            if branch.get("type") != "null":
                node = branch
                break
        ref = node.get("$ref")
        if ref:
            return schema.get("$defs", {}).get(ref.rsplit("/", 1)[-1], {})
        return node

    node = schema
    for part in path.split("."):
        into_items = part.endswith("[]")
        key = part[:-2] if into_items else part
        node = (_deref(node).get("properties") or {}).get(key)
        if node is None:
            return None
        if into_items:
            node = _deref(node).get("items") or {}
    return node


def _mark_tier_scoped(schema: dict) -> dict:
    """Stamp `x_tier`/`x_tier_reason` on the fields TIER_SCOPED names.

    Marks the shared $defs node, so a field reached through several parents is
    labelled everywhere it appears — which is what the user needs, since the
    limitation belongs to the field and not to the route taken to it.
    """
    # WHICH CONSUMERS READ EACH FIELD, from the measurement rather than from
    # anyone's belief. `tools/param_audit.py --consumers` perturbs every
    # field and diffs all three consumers; this stamps the result so the
    # editor can say "the slow tier reads this and the fast tier does not"
    # on the 119 fields where that is true, instead of only on the dozen
    # someone remembered to list. Absent file, absent stamp — a stale
    # measurement is worse than none, so nothing is guessed here.
    try:
        import json as _json
        import os as _os
        _f = _os.path.join(_os.path.dirname(_os.path.dirname(
            _os.path.abspath(__file__))), "tools", "param_consumers.json")
        _reads = _json.load(open(_f))["fields"]
    except Exception:                                        # noqa: BLE001
        _reads = {}
    for path, who in _reads.items():
        node = _resolve_path(schema, path)
        if node is not None:
            node["x_reads"] = who

    for path, why in TIER_SCOPED.items():
        node = _resolve_path(schema, path)
        if node is not None:
            # The badge says WHICH consumer, not "analytical" — that tier
            # no longer exists and the word told a reader nothing about
            # whether their change would do anything.
            node["x_tier"] = ("fast" if why.startswith("FAST TIER")
                              else "partial" if why.startswith("PARTLY")
                              else "slow" if why.startswith("SLOW TIER")
                              else "label" if why.startswith("A LABEL")
                              else "coverage")
            node["x_tier_reason"] = why
    return schema


# ═══════════════════════════════════════════════════════════════════════════════
# ROOT MODEL
# ═══════════════════════════════════════════════════════════════════════════════

class AnomalyType(str, Enum):
    """What goes wrong. Only faults the engine can actually stage are here.

    Not offered: a PCI conflict. In this engine the cell id IS the RRC's map
    key, so two cells sharing one collide in the simulator's own bookkeeping
    rather than reproducing the real symptom — a UE that cannot tell two cells
    apart on the air. Staging it would produce a crash that looks like a finding.

    Also not offered: an RRC connection reject. Every UE attaches at t=0 and
    never re-attaches, so refusing new connections mid-run would be a switch
    with nothing behind it.
    """
    RADIO_DEGRADATION = "radio_degradation"  # cell drops to a lower TX power
    CELL_OUTAGE       = "cell_outage"        # cell goes off the air entirely
    NOISE_RISE        = "noise_rise"         # cell's receiver noise figure rises
    HANDOVER_BLOCK    = "handover_block"     # the network refuses handovers


class Anomaly(BaseModel):
    """One fault injected into the RUNNING simulation.

    Injected, not annotated: the cell's transmit power really changes mid-run,
    so the UEs on it really lose SINR, really re-select, and the KPM the RIC
    sees really degrades. Anything less would be editing the results.
    """
    type: AnomalyType = Field(
        ...,
        description=(
            "Which fault to stage.\n"
            "radio_degradation — the cell drops to a lower transmit power: "
            "coverage shrinks, RSRP falls with it.\n"
            "cell_outage — the cell goes off the air entirely.\n"
            "noise_rise — the cell keeps transmitting at full power but its "
            "receiver noise figure rises, so RSRP is untouched and only SINR / "
            "link quality degrades. This is what interference and a failing LNA "
            "look like, and no power drop can express it.\n"
            "handover_block — the network refuses handover admission. Nothing "
            "about the radio changes; what breaks is the RIC's ability to move "
            "a UE, so it is the fault that tests a control loop FAILING."
        ),
    )
    cell_id: str = Field(
        ...,
        # Tell the editor to offer the ids that actually exist in this config.
        # A typed id that matches no cell is DROPPED by the translator — for
        # good reason, since breaking a different cell than the one asked for
        # is worse than breaking none — but silently, so the scenario runs
        # healthy and the fault the user configured simply never happens.
        json_schema_extra={"x_options_from": "base_stations[].id"},
        description="id of the base station that fails, matching "
                    "base_stations[].id. Ignored for 'handover_block': one LTE "
                    "anchor admits handovers for every cell in this engine, so "
                    "the block is network-wide."
    )
    start_s: float = Field(
        ..., ge=0.0,
        description="When the fault begins, in SIMULATED seconds from the start "
                    "of the run — and the simulated horizon is far shorter than "
                    "settings.duration_s suggests. MEASURED on the real engine: a "
                    "batch run at the automatic window reaches about 1 s of "
                    "simulated time, so a fault at start_s=5.0 NEVER FIRED and the "
                    "run came back byte-identical to one with no fault at all "
                    "(10505.89 kbps both times), while the same fault at "
                    "start_s=0.05 cost 9.2%. THE FIX IS TO BUY THE HORIZON, not to "
                    "shrink the fault time: settings.kpm_collect_s=120 took the "
                    "same 3-cell config to 3.6 simulated seconds and a cell outage "
                    "at start_s=2.0 then fired for real (the cell went 9840 J -> "
                    "4841 J while its neighbours picked up the load). Set "
                    "kpm_collect_s to roughly 40x the start_s you want, or keep "
                    "faults in the first tenths of a second. Either way the engine "
                    "reports the horizon it reached as energy.span_s — check it."
    )
    duration_s: float = Field(
        0.0, ge=0.0,
        description="How long it lasts. 0 = for the rest of the run — a cell "
                    "that never comes back is a legitimate scenario, not a "
                    "missing value."
    )
    tx_power_dbm: Optional[float] = Field(
        None, ge=0.0, le=50.0,
        description="Power the cell drops TO, for 'radio_degradation'. null "
                    "means half the configured power in dB terms (-3 dB). "
                    "Ignored for 'cell_outage', which is always 0."
    )
    noise_rise_db: Optional[float] = Field(
        None, ge=1.0, le=60.0,
        description="How far the cell's receiver noise figure rises, in dB, for "
                    "'noise_rise'. null = 20 dB, the mildest rise anyone would "
                    "call a fault. Ignored by every other type."
    )

    @model_validator(mode="after")
    def _value_matches_type(self) -> "Anomaly":
        # A magnitude set on a fault that cannot use it means the user expected
        # something the run will not do. Cheaper to reject here than to explain
        # a flat KPM trace afterwards.
        if self.noise_rise_db is not None and self.type is not AnomalyType.NOISE_RISE:
            raise ValueError(
                f"noise_rise_db only applies to 'noise_rise', not '{self.type.value}'")
        if self.tx_power_dbm is not None and self.type is not AnomalyType.RADIO_DEGRADATION:
            raise ValueError(
                f"tx_power_dbm only applies to 'radio_degradation', not "
                f"'{self.type.value}'")
        return self


class SlicePolicy(BaseModel):
    """How a slice's share of the radio is partitioned.

    A slice here is a 5QI: the MAC scheduler groups UEs by the 5QI of their
    data bearer and enforces these ratios on the downlink symbol budget every
    subframe. Until now the only ratio the platform could express was a
    ceiling, and only over the live E2 control path — a scenario could not be
    WRITTEN with a slice policy at all. These three are what an operator
    actually partitions a RAN with.

    Ratios apply per cell. They are not required to sum to 100 across slices:
    over-subscribing is a legitimate scenario (it is how you test what gives
    way first), and the scheduler simply runs out of symbols to reserve.
    """
    qos_id: int = Field(
        ...,
        json_schema_extra={
            "enum": sorted(QOS_5QI_CATALOGUE),
            "x_enum_labels": {str(k): f"{k} — {v}"
                              for k, v in sorted(QOS_5QI_CATALOGUE.items())},
        },
        description="The 5QI this policy governs. Matches a service's qos_id."
    )

    @field_validator("qos_id")
    @classmethod
    def _known_5qi(cls, v: int) -> int:
        # Same rule as ServiceRequirement.qos_id, and for a sharper reason: a
        # policy naming a 5QI the translator rewrites is a policy that governs
        # a slice the user did not mean. Measured — the audit caught this one
        # as INERT, because every perturbation of it came back as 9.
        if v not in QOS_5QI_CATALOGUE:
            raise ValueError(
                f"{v} is not a standardised 5QI this engine can carry. "
                f"Valid: {sorted(QOS_5QI_CATALOGUE)}")
        return v
    max_pct: float = Field(
        100.0, ge=0.0, le=100.0,
        description="Ceiling: never more than this share of a cell's downlink "
                    "symbols, even when the cell is otherwise idle. 100 (the "
                    "default) never binds; 0 also means no ceiling."
        # Defaults to 100 rather than 0 so that a slice added in the config UI
        # is a MEANINGFUL object from the moment it appears. With every ratio
        # at 0 the translator drops the entry — correctly, since "capped at
        # nothing" would starve the slice — and the user is left with a policy
        # on screen that reaches the engine as an empty string.
    )
    min_pct: float = Field(
        0.0, ge=0.0, le=100.0,
        description="Guaranteed floor under contention — but only as much of "
                    "it as the slice actually asks for, so an idle protected "
                    "slice costs the network nothing (work-conserving). "
                    "0 = no guarantee."
    )
    dedicated_pct: float = Field(
        0.0, ge=0.0, le=100.0,
        description="Reserved outright: held for this slice whether it has "
                    "traffic or not, and unusable by anyone else "
                    "(non-work-conserving). This is the one that costs the "
                    "network capacity when the slice is quiet — which is "
                    "exactly what makes it different from min_pct. Overlaps "
                    "with min_pct rather than stacking: 20% dedicated plus a "
                    "30% floor guarantees 30%, not 50%."
    )

    @model_validator(mode="after")
    def _floor_under_ceiling(self) -> "SlicePolicy":
        # The engine clamps this too, but a config that says one thing and runs
        # as another is worse than a rejected one.
        if self.max_pct > 0 and self.min_pct > self.max_pct:
            raise ValueError(
                f"5QI {self.qos_id}: min_pct {self.min_pct} exceeds max_pct "
                f"{self.max_pct} — a floor above the ceiling cannot hold")
        if self.max_pct > 0 and self.dedicated_pct > self.max_pct:
            raise ValueError(
                f"5QI {self.qos_id}: dedicated_pct {self.dedicated_pct} exceeds "
                f"max_pct {self.max_pct}")
        if self.max_pct <= 0 and self.min_pct <= 0 and self.dedicated_pct <= 0:
            # All three at zero is a policy that says nothing. The translator
            # drops it, so it would sit in the config looking configured while
            # reaching the engine as nothing at all.
            raise ValueError(
                f"5QI {self.qos_id}: a slice policy with every ratio at 0 does "
                f"nothing — set max_pct, min_pct or dedicated_pct")
        return self


class NSGSimulationConfig(BaseModel):
    """
    Root configuration object for one NSG AI simulation run.

    Workflow:
      1. LLM generates this from a natural-language user request.
      2. Frontend renders it as an editable React Flow topology.
      3. ns3_bridge.translate_nsg_to_ns3() normalises and range-checks it, and
         produces the record written alongside a map (the rem hash the UI caches
         on). It is no longer input to an external program.
      4. ns3_bridge.run_simulation() computes the RSRP coverage grid IN PROCESS.
         It used to spawn ns3-dev-rem-gen, a binary out of the Tera5G tree; that
         dependency was removed 2026-09-15 and nothing shells out any more.
    """
    name: str = Field(
        ...,
        description="Short descriptive scenario name, e.g. '5G Urban 3-Site Coverage Test'"
    )
    description: str = Field(
        "",
        description="Free-text explanation of the scenario — used in reports and history log"
    )

    base_stations: list[BaseStation] = Field(
        ..., min_length=1,
        description="gNB/eNB transmitters. At least one is required."
    )
    ue_groups: list[UEGroup] = Field(
        default_factory=list,
        description=(
            "User equipment population groups. "
            "Can be empty for pure coverage-map (REM-only) runs."
        )
    )
    buildings: list[Building] = Field(
        default_factory=list,
        description="RF-blocking building obstacles. Empty = open-field "
                    "propagation.\n"
                    "\n"
                    "ADDING ANY BUILDING CHANGES EVERY LINK, not just the ones "
                    "behind it. With no buildings the engine decides LOS/NLOS "
                    "STATISTICALLY (the 3GPP scenario's condition model); with "
                    "buildings it switches the whole network to "
                    "BuildingsChannelConditionModel and derives LOS/NLOS from "
                    "the real geometry. So a building parked far from every cell "
                    "still moves the numbers — measured, one off-path building "
                    "took mean throughput from 3377 to 3193 kbps against the "
                    "empty scene, because links the statistical model called "
                    "NLOS became deterministically LOS.\n"
                    "\n"
                    "Blocking is real when the geometry blocks: a 40 m building "
                    "placed across the path to the serving cell moved the UEs "
                    "onto the stronger neighbour — handovers 3 -> 0 and mean "
                    "throughput 3193 -> 7857 kbps — while the SAME building off "
                    "the path changed nothing (1 kbps).\n"
                    "\n"
                    "Imports are safe: overlapping footprints are dropped before "
                    "the run (ns-3 NS_FATALs on a position inside two Buildings) "
                    "and the list is capped at 30. A 200-building OSM-style "
                    "import reduced to 30 and ran clean."
    )

    anomalies: list[Anomaly] = Field(
        default_factory=list,
        description=(
            "Faults staged during the run — a cell degrading or going dark at "
            "a chosen moment. Empty = a network where nothing breaks, which is "
            "the least interesting thing a test bench can simulate."
        )
    )

    slices: list[SlicePolicy] = Field(
        default_factory=list,
        description=(
            "Per-5QI radio partitioning enforced by the MAC scheduler. Empty = "
            "no slicing, every UE competes on equal terms. A policy only bites "
            "when some UE actually carries that 5QI."
        )
    )

    area: SimulationArea = Field(
        default_factory=SimulationArea,
        description="Coverage map bounding box for the REM computation"
    )
    settings: SimulationSettings = Field(
        default_factory=SimulationSettings,
        description="Simulation engine control parameters"
    )

    @model_validator(mode="after")
    def _cells_within_area(self) -> "NSGSimulationConfig":
        a = self.area
        outside = [
            bs.id for bs in self.base_stations
            if not (a.x_min <= bs.position.x <= a.x_max and a.y_min <= bs.position.y <= a.y_max)
        ]
        if outside:
            raise ValueError(
                f"base station(s) {outside} are outside the simulation area "
                f"x[{a.x_min}, {a.x_max}] y[{a.y_min}, {a.y_max}] — the coverage sim needs the "
                "cells inside the area; move them in or enlarge the area."
            )
        return self

    @classmethod
    def model_json_schema(cls, **kwargs) -> dict:
        """Return the JSON schema with numeric constraint keywords stripped out.

        Anthropic's structured output API rejects 'exclusiveMinimum', 'maximum',
        etc. on number/integer fields. Descriptions already convey valid ranges
        to the LLM; Python-level Pydantic validation enforces them on the way in.
        """
        return _strip_numeric_constraints(super().model_json_schema(**kwargs))

    @classmethod
    def ui_json_schema(cls, **kwargs) -> dict:
        """The schema WITH its numeric bounds, for the config UI.

        The override above strips minimum/maximum because Anthropic's
        structured-output API rejects them. That is right for the LLM and wrong
        for everything else — and `GET /schema`, which the config editor reads,
        was getting the stripped version as collateral damage. Every one of the
        96 settings fields arrived at the UI with no range, so the editor could
        not show a valid interval or tell a bounded field from a free one.

        Two consumers, two schemas: the LLM keeps the stripped one, the UI gets
        the real one. Pydantic enforces the bounds either way.

        The UI copy also carries `x_key` (see KEY_PARAMETERS) so the editor can
        tell the handful of parameters that decide the outcome from the hundreds
        that fine-tune it, and `x_tier` (see TIER_SCOPED) so it can say which of
        those never reach the real engine.
        """
        return _mark_tier_scoped(
            _mark_key_parameters(super().model_json_schema(**kwargs)))
