/**
 * NSG AI Platform — Global State (Zustand)
 *
 * Install:  npm install zustand immer
 *
 * This store is the single source of truth for:
 *   - The active simulation config (config)
 *   - The LLM-proposed config waiting for approval (proposedConfig)
 *   - The chat message history
 *   - Simulation run status
 *
 * Components read state with useNSGStore(s => s.someField).
 * Components dispatch actions with useNSGStore(s => s.someAction).
 * Neither requires prop drilling.
 */
import { create } from 'zustand';
import { immer } from 'zustand/middleware/immer';
import * as jsondiffpatch from 'jsondiffpatch';

import { modifyConfig, runSimulation, resetAgent, wsUrl, validateNsOran, normalizeConfig } from '@/lib/api';
import { scoreKpm, scoreRealFrame, summariseRun, captureFinalPowers, type RunPoint, type SavedRun } from '@/utils/ricScore';
import type {
  NSGSimulationConfig, SimulationRunResult, ChatMessage, RemPoint, LiveTrafficFrame,
  RicKpmReport, RicControlCommand, RicControlLogEntry, RicAnomalyCommand, NsOranKpm,
  NsOranLiveFrame, NsOranLiveUe, RicRealLogEntry,
} from '@/types/nsg';

// ── WebSocket handles ────────────────────────────────────────────────────────────
// Kept outside Immer state — a WebSocket instance is not serializable/plain
// state and shouldn't be tracked by Immer's draft proxying.
let liveTrafficSocket: WebSocket | null = null;
let ricSocket: WebSocket | null = null;
let ricRealSocket: WebSocket | null = null;
const LIVE_TRAFFIC_MAX_POINTS = 60; // ring buffer — keep the chart to a fixed window
const RIC_LOG_MAX = 50;             // control-history cap

// ── Diff helper ───────────────────────────────────────────────────────────────

const differ = jsondiffpatch.create({ arrays: { detectMove: false } });

/**
 * Build a short human-readable summary of what changed between two configs.
 * Shown as the assistant's reply in the chat, so the user doesn't need to open
 * the diff modal to understand what the LLM did.
 */
function summariseDiff(
  prev: Partial<NSGSimulationConfig>,
  next: NSGSimulationConfig,
): string {
  const parts: string[] = [];

  // Base stations
  const prevBS = (prev.base_stations ?? []).length;
  const nextBS = next.base_stations.length;
  if (prevBS !== nextBS) {
    const d = nextBS - prevBS;
    parts.push(`${d > 0 ? '+' : ''}${d} gNB sector${Math.abs(d) !== 1 ? 's' : ''} (${nextBS} total)`);
  } else if (JSON.stringify(prev.base_stations) !== JSON.stringify(next.base_stations)) {
    parts.push(`${nextBS} gNB sector${nextBS !== 1 ? 's' : ''} reconfigured`);
  }

  // UE groups
  const prevUE = (prev.ue_groups ?? []).length;
  const nextUE = next.ue_groups.length;
  if (prevUE !== nextUE) {
    const d = nextUE - prevUE;
    parts.push(`${d > 0 ? '+' : ''}${d} UE group${Math.abs(d) !== 1 ? 's' : ''}`);
  } else if (JSON.stringify(prev.ue_groups) !== JSON.stringify(next.ue_groups)) {
    parts.push('UE groups updated');
  }

  // Buildings
  const prevBld = (prev.buildings ?? []).length;
  const nextBld = next.buildings.length;
  if (prevBld !== nextBld) {
    const d = nextBld - prevBld;
    parts.push(`${d > 0 ? '+' : ''}${d} building${Math.abs(d) !== 1 ? 's' : ''}`);
  } else if (JSON.stringify(prev.buildings) !== JSON.stringify(next.buildings)) {
    parts.push('buildings repositioned');
  }

  // Settings
  if (JSON.stringify(prev.settings) !== JSON.stringify(next.settings)) {
    parts.push('simulation settings adjusted');
  }

  // Area
  if (JSON.stringify(prev.area) !== JSON.stringify(next.area)) {
    parts.push('coverage area updated');
  }

  // Name
  if (prev.name !== next.name && next.name) {
    parts.push(`renamed to "${next.name}"`);
  }

  if (parts.length === 0) return 'No changes detected.';
  return parts.join(' · ') + '.';
}

/**
 * Short confirmation for a brand-new scenario (first prompt, no prior config).
 * There's nothing to diff against yet — DEFAULT_CONFIG is just an internal
 * starting template the user never sees, so describe the result, not a delta.
 */
function summariseNewConfig(cfg: NSGSimulationConfig): string {
  const parts = [
    `${cfg.base_stations.length} gNB sector${cfg.base_stations.length !== 1 ? 's' : ''}`,
    `${cfg.ue_groups.length} UE group${cfg.ue_groups.length !== 1 ? 's' : ''}`,
  ];
  if (cfg.buildings.length > 0) {
    parts.push(`${cfg.buildings.length} building${cfg.buildings.length !== 1 ? 's' : ''}`);
  }
  return `Scenario created — ${parts.join(' · ')}.`;
}

// ── Default starting configuration ───────────────────────────────────────────

export const DEFAULT_CONFIG: NSGSimulationConfig = {
  name: 'Urban Dense Network — Starter Scenario',
  description: '3-sector macro gNB + mMIMO hotspot + low-band coverage layer. Dense urban buildings. Mix of eMBB and uRLLC slices. Ready to run.',
  base_stations: [
    // ── Site A: 3-sector macro (NR-N77, 3.6 GHz) at origin ──────────────────
    {
      id: 'gnb-site-a-s1', name: 'Site-A / N77 / S1 (North)',
      position: { x: 0, y: 0, z: 30 },
      radio: { band: { label: 'NR-N77', frequency_mhz: 3600, bandwidth_mhz: 100, numerology: 1 }, tx_power_dbm: 46,
        antenna: { type: 'cosine', azimuth_deg: 0,   tilt_deg: 3, gain_dbi: 17, horizontal_beamwidth_deg: 65, vertical_beamwidth_deg: 7.5 } },
    },
    {
      id: 'gnb-site-a-s2', name: 'Site-A / N77 / S2 (South-East)',
      position: { x: 0, y: 0, z: 30 },
      radio: { band: { label: 'NR-N77', frequency_mhz: 3600, bandwidth_mhz: 100, numerology: 1 }, tx_power_dbm: 46,
        antenna: { type: 'cosine', azimuth_deg: 120, tilt_deg: 3, gain_dbi: 17, horizontal_beamwidth_deg: 65, vertical_beamwidth_deg: 7.5 } },
    },
    {
      id: 'gnb-site-a-s3', name: 'Site-A / N77 / S3 (South-West)',
      position: { x: 0, y: 0, z: 30 },
      radio: { band: { label: 'NR-N77', frequency_mhz: 3600, bandwidth_mhz: 100, numerology: 1 }, tx_power_dbm: 46,
        antenna: { type: 'cosine', azimuth_deg: 240, tilt_deg: 3, gain_dbi: 17, horizontal_beamwidth_deg: 65, vertical_beamwidth_deg: 7.5 } },
    },
    // ── Site B: mMIMO hotspot (NR-N77) — East industrial zone ───────────────
    {
      id: 'gnb-site-b-s1', name: 'Site-B / N77 / mMIMO (East Hotspot)',
      position: { x: 400, y: 200, z: 25 },
      radio: { band: { label: 'NR-N77', frequency_mhz: 3600, bandwidth_mhz: 100, numerology: 1 }, tx_power_dbm: 43,
        antenna: { type: 'massive_mimo', azimuth_deg: 270, tilt_deg: 6, gain_dbi: 25, horizontal_beamwidth_deg: 100, vertical_beamwidth_deg: 10,
          massive_mimo: { rows: 8, cols: 8, beamforming: 'hybrid', digital_azimuth_deg: 0, digital_tilt_deg: 0, polarization_deg: 45 } } },
    },
    // ── Site C: low-band coverage layer (NR-N28, 700 MHz) — South ────────────
    {
      id: 'gnb-site-c-s1', name: 'Site-C / N28 / S1 (South Coverage)',
      position: { x: -200, y: -300, z: 35 },
      radio: { band: { label: 'NR-N28', frequency_mhz: 700, bandwidth_mhz: 20, numerology: 0 }, tx_power_dbm: 46,
        antenna: { type: 'cosine', azimuth_deg: 60, tilt_deg: 2, gain_dbi: 15, horizontal_beamwidth_deg: 90, vertical_beamwidth_deg: 12 } },
    },
  ],
  ue_groups: [
    {
      id: 'ue-embb-downtown', name: 'eMBB — Downtown Pedestrians',
      count: 80, spawn_position: { x: 80, y: 80 },
      mobility: { pattern: 'pedestrian', speed_mps: 1.4 },
      services: [{ slice_type: 'eMBB', qos_id: 8, target_throughput_gbps: 0.1 }],
    },
    {
      id: 'ue-urllc-industrial', name: 'uRLLC — Industrial IoT (East Zone)',
      count: 40, spawn_position: { x: 390, y: 190 },
      mobility: { pattern: 'static', speed_mps: 0 },
      services: [{ slice_type: 'uRLLC', qos_id: 1, target_throughput_gbps: 0.005 }],
    },
    {
      id: 'ue-embb-vehicle', name: 'eMBB — Vehicle Users (Ring Road)',
      count: 20, spawn_position: { x: -150, y: -200 },
      mobility: { pattern: 'vehicle', speed_mps: 13.9 },
      services: [{ slice_type: 'eMBB', qos_id: 8, target_throughput_gbps: 0.05 }],
    },
  ],
  buildings: [
    { id: 'bldg-office-a',
      bounds: { x_min: 80,   x_max: 220,  y_min: 60,   y_max: 150,  z_min: 0, z_max: 40 },
      building_type: 'office',      wall_material: 'concrete_with_windows', floors: 10, rooms_x: 4, rooms_y: 4 },
    { id: 'bldg-residential-a',
      bounds: { x_min: -200, x_max: -80,  y_min: 80,   y_max: 180,  z_min: 0, z_max: 24 },
      building_type: 'residential', wall_material: 'concrete_with_windows', floors: 6,  rooms_x: 3, rooms_y: 5 },
    { id: 'bldg-industrial-a',
      bounds: { x_min: 300,  x_max: 490,  y_min: 120,  y_max: 270,  z_min: 0, z_max: 18 },
      building_type: 'industrial',  wall_material: 'concrete',              floors: 2,  rooms_x: 6, rooms_y: 6 },
    { id: 'bldg-office-b',
      bounds: { x_min: 100,  x_max: 230,  y_min: -130, y_max: -40,  z_min: 0, z_max: 32 },
      building_type: 'office',      wall_material: 'concrete',              floors: 8,  rooms_x: 4, rooms_y: 3 },
    { id: 'bldg-residential-b',
      bounds: { x_min: -160, x_max: -60,  y_min: -230, y_max: -140, z_min: 0, z_max: 16 },
      building_type: 'residential', wall_material: 'wood',                  floors: 4,  rooms_x: 3, rooms_y: 4 },
  ],
  area: { x_min: -600, x_max: 800, y_min: -600, y_max: 500, resolution_m: 10 },
  settings: {
    duration_s: 60, report_interval_ms: 1000, scenario: 'UMa-Buildings',
    shadowing_enabled: true, random_seed: 42,
  },
};

// ── Store shape ───────────────────────────────────────────────────────────────

interface NSGStore {
  // ── Config state ────────────────────────────────────────────────────────────
  /** The live, accepted configuration. null until the first LLM response is accepted. */
  config: NSGSimulationConfig | null;
  /** LLM-proposed config pending user approval. */
  proposedConfig: NSGSimulationConfig | null;
  /** Controls whether the ConfigDiffViewer modal is open. */
  isDiffOpen: boolean;

  /** Which tab the preview pane shows. Here rather than in the pane's local
   *  state so the chat can send the user to the config editor. */
  activeTab: string;
  /** Chosen partition shape (processes to split the scenario across).
   *  Here rather than in the panel's own state because the panel unmounts —
   *  on a tab change, and again on the re-render after Apply — and a choice
   *  that lives for as long as the component silently snaps back to the
   *  recommendation. null means "not chosen yet, use the recommendation". */
  partitionGroups: number | null;
  /**
   * A brand-new scenario answers every parameter, and most of those answers are
   * schema defaults the user never asked for. True from the moment the first
   * config lands until the user has either looked at them or waved them through.
   */
  configReviewPending: boolean;
  /** Bumped each time the chat asks the editor to open its review filter. */
  configReviewSignal: number;

  // ── Chat state ───────────────────────────────────────────────────────────────
  messages: ChatMessage[];
  /** True while waiting for the /ai/modify response. */
  isModifying: boolean;

  // ── Simulation state ─────────────────────────────────────────────────────────
  /** True while waiting for the /run-simulation response. */
  isSimulating: boolean;
  simulationResult: SimulationRunResult | null;
  /** REM coverage points from the last successful run. [] until a simulation completes. */
  heatmapData: RemPoint[];

  // ── Live Traffic state ───────────────────────────────────────────────────────
  /** Ring buffer of recent live-traffic ticks (capped at LIVE_TRAFFIC_MAX_POINTS). */
  liveTrafficData: LiveTrafficFrame[];
  liveTrafficStatus: 'idle' | 'connecting' | 'streaming' | 'stopped' | 'error';
  /** Why, when liveTrafficStatus === 'error' (the backend's error frame message). */
  liveTrafficError: string | null;

  // ── RIC Test state (simplified E2) ────────────────────────────────────────────
  /** Latest KPM report from /ws/e2. null until first frame arrives. */
  ricKpm: RicKpmReport | null;
  ricStatus: 'idle' | 'connecting' | 'connected' | 'stopped' | 'error';
  /** Control-command history (most recent last, capped at RIC_LOG_MAX). */
  ricControlLog: RicControlLogEntry[];
  /** Session id assigned by the server — an external xApp joins this to co-drive. */
  ricSessionId: string | null;
  /** Labels of clients currently connected to the session (browser + any xApps). */
  ricRoster: string[];
  /** Whether the built-in LLM-driven AI xApp is currently running on the session. */
  ricAiRunning: boolean;
  /** The goal the AI xApp is optimising toward. */
  ricAiGoal: string;
  /** Scored time-series of the CURRENT run (accumulates from KPM frames). */
  ricRunSeries: RunPoint[];
  /** Saved runs, for side-by-side AI performance comparison. */
  ricSavedRuns: SavedRun[];
  /** Real ns-O-RAN engine (FlexRIC + ns-3 mmWave, real E2AP/SCTP) validation result. */
  nsOranKpm: NsOranKpm | null;
  nsOranValidating: boolean;
  nsOranError: string | null;
  /**
   * Per-saved-run real-engine validation: each AI's end-state replayed on
   * ns-O-RAN, keyed by run id. This is the "winner on real data" — the same
   * comparison as the analytical leaderboard, but scored by the real engine.
   */
  nsOranRuns: Record<string, { validating: boolean; kpm: NsOranKpm | null; error: string | null }>;

  // ── REAL-engine LIVE session (/ws/e2-real) ─────────────────────────────────────
  // A live session on the REAL ns-O-RAN engine: the ns-3 RAN runs while an xApp
  // (browser here, or the customer's own) drives real E2SM-RC control. Separate
  // from the analytical RIC test above — this is the real hardware-grade loop.
  ricRealStatus: 'idle' | 'connecting' | 'connected' | 'stopped' | 'error';
  ricRealKpm: NsOranLiveFrame | null;   // latest live KPM indication (tick, ue_ids)
  /** Last-known value per metric, merged across frames — so headline KPIs don't
   *  flicker to "—" on indications that omit a given measurement. */
  ricRealMetrics: Record<string, { count: number; min: number; max: number; mean: number }>;
  /** Per-UE live rows merged by IMSI (a stable per-UE table across sparse indications). */
  ricRealUes: Record<number, NsOranLiveUe>;
  /** Scored time-series of the CURRENT live real run (accumulates from KPM frames). */
  /** Whether the built-in AI xApp is driving the REAL engine. */
  ricRealAiRunning: boolean;
  ricRealSeries: RunPoint[];
  /** Peak cells/UEs seen this run — the denominator for the energy/reliability terms. */
  ricRealPeak: { cells: number; ues: number };
  ricRealLog: RicRealLogEntry[];        // session boot + control echoes
  ricRealControlCount: number;          // control commands sent this session
  /** Id an external xApp uses to JOIN this live run (same engine session). */
  ricRealSessionId: string | null;
  /** Everyone attached to the live session: the operator plus any joined xApp. */
  ricRealClients: string[];
  /** The shape the engine CONFIRMED for the live real session, or null when whole. */
  ricRealPartition: { groups: number; cellsPerGroup: number; handover: string } | null;

  // ── Actions ──────────────────────────────────────────────────────────────────

  /**
   * Send the user's prompt to /ai/modify.
   * On success: opens the diff modal with the proposed change.
   * On failure: pushes an error message to the chat.
   */
  requestModification: (prompt: string) => Promise<void>;

  /** Accept the proposed config — replaces the live config and closes the modal. */
  acceptProposal: () => void;

  /** Discard the proposed config — leaves the live config unchanged. */
  rejectProposal: () => void;

  /** Open the diff modal for the current proposal (does not create a new proposal). */
  openDiff: () => void;

  /** Switch the preview pane to a tab. */
  setActiveTab: (tab: string) => void;
  setPartitionGroups: (groups: number | null) => void;

  /** Send the user to the config editor, filtered to the key parameters that
   *  are still on their default value. */
  openConfigReview: () => void;

  /** "The defaults are fine" — stop offering the review for this scenario. */
  dismissConfigReview: () => void;

  /**
   * Overwrite the live config directly. For INCREMENTAL edits to a config that
   * is already valid — a canvas drag, a position nudge — where a round trip per
   * mouse-move would be intolerable and no new structure is introduced.
   *
   * For a whole config arriving from OUTSIDE, use loadConfig instead.
   */
  setConfig: (config: NSGSimulationConfig) => void;

  /**
   * Adopt a whole config that came from outside the app — a saved scenario, a
   * preset — after the backend has completed and validated it.
   *
   * The store feeds the canvas, which reads nested fields directly and dies on a
   * missing one. A scenario saved before a schema field existed is exactly that
   * shape, and localStorage keeps it forever. Normalising on the way in is what
   * makes an old save as trustworthy as a fresh generation. Falls back to the raw
   * object if the backend is unreachable — refusing to load would be a worse
   * failure than loading what we already had.
   */
  loadConfig: (raw: unknown) => Promise<void>;

  /** Run the current config through ns3-dev-rem-gen. */
  runSimulation: () => Promise<SimulationRunResult>;

  /** Directly overwrite the heatmap (rarely needed — runSimulation sets this automatically). */
  setHeatmapData: (data: RemPoint[]) => void;

  /**
   * Open the /ws/live-traffic WebSocket, send the current config, and stream
   * incoming frames into liveTrafficData. No-op if already connected or if
   * there's no config to simulate.
   */
  startLiveTraffic: () => void;

  /** Close the live-traffic WebSocket. Idempotent — safe to call when already stopped. */
  stopLiveTraffic: () => void;

  /**
   * Open the /ws/e2 RIC-test WebSocket and send the current config. Streams KPM
   * reports into ricKpm; control acks are appended to ricControlLog.
   */
  startRicTest: () => void;

  /** Close the RIC-test WebSocket. Idempotent. */
  stopRicTest: () => void;

  /** Send one RC control command to the RIC-test sim (no-op if not connected). */
  sendRicControl: (cmd: RicControlCommand) => void;

  /** Inject an operator anomaly (cell fault / traffic surge) into the sim. */
  injectAnomaly: (cmd: RicAnomalyCommand) => void;

  /** Run the REAL ns-O-RAN engine (real E2AP/SCTP) validation (highest fidelity). */
  runNsOranValidation: () => Promise<void>;

  /**
   * Start/stop the AI xApp on the current session. Pass `llmConfig` to use the
   * user's own OpenAI-compatible model instead of the built-in one.
   */
  setRicAiXapp: (
    enabled: boolean,
    goal?: string,
    llmConfig?: { model: string; base_url?: string; api_key: string },
  ) => void;

  /** Snapshot the current run into the comparison set under `label`, then reset it. */
  saveRicRun: (label: string) => void;
  /**
   * Replay saved runs' AI end-states on the REAL ns-O-RAN engine and rank them
   * by real KPM. Sequential (one real run at a time, ~1 min each). Pass ids to
   * validate a subset; omit to (re)validate every run that captured an end-state.
   */
  validateSavedRunsOnNsOran: (ids?: string[]) => Promise<void>;
  /** Clear the current run series (start a fresh measurement). */
  clearRicRun: () => void;
  /** Remove all saved comparison runs. */
  clearRicSavedRuns: () => void;

  /** Open the /ws/e2-real WebSocket — a LIVE session on the REAL ns-O-RAN engine.
   *  Streams live KPM frames into ricRealKpm; session/control lines into ricRealLog. */
  startRicRealTest: () => void;
  /** Close the real-engine live session. Idempotent. */
  stopRicRealTest: () => void;
  /** Send one control command to the real engine (fires real E2SM-RC on the live RAN). */
  sendRicRealControl: (cmd?: Record<string, unknown>) => void;
  /** Operator-inject a cell fault on the live real engine (the xApp must route around it). */
  injectRicRealFault: (cell: number) => void;
  /** Start/stop the built-in AI xApp ON THE REAL ENGINE. */
  setRicRealAi: (enabled: boolean, goal?: string) => void;
  /** Snapshot the current LIVE REAL run into the comparison set, then reset it. */
  saveRicRealRun: (label: string) => void;

  /** Reset the session: clear config, chat history, and reinitialise the backend agent. */
  resetSession: () => Promise<void>;
}

// ── Store implementation ──────────────────────────────────────────────────────

export const useNSGStore = create<NSGStore>()(
  immer((set, get) => ({

    config:           null,
    proposedConfig:   null,
    isDiffOpen:       false,
    activeTab:            'visual',
    partitionGroups:      null,
    configReviewPending:  false,
    configReviewSignal:   0,
    messages:         [],
    isModifying:      false,
    isSimulating:     false,
    simulationResult: null,
    heatmapData:      [],
    liveTrafficData:   [],
    liveTrafficStatus: 'idle',
    liveTrafficError: null,
    ricKpm:            null,
    ricStatus:         'idle',
    ricControlLog:     [],
    ricSessionId:      null,
    ricRoster:         [],
    ricAiRunning:      false,
    ricAiGoal:         '',
    ricRunSeries:      [],
    ricSavedRuns:      [],
    nsOranKpm:         null,
    nsOranValidating:  false,
    nsOranError:       null,
    nsOranRuns:        {},
    ricRealStatus:       'idle',
    ricRealKpm:          null,
    ricRealMetrics:      {},
    ricRealUes:          {},
    ricRealAiRunning:    false,
    ricRealSeries:       [],
    ricRealPeak:         { cells: 0, ues: 0 },
    ricRealLog:          [],
    ricRealControlCount: 0,
    ricRealSessionId:    null,
    ricRealClients:      [],
    ricRealPartition:    null,

    // ── requestModification ────────────────────────────────────────────────────
    requestModification: async (prompt) => {
      const { config, messages } = get();
      const isFirstCreation = config === null;

      // Add user message to chat immediately for responsive UX
      set(s => {
        s.isModifying = true;
        s.messages.push({
          id: crypto.randomUUID(),
          role: 'user',
          content: prompt,
          timestamp: new Date(),
        });
      });

      try {
        // Send only user-role turns as history — the full currentConfig already
        // captures all previous state; assistant JSON blobs would waste tokens.
        const historyForBackend = messages
          .filter(m => m.role === 'user')
          .map(m => ({ role: m.role, content: m.content }));

        const proposed = await modifyConfig(
          config ?? DEFAULT_CONFIG,
          prompt,
          historyForBackend,
        );

        if (isFirstCreation) {
          // Nothing to diff against — DEFAULT_CONFIG is an internal template the
          // user never asked for and never saw. Apply directly so the canvas
          // populates immediately; skip the review modal entirely.
          set(s => {
            s.config         = proposed;
            s.proposedConfig = null;
            s.isDiffOpen     = false;
            s.isModifying    = false;
            // Everything the prompt did not specify was answered by a default.
            // Offer that fact once, up front, while it is still cheap to change
            // — finding it out after a run has already been scored is not.
            s.configReviewPending = true;
            s.messages.push({
              id:        crypto.randomUUID(),
              role:      'assistant',
              content:   summariseNewConfig(proposed),
              timestamp: new Date(),
            });
          });
        } else {
          const summary = summariseDiff(config ?? DEFAULT_CONFIG, proposed);

          set(s => {
            s.proposedConfig = proposed;
            s.isDiffOpen     = false;  // user opens the diff manually via "Review Changes"
            s.isModifying    = false;
            s.messages.push({
              id:        crypto.randomUUID(),
              role:      'assistant',
              content:   summary,
              // Attach the full config as meta — ConfigDiffViewer reads it from
              // the store directly, so this is just for history serialisation.
              meta:      { proposed_config: proposed },
              timestamp: new Date(),
            });
          });
        }

      } catch (err) {
        const raw = err instanceof Error ? err.message : String(err);
        // Parse structured error from the backend if available
        let friendly = raw;
        try {
          const parsed = JSON.parse(raw);
          if (parsed?.message) {
            friendly = parsed.message;
            if (Array.isArray(parsed?.errors) && parsed.errors.length > 0) {
              const fields = parsed.errors
                .slice(0, 3)
                .map((e: { loc?: unknown[]; msg?: string }) =>
                  `${(e.loc ?? []).slice(-2).join('.')}: ${e.msg ?? ''}`
                )
                .join('; ');
              friendly += ` (${fields})`;
            }
          }
        } catch {
          // raw message was not JSON — use as-is
        }
        set(s => {
          s.isModifying = false;
          s.messages.push({
            id:        crypto.randomUUID(),
            role:      'assistant',
            content:   `Something went wrong: ${friendly}`,
            timestamp: new Date(),
          });
        });
      }
    },

    // ── acceptProposal ─────────────────────────────────────────────────────────
    acceptProposal: () => {
      set(s => {
        s.config        = s.proposedConfig;
        s.proposedConfig = null;
        s.isDiffOpen    = false;
      });
    },

    // ── rejectProposal ─────────────────────────────────────────────────────────
    rejectProposal: () => {
      set(s => {
        s.proposedConfig = null;
        s.isDiffOpen    = false;
      });
    },

    // ── openDiff ───────────────────────────────────────────────────────────────
    openDiff: () => {
      set(s => { s.isDiffOpen = true; });
    },

    // ── config review ──────────────────────────────────────────────────────────
    setActiveTab: (tab) => {
      set(s => { s.activeTab = tab; });
    },

    setPartitionGroups: (groups) => {
      set(s => { s.partitionGroups = groups; });
    },

    openConfigReview: () => {
      set(s => {
        s.activeTab           = 'config';
        s.configReviewSignal += 1;
        s.configReviewPending = false;
      });
    },

    dismissConfigReview: () => {
      set(s => { s.configReviewPending = false; });
    },

    // ── setConfig ──────────────────────────────────────────────────────────────
    setConfig: (config) => {
      set(s => { s.config = config; });
    },

    // ── loadConfig ─────────────────────────────────────────────────────────────
    loadConfig: async (raw) => {
      try {
        const { config } = await normalizeConfig(raw);
        set(s => { s.config = config; });
      } catch (err) {
        // Backend down, or the stored scenario is genuinely invalid. Load it as
        // it is: the user asked for their scenario, and this is no worse than
        // the behaviour before normalisation existed.
        console.warn('config could not be normalised, loading as stored', err);
        set(s => { s.config = raw as NSGSimulationConfig; });
      }
    },

    // ── runSimulation ──────────────────────────────────────────────────────────
    runSimulation: async () => {
      const { config } = get();
      if (!config) throw new Error('No configuration loaded. Generate a config first.');

      set(s => { s.isSimulating = true; });

      try {
        const result = await runSimulation(config);
        set(s => {
          s.simulationResult = result;
          s.heatmapData      = result.heatmap ?? [];
          s.isSimulating     = false;
        });
        return result;
      } catch (err) {
        set(s => { s.isSimulating = false; });
        throw err;
      }
    },

    // ── setHeatmapData ─────────────────────────────────────────────────────────
    setHeatmapData: (data) => {
      set(s => { s.heatmapData = data; });
    },

    // ── startLiveTraffic ───────────────────────────────────────────────────────
    // NOTE (2026-09-10): nothing in the UI calls this any more. The Analytics tab
    // had a Live Traffic view beside the coverage map; Campaign answers the same
    // question better — a run's traffic over time, scored against a criterion —
    // so that view and its chart were removed. The socket lifecycle is left
    // intact rather than deleted: it works, and re-exposing it is one component.
    startLiveTraffic: () => {
      const { config } = get();
      if (!config || liveTrafficSocket) return;
      // Single-session engine: a running RIC live session would make this
      // stream collide as [BUSY] — stop it first (same rule in reverse there).
      if (ricRealSocket) get().stopRicRealTest();

      set(s => {
        s.liveTrafficStatus = 'connecting';
        s.liveTrafficError  = null;
        s.liveTrafficData   = [];
      });

      const ws = new WebSocket(wsUrl('/ws/live-traffic'));
      liveTrafficSocket = ws;

      ws.onopen = () => {
        ws.send(JSON.stringify(config));
        set(s => { s.liveTrafficStatus = 'streaming'; });
      };

      ws.onmessage = (event) => {
        let payload: any;
        try {
          payload = JSON.parse(event.data);
        } catch {
          return; // malformed frame — drop it, don't crash the stream
        }

        if (payload?.type === 'error') {
          console.error('Live traffic stream error:', payload.message, payload.errors);
          set(s => {
            s.liveTrafficStatus = 'error';
            s.liveTrafficError = typeof payload.message === 'string' ? payload.message : null;
          });
          return;
        }

        set(s => {
          s.liveTrafficData.push(payload as LiveTrafficFrame);
          if (s.liveTrafficData.length > LIVE_TRAFFIC_MAX_POINTS) {
            s.liveTrafficData.shift();
          }
        });
      };

      ws.onerror = () => {
        set(s => { s.liveTrafficStatus = 'error'; });
      };

      ws.onclose = () => {
        liveTrafficSocket = null;
        // Don't clobber an 'error' status that was already set by onerror/onmessage.
        set(s => {
          if (s.liveTrafficStatus !== 'error') s.liveTrafficStatus = 'stopped';
        });
      };
    },

    // ── stopLiveTraffic ────────────────────────────────────────────────────────
    stopLiveTraffic: () => {
      liveTrafficSocket?.close();
      liveTrafficSocket = null;
      set(s => { s.liveTrafficStatus = 'stopped'; });
    },

    // ── startRicTest ───────────────────────────────────────────────────────────
    startRicTest: () => {
      const { config } = get();
      if (!config || ricSocket) return;

      set(s => {
        s.ricStatus = 'connecting';
        s.ricKpm = null;
        s.ricControlLog = [];
        s.ricSessionId = null;
        s.ricRoster = [];
        s.ricAiRunning = false;
        s.ricRunSeries = [];  // fresh run measurement; saved runs persist for comparison
      });

      const ws = new WebSocket(wsUrl('/ws/e2'));
      ricSocket = ws;

      ws.onopen = () => {
        // Create a session as the "browser" client; an external xApp joins its id.
        ws.send(JSON.stringify({ config, label: 'browser' }));
        set(s => { s.ricStatus = 'connected'; });
      };

      ws.onmessage = (event) => {
        let payload: any;
        try {
          payload = JSON.parse(event.data);
        } catch {
          return;
        }

        if (payload?.type === 'kpm') {
          const point = scoreKpm(payload as RicKpmReport);
          set(s => {
            s.ricKpm = payload as RicKpmReport;
            s.ricRunSeries.push(point);
            if (s.ricRunSeries.length > 300) s.ricRunSeries.shift();
          });
        } else if (payload?.type === 'session') {
          set(s => { s.ricSessionId = payload.session_id; });
        } else if (payload?.type === 'roster') {
          set(s => { s.ricRoster = payload.clients ?? []; });
        } else if (payload?.type === 'ai_status') {
          set(s => {
            s.ricAiRunning = !!payload.running;
            if (payload.goal) s.ricAiGoal = payload.goal;
          });
        } else if (payload?.type === 'control_event') {
          // Any control action on the shared session (from us OR an external
          // xApp) — log it with its source so the browser observes the AI.
          set(s => {
            s.ricControlLog.push({
              id: crypto.randomUUID(),
              command: payload.command,
              ack: payload.ack,
              source: payload.source ?? 'unknown',
              timestamp: new Date(),
            });
            if (s.ricControlLog.length > RIC_LOG_MAX) s.ricControlLog.shift();
          });
        } else if (payload?.type === 'anomaly_event') {
          // Operator injected a fault / surge — log it alongside control so the
          // timeline shows what the AI was reacting to. Tagged 'fault'.
          set(s => {
            s.ricControlLog.push({
              id: crypto.randomUUID(),
              command: payload.command,
              ack: payload.ack,
              source: 'fault',
              timestamp: new Date(),
            });
            if (s.ricControlLog.length > RIC_LOG_MAX) s.ricControlLog.shift();
          });
        } else if (payload?.type === 'error') {
          console.error('RIC test stream error:', payload.message, payload.errors);
          set(s => { s.ricStatus = 'error'; });
        }
        // control_ack (direct reply to our own command) is intentionally ignored:
        // the control_event broadcast already logs it, avoiding a double entry.
      };

      ws.onerror = () => {
        set(s => { s.ricStatus = 'error'; });
      };

      ws.onclose = () => {
        ricSocket = null;
        set(s => { if (s.ricStatus !== 'error') s.ricStatus = 'stopped'; });
      };
    },

    // ── stopRicTest ────────────────────────────────────────────────────────────
    stopRicTest: () => {
      ricSocket?.close();
      ricSocket = null;
      set(s => { s.ricStatus = 'stopped'; });
    },

    // ── sendRicControl ─────────────────────────────────────────────────────────
    sendRicControl: (cmd) => {
      if (!ricSocket || ricSocket.readyState !== WebSocket.OPEN) return;
      // No optimistic logging — the server echoes every control as a
      // control_event broadcast, which is what populates the log (so browser
      // and xApp see a single consistent history).
      ricSocket.send(JSON.stringify(cmd));
    },

    // ── injectAnomaly ──────────────────────────────────────────────────────────
    injectAnomaly: (cmd) => {
      if (!ricSocket || ricSocket.readyState !== WebSocket.OPEN) return;
      // Server echoes an anomaly_event broadcast which populates the log.
      ricSocket.send(JSON.stringify({ inject: cmd }));
    },

    // ── startRicRealTest (REAL engine, /ws/e2-real) ──────────────────────────────
    startRicRealTest: () => {
      if (ricRealSocket) return;
      // The engine runs ONE live session (shared lock): starting the RIC live
      // session while Live Traffic streams would silently collide as [BUSY].
      // Stop the other consumer first instead of letting the user hit a wall.
      if (liveTrafficSocket) get().stopLiveTraffic();
      set(s => {
        s.ricRealStatus = 'connecting';
        s.ricRealKpm = null;
        s.ricRealMetrics = {};
        s.ricRealUes = {};
        s.ricRealAiRunning = false;
        s.ricRealSeries = [];
        s.ricRealPeak = { cells: 0, ues: 0 };
        s.ricRealLog = [];
        s.ricRealControlCount = 0;
        s.ricRealSessionId = null;
        s.ricRealClients = [];
        s.ricRealPartition = null;
      });
      const ws = new WebSocket(wsUrl('/ws/e2-real'));
      ricRealSocket = ws;

      ws.onopen = () => {
        // First message = the scenario config, so the real session runs the user's
        // actual design (cell/UE count, band, scale, positions, power). Always sent
        // (null config → engine default) so the backend never waits on it.
        //
        // The shape chosen in Sim Options rides along. A live session is where the
        // horizon hurts most — the operator watches a clock advancing at a few
        // percent of real time — and at three or more cells per group the split
        // costs no handovers, which is what a RIC session exists to exercise.
        // A shape that does not divide this scenario's cells is not sent: the
        // backend would run whole anyway, and saying so here keeps the request
        // honest about what was asked for.
        const cfg = get().config;
        const cells = cfg?.base_stations?.length ?? 0;
        const wanted = get().partitionGroups ?? 1;
        const groups = (wanted > 1 && cells > 0 && cells % wanted === 0) ? wanted : 1;
        ws.send(JSON.stringify({ config: cfg, partition_groups: groups }));
        set(s => { s.ricRealStatus = 'connected'; });
      };
      ws.onmessage = (event) => {
        let p: any;
        try { p = JSON.parse(event.data); } catch { return; }
        if (p?.type === 'kpm') {
          set(s => {
            s.ricRealKpm = p as NsOranLiveFrame;
            // Merge so headline KPIs + the per-UE table keep last-known values
            // across sparse indications (some ticks report only a subset of UEs).
            for (const [k, v] of Object.entries(p.measurements ?? {})) {
              s.ricRealMetrics[k] = v as { count: number; min: number; max: number; mean: number };
            }
            for (const u of ((p.ues ?? []) as NsOranLiveUe[])) s.ricRealUes[u.imsi] = u;
            // Score the live real frame onto the same RunPoint shape as the twin,
            // so a real run ranks in the SAME comparison leaderboard.
            s.ricRealPeak.cells = Math.max(s.ricRealPeak.cells, (p.cells ?? []).length);
            s.ricRealPeak.ues = Math.max(s.ricRealPeak.ues, (p.ues ?? []).length);
            s.ricRealSeries.push(scoreRealFrame(p as NsOranLiveFrame, s.ricRealPeak.cells, s.ricRealPeak.ues));
            if (s.ricRealSeries.length > 300) s.ricRealSeries.shift();
          });
        } else if (p?.type === 'live_session') {
          // id an external xApp uses to JOIN this run (same engine session)
          set(s => { s.ricRealSessionId = p.session_id ?? null; });
        } else if (p?.type === 'live_partition') {
          // The engine confirms the shape it actually started. Kept separate
          // from what was REQUESTED: the backend runs whole when the shape does
          // not divide the cells, and a session that silently did something
          // else is the one thing this feature must not do.
          set(s => {
            s.ricRealPartition = { groups: p.groups, cellsPerGroup: p.cells_per_group,
                                   handover: p.handover };
          });
        } else if (p?.type === 'live_roster') {
          set(s => { s.ricRealClients = p.clients ?? []; });
        } else if (p?.type === 'live_status') {
          const line: string = p.line ?? p.state ?? '';
          set(s => {
            s.ricRealLog.push({
              id: crypto.randomUUID(),
              line,
              kind: line.includes('ctrl cmd rx') ? 'control' : 'status',
              ts: Date.now(),
            });
            if (s.ricRealLog.length > 120) s.ricRealLog.shift();
          });
        } else if (p?.type === 'error') {
          set(s => {
            s.ricRealStatus = 'error';
            s.ricRealLog.push({ id: crypto.randomUUID(), line: p.message ?? 'error', kind: 'error', ts: Date.now() });
          });
        }
      };
      ws.onerror = () => set(s => { s.ricRealStatus = 'error'; });
      ws.onclose = () => {
        ricRealSocket = null;
        set(s => { if (s.ricRealStatus !== 'error') s.ricRealStatus = 'stopped'; });
      };
    },

    // ── stopRicRealTest ──────────────────────────────────────────────────────────
    stopRicRealTest: () => {
      ricRealSocket?.close();
      ricRealSocket = null;
      set(s => { if (s.ricRealStatus !== 'error') s.ricRealStatus = 'stopped'; });
    },

    // ── sendRicRealControl ───────────────────────────────────────────────────────
    sendRicRealControl: (cmd) => {
      if (!ricRealSocket || ricRealSocket.readyState !== WebSocket.OPEN) return;
      // Any command fires a real E2SM-RC CONTROL on the live RAN. The action is
      // fixed (QoS flow mapping) for now; the payload is carried for future
      // parameterised control. The live xApp echoes it back as a status line.
      ricRealSocket.send(JSON.stringify(cmd ?? { cmd: 'rc_qos', by: 'browser' }));
      set(s => { s.ricRealControlCount += 1; });
    },

    // ── injectRicRealFault ───────────────────────────────────────────────────────
    // Operator kills a cell on the LIVE real engine. Same actuator as cell_off, but
    // the backend marks it FAULTED: the xApp must route around it and cannot wake it.
    injectRicRealFault: (cell) => {
      if (!ricRealSocket || ricRealSocket.readyState !== WebSocket.OPEN) return;
      ricRealSocket.send(JSON.stringify({ inject: { cell } }));
    },

    // ── setRicRealAi ─────────────────────────────────────────────────────────────
    // Start/stop the built-in LLM xApp ON THE REAL ENGINE (it reads the live KPM and
    // fires real E2SM-RC). Same contract as the analytical sandbox's AI.
    setRicRealAi: (enabled, goal) => {
      if (!ricRealSocket || ricRealSocket.readyState !== WebSocket.OPEN) return;
      ricRealSocket.send(JSON.stringify({ ai_xapp: { enabled, goal } }));
    },

    // ── saveRicRealRun ───────────────────────────────────────────────────────────
    // Snapshot the LIVE REAL run into the SAME comparison set as the sandbox runs
    // (scored onto the same RunPoint shape), so AIs rank head-to-head on real data.
    saveRicRealRun: (label) => {
      set(s => {
        if (s.ricRealSeries.length === 0) return;
        const run = summariseRun(crypto.randomUUID(), label, [...s.ricRealSeries]);
        run.source = 'real';
        s.ricSavedRuns.push(run);
        s.ricRealSeries = [];   // fresh measurement for the next AI
      });
    },

    // ── runNsOranValidation ─────────────────────────────────────────────────────
    runNsOranValidation: async () => {
      const { config, partitionGroups } = get();
      if (!config) return;
      set(s => { s.nsOranValidating = true; s.nsOranError = null; s.nsOranKpm = null; });
      try {
        // The shape chosen in Sim Options. null means the user never chose, which
        // is one process — the adviser's recommendation is shown as a default in
        // the panel, not applied behind their back.
        const { kpm } = await validateNsOran(config, 20, partitionGroups ?? 1);
        set(s => { s.nsOranKpm = kpm; s.nsOranValidating = false; });
      } catch (e) {
        set(s => { s.nsOranError = e instanceof Error ? e.message : String(e); s.nsOranValidating = false; });
      }
    },

    // ── setRicAiXapp ───────────────────────────────────────────────────────────
    setRicAiXapp: (enabled, goal, llmConfig) => {
      if (!ricSocket || ricSocket.readyState !== WebSocket.OPEN) return;
      ricSocket.send(JSON.stringify({ ai_xapp: { enabled, goal, llm: llmConfig } }));
      // Optimistic goal echo; the authoritative running flag comes via ai_status.
      if (goal !== undefined) set(s => { s.ricAiGoal = goal; });
    },

    // ── run comparison ─────────────────────────────────────────────────────────
    saveRicRun: (label) => {
      set(s => {
        if (s.ricRunSeries.length === 0) return;
        // Capture the AI's end-state (per-cell on/off + power) plus the scenario,
        // so this run can later be replayed on the real ns-O-RAN engine.
        const snapshot = s.ricKpm && s.config
          ? { ...captureFinalPowers(s.ricKpm), config: JSON.parse(JSON.stringify(s.config)) as NSGSimulationConfig }
          : undefined;
        s.ricSavedRuns.push(summariseRun(crypto.randomUUID(), label, [...s.ricRunSeries], snapshot));
        s.ricRunSeries = [];  // fresh measurement for the next AI
      });
    },
    clearRicRun: () => set(s => { s.ricRunSeries = []; }),
    clearRicSavedRuns: () => set(s => { s.ricSavedRuns = []; s.nsOranRuns = {}; }),

    // ── validateSavedRunsOnNsOran ──────────────────────────────────────────────
    // Replay each AI's captured end-state on the REAL engine: bake its per-cell
    // power (0 = slept) into the scenario and run ns-O-RAN. Sequential — the
    // engine handles one real run at a time (~1 min each).
    validateSavedRunsOnNsOran: async (ids) => {
      const runs = get().ricSavedRuns.filter(
        r => r.finalPowers && r.config && (!ids || ids.includes(r.id)),
      );
      // Mark all queued up-front so the UI shows the whole batch as pending.
      set(s => { for (const r of runs) s.nsOranRuns[r.id] = { validating: true, kpm: null, error: null }; });
      // The shape chosen in Sim Options applies HERE. This loop is sequential —
      // one real run at a time on one engine — so splitting a run across
      // processes is pure gain, unlike a campaign, which already parallelises
      // across configs on the same cores.
      const wanted = get().partitionGroups ?? 1;
      for (const run of runs) {
        const cfg = JSON.parse(JSON.stringify(run.config)) as NSGSimulationConfig;
        cfg.base_stations.forEach((b, i) => {
          const p = run.finalPowers![i];
          if (typeof p === 'number') b.radio.tx_power_dbm = p;
        });
        try {
          // Each saved run has its own cell count, and the backend refuses a
          // shape that does not divide it. Falling back to whole for those is
          // better than failing the run: the user asked to validate, not to
          // partition, and one config being the wrong size should not take the
          // batch down with it.
          const cells = cfg.base_stations?.length ?? 0;
          const groups = (wanted > 1 && cells > 0 && cells % wanted === 0) ? wanted : 1;
          const { kpm } = await validateNsOran(cfg, 15, groups);
          set(s => { s.nsOranRuns[run.id] = { validating: false, kpm, error: null }; });
        } catch (e) {
          set(s => {
            s.nsOranRuns[run.id] = { validating: false, kpm: null, error: e instanceof Error ? e.message : String(e) };
          });
        }
      }
    },

    // ── resetSession ───────────────────────────────────────────────────────────
    resetSession: async () => {
      liveTrafficSocket?.close();
      liveTrafficSocket = null;
      ricSocket?.close();
      ricSocket = null;
      await resetAgent();
      set(s => {
        s.config              = null;
        s.proposedConfig      = null;
        s.isDiffOpen          = false;
        s.configReviewPending = false;
        s.messages            = [];
        s.isModifying       = false;
        s.isSimulating      = false;
        s.simulationResult  = null;
        s.heatmapData       = [];
        s.liveTrafficData   = [];
        s.liveTrafficStatus = 'idle';
        s.ricKpm            = null;
        s.ricStatus         = 'idle';
        s.ricControlLog     = [];
        s.ricSessionId      = null;
        s.ricRoster         = [];
        s.ricAiRunning      = false;
        s.ricAiGoal         = '';
        s.ricRunSeries      = [];
        s.ricSavedRuns      = [];
      });
    },

  })),
);

// ── Convenience selectors ─────────────────────────────────────────────────────
// Import these in components instead of re-writing the selector inline.

export const selectConfig        = (s: NSGStore) => s.config;
export const selectProposedConfig = (s: NSGStore) => s.proposedConfig;
export const selectIsDiffOpen    = (s: NSGStore) => s.isDiffOpen;
export const selectMessages      = (s: NSGStore) => s.messages;
export const selectActiveTab           = (s: NSGStore) => s.activeTab;
export const selectConfigReviewPending = (s: NSGStore) => s.configReviewPending;
export const selectConfigReviewSignal  = (s: NSGStore) => s.configReviewSignal;
export const selectIsModifying   = (s: NSGStore) => s.isModifying;
export const selectIsSimulating  = (s: NSGStore) => s.isSimulating;
export const selectHeatmapData   = (s: NSGStore) => s.heatmapData;
export const selectLiveTrafficData   = (s: NSGStore) => s.liveTrafficData;
export const selectLiveTrafficStatus = (s: NSGStore) => s.liveTrafficStatus;
export const selectRicKpm            = (s: NSGStore) => s.ricKpm;
export const selectRicStatus         = (s: NSGStore) => s.ricStatus;
export const selectRicControlLog     = (s: NSGStore) => s.ricControlLog;
export const selectRicSessionId      = (s: NSGStore) => s.ricSessionId;
export const selectRicRoster         = (s: NSGStore) => s.ricRoster;
export const selectRicAiRunning      = (s: NSGStore) => s.ricAiRunning;
export const selectRicAiGoal         = (s: NSGStore) => s.ricAiGoal;
export const selectRicRunSeries      = (s: NSGStore) => s.ricRunSeries;
export const selectRicSavedRuns      = (s: NSGStore) => s.ricSavedRuns;
export const selectNsOranKpm         = (s: NSGStore) => s.nsOranKpm;
export const selectNsOranValidating  = (s: NSGStore) => s.nsOranValidating;
export const selectNsOranError       = (s: NSGStore) => s.nsOranError;
export const selectNsOranRuns        = (s: NSGStore) => s.nsOranRuns;
export const selectRicRealStatus       = (s: NSGStore) => s.ricRealStatus;
export const selectRicRealKpm          = (s: NSGStore) => s.ricRealKpm;
export const selectRicRealMetrics      = (s: NSGStore) => s.ricRealMetrics;
export const selectRicRealUes          = (s: NSGStore) => s.ricRealUes;
export const selectRicRealSeries       = (s: NSGStore) => s.ricRealSeries;
export const selectRicRealAiRunning    = (s: NSGStore) => s.ricRealAiRunning;
export const selectRicRealLog          = (s: NSGStore) => s.ricRealLog;
export const selectRicRealControlCount = (s: NSGStore) => s.ricRealControlCount;
