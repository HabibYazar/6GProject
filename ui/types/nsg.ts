/**
 * TypeScript mirror of the Python NSGSimulationConfig Pydantic schema.
 * Keep in sync with nsg_ai/schema.py — these types are the single source
 * of truth for all frontend components and API calls.
 */

// ── Enums ─────────────────────────────────────────────────────────────────────

export type AntennaType       = 'cosine' | 'massive_mimo' | 'isotropic';
export type BeamformingMode   = 'analog' | 'digital' | 'hybrid';
export type MobilityPattern   = 'static' | 'pedestrian' | 'vehicle' | 'drone' | 'random_walk';
export type NetworkSlice      = 'eMBB' | 'uRLLC' | 'voice' | 'mMTC';
export type BuildingType      = 'residential' | 'office' | 'industrial';
export type WallMaterial      = 'wood' | 'concrete' | 'concrete_with_windows';
export type PropagationScenario = 'UMa' | 'UMa-Buildings' | 'UMi' | 'RMa';

// ── Geometry ──────────────────────────────────────────────────────────────────

export interface Position3D {
  x: number;   // meters East from origin
  y: number;   // meters North from origin
  z: number;   // meters above ground
}

export interface Position2D {
  x: number;
  y: number;
}

export interface BoundingBox3D {
  x_min: number;
  x_max: number;
  y_min: number;
  y_max: number;
  z_min: number;
  z_max: number;
}

// ── Radio ─────────────────────────────────────────────────────────────────────

/**
 * The gNB panel. `rows`/`cols` default to 8 to match the engine, which uses an
 * 8x8 panel for any config without this block — and they are the only config
 * lever measured to move simulation speed: 8x8 -> 4x4 was 1.43x more simulated
 * time per run (the 3GPP steering vector is computed per element), at the cost
 * of about 35% of PEAK throughput while handovers and mean throughput held.
 */
export interface MassiveMIMOConfig {
  rows: number;                  // 1–16 vertical elements (default 8)
  cols: number;                  // 1–16 horizontal elements (default 8)
  beamforming: BeamformingMode;
  digital_azimuth_deg: number;   // digital steering offset from mechanical azimuth
  digital_tilt_deg: number;      // digital steering offset from mechanical tilt
  polarization_deg: number;      // typically ±45
}

export interface AntennaConfig {
  type: AntennaType;
  azimuth_deg: number;           // 0 = North, clockwise
  tilt_deg: number;              // positive = downtilt
  gain_dbi: number;
  horizontal_beamwidth_deg: number;  // cosine model only
  vertical_beamwidth_deg: number;    // cosine model only
  massive_mimo?: MassiveMIMOConfig;  // required when type === 'massive_mimo'
}

export interface FrequencyBand {
  label: string;             // e.g. 'NR-N77'
  frequency_mhz: number;    // e.g. 3600
  bandwidth_mhz: number;    // e.g. 100
  numerology: number;        // 0–4 (5G NR subcarrier spacing µ)
}

export interface RadioConfig {
  band: FrequencyBand;
  tx_power_dbm: number;
  antenna: AntennaConfig;
}

// ── Base Station ──────────────────────────────────────────────────────────────

export interface BaseStation {
  id: string;        // e.g. 'gnb-site-a-sector-1'
  name: string;      // e.g. 'Site-A / N77 / Cell-1'
  position: Position3D;
  radio: RadioConfig;
}

// ── User Equipment ────────────────────────────────────────────────────────────

export interface ServiceRequirement {
  slice_type: NetworkSlice;
  qos_id: number;                       // 5QI per 3GPP TS 23.501
  target_throughput_gbps: number;
}

export interface WaypointStep {
  position: Position3D;
  speed_mps: number;
}

export interface MobilityConfig {
  pattern: MobilityPattern;
  speed_mps: number;
  /** Phase 2b: a path the group really TRAVERSES on the ns-O-RAN engine
   *  (WaypointMobilityModel). The default random walk diffuses and nets almost
   *  no displacement, so this is the mobility lever that crosses cell
   *  boundaries and produces handovers. */
  waypoints?: WaypointStep[];
  /** What happens at the end of the path. Default 'pingpong' — a path walked
   *  once usually finishes before the run does. */
  waypoint_mode?: 'once' | 'repeat' | 'pingpong';
}

export interface UEGroup {
  id: string;
  name: string;
  count: number;
  spawn_position: Position2D;
  mobility: MobilityConfig;
  services: ServiceRequirement[];
}

// ── Environment ───────────────────────────────────────────────────────────────

export interface Building {
  id: string;
  bounds: BoundingBox3D;
  building_type: BuildingType;
  wall_material: WallMaterial;
  floors: number;
  rooms_x: number;
  rooms_y: number;
}

// ── Simulation Control ────────────────────────────────────────────────────────

export interface SimulationArea {
  x_min: number;
  x_max: number;
  y_min: number;
  y_max: number;
  resolution_m: number;
}

export type HandoverMode = 'FixedTtt' | 'DynamicTtt' | 'Threshold' | 'NoAuto';

export interface SimulationSettings {
  duration_s: number;
  report_interval_ms: number;
  scenario: PropagationScenario;
  shadowing_enabled: boolean;
  random_seed?: number;
  /** Handover SINR margin (dB) a neighbour must beat serving by. Real ns-O-RAN
   *  lever (LteEnbRrc::HoSinrDifference). */
  ho_sinr_margin_db?: number;
  /** Secondary-cell handover trigger mode — real engine lever
   *  (LteEnbRrc::SecondaryCellHandoverMode). */
  handover_mode?: HandoverMode;
  /** Time-to-trigger (ms) for FixedTtt mode (LteEnbRrc::FixedTttValue). */
  ho_ttt_ms?: number;
  /** Phase 2b. How often (ms) the RAN re-evaluates handover per UE. 0 = decide
   *  once at t=0, which is the engine's inherited behaviour and makes every
   *  other mobility lever inert (LteEnbRrc::HoCheckPeriodMs). */
  ho_check_period_ms?: number;
  /** Require the 3GPP A3 entry condition before scheduling a handover
   *  (LteEnbRrc::HoA3Entry). */
  ho_a3_entry?: boolean;
  /** A3 hysteresis (dB) on top of ho_sinr_margin_db (LteEnbRrc::HoHysteresisDb). */
  ho_hysteresis_db?: number;
  /** A2 event: serving SINR below this (dB) hands over immediately, bypassing
   *  the TTT. null/undefined = disabled (LteEnbRrc::HoA2ThresholdDb). */
  ho_a2_threshold_db?: number;
  /** Phase 2c. Array element pattern. 'isotropic' (engine default) makes each
   *  cell's antenna.azimuth_deg / tilt_deg inert — rotating isotropic elements
   *  cancels out. '3gpp' uses the TR 38.901 element (65° beamwidth, 30 dB
   *  front-to-back), which makes orientation and sectorisation real, at ~8 dB
   *  more boresight gain (so the two are not comparable). */
  antenna_element?: 'isotropic' | '3gpp';
  /* No scheduler field: the fork's three alternatives to the flex-TTI
   * scheduler are dead code (two allocate nothing, one aborts ns-3), so the
   * platform always leaves the engine on the working one. */
  /** Phase 2c radio. MCS selection: real 3GPP tables at a target BLER, or an optimistic
   *  Shannon bound (MmWaveAmc::AmcModel). */
  amc_model?: 'error_model' | 'shannon';
  /** Target BER for the Shannon AMC model only (MmWaveAmc::Ber). */
  amc_target_ber?: number;
  /** HARQ retransmissions (MmWaveHelper::HarqEnabled). */
  harq_enabled?: boolean;
  /** Concurrent stop-and-wait HARQ processes per UE
   *  (MmWavePhyMacCommon::NumHarqProcess). */
  harq_processes?: number;
}

/* Fields that reach only the fast analytical tier, never the ns-O-RAN engine:
 * AntennaConfig.type / gain_dbi / horizontal_beamwidth_deg /
 * vertical_beamwidth_deg, MassiveMIMOConfig.beamforming /
 * fields deleted 2026-09-21 after tools/tier_scoped_audit.py found nothing read them,
 * MobilityConfig.pattern. The real-engine equivalents are
 * settings.antenna_element, antenna.azimuth_deg / tilt_deg,
 * massive_mimo.rows / cols / polarization_deg, and mobility.waypoints.
 * SimulationSettings.duration_s is a target, not a guarantee — real-engine runs
 * end on a wall-clock cap. */

// ── Root ──────────────────────────────────────────────────────────────────────

/** One fault staged into the RUNNING simulation. Mirrors schema.Anomaly. */
export interface Anomaly {
  type: 'radio_degradation' | 'cell_outage' | 'noise_rise' | 'handover_block';
  /** Ignored for handover_block — one anchor admits handovers for every cell. */
  cell_id: string;
  /**
   * SIMULATED seconds. A batch run reaches only a fraction of one, so a fault at
   * start_s = 5 never fires and the run comes back identical to a healthy one.
   */
  start_s: number;
  /** 0 = for the rest of the run. */
  duration_s?: number;
  tx_power_dbm?: number | null;
  noise_rise_db?: number | null;
}

/** Per-5QI radio partitioning enforced by the MAC scheduler. */
export interface SlicePolicy {
  qos_id: number;
  max_pct?: number;
  min_pct?: number;
  dedicated_pct?: number;
}

export interface NSGSimulationConfig {
  name: string;
  description: string;
  base_stations: BaseStation[];
  ue_groups: UEGroup[];
  buildings: Building[];
  /** Optional layers — present only when the scenario uses them. These existed
   *  in the backend schema for a while before this mirror caught up, which is
   *  why the report could not describe a fault scenario. */
  anomalies?: Anomaly[];
  slices?: SlicePolicy[];
  area: SimulationArea;
  settings: SimulationSettings;
}

// ── API Response Types ────────────────────────────────────────────────────────

/** One sampled point from the NS-3 Radio Environment Map (REM). */
export interface RemPoint {
  x: number;     // meters East from origin
  y: number;     // meters North from origin
  rsrp: number;  // dBm
}

export interface SimulationRunResult {
  status: string;
  rem_hash: string;
  config_path: string;
  output_npy: string;
  stdout: string;
  heatmap: RemPoint[];
}

/**
 * One tick from the WS /ws/live-traffic stream.
 * eMBB/uRLLC/mMTC/voice: aggregate Mbps delivered per network slice (these
 * ARE the throughput numbers, just broken out by slice instead of summed).
 * tput is their sum. The remaining fields are derived system-level KPIs —
 * see estimate_live_throughput()'s docstring in ns3_bridge.py for exactly
 * how each is computed and what it approximates. No `handover` field: this
 * estimator has no time-stepped mobility simulation, so there's nothing to
 * hand over between ticks.
 */
export interface LiveTrafficFrame {
  time: number;
  eMBB: number;
  uRLLC: number;
  mMTC: number;
  voice: number;
  tput: number;        // Mbps, sum of the four slices above
  power: number;        // W, total configured tx power across all sectors
  prb: number;           // %, demand/capacity utilization proxy
  qos: number;            // /100, avg QoS-target satisfaction across active UEs
  conn_ues: number;        // estimated count of actively-transmitting UEs
  rsrp: number;              // dBm, avg best-server RSRP across UE groups
  energy_eff: number;         // bits/J, tput ÷ power
  /** "nsoran" when the frame was MEASURED on the real engine (Live Traffic is
   *  engine-driven now): power is the radio-energy-model draw, per-slice Mbps
   *  come from real per-UE KPM, and the signal value is SINR (dB), not RSRP —
   *  the UI relabels the tile. */
  source?: 'nsoran';
  sinr?: number;               // dB, mean serving SINR (engine frames)
  handovers?: number;          // serving-cell changes since session start
}

export interface LiveMetricsSeries {
  columns: string[];
  values: [string, number, number, number, number, number, number, number, number][];
}

export interface LiveMetricsResponse {
  results: Array<{ series?: LiveMetricsSeries[] }>;
}

// ── RIC Test (simplified E2 / ws/e2) ────────────────────────────────────────────

/** Per-cell KPM row (E2SM-KPM analogue). */
export interface RicCellKpm {
  id: string;
  name: string;
  active: boolean;
  /** True when offline due to an injected fault (vs an AI energy-saving off). */
  faulted?: boolean;
  tx_power_dbm: number;
  power_w: number;
  prb_used_pct: number;
  throughput_mbps: number;
  connected_ues: number;
}

/** Per-UE-group KPM row. serving_cell/rsrp/sinr are null when in outage. */
export interface RicUeKpm {
  id: string;
  name: string;
  serving_cell: string | null;
  rsrp_dbm: number | null;
  sinr_db: number | null;
  throughput_mbps: number;
  count: number;
}

export interface RicSliceKpm {
  slice: string;
  throughput_mbps: number;
  prb_pct: number;
  qos_score: number;
}

/** Full KPM report frame pushed from /ws/e2 ({"type":"kpm", ...}). */
export interface RicKpmReport {
  /** Simulated seconds this report covers, and how many windows have closed.
   *  Both tiers publish them; the dashboard leads with them because every
   *  other number is only as meaningful as the time behind it. */
  sim_t?: number;
  handovers?: number;
  /** PER UE, in the same shape the slow tier publishes (`NsOranLiveUe`), so
   *  one map and one table serve both tiers. `ue_groups` folds these back to
   *  one row per group for the control surface; the fold is what used to
   *  force the fast tier's map to be a schematic. */
  ues?: NsOranLiveUe[];
  /** How fast simulated time is running against the wall clock, and what is
   *  untrue about it above 1x. `rate_achieved` is set only when the loop
   *  could NOT hold the rate it was asked for. */
  rate?: number;
  rate_achieved?: number | null;
  rate_warning?: string | null;
  tick: number;
  cells: RicCellKpm[];
  ue_groups: RicUeKpm[];
  slices: RicSliceKpm[];
  control_state: {
    disabled_cells: string[];
    power_override: Record<string, number>;
    forced_serving: Record<string, string>;
    slice_prb_quota: Record<string, number>;
  };
  /** Operator-injected anomalies currently active. */
  anomalies?: {
    faulted_cells: string[];
    demand_multiplier: number;
  };
}

/** RC control command (E2SM-RC analogue) sent up to /ws/e2. */
export type RicControlCommand =
  | { action: 'handover'; ue_group: string; target_cell: string }
  | { action: 'cell_power'; cell: string; state: 'on' | 'off' }
  | { action: 'cell_power'; cell: string; tx_power_dbm: number }
  | { action: 'slice_quota'; slice: string; prb_percent: number }
  /** How fast simulated time runs against the wall clock. A RIG control, not
   *  a RAN one — it changes how fast the world runs, not anything in it.
   *  1x is real time and the default, because what a RIC test rig tests is
   *  an xApp and an xApp is a control loop with a wall-clock deadline. */
  | { action: 'set_rate'; rate: number }
  | { action: 'reset' };

/** Anomaly-injection command (operator perturbs the RAN) sent as {"inject":cmd}. */
export type RicAnomalyCommand =
  | { kind: 'cell_fault'; cell: string }
  | { kind: 'clear_fault'; cell?: string }
  | { kind: 'traffic_surge'; multiplier: number }
  | { kind: 'clear_surge' }
  | { kind: 'clear_all' };

/** Server ack for a control command ({"type":"control_ack", ...}). */
export interface RicControlAck {
  action: string;
  success: boolean;
  detail: string;
}

/**
 * One entry in the on-screen control history. Built from a `control_event`
 * broadcast, so it carries the source (which client issued it — the browser
 * or an external xApp) and always has its ack resolved.
 */
export interface RicControlLogEntry {
  id: string;
  command: RicControlCommand | RicAnomalyCommand;
  ack: RicControlAck;
  source: string;
  timestamp: Date;
}

/**
 * ns-O-RAN engine KPM — MEASURED 5G metrics from the real FlexRIC + ns-3.42
 * mmWave stack over real E2AP/SCTP (the industry-standard fidelity tier). Each
 * measurement is summarised over the run. Names are 3GPP KPM (DRB.UEThpDl =
 * throughput kbps, RRU.PrbUsedDl = PRB, L3servingSINR = SINR dB, …).
 */
export interface NsOranMeasurement {
  count: number;
  min: number;
  max: number;
  mean: number;
}
/** Config-derived knobs the run was driven with (echoed back by the engine). */
export interface NsOranRequested {
  n_gnb?: number;
  n_ue?: number;
  freq_hz?: number;
  bw_hz?: number;
  isd_cell?: number;
  isd_ue?: number;
}
/** KPIs derived from raw TB counters the engine emits (not named directly). */
export interface NsOranDerived {
  bler_pct: number;        // DL block error rate (TB errors / TB total)
  mod_64qam_pct: number;   // share of TBs sent with 64QAM (higher = better channel)
}
/** Real E2SM-RC down-link control loop the xApp drives over E2AP. */
export interface NsOranRcControl {
  requests_sent: number;
  acks_received: number;
  action: string;
  closed_loop: boolean;   // requests all acknowledged by the RAN
}
/** One live KPM indication frame from the REAL engine (via /ws/e2-real). Unlike
 *  NsOranKpm (a whole batch run summary), this is a single indication streamed
 *  live while the ns-3 RAN runs and the customer's xApp controls it. */
/** One UE's live row: serving cell + SINR + throughput + PRB (its KPM id = IMSI). */
export interface NsOranLiveUe {
  imsi: number;
  /** Fields the FAST tier can answer and ns-3's KPM set does not: the
   *  reference-signal power, the MCS its AMC chose, the block error rate at
   *  that MCS, and what is still queued. Optional because the slow tier
   *  leaves them out rather than inventing them. */
  rsrp?: number;
  mcs?: number;
  bler_pct?: number;
  queued_kb?: number;
  sinr_ul?: number;
  cell?: number;
  sinr?: number;
  thp_kbps?: number;
  prb?: number;
  ul_kbps?: number;
  delay_ms?: number;
  /** The slice this UE's traffic was built with (eMBB/uRLLC/mMTC/voice). */
  slice?: string;
  /** REAL position from the running sim, converted to CONFIG coordinates —
   *  drives the live map / flow overlays. */
  x?: number;
  y?: number;
}
/** Per-cell aggregate of the live UEs (serving that cell). */
export interface NsOranLiveCell {
  cell: number;
  ues: number;
  thp_kbps: number;
  prb: number;
  sinr: number | null;
  ul_kbps?: number;
  /** MEASURED draw from the sim's radio energy model (bridge-tailed), not a
   *  config-derived estimate. */
  power_w?: number | null;
  /** RU sleep state: why this cell's power is low. 'asleep' = descended into
   *  its sleep depth, 'waking' = paying the wake-up latency, 'awake' = serving. */
  sleep_state?: 'awake' | 'asleep' | 'waking' | null;
  sleep_depth?: 'none' | 'micro' | 'light' | 'deep' | null;
  /** Share of elapsed time this cell has spent asleep. */
  sleep_pct?: number | null;
}
/** Per-cell RU sleep-state record (Phase 2a energy saving). */
export interface NsOranSleepCell {
  depth: 'none' | 'micro' | 'light' | 'deep';
  state: 'awake' | 'asleep' | 'waking';
  sleeps: number;
  wakes: number;
  sleep_s: number;
  sleep_pct: number;
  duty_pct?: number;
  ues?: number;
  micro_s?: number;
  light_s?: number;
  deep_s?: number;
  off_s?: number;
}
export interface NsOranLiveFrame {
  type: 'kpm';
  tick: number;
  /** WHERE THIS TICK IS IN SIMULATED TIME, seconds. Ticks are KPM windows,
   *  not seconds: the engine runs at a few percent of real time, so tick 5
   *  can be 0.1 s in. Without this a thin early tick reads as "the engine
   *  produced nothing" when it means "almost no time has passed yet".
   *  Null on a frame the engine had no position sample for. */
  sim_t?: number | null;
  measurements: Record<string, NsOranMeasurement>;
  ues?: NsOranLiveUe[];
  cells?: NsOranLiveCell[];
  /** Per-slice DL kbps totals, from each UE's real slice tag. */
  slices?: Record<string, number>;
  /** Serving-cell changes observed since session start (live handover count). */
  handovers?: number;
  /** Latest MEASURED energy: total W + per-cell {j, w}. */
  energy?: { total_w: number; per_cell: Record<string, { j: number; w: number }> } | null;
  /** RU sleep states — which cells are down, how deep, and for how long. */
  sleep?: {
    per_cell: Record<string, NsOranSleepCell>;
    cells_asleep: number;
    wake_events: number;
  } | null;
  ue_ids: number[];
}
/** A line in the live-session log: session boot progress, a control echo, or an error. */
export interface RicRealLogEntry {
  id: string;
  line: string;
  kind: 'status' | 'control' | 'error';
  ts: number;
}

export interface NsOranKpm {
  engine: string;
  sim_time: number;
  requested?: NsOranRequested;  // cell/UE count, band, scale derived from the scenario
  e2_agents: number;          // gNBs that completed the real E2 SETUP handshake
  kpm_indications: number;    // E2SM-KPM indication messages received
  ue_count: number;
  /** Handovers observed in the sampled KPM (NGAP connection-id churn — a
   *  lower bound; counts control-driven mobility). */
  handovers?: number;
  measurements: Record<string, NsOranMeasurement>;
  derived?: NsOranDerived;
  /** MEASURED energy from the sim's own radio energy model (per-cell Joules) —
   *  not the config-derived estimate. A slept cell measures 0 J, so the
   *  energy-saving use case is provable with real data. */
  energy?: {
    per_cell_j: Record<string, number>;
    total_j: number;
    span_s: number;
    mean_power_w: number | null;
    source: string;
  } | null;
  /** RU sleep states (Phase 2a): which cells descended into a sleep depth and
   *  for how much of the run — the duty cycle the energy saving came from. */
  sleep?: {
    per_cell: Record<string, NsOranSleepCell>;
    cells_asleep: number;
    sleep_events: number;
    wake_events: number;
    /** Share of the whole cell layer's airtime spent asleep. */
    layer_sleep_pct: number;
    span_s: number;
  } | null;
  rc_control?: NsOranRcControl;  // real E2SM-RC down-link control loop
  /** ok | truncated | busy | crashed | partial | no_data. Only "ok" guarantees
   *  the DU metrics (throughput/SINR/PRB) are present AND that the run reached
   *  the end it was asked for — anything else must be shown, not rendered as
   *  blank cells. "truncated" carries real numbers over a shorter span. */
  status: string;
  /** Why the run ended: "budget" (wall clock ran out), "crash" (ns-3 died
   *  part way), or null/absent when it ran to the end. A mid-run crash with
   *  data used to report as a clean "ok"; this is what distinguishes them. */
  ended_by?: 'budget' | 'crash' | null;
  /** The ns-3 log line that ended the run, when one did. */
  crash_signature?: string | null;
  /** True when the wall budget stopped the run. */
  truncated?: boolean;
  /** The wall budget the engine allowed this run, in seconds. */
  budget_s?: number;
  error?: string | null;   // why, when status !== "ok"
  retried?: boolean;       // engine re-ran after an ns-3 startup abort
}

// ── Test campaigns ────────────────────────────────────────────────────────────
// A suite of scenarios run at fixed seeds and scored against declared
// thresholds. One run says what happened; a campaign says whether it is still
// good enough. Mirrors nsg_ai/campaign.py.

export type ThresholdOp = '>=' | '<=' | '>' | '<';

/** pass/fail is about the scenario; error is about the bench (engine down, crash). */
export type Verdict = 'pass' | 'fail' | 'error' | 'no_thresholds' | 'empty';

export interface CampaignThreshold {
  /** Dotted path into a run summary, e.g. "measurements.L3servingSINR.mean". */
  metric: string;
  op: ThresholdOp;
  value: number;
  label?: string;
}

export interface CampaignViolation {
  metric: string;
  expected: string;
  measured: number | null;
  reason: 'below_bound' | 'above_bound' | 'missing';
}

export interface CampaignRun {
  label: string;
  item_label: string;
  repeat: number;
  seed: number;
  run_id: string | null;
  status: string;
  verdict: Verdict | null;
  violations: CampaignViolation[];
  summary: Record<string, unknown> | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
  /** The exact scenario this run executed, seed included. */
  config?: NSGSimulationConfig;
}

export interface CampaignRecord {
  id: string;
  name: string;
  description: string;
  /** running | done | interrupted (the process died mid-flight). */
  status: string;
  verdict: Verdict | null;
  created_at: string;
  finished_at: string | null;
  base_seed: number;
  /** Processes per scenario. 1, or absent on campaigns that predate the field,
   *  means each scenario ran whole. Part of what the campaign IS: a split suite
   *  and a whole one are different instruments, and History lists them together. */
  partition_groups?: number;
  thresholds: CampaignThreshold[];
  /** Username of the owner, when the row is not the caller's own. */
  owner?: string | null;
  planned?: { label: string; seed: number; frames: number }[];
  runs: CampaignRun[];
}

/** List view — same record minus per-run detail, plus counts. */
export type CampaignBrief = Omit<CampaignRecord, 'runs'> & {
  run_count: number;
  failed: number;
};

// ── Regression diff ───────────────────────────────────────────────────────────

export interface CampaignDiffMetric {
  metric: string;
  baseline: number;
  current: number;
  delta: number;
  /** null when the baseline value is 0 — a percentage off zero says nothing. */
  pct: number | null;
  /** Seed-to-seed spread of the BASELINE's repeats, the yardstick for noise. */
  baseline_spread: number;
  samples: [number, number];
  /** False when the baseline had a single repeat: with no spread to compare
   *  against, the noise question cannot be answered at all. */
  noise_known: boolean;
  /** The move is no larger than the baseline's own spread — not evidence. */
  within_noise: boolean;
  is_threshold: boolean;
  /** Only set where a threshold declared which way is good. */
  direction: 'better' | 'worse' | 'same' | null;
}

export interface CampaignDiffItem {
  label: string;
  status: 'matched' | 'added' | 'removed';
  baseline_verdict: Verdict | null;
  current_verdict: Verdict | null;
  change: 'regressed' | 'fixed' | 'unchanged' | 'changed' | 'incomparable';
  metrics: CampaignDiffMetric[];
  /** How the two sides were paired. "label" is a declared match; 
   *  "position" means the names differed and each campaign held exactly one
   *  scenario, so they were paired by inference — shown, never silent, since
   *  an unrelated pair would otherwise read as a like-for-like regression. */
  matched_by?: 'label' | 'position';
  /** The baseline side's own label, which differs from `label` only when
   *  the pairing was inferred. */
  baseline_label?: string;
  /** WHAT WAS DIFFERENT ABOUT THE RUN, beside what came out of it. The
   *  metric rows say whether it got better; this says because of what —
   *  the question anyone tuning a baseline is actually asking. Leaf-level
   *  dotted paths, so a per-cell change reads as
   *  `base_stations[2].radio.antenna.tilt_deg`. */
  config_delta?: {
    changed: { path: string; baseline: unknown; current: unknown }[];
    available: boolean;
    truncated: boolean;
    total: number;
  };
}

export interface CampaignDiff {
  baseline: { id: string; name: string; created_at: string; verdict: Verdict | null; base_seed: number };
  current: { id: string; name: string; created_at: string; verdict: Verdict | null; base_seed: number };
  overall: 'regressed' | 'fixed' | 'unchanged' | 'changed';
  /** False when the two ran at different base seeds — the diff is noisier. */
  seed_match: boolean;
  /** False when the pass criteria themselves changed between the two. */
  threshold_match: boolean;
  items: CampaignDiffItem[];
}

// ── Batch run datasets (the per-tick time series) ─────────────────────────────
// A campaign reports a run's SUMMARY. This is the ground truth underneath it:
// every KPM window the engine reported, with its per-UE and per-cell rows.

export interface SimFrameUe {
  imsi: number;
  amf?: number;
  cell: number;
  sinr: number | null;
  prb: number | null;
  thp_kbps: number | null;
  delay_ms: number | null;
  slice?: string;
  /** True when this UE is attached to the LTE ANCHOR rather than a mmWave
   *  cell. The user plane and every DU metric live on mmWave, so a UE here
   *  has lost its mmWave link — a coverage failure the cell id alone does
   *  not reveal. */
  on_lte?: boolean;
  ul_kbps?: number | null;
  x?: number;
  y?: number;
}

export interface SimFrameCell {
  cell: number;
  ues: number;
  thp_kbps: number | null;
  ul_kbps: number | null;
  prb: number | null;
  sinr: number | null;
  power_w: number | null;
  sleep_state?: string | null;
  sleep_depth?: string | null;
  sleep_pct?: number | null;
}

export interface SimFrame {
  type: string;
  tick: number;
  /** WHERE THIS TICK IS IN SIMULATED TIME, seconds. A tick is a KPM WINDOW,
   *  not a second: the engine runs at a few percent of real time, so tick 5
   *  is routinely 0.1 s in. Without it a thin early tick reads as "the
   *  engine produced nothing" when it means "almost no time has passed".
   *  Null on a frame with no position sample. */
  sim_t?: number | null;
  /** Whether this frame is an INDEPENDENT OBSERVATION or a repeat of the
   *  last one. The live stream emits frames faster than the RAN reports
   *  over E2, so consecutive ticks routinely carry the same KPM values.
   *  Measured on a real run: only 2 of the first 8 ticks were fresh.
   *  Counting ticks as samples overstates the evidence by several times. */
  kpm_fresh?: boolean;
  /** Which cells reported new KPM in this frame. */
  kpm_fresh_cells?: number[];
  measurements: Record<string, { count: number; min: number; max: number; mean: number }>;
  ues: SimFrameUe[];
  cells: SimFrameCell[];
  slices: Record<string, number>;
  /** Cumulative across the run, not per tick. */
  handovers: number | null;
  energy: { total_w?: number; per_cell?: Record<string, { j: number; w: number }> } | null;
  ue_ids: number[];
}

/** What a run has produced SO FAR — published while it is still going. */
export interface SimRunProgress {
  n_frames: number;
  frames_target: number;
  pct: number;
  last_tick: number | null;
  elapsed_s: number;
  ues_reporting: number;
  cells_reporting: number;
  dl_mbps: number;
  power_w: number | null;
  handovers: number | null;
  anomalies_fired: number;
  /** The bridge's own last status line — the honest answer to "doing what?". */
  stage: string | null;
}

export interface SimRunBrief {
  id: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  config_name: string | null;
  n_frames: number;
  requested?: Record<string, unknown>;
  campaign_id?: string;
  error?: string | null;
  frames_target?: number;
  progress?: SimRunProgress;
  /** The listing carries the run summary, and `sim_span_s` in it is the
   *  HORIZON — how much simulated time the run covered. Shown next to the
   *  tick count, because 12 ticks sounds substantial until you see it was
   *  1.1 simulated seconds. */
  summary?: ({ sim_span_s?: number | null } & Record<string, unknown>) | null;
}

export interface SimRunDataset extends SimRunBrief {
  config: NSGSimulationConfig;
  /** `anomaly_events` are the engine's own `[NSG-FAULT] t=…s cell=…` lines
   *  for faults that ACTUALLY FIRED, and `anomalies_configured` is how many
   *  the scenario staged. The two disagreeing is the anomaly-horizon trap:
   *  a start_s beyond the run's simulated span never happens, and the run
   *  then passes its thresholds honestly. */
  summary:
    | ({
        sim_span_s?: number | null;
        anomaly_events?: string[];
        anomalies_configured?: number;
        anomalies_fired?: number;
      } & Record<string, unknown>)
    | null;
  status_lines: string[];
  frames: SimFrame[];
}

// ── Suite history (scenario × campaign over time) ─────────────────────────────

export interface CampaignHistoryItem {
  label: string;
  verdict: Verdict;
  repeats: number;
  metrics: Record<string, number>;
  spread: Record<string, number>;
  /** False when any aggregate came from a single repeat — no spread to judge by. */
  noise_known: boolean;
}

export interface CampaignHistoryColumn {
  id: string;
  created_at: string;
  verdict: Verdict | null;
  status: string;
  /** Processes per scenario. A split column and a whole one are different
   *  instruments and must not be read as one trend. */
  partition_groups?: number;
  thresholds: CampaignThreshold[];
  items: CampaignHistoryItem[];
}

export interface CampaignHistory {
  /** The suite being shown — campaigns sharing a name are reruns of it. */
  suite: string | null;
  suites: string[];
  scenarios: string[];
  campaigns: CampaignHistoryColumn[];
}

export interface CampaignItemSpec {
  label: string;
  config: NSGSimulationConfig;
  frames?: number;
  repeats?: number;
}

export interface CampaignSpec {
  name: string;
  description?: string;
  items: CampaignItemSpec[];
  thresholds: CampaignThreshold[];
  base_seed?: number;
  /** Run each scenario as this many ns-3 processes of whole cells. Does not make
   *  the suite finish sooner — it buys a longer horizon per run. */
  partition_groups?: number;
  /** Wall-clock budget per run. The REAL horizon control: ns-3 runs at a few
   *  percent of real time, so a run usually ends here, not at its frame target.
   *  Meaningless on the fast tier, which gives exactly the frames asked for. */
  wall_cap_s?: number;
  /** Which tier of the engine runs the suite. 'slow' is ns-3 itself and the
   *  authority; 'fast' is the per-window model. Both emit the same frames and
   *  are scored on the same thresholds, which is what lets a suite be run on
   *  both and compared. Defaults to 'slow' so an existing suite reruns
   *  unchanged. `partition_groups` is a slow-tier control and the backend
   *  refuses the two together. */
  tier?: 'fast' | 'slow';
}

// ── Chat ──────────────────────────────────────────────────────────────────────

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  /** Human-readable content for the chat UI. */
  content: string;
  /** Optional: diff summary or structured data attached to this message. */
  meta?: Record<string, unknown>;
  timestamp: Date;
}
