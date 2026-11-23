/**
 * NSG AI Platform — Typed API Client
 *
 * All backend calls go through this module.
 * Set NEXT_PUBLIC_API_URL in .env.local to override the default.
 *
 * .env.local example:
 *   NEXT_PUBLIC_API_URL=http://localhost:8000
 */
import type {
  NSGSimulationConfig,
  SimulationRunResult,
  LiveMetricsResponse,
  ChatMessage,
  NsOranKpm,
  CampaignSpec,
  CampaignBrief,
  CampaignRecord,
  CampaignDiff,
  CampaignHistory,
  SimRunBrief,
  SimRunDataset,
} from '@/types/nsg';

import { authHeader, clearToken, getToken } from '@/lib/auth';

const BASE = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

/** Build a ws(s):// URL for a given path, mirroring BASE's http(s) scheme.
 *
 *  The token rides the query string because a browser cannot set headers on a
 *  WebSocket handshake — there is no other option on this side. It is appended
 *  only when there is one, so an unauthenticated deployment produces the same
 *  URL it always did.
 */
export function wsUrl(path: string): string {
  const url = `${BASE.replace(/^http/, 'ws')}${path}`;
  const token = getToken();
  if (!token) return url;
  return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`;
}

/** Build an absolute http(s) URL to the backend for a given path. */
export function apiUrl(path: string): string {
  return `${BASE}${path}`;
}

// ── Internal helper ───────────────────────────────────────────────────────────

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    // Spread init FIRST so an explicit per-call header can still win, but merge
    // rather than replace: the previous version let `init.headers` drop both the
    // content type and the bearer token.
    headers: {
      'Content-Type': 'application/json',
      ...authHeader(),
      ...(init?.headers as Record<string, string> | undefined),
    },
  });

  // An expired or revoked token must not leave the app in a half-logged-in
  // state where every call fails silently. Drop it and send the user to the
  // login screen; when the deployment does not enforce auth this never fires,
  // because those routes do not answer 401.
  if (res.status === 401 && typeof window !== 'undefined') {
    clearToken();
    if (!window.location.pathname.startsWith('/login')) {
      window.location.href = '/login';
    }
  }

  if (!res.ok) {
    // FastAPI returns {"detail": "..."} or {"detail": [{...}]} on errors
    const body = await res.json().catch(() => ({}));
    const message =
      typeof body.detail === 'string'
        ? body.detail
        : JSON.stringify(body.detail ?? `HTTP ${res.status}`);
    throw new Error(message);
  }

  return res.json() as Promise<T>;
}

// ── NS-3 Path ─────────────────────────────────────────────────────────────────

/**
 * Ask the LLM to modify (or create) a simulation configuration.
 *
 * @param currentConfig  The current NSGSimulationConfig as a plain object.
 *                       Pass {} to generate a fresh configuration from scratch.
 * @param prompt         The user's natural-language request.
 * @param history        Optional prior conversation turns for multi-turn context.
 *                       Only user-role messages are meaningful here — assistant
 *                       context is captured in currentConfig itself.
 */
export async function modifyConfig(
  currentConfig: Partial<NSGSimulationConfig> | Record<string, never>,
  prompt: string,
  history: Pick<ChatMessage, 'role' | 'content'>[] = [],
): Promise<NSGSimulationConfig> {
  return request<NSGSimulationConfig>('/ai/modify', {
    method: 'POST',
    body: JSON.stringify({ current_config: currentConfig, prompt, history }),
  });
}

/**
 * Translate config to NS-3 JSON and run the simulation binary.
 * The backend returns the path to the output remPoints.npy file.
 */
export async function runSimulation(
  config: NSGSimulationConfig,
): Promise<SimulationRunResult> {
  return request<SimulationRunResult>('/run-simulation', {
    method: 'POST',
    body: JSON.stringify(config),
  });
}

/**
 * Run the REAL ns-O-RAN engine (FlexRIC + ns-3.42 mmWave over real E2AP/SCTP,
 * containerised) DRIVEN BY `config` (cell/UE count, band, scale) and return its
 * measured 5G KPM (throughput / PRB / SINR). Highest-fidelity tier — takes
 * ~1 min. Not a live feed.
 */
export async function validateNsOran(
  config: NSGSimulationConfig,
  simTime = 20,
  /**
   * Run the scenario as this many processes of whole cells instead of one.
   * 1 (the default) is the unpartitioned run and behaves exactly as before.
   * The backend refuses a count that does not divide the cells, or that needs
   * more processes than the machine has physical cores — see Sim Options for
   * what each shape costs.
   */
  partitionGroups = 1,
): Promise<{ status: string; kpm: NsOranKpm }> {
  return request(
    `/ric/validate-nsoran?sim_time=${simTime}&partition_groups=${partitionGroups}`,
    { method: 'POST', body: JSON.stringify(config) },
  );
}

/**
 * Run a config through the backend schema and get it back COMPLETE — every
 * default filled, every nested model built, every value range-checked.
 *
 * The config editor edits a plain object, so fields pydantic would have created
 * (anything declared with `default_factory`, e.g. `radio.antenna`) are simply
 * absent from what it produces. Normalising before it reaches the store is what
 * keeps a hand-edited config as trustworthy as a generated one.
 */
export async function normalizeConfig(
  config: unknown,
): Promise<{ config: NSGSimulationConfig }> {
  return request('/config/normalize', {
    method: 'POST',
    body: JSON.stringify(config),
  });
}

/**
 * Return the JSON schema for NSGSimulationConfig.
 * Use for frontend form generation or client-side validation.
 */
export async function fetchSchema(): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>('/schema');
}

// ── Live Metrics Path ─────────────────────────────────────────────────────────

/** Poll live KPIs from InfluxDB (populated by the analytics engine). */
export async function getLiveMetrics(): Promise<LiveMetricsResponse> {
  return request<LiveMetricsResponse>('/live-metrics');
}

/** Generate an LLM executive summary from simulation KPIs. */
export async function generateReport(metrics: {
  tput: number;
  power: number;
  prb: number;
  qos: number;
}): Promise<{ status: string; report: string }> {
  return request('/generate-report', {
    method: 'POST',
    body: JSON.stringify(metrics),
  });
}

/** Check whether the analytics engine is online. */
export async function getEngineStatus(): Promise<{ status: string }> {
  return request<{ status: string }>('/engine-status');
}

// ── Batch run datasets ────────────────────────────────────────────────────────

/** Stored batch runs, newest first — metadata only, no frames. */
export async function listSimRuns(): Promise<{ runs: SimRunBrief[] }> {
  return request('/sim/runs');
}

/**
 * The FULL dataset for one run: every KPM window with its per-UE and per-cell
 * rows. Can be large (hundreds of frames), so fetch it only when a run is opened.
 */
export async function getSimRun(runId: string): Promise<SimRunDataset> {
  return request(`/sim/run/${runId}`);
}

/**
 * Every run's full dataset for one campaign, in a single request.
 *
 * The exports need all of them at once. Fetching run by run meant one awaited
 * round-trip per run — twenty-one of them for a three-seed matrix, each pulling
 * a complete frame list — when the server can answer it with one query.
 */
export async function getCampaignDatasets(
  campaignId: string,
): Promise<{ datasets: Record<string, SimRunDataset> }> {
  return request(`/campaigns/${campaignId}/datasets`);
}

// ── Test campaigns ────────────────────────────────────────────────────────────

/**
 * Start a campaign. Returns as soon as it is queued — the runs execute
 * sequentially on the engine (minutes each), so the caller polls getCampaign.
 */
export async function createCampaign(
  spec: CampaignSpec,
): Promise<{ campaign_id: string; status: string; planned_runs: number }> {
  return request('/campaigns', { method: 'POST', body: JSON.stringify(spec) });
}

/** Campaigns newest first, without per-run detail. */
/** `allUsers` is an administrator asking to see other people's campaigns too;
 *  without it the list is the caller's own plus rows that predate auth. */
export async function listCampaigns(allUsers = false): Promise<{ campaigns: CampaignBrief[] }> {
  return request(`/campaigns${allUsers ? '?all=true' : ''}`);
}

/** Full record: thresholds, per-run verdicts and violations. */
export async function getCampaign(id: string): Promise<CampaignRecord> {
  return request(`/campaigns/${id}`);
}

/** Forget a campaign. The underlying run datasets are kept — they are evidence. */
export async function deleteCampaign(id: string): Promise<{ deleted: string }> {
  return request(`/campaigns/${id}`, { method: 'DELETE' });
}

/**
 * Stop a running campaign after its current run. The run on the engine is
 * allowed to finish and be scored; the rest of the queue is abandoned.
 */
export async function stopCampaign(
  id: string,
): Promise<{ campaign_id: string; status: string; stop_requested: boolean }> {
  return request(`/campaigns/${id}/stop`, { method: 'POST' });
}

/**
 * One suite's campaigns lined up in time — scenario × campaign, with the
 * metrics each was judged on. Omit `name` to get the newest campaign's suite.
 */
export async function campaignHistory(
  name?: string,
  limit = 12,
): Promise<CampaignHistory> {
  const q = new URLSearchParams({ limit: String(limit) });
  if (name) q.set('name', name);
  return request(`/campaigns/history?${q}`);
}

/**
 * Diff a campaign against a baseline: per scenario, what moved, by how much,
 * and whether the move is larger than the baseline's own seed-to-seed spread.
 * `baselineId` is the thing being compared AGAINST.
 */
export async function compareCampaigns(
  id: string,
  baselineId: string,
): Promise<CampaignDiff> {
  return request(`/campaigns/${id}/compare?to=${encodeURIComponent(baselineId)}`);
}

// ── Session Management ────────────────────────────────────────────────────────

/** Stop the currently running simulation. */
export async function stopSimulation(): Promise<void> {
  await request('/stop', { method: 'POST' });
}

/** Reinitialise the LLM agent on the backend (clears conversation cache). */
export async function resetAgent(): Promise<void> {
  await request('/reset', { method: 'POST' });
}

/** How many ns-O-RAN engines will actually serve a campaign. A configured
 *  instance that is not running is skipped, which serialises the campaign —
 *  correct, but invisible unless it is shown. */
/** The measured engine envelope, so the canvas can warn while a scenario is
 *  being DRAWN rather than letting the backend clamp it silently at run time. */
/**
 * The measured engine envelope. Pass what is being drawn and the backend also
 * returns the per-cell cap for this bandwidth — an interpolation over measured
 * crash points, not a flat ratio, computed server-side so the warning and the
 * clamp cannot drift apart.
 */
export async function getEnvelope(opts?: {
  bw_mhz?: number; numerology?: number; n_gnb?: number;
}): Promise<{
  gnb_min: number; gnb_max: number;
  ue_min: number; ue_max: number;
  ue_per_cell_min: number; ue_per_cell_max: number;
  bw_mhz_min: number; bw_mhz_max: number;
  min_cell_separation_m: number;
  ue_per_cell_max_for_bw: number | null;
}> {
  const q = new URLSearchParams();
  if (opts?.bw_mhz)    q.set('bw_mhz', String(opts.bw_mhz));
  if (opts?.numerology) q.set('numerology', String(opts.numerology));
  if (opts?.n_gnb)     q.set('n_gnb', String(opts.n_gnb));
  const qs = q.toString();
  return request(qs ? `/envelope?${qs}` : '/envelope');
}

export async function getEnginePool(): Promise<{
  configured: number; alive: number; concurrent_runs: number;
  /** Runs the backend has in flight. An engine mid-run refuses the health probe
   *  exactly as a stopped one does, so this is what tells busy from broken. */
  runs_in_flight?: number;
  instances: { address: string; alive: boolean; state?: 'up' | 'busy' | 'down' }[];
}> {
  return request('/engine-pool');
}
