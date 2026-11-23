/**
 * Named scenarios, stored server-side.
 *
 * These used to live in localStorage. That made them invisible from any other
 * machine, impossible to share, and — the reason the load path needs a
 * normaliser — able to keep whatever shape the schema had on the day they were
 * saved, forever. Saving through the API validates the configuration against the
 * current schema before it is stored, so a record that cannot be loaded is
 * rejected at the point it is created rather than months later on a canvas that
 * reads a field the record never had.
 *
 * The functions are async now; that is the only change callers see. The old
 * localStorage entries are deliberately NOT deleted — nothing reads them any
 * more, but destroying a user's saved work as a side effect of a migration is
 * not a trade worth making.
 */
import type { NSGSimulationConfig } from '@/types/nsg';
import { authHeader } from '@/lib/auth';

const API = process.env.NEXT_PUBLIC_NSG_API ?? 'http://localhost:8000';

export interface SavedScenario {
  id: string;
  name: string;
  config: NSGSimulationConfig;
  description?: string | null;
  source?: string | null;
  /** ISO-8601, UTC. The server is the clock now, not the browser. */
  created_at?: string | null;
  /** Epoch ms, derived from created_at — kept so `relativeTime` is unchanged. */
  savedAt: number;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${API}${path}`, {
    ...init,
    // Merged, not replaced, and carrying the bearer token: this module has its
    // own fetch rather than going through lib/api.ts, so it silently missed
    // authentication entirely and the sidebar reported "authentication
    // required" while the user was signed in.
    headers: {
      'Content-Type': 'application/json',
      ...authHeader(),
      ...(init?.headers as Record<string, string> | undefined),
    },
  });
  if (!r.ok) {
    let detail = `${r.status} ${r.statusText}`;
    try {
      const body = await r.json();
      if (body?.detail) detail = typeof body.detail === 'string'
        ? body.detail : JSON.stringify(body.detail);
    } catch { /* keep the status line */ }
    throw new Error(detail);
  }
  return r.json() as Promise<T>;
}

function hydrate(s: Omit<SavedScenario, 'savedAt'>): SavedScenario {
  return { ...s, savedAt: s.created_at ? Date.parse(s.created_at) : Date.now() };
}

/** All saved scenarios, newest first. */
export async function listScenarios(): Promise<SavedScenario[]> {
  const { scenarios } = await call<{ scenarios: Omit<SavedScenario, 'savedAt'>[] }>('/scenarios');
  return scenarios.map(hydrate);
}

/** Save a snapshot of `config` under `name`. Rejects a config the schema refuses. */
export async function saveScenario(
  name: string,
  config: NSGSimulationConfig,
  source?: 'ai' | 'manual' | 'preset',
): Promise<SavedScenario> {
  const { scenario } = await call<{ scenario: Omit<SavedScenario, 'savedAt'> }>('/scenarios', {
    method: 'POST',
    body: JSON.stringify({ name, config, source: source ?? 'manual' }),
  });
  return hydrate(scenario);
}

/** Remove a scenario by id. */
export async function deleteScenario(id: string): Promise<void> {
  await call(`/scenarios/${id}`, { method: 'DELETE' });
}

/** Compact relative-time label for the sidebar (e.g. "3h ago", "just now"). */
export function relativeTime(ms: number): string {
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (s < 45) return 'just now';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  return new Date(ms).toLocaleDateString();
}
