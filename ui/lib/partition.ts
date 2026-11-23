/**
 * How should this scenario be split across processes?
 *
 * A scenario runs either as one ns-3 process or as N processes of whole cells.
 * Splitting is the only speed lever with real headroom — every single-process
 * lever stacked is about 2.2x — but what it costs depends on the shape, and the
 * backend computes that per scenario rather than handing out a fixed rule.
 *
 * The numbers are estimates from a law fitted to measured runs (~7%), not
 * per-scenario predictions. `caveats` comes back with the response and is meant
 * to be SHOWN, not logged: it carries the traps a user would otherwise walk
 * into, including that a one-cell group's handovers are anchor transitions
 * rather than real ones.
 */
import { authHeader } from '@/lib/auth';
import type { NSGSimulationConfig } from '@/types/nsg';

const BASE = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

export interface PartitionOption {
  groups: number;
  cells_per_group: number;
  /** The BUSIEST partition's UE count — a parallel run is paced by its slowest. */
  ues_per_group: number;
  /** What each partition would actually get. Empty when the backend could not split. */
  ues_split: number[];
  ues_per_cell_max: number;
  /** Busiest partition close to the average? */
  balanced: boolean;
  /** Partitions that would simulate nothing. */
  empty_groups: number;
  /** Busiest partition past the density with a known abort. */
  over_density: boolean;
  /** How much more concentrated than running whole. */
  density_vs_whole: number;
  /** 'measured split' when the real per-partition counts were used. */
  speed_from: string;
  /** Share of each UE's interference this cut LEAVES IN PLACE, 0-100, or null
   *  when the geometry was not available. The number that says whether a split
   *  result describes the same network. */
  interference_kept_pct: number | null;
  /** 'kept' | 'approximate' | 'different network' | 'unknown' */
  fidelity: string;
  speed_x: number;
  /** 'intact' | 'reduced' | 'none' */
  handover: string;
  handover_keep_pct: number;
  /** Needs more processes than the machine has PHYSICAL cores. */
  over_cores: boolean;
  recommended: boolean;
}

export interface PartitionAdvice {
  cells: number;
  ues: number;
  physical_cores: number;
  recommended_groups: number;
  options: PartitionOption[];
  model: { speed_law: string; fit_error_pct: number; measured_on: string };
  caveats: string[];
}

export async function fetchPartitionAdvice(
  config: NSGSimulationConfig,
): Promise<PartitionAdvice> {
  const res = await fetch(`${BASE}/partition/advice`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeader() },
    body: JSON.stringify(config),
  });
  if (!res.ok) throw new Error(`partition advice: HTTP ${res.status}`);
  return res.json();
}
