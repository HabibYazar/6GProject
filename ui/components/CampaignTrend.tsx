'use client';

/**
 * Suite trend — one scenario suite across every time it has been run.
 *
 * The Campaign tab answers "did tonight pass?" and its baseline diff answers
 * "is tonight worse than last night?". Neither answers "which scenario has been
 * sliding for three weeks", because that needs the runs lined up in time rather
 * than a pair held against each other. This is that view, and it is why it sits
 * next to the AI comparison instead of inside the campaign console: both tabs
 * here answer "compared to what?", just about different things.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid, Legend,
} from 'recharts';
import { CheckCircle2, XCircle, AlertTriangle, ListChecks, RefreshCw, History } from 'lucide-react';
import { cn } from '@/lib/utils';
import { campaignHistory } from '@/lib/api';
import type { CampaignHistory, Verdict } from '@/types/nsg';
import { METRIC_GROUPS, metricLabel, metricPreset } from '@/lib/metrics';

const LINE_COLORS = ['#14b8a6', '#f43f5e', '#a78bfa', '#fbbf24', '#38bdf8', '#f97316'];

const VERDICT_CELL: Record<string, { cls: string; icon: typeof CheckCircle2 }> = {
  pass:          { cls: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/25', icon: CheckCircle2 },
  fail:          { cls: 'text-red-400 bg-red-500/10 border-red-500/25',             icon: XCircle },
  error:         { cls: 'text-amber-400 bg-amber-500/10 border-amber-500/25',       icon: AlertTriangle },
  no_thresholds: { cls: 'text-zinc-400 bg-zinc-800/40 border-zinc-700/40',          icon: ListChecks },
};

const shortDate = (iso: string) =>
  new Date(iso).toLocaleDateString(undefined, { day: '2-digit', month: 'short' });

const shortTime = (iso: string) =>
  new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });

/**
 * Column labels: the date, plus the time when that date is not unique.
 * A nightly and an ad-hoc rerun on the same day are two different results, and
 * two columns both reading "29 Jul" make the grid unreadable exactly when it
 * matters most.
 */
function columnLabels(dates: string[]): string[] {
  const days = dates.map(shortDate);
  const seen = days.reduce<Record<string, number>>((a, d) => ({ ...a, [d]: (a[d] ?? 0) + 1 }), {});
  return dates.map((iso, i) => (seen[days[i]] > 1 ? `${days[i]} ${shortTime(iso)}` : days[i]));
}

export default function CampaignTrend() {
  const [history, setHistory] = useState<CampaignHistory | null>(null);
  const [suite, setSuite] = useState<string | undefined>(undefined);
  const [metric, setMetric] = useState<string>('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (name?: string) => {
    setLoading(true);
    try {
      const h = await campaignHistory(name);
      setHistory(h);
      setSuite(h.suite ?? undefined);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load() }, [load]);

  /** Metrics present anywhere in the suite, thresholded ones first. */
  const metrics = useMemo(() => {
    if (!history) return [] as string[];
    const bounded = new Set(
      history.campaigns.flatMap(c => c.thresholds.map(t => t.metric)),
    );
    const all = new Set<string>();
    for (const c of history.campaigns) for (const it of c.items) {
      for (const m of Object.keys(it.metrics)) all.add(m);
    }
    return [...all].sort((a, b) =>
      Number(bounded.has(b)) - Number(bounded.has(a)) || a.localeCompare(b));
  }, [history]);

  useEffect(() => {
    if (metrics.length && !metrics.includes(metric)) setMetric(metrics[0]);
  }, [metrics, metric]);

  const colLabels = useMemo(
    () => {
      const base = columnLabels((history?.campaigns ?? []).map(c => c.created_at));
      // Say which columns were SPLIT. A suite run as three processes and the
      // same suite run whole sit next to each other here and are scored on the
      // same axes, but they are not the same measurement: at one window budget
      // the split run reaches several times more simulated time and serves more
      // UEs, and it has no interference between groups. Only marked when some
      // column differs — a chart where every column says the same thing is
      // noise.
      const groups = (history?.campaigns ?? []).map(c => c.partition_groups ?? 1);
      if (new Set(groups).size < 2) return base;
      return base.map((l, i) => (groups[i] > 1 ? `${l} ÷${groups[i]}` : `${l} whole`));
    },
    [history],
  );

  /** One row per campaign, one series per scenario — the shape recharts wants. */
  const chartData = useMemo(() => {
    if (!history || !metric) return [];
    return history.campaigns.map((c, i) => {
      const row: Record<string, string | number> = { when: colLabels[i] };
      for (const it of c.items) {
        const v = it.metrics[metric];
        if (typeof v === 'number') row[it.label] = v;
      }
      return row;
    });
  }, [history, metric, colLabels]);

  const hasData = !!history && history.campaigns.length > 0;

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto p-4 custom-scrollbar">
      {/* Controls */}
      <div className="flex flex-wrap items-center gap-2">
        <History className="h-4 w-4 text-amber-400" />
        <select
          value={suite ?? ''}
          onChange={e => void load(e.target.value)}
          className="rounded border border-zinc-800 bg-zinc-900 px-2 py-1 text-xs text-zinc-300 outline-none focus:border-teal-500"
        >
          {(history?.suites ?? []).map(s => <option key={s} value={s}>{s}</option>)}
          {!history?.suites?.length && <option value="">no suites yet</option>}
        </select>
        {metrics.length > 0 && (
          <select
            value={metric}
            onChange={e => setMetric(e.target.value)}
            className="min-w-0 max-w-[280px] rounded border border-zinc-800 bg-zinc-900 px-2 py-1 font-mono text-[11px] text-zinc-300 outline-none focus:border-teal-500"
          >
            {/* Named, grouped and with units — the dropdown used to show raw
                field paths, which say neither what a number is nor what it is
                in. Anything the dictionary does not know still appears, under
                its own path, rather than going missing. */}
            {METRIC_GROUPS.map(g => {
              const inGroup = metrics.filter(m => metricPreset(m)?.group === g);
              return inGroup.length ? (
                <optgroup key={g} label={g}>
                  {inGroup.map(m => (
                    <option key={m} value={m} title={metricPreset(m)?.hint}>
                      {metricLabel(m)}
                    </option>
                  ))}
                </optgroup>
              ) : null;
            })}
            {metrics.some(m => !metricPreset(m)) && (
              <optgroup label="Other counters">
                {metrics.filter(m => !metricPreset(m)).map(m => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </optgroup>
            )}
          </select>
        )}
        <button
          onClick={() => void load(suite)}
          className="rounded border border-zinc-800 p-1.5 text-zinc-500 hover:border-teal-500/50 hover:text-teal-400"
          title="Reload"
        >
          <RefreshCw className={cn('h-3 w-3', loading && 'animate-spin')} />
        </button>
      </div>

      {error && (
        <div className="rounded border border-red-900/40 bg-red-950/20 px-3 py-2 text-[11px] text-red-300">
          {error}
        </div>
      )}

      {!hasData ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 text-zinc-600">
          <History className="h-8 w-8 opacity-40" />
          <p className="text-xs">No campaigns yet — run one from the Campaign tab.</p>
          <p className="max-w-sm text-center text-[10px] leading-relaxed text-zinc-700">
            A trend needs the same suite run more than once. Re-running a campaign
            with the same name is what lines them up here.
          </p>
        </div>
      ) : (
        <>
          {/* Verdict grid: scenario × campaign */}
          <div className="overflow-x-auto rounded-lg border border-zinc-800 bg-zinc-950">
            <table className="w-full border-collapse text-[11px]">
              <thead>
                <tr>
                  <th className="sticky left-0 z-10 bg-zinc-950 px-3 py-2 text-left text-[10px] font-bold uppercase tracking-wide text-zinc-500">
                    Scenario
                  </th>
                  {history!.campaigns.map((c, i) => (
                    <th
                      key={c.id}
                      title={`${new Date(c.created_at).toLocaleString()} · ${c.verdict ?? c.status}`}
                      className="px-2 py-2 text-center text-[10px] font-medium text-zinc-500"
                    >
                      {colLabels[i]}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {history!.scenarios.map(label => (
                  <tr key={label} className="border-t border-zinc-900">
                    <td className="sticky left-0 z-10 max-w-[220px] truncate bg-zinc-950 px-3 py-2 text-zinc-300">
                      {label}
                    </td>
                    {history!.campaigns.map(c => {
                      const item = c.items.find(i => i.label === label);
                      if (!item) {
                        return (
                          <td key={c.id} className="px-2 py-2 text-center text-zinc-700" title="not in this campaign">
                            ·
                          </td>
                        );
                      }
                      const style = VERDICT_CELL[item.verdict] ?? VERDICT_CELL.no_thresholds;
                      const Icon = style.icon;
                      const value = item.metrics[metric];
                      return (
                        <td key={c.id} className="px-2 py-1.5 text-center">
                          <div
                            className={cn('mx-auto flex w-fit items-center gap-1 rounded border px-1.5 py-0.5', style.cls)}
                            title={`${item.verdict} · ${item.repeats} repeat(s)`
                              + (typeof value === 'number' ? ` · ${metric} ${value}` : '')}
                          >
                            <Icon className="h-3 w-3" />
                            <span className="font-mono text-[10px]">
                              {typeof value === 'number' ? value : '—'}
                            </span>
                          </div>
                          {!item.noise_known && (
                            <span className="mt-0.5 block text-[8px] uppercase text-amber-500/50" title="Single repeat — no spread to judge a change by">
                              1×
                            </span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Metric trend */}
          <div className="min-h-[260px] flex-1 rounded-lg border border-zinc-800 bg-zinc-950 p-3">
            <p className="mb-2 text-[10px] text-zinc-400">
              {metricLabel(metric)}
              <span className="ml-2 font-mono text-zinc-600">{metric}</span>
            </p>
            {new Set((history?.campaigns ?? []).map(c => c.partition_groups ?? 1)).size > 1 && (
              <p className="mb-2 rounded border border-amber-500/20 bg-amber-500/5 px-2 py-1.5 text-[10px] leading-relaxed text-amber-300/80">
                These columns were not all measured the same way — some ran SPLIT
                across processes (marked ÷N) and some whole. At one window budget a
                split run covers several times more simulated time and serves more
                UEs, and it has no interference between groups. Read them as two
                experiments, not one trend.
              </p>
            )}
            <ResponsiveContainer width="100%" height={260}>
              <LineChart data={chartData} margin={{ top: 5, right: 12, bottom: 5, left: 0 }}>
                <CartesianGrid stroke="#27272a" strokeDasharray="3 3" />
                <XAxis dataKey="when" stroke="#52525b" fontSize={10} />
                <YAxis stroke="#52525b" fontSize={10} width={54} />
                <Tooltip
                  contentStyle={{ background: '#09090b', border: '1px solid #27272a', fontSize: 11 }}
                  labelStyle={{ color: '#a1a1aa' }}
                />
                <Legend wrapperStyle={{ fontSize: 10 }} />
                {history!.scenarios.map((label, i) => (
                  <Line
                    key={label}
                    type="monotone"
                    dataKey={label}
                    stroke={LINE_COLORS[i % LINE_COLORS.length]}
                    strokeWidth={2}
                    dot={{ r: 2.5 }}
                    connectNulls
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>

          <p className="text-[10px] leading-relaxed text-zinc-600">
            Each point is the mean across that scenario&apos;s repeats in one campaign.
            A <span className="text-amber-500/70">1×</span> mark means the scenario ran once there,
            so its movement cannot be told apart from run-to-run variation — this engine
            moves throughput a few percent even at a fixed seed.
          </p>
        </>
      )}
    </div>
  );
}
