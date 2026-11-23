/**
 * RIC run scoring — turns a KPM report into a single comparable performance
 * point, so different AIs (or manual control) can be benchmarked on the same
 * scenario.
 *
 * QoS is the primary objective; reliability (no outages) gates it; switching
 * cells off grants up to a +50% energy bonus. The energy bonus is multiplicative
 * so it only pays off when QoS and coverage hold — an AI can't win by switching
 * off cells that are actually needed (that drops QoS/reliability, cancelling the
 * bonus). Higher = better. Components are kept so the table can show WHY one AI
 * wins, and the score stays meaningful even when QoS is intrinsically low
 * (heavily congested scenarios where the ceiling is a few percent).
 */
import type { RicKpmReport, NSGSimulationConfig, NsOranLiveFrame, NsOranLiveUe } from '@/types/nsg';

export interface RunPoint {
  tick: number;
  qos: number;       // composite per-UE quality of service (0–100)
  energyPct: number; // % of cells kept active (energy proxy; lower = greener)
  outages: number;   // UE groups with no serving cell
  tput: number;      // total delivered throughput (Mbps)
  score: number;     // qos · reliability · (1 + 0.5·energySaved)
}

const r1 = (x: number) => Math.round(x * 10) / 10;
const clamp01 = (x: number) => Math.max(0, Math.min(1, x));
const sigmoid = (z: number) => 1 / (1 + Math.exp(-z));

/** Max fractional boost from switching every cell off (bounded so QoS leads). */
const ENERGY_BONUS = 0.5;

// ─── Composite QoS math (real engine) ────────────────────────────────────────
// The old real-engine QoS was a single linear map, qos = (SINR+10)/40·100, which
// HARD-FLOORS at 0 below -10 dB. A dense stadium runs at negative wideband SINR,
// so the whole score collapsed to 0 and no AI action could move it (the cliff the
// score chart showed). This replaces it with a smooth, multi-signal quality: each
// UE is scored by what its SLICE cares about (throughput / latency / connectivity),
// gently modulated by SINR, then fairness-aggregated. Everything is continuous, so
// the score moves smoothly instead of stepping.

/** Smooth saturating satisfaction: 0 at zero, ~0.5 at `ref`, asymptotes to 1.
 *  Never floors or steps, unlike a clamped linear map. */
const sat = (value: number, ref: number) => (value <= 0 ? 0 : value / (value + ref));

/** Serving SINR as a GENTLE quality modifier in [0.6, 1]. A UE delivering data at
 *  low wideband SINR (mmWave beamforming lifts the effective link the wideband
 *  measurement never sees) is still mostly credited — never zeroed. Missing SINR
 *  is treated as neutral. */
function sinrModifier(sinr?: number): number {
  if (sinr == null || !Number.isFinite(sinr)) return 0.8;
  return 0.6 + 0.4 * sigmoid((sinr + 5) / 6);
}

/** Per-UE quality (0..1), scored by what the UE's SLICE actually optimises for:
 *  eMBB → throughput, uRLLC/voice → latency (with a throughput floor), mMTC →
 *  being connected at all. Smooth everywhere. */
function ueQuality(u: NsOranLiveUe): number {
  const dl = (u.thp_kbps ?? 0) / 1000; // Mbps
  const slice = (u.slice ?? 'eMBB').toLowerCase();
  const delaySat = (budgetMs: number) =>
    u.delay_ms == null ? sat(dl, 2) : 1 / (1 + (u.delay_ms / budgetMs) ** 2);
  let base: number;
  if (slice.includes('urllc')) base = 0.7 * delaySat(20) + 0.3 * sat(dl, 1);
  else if (slice.includes('voice')) base = 0.6 * delaySat(50) + 0.4 * sat(dl, 0.1);
  else if (slice.includes('mmtc')) base = sat(dl, 0.05);
  else base = sat(dl, 3); // eMBB / default: throughput-led
  return clamp01(base) * sinrModifier(u.sinr);
}

/** Power mean with exponent 0.5 — a fairness-leaning average that rewards lifting
 *  the WORST-served UEs (an AI that balances load beats one that maxes a few and
 *  starves the rest), without the geometric mean's collapse-to-zero on one bad UE. */
function fairMean(xs: number[]): number {
  if (!xs.length) return 0;
  const m = xs.reduce((a, x) => a + Math.sqrt(Math.max(0, x)), 0) / xs.length;
  return m * m;
}

export function scoreKpm(kpm: RicKpmReport): RunPoint {
  const served = kpm.slices.filter(s => s.throughput_mbps > 0);
  const qos = served.length ? served.reduce((a, s) => a + s.qos_score, 0) / served.length : 0;

  // Energy accounting only counts healthy cells: a faulted cell reports inactive
  // but that's not the AI's saving, so it must not earn the energy bonus.
  const healthyCells = kpm.cells.filter(c => !c.faulted);
  const activeCells = healthyCells.filter(c => c.active).length;
  const energyPct = healthyCells.length ? (activeCells / healthyCells.length) * 100 : 100;

  const totalGroups = kpm.ue_groups.length;
  const outages = kpm.ue_groups.filter(u => u.serving_cell === null).length;
  const reliability = totalGroups ? (totalGroups - outages) / totalGroups : 1;

  const tput = kpm.cells.reduce((a, c) => a + c.throughput_mbps, 0);

  const energySaved = (100 - energyPct) / 100; // 0..1, greener = higher
  const score = qos * reliability * (1 + ENERGY_BONUS * energySaved);

  return { tick: kpm.tick, qos: r1(qos), energyPct: r1(energyPct), outages, tput: r1(tput), score: r1(score) };
}

/**
 * Score ONE live frame from the REAL engine, onto the same RunPoint shape as the
 * analytical scorer — so real live runs rank in the SAME leaderboard.
 *
 * QoS is the composite, slice-aware, SINR-softened, fairness-aggregated quality
 * above (not the old SINR-only linear map). Reliability is the share of the run's
 * peak UE count still being served (a UE that loses service drops out of the KPM),
 * and energy is MEASURED WATTS against what the same cells would draw fully
 * active — so sleeping an idle cell earns a partial bonus and switching one
 * off earns the whole of it.
 */
export function scoreRealFrame(frame: NsOranLiveFrame, peakCells: number, peakUes: number): RunPoint {
  const ues = frame.ues ?? [];
  const cells = frame.cells ?? [];

  const tput = ues.reduce((a, u) => a + (u.thp_kbps ?? 0), 0) / 1000; // Mbps

  // Composite QoS: per-UE, slice-aware, SINR-softened, fairness-aggregated.
  const qos = 100 * fairMean(ues.map(ueQuality));

  const served = ues.length;
  const reliability = peakUes > 0 ? Math.min(1, served / peakUes) : 1;
  const outages = Math.max(0, peakUes - served);

  // WATTS, NOT A CELL COUNT. This counted cells drawing more than 50 W, which
  // was a clean on/off test while `power_w` was the RADIATED power — 10 W at
  // 40 dBm, 0 W off. It is now the RU's real draw off the engine's own state
  // currents: about 3 kW transmitting, 680 W idle, 450-580 W asleep, 0 W off.
  // Against a 50 W line every one of those reads "active", so the comment
  // above it — that a slept cell sits near 0 W — stopped being true and sleep
  // silently stopped earning anything.
  //
  // The reference is the busiest cell in this frame times the run's peak cell
  // count: what this network would draw with every cell working. A cell that
  // sleeps, idles or switches off all move it, in proportion to what they
  // actually save. Frames with no power reported fall back to counting cells.
  const drawn = cells.reduce((a, c) => a + (c.power_w ?? 0), 0);
  const busiest = Math.max(0, ...cells.map(c => c.power_w ?? 0));
  const haveWatts = busiest > 0 && cells.some(c => c.power_w !== undefined);
  const energyPct = !haveWatts
    ? (peakCells > 0 ? Math.min(100, (cells.length / peakCells) * 100) : 100)
    : Math.min(100, (drawn / (busiest * Math.max(peakCells, cells.length))) * 100);
  const energySaved = (100 - energyPct) / 100;

  const score = qos * reliability * (1 + ENERGY_BONUS * energySaved);
  return { tick: frame.tick, qos: r1(qos), energyPct: r1(energyPct), outages, tput: r1(tput), score: r1(score) };
}

export interface SavedRun {
  id: string;
  label: string;
  series: RunPoint[];
  /** Where the run came from: the analytical twin, or a LIVE run on the real engine. */
  source?: 'sandbox' | 'real';
  /** Averages over the run, for the comparison table. */
  avgScore: number;
  avgQos: number;
  avgEnergyPct: number;
  totalOutageTicks: number;
  /**
   * The AI's END-STATE, so the run can be re-validated on the REAL ns-O-RAN
   * engine. `finalPowers` is the effective TX power (dBm) per cell in
   * base-station order — a slept/faulted cell is 0, so the real engine
   * reproduces the exact on/off + power pattern the AI left. `config` is the
   * scenario snapshot at save time (same-scenario replay, edit-proof).
   */
  finalPowers?: number[];
  cellNames?: string[];
  config?: NSGSimulationConfig;
}

/** One run's replayable end-state, derived from its last KPM frame. */
export interface RunSnapshot {
  finalPowers: number[];
  cellNames: string[];
  config: NSGSimulationConfig;
}

/** Capture an AI run's end-state from its latest KPM report: each cell's
 *  effective TX power (dBm), 0 when the cell is off (slept or faulted). Aligned
 *  to base-station order so it maps 1:1 onto the config for real-engine replay. */
export function captureFinalPowers(
  kpm: RicKpmReport,
): { finalPowers: number[]; cellNames: string[] } {
  return {
    finalPowers: kpm.cells.map(c => (c.active ? c.tx_power_dbm : 0)),
    cellNames: kpm.cells.map(c => c.name),
  };
}

/**
 * Trim the leading WARM-UP ramp before averaging. A real ns-O-RAN session boots
 * from ~0 throughput and takes tens of ticks for UEs to attach and the RAN to
 * reach steady state; a run saved from the very start therefore averages in that
 * dead ramp and scores unfairly low (the "first save looks worse" the user hit).
 * We drop leading ticks whose throughput is below half the run's MEDIAN — that is
 * exactly the boot ramp — but never more than half the run (guards a genuinely
 * low-throughput steady state), and never for very short runs. The full `series`
 * is still kept for the chart; only the averages use the steady window.
 */
function steadyWindow(series: RunPoint[]): RunPoint[] {
  if (series.length < 6) return series;
  const sorted = series.map(p => p.tput).sort((a, b) => a - b);
  const median = sorted[Math.floor(sorted.length / 2)];
  if (median <= 0) return series;
  const threshold = 0.5 * median;
  let start = series.findIndex(p => p.tput >= threshold);
  if (start < 0) start = 0;
  start = Math.min(start, Math.floor(series.length / 2));
  return start > 0 ? series.slice(start) : series;
}

export function summariseRun(
  id: string,
  label: string,
  series: RunPoint[],
  snapshot?: RunSnapshot,
): SavedRun {
  const eff = steadyWindow(series);
  const n = eff.length || 1;
  return {
    id,
    label,
    series,
    avgScore:  r1(eff.reduce((a, p) => a + p.score, 0) / n),
    avgQos:    r1(eff.reduce((a, p) => a + p.qos, 0) / n),
    avgEnergyPct: r1(eff.reduce((a, p) => a + p.energyPct, 0) / n),
    totalOutageTicks: eff.filter(p => p.outages > 0).length,
    ...(snapshot ?? {}),
  };
}
