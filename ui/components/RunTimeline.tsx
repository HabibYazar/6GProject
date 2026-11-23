'use client';

/**
 * Per-tick timeline of one batch run — the ground truth under a campaign verdict.
 *
 * A campaign says "this run passed". A summary says "the mean was 58 Mbps". Neither
 * answers the question a handover demo is actually about: *when* did it happen, and
 * what did that UE see on either side of it. The engine already reports every KPM
 * window with its per-UE and per-cell rows; nothing read them until now.
 *
 * Shape of the interaction: scrub to a tick, then click a UE or a cell to pin it
 * and see its whole trajectory across the run. The tick view answers "what is true
 * right now", the pinned series answers "how did it get here".
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { wattParts } from '@/utils/power';
import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid,
  Legend, ReferenceLine,
} from 'recharts';
import {
  Clock, RefreshCw, Radio, Users, Zap, ArrowLeftRight, Activity, Pin, PinOff,
  ChevronLeft, ChevronRight, AlertTriangle,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { listSimRuns, getSimRun } from '@/lib/api';
import RicRealTopology, { type MapBounds } from './RicRealTopology';
import type { SimRunBrief, SimRunDataset, SimFrame, NsOranLiveUe } from '@/types/nsg';

const num = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : v.toFixed(digits);

/** Ticks where the cumulative handover counter went up — the moments that matter. */
function handoverTicks(frames: SimFrame[]): number[] {
  const out: number[] = [];
  let last = 0;
  for (const f of frames) {
    const h = f.handovers ?? 0;
    if (h > last) out.push(f.tick);
    last = Math.max(last, h);
  }
  return out;
}

function Kpi({ icon: Icon, label, value, unit }: {
  icon: typeof Radio; label: string; value: string; unit?: string;
}) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2">
      <div className="flex items-center gap-1.5 text-[9px] uppercase tracking-wide text-zinc-500">
        <Icon className="h-3 w-3" /> {label}
      </div>
      <div className="mt-0.5 font-mono text-sm text-teal-400">
        {value}{unit && <span className="ml-1 text-[10px] text-zinc-500">{unit}</span>}
      </div>
    </div>
  );
}

export default function RunTimeline() {
  const [runs, setRuns] = useState<SimRunBrief[]>([]);
  const [runId, setRunId] = useState<string>('');
  const [data, setData] = useState<SimRunDataset | null>(null);
  const [idx, setIdx] = useState(0);
  const [pinnedUe, setPinnedUe] = useState<number | null>(null);
  const [pinnedCell, setPinnedCell] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const loadRuns = useCallback(async () => {
    try {
      const { runs } = await listSimRuns();
      const usable = runs.filter(r => (r.n_frames ?? 0) > 0);
      setRuns(usable);
      setError(null);
      return usable;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return [];
    }
  }, []);

  useEffect(() => { void loadRuns() }, [loadRuns]);

  const openRun = useCallback(async (id: string) => {
    setRunId(id);
    setLoading(true);
    setPinnedUe(null);
    setPinnedCell(null);
    try {
      const d = await getSimRun(id);
      setData(d);
      // Land on the first tick that actually has UEs: the engine spends the first
      // several windows attaching, and opening on an empty frame looks broken.
      const first = (d.frames ?? []).findIndex(f => (f.ues?.length ?? 0) > 0);
      setIdx(first >= 0 ? first : 0);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  const frames = data?.frames ?? [];
  const frame: SimFrame | undefined = frames[idx];
  const hoTicks = useMemo(() => handoverTicks(frames), [frames]);

  /** STAGED FAULTS THAT ACTUALLY FIRED, placed on the timeline.
   *
   * The engine logs each one as `[NSG-FAULT] t=2.1s cell=3 degraded to 20 dBm`
   * and the summary carries the lines, but the timeline drew none of them — so a
   * run whose whole point was "the cell drops at 2 s" showed a throughput step
   * with nothing to attribute it to. Worse, a fault scheduled beyond the run's
   * horizon never fires at all, and the only tell was a metric that failed to
   * move; now an empty list next to a configured anomaly is visible.
   *
   * Matched to a tick by simulated time, since that is what the fault line
   * carries and what a tick now reports.
   */
  const faults = useMemo(() => {
    const out: { tick: number; t: number; text: string }[] = [];
    for (const line of data?.summary?.anomaly_events ?? []) {
      const m = /t=([\d.]+)s/.exec(line);
      if (!m) continue;
      const t = Number(m[1]);
      let best = -1;
      let bestGap = Infinity;
      frames.forEach(f => {
        if (f.sim_t == null) return;
        const gap = Math.abs(f.sim_t - t);
        if (gap < bestGap) { bestGap = gap; best = f.tick; }
      });
      out.push({ tick: best, t, text: line.replace('[NSG-FAULT] ', '') });
    }
    return out;
  }, [data, frames]);

  // One extent for the WHOLE run — the cells and every position any UE reaches —
  // so scrubbing moves the UE across a map that holds still, instead of
  // reframing the map around the UE at every tick.
  const mapBounds: MapBounds | null = useMemo(() => {
    const xs: number[] = [];
    const ys: number[] = [];
    for (const b of data?.config?.base_stations ?? []) {
      xs.push(b.position.x); ys.push(b.position.y);
    }
    for (const f of frames) {
      for (const u of f.ues ?? []) {
        if (u.x != null && u.y != null) { xs.push(u.x); ys.push(u.y); }
      }
    }
    if (!xs.length) return null;
    let minX = Math.min(...xs), maxX = Math.max(...xs);
    let minY = Math.min(...ys), maxY = Math.max(...ys);
    const dx = (maxX - minX) || 1, dy = (maxY - minY) || 1;
    // A walking UE covers a few metres between two cells hundreds apart, so pad
    // generously on the short axis or the whole path collapses onto one pixel.
    const pad = Math.max(dx, dy) * 0.12;
    minX -= pad; maxX += pad; minY -= pad; maxY += pad;
    return { minX, maxX, minY, maxY };
  }, [frames, data?.config]);

  // Positions up to and including the tick on screen, per UE.
  const trails = useMemo(() => {
    const m = new Map<number, { x: number; y: number }[]>();
    for (let i = 0; i <= idx && i < frames.length; i++) {
      for (const u of frames[i].ues ?? []) {
        if (u.x == null || u.y == null) continue;
        const pts = m.get(u.imsi) ?? [];
        const last = pts[pts.length - 1];
        // The engine reports position at its own cadence, slower than a KPM
        // window, so consecutive ticks repeat a point — keep only the moves.
        if (!last || last.x !== u.x || last.y !== u.y) pts.push({ x: u.x, y: u.y });
        m.set(u.imsi, pts);
      }
    }
    return m;
  }, [frames, idx]);

  // SimFrameUe carries nulls where NsOranLiveUe expects undefined; the map only
  // reads position, cell, SINR and slice.
  const tickUes: NsOranLiveUe[] = useMemo(
    () => (frame?.ues ?? []).map(u => ({
      imsi: u.imsi,
      cell: u.cell ?? undefined,
      sinr: u.sinr ?? undefined,
      slice: u.slice,
      x: u.x,
      y: u.y,
    })),
    [frame],
  );

  /** HOW MANY OF THESE TICKS ARE REAL OBSERVATIONS.
   *
   * The live stream emits frames faster than the RAN reports over E2, so
   * consecutive ticks carry the same KPM values. Measured on a stored run: ticks
   * 1-5 all `kpm_fresh: false`, then 6 and 8 true. Scrubbing twelve ticks looks
   * like twelve samples and is nearer three, and nothing on screen said so —
   * which is the difference between "the trend is flat" and "we looked once".
   */
  const freshCount = useMemo(
    () => frames.filter(f => f.kpm_fresh).length,
    [frames],
  );

  /** WHEN THE KPM ON SCREEN WAS ACTUALLY MEASURED.
   *
   * `sim_t` comes from the position telemetry, which keeps advancing; the KPM
   * values come from the last indication that carried them. On a repeat frame
   * those are two different instants, and a real run makes the gap glaring:
   * ticks 1-10 all sit at t=0.20 s, then tick 11 jumps to 1.10 s while its KPM
   * is still the round measured at 0.20. Labelling that frame "t = 1.10 s" and
   * saying nothing else claims a freshness the numbers do not have.
   */
  const kpmAgeAt = useMemo(() => {
    const out: (number | null)[] = [];
    let lastFresh: number | null = null;
    for (const f of frames) {
      if (f.kpm_fresh && f.sim_t != null) lastFresh = f.sim_t;
      out.push(lastFresh);
    }
    return out;
  }, [frames]);

  /** The pinned UE's or cell's whole trajectory, for the lower chart. */
  const series = useMemo(() => {
    if (!frames.length) return [];
    return frames.map(f => {
      const row: Record<string, number | null> = { tick: f.tick, 't (s)': f.sim_t ?? null };
      if (pinnedUe !== null) {
        const u = f.ues?.find(x => x.imsi === pinnedUe);
        row['SINR (dB)'] = u?.sinr ?? null;
        row['Throughput (Mbps)'] = u?.thp_kbps != null ? u.thp_kbps / 1000 : null;
        row['PRB'] = u?.prb ?? null;
        row['Delay (ms)'] = u?.delay_ms ?? null;
      } else if (pinnedCell !== null) {
        const c = f.cells?.find(x => x.cell === pinnedCell);
        row['SINR (dB)'] = c?.sinr ?? null;
        row['Throughput (Mbps)'] = c?.thp_kbps != null ? c.thp_kbps / 1000 : null;
        row['PRB'] = c?.prb ?? null;
        row['Power (W)'] = c?.power_w ?? null;
        row['UEs'] = c?.ues ?? null;
      } else {
        // Nothing pinned: the network view.
        row['Throughput (Mbps)'] =
          (f.ues ?? []).reduce((a, u) => a + (u.thp_kbps ?? 0), 0) / 1000;
        row['UEs'] = f.ues?.length ?? 0;
        row['Cells'] = f.cells?.length ?? 0;
        row['Power (W)'] = f.energy?.total_w ?? null;
        // Per-slice DL was on every frame and drawn nowhere. A slicing scenario
        // is judged on whether the protected slice HELD while the others moved,
        // and that is a question about lines over time, not a total.
        for (const [name, kbps] of Object.entries(f.slices ?? {})) {
          row[`${name} (Mbps)`] = kbps / 1000;
        }
      }
      return row;
    });
  }, [frames, pinnedUe, pinnedCell]);

  const seriesKeys = useMemo(
    // 't (s)' rides along for the tooltip and must not be drawn as a series.
    () => (series.length
      ? Object.keys(series[0]).filter(k => k !== 'tick' && k !== 't (s)')
      : []),
    [series],
  );
  const COLORS = ['#14b8a6', '#f43f5e', '#a78bfa', '#fbbf24', '#38bdf8'];

  const dlTotal = frame ? (frame.ues ?? []).reduce((a, u) => a + (u.thp_kbps ?? 0), 0) / 1000 : 0;

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto p-4 custom-scrollbar">
      {/* Run picker */}
      <div className="flex flex-wrap items-center gap-2">
        <Clock className="h-4 w-4 text-teal-400" />
        <select
          value={runId}
          onChange={e => void openRun(e.target.value)}
          className="min-w-0 max-w-[420px] flex-1 rounded border border-zinc-800 bg-zinc-900 px-2 py-1 text-xs text-zinc-300 outline-none focus:border-teal-500"
        >
          <option value="">Pick a run…</option>
          {runs.map(r => (
            <option key={r.id} value={r.id}>
              {r.config_name ?? r.id} · {r.n_frames} ticks
              {/* The HORIZON, next to the tick count. A run of 12 ticks sounds
                  substantial until you see it covered 1.1 simulated seconds —
                  which is the number that explains a thin-looking timeline. */}
              {r.summary?.sim_span_s != null && ` · ${r.summary.sim_span_s}s sim`} ·{' '}
              {r.started_at ? new Date(r.started_at).toLocaleString() : '—'}
            </option>
          ))}
        </select>
        <button
          onClick={() => void loadRuns()}
          title="Reload the run list"
          className="rounded border border-zinc-800 p-1.5 text-zinc-500 hover:border-teal-500/50 hover:text-teal-400"
        >
          <RefreshCw className={cn('h-3 w-3', loading && 'animate-spin')} />
        </button>
      </div>

      {error && (
        <div className="rounded border border-red-900/40 bg-red-950/20 px-3 py-2 text-[11px] text-red-300">
          {error}
        </div>
      )}

      {!data ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 text-zinc-600">
          <Clock className="h-8 w-8 opacity-40" />
          <p className="text-xs">Pick a run to step through it tick by tick.</p>
          <p className="max-w-md text-center text-[10px] leading-relaxed text-zinc-700">
            Every campaign run stores its full KPM time series. This is where the
            handover actually shows up — the summary only has the average.
          </p>
        </div>
      ) : (
        <>
          {/* Scrubber */}
          <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-3">
            <div className="mb-2 flex items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setIdx(i => Math.max(0, i - 1))}
                  disabled={idx === 0}
                  className="rounded border border-zinc-800 p-1 text-zinc-400 hover:border-teal-500/50 hover:text-teal-400 disabled:opacity-30"
                >
                  <ChevronLeft className="h-3 w-3" />
                </button>
                <span className="font-mono text-xs text-zinc-300">
                  tick <span className="text-teal-400">{frame?.tick ?? '—'}</span>
                  <span className="text-zinc-600"> / {frames.length}</span>
                  {/* A tick is a KPM WINDOW, not a second. Saying where it lands in
                      simulated time is what stops a thin early tick reading as a
                      broken engine — tick 5 is routinely 0.1 s in. */}
                  {frame?.sim_t != null && (
                    <span className="ml-2 text-zinc-400">
                      t = <span className="text-amber-400">{frame.sim_t.toFixed(2)} s</span>
                      <span className="text-zinc-600"> sim</span>
                    </span>
                  )}
                  {/* Is this tick new information, or the previous one repeated? */}
                  {frame?.kpm_fresh !== undefined && (
                    <span
                      className={cn(
                        'ml-2 rounded border px-1 py-px text-[9px] uppercase tracking-wide',
                        frame.kpm_fresh
                          ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400'
                          : 'border-zinc-700/50 bg-zinc-800/40 text-zinc-500',
                      )}
                      title={
                        frame.kpm_fresh
                          ? `New KPM from cell(s) ${(frame.kpm_fresh_cells ?? []).join(', ') || '—'}`
                          : 'No cell reported new KPM in this window — the values are the previous tick repeated'
                      }
                    >
                      {frame.kpm_fresh ? 'fresh' : 'repeat'}
                    </span>
                  )}
                  {/* The gap between where the UEs ARE and when the numbers beside
                      them were measured. Only worth saying when it is real. */}
                  {!frame?.kpm_fresh && frame?.sim_t != null
                    && kpmAgeAt[idx] != null && kpmAgeAt[idx] !== frame.sim_t && (
                    <span
                      className="ml-1.5 text-[10px] text-zinc-500"
                      title="Positions come from the telemetry stream and keep advancing; the KPM values are from the last indication that carried them. On this tick they are different instants."
                    >
                      KPM from t = {kpmAgeAt[idx]!.toFixed(2)} s
                    </span>
                  )}
                </span>
                <button
                  onClick={() => setIdx(i => Math.min(frames.length - 1, i + 1))}
                  disabled={idx >= frames.length - 1}
                  className="rounded border border-zinc-800 p-1 text-zinc-400 hover:border-teal-500/50 hover:text-teal-400 disabled:opacity-30"
                >
                  <ChevronRight className="h-3 w-3" />
                </button>
              </div>
              {hoTicks.length > 0 && (
                <div className="flex items-center gap-1.5 text-[10px] text-amber-400">
                  <ArrowLeftRight className="h-3 w-3" />
                  handover at tick {hoTicks.join(', ')}
                </div>
              )}
              {/* THE HONEST SAMPLE COUNT, said once for the whole run. Ticks are
                  emitted faster than the RAN reports over E2, so most of them
                  repeat the previous values — reading twelve ticks as twelve
                  measurements overstates the evidence several times over. */}
              {faults.length > 0 && (
                <div className="flex flex-wrap items-center gap-1.5 text-[10px] text-red-400">
                  <Zap className="h-3 w-3" />
                  {faults.map((f, i) => (
                    <button
                      key={i}
                      onClick={() => {
                        const at = frames.findIndex(fr => fr.tick === f.tick);
                        if (at >= 0) setIdx(at);
                      }}
                      title={f.text}
                      className="rounded border border-red-500/30 bg-red-500/10 px-1 py-px hover:border-red-400"
                    >
                      fault t={f.t}s
                      {f.tick >= 0 && <span className="text-red-300/60"> · tick {f.tick}</span>}
                    </button>
                  ))}
                </div>
              )}
              {/* A fault was staged and the engine never reported it firing. That
                  is the anomaly-horizon trap: a start_s beyond the run's
                  simulated span simply does not happen, and the run then passes
                  its thresholds honestly. Worth saying out loud. */}
              {faults.length === 0 && (data?.summary?.anomalies_configured ?? 0) > 0 && (
                <div
                  className="flex items-center gap-1.5 text-[10px] text-amber-400"
                  title="The scenario stages a fault but the engine never reported one firing — usually its start_s lands beyond the simulated time this run covered."
                >
                  <AlertTriangle className="h-3 w-3" />
                  {data?.summary?.anomalies_configured} fault(s) staged, none fired
                </div>
              )}
              {freshCount > 0 && (
                <div
                  className="flex items-center gap-1.5 text-[10px] text-zinc-500"
                  title="A tick is a frame of the live stream; a cell only reports new KPM every so often. The fresh count is how many independent observations this run really contains."
                >
                  <Activity className="h-3 w-3" />
                  <span className="text-emerald-400/90">{freshCount}</span>
                  <span>of {frames.length} ticks carry new KPM</span>
                </div>
              )}
            </div>
            <input
              type="range"
              min={0}
              max={Math.max(0, frames.length - 1)}
              value={idx}
              onChange={e => setIdx(Number(e.target.value))}
              className="w-full accent-teal-500"
            />
          </div>

          {/* Network KPIs at this tick */}
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
            <Kpi icon={Activity} label="Downlink" value={num(dlTotal)} unit="Mbps" />
            <Kpi icon={Users} label="UEs reporting" value={String(frame?.ues?.length ?? 0)} />
            <Kpi icon={Radio} label="Cells reporting" value={String(frame?.cells?.length ?? 0)} />
            <Kpi icon={Zap} label="Power" value={wattParts(frame?.energy?.total_w ?? null).value}
                 unit={wattParts(frame?.energy?.total_w ?? null).unit} />
            <Kpi icon={ArrowLeftRight} label="Handovers (cum.)"
                 value={String(frame?.handovers ?? 0)} />
          </div>

          {/* Where those numbers are, on the ground. The same map the RIC Test
              real-engine view uses live — here it is driven by the scrubber, so
              a handover is watched rather than read: the dashed line swings from
              one cell to the other on the tick the counter moves. */}
          <RicRealTopology
            ues={tickUes}
            config={data?.config ?? null}
            bounds={mapBounds}
            trails={trails}
            title={
              `Tick ${frame?.tick ?? '—'}` +
              (frame?.sim_t != null ? ` · t = ${frame.sim_t.toFixed(2)} s sim` : '') +
              ' — UEs & serving cells'
            }
          />

          {(frame?.ues?.length ?? 0) === 0 && (
            <p className="rounded border border-zinc-800 px-3 py-2 text-[10px] text-zinc-500">
              No UE reported in this window
              {frame?.sim_t != null && (
                <> — and this tick is only <span className="text-amber-400">
                  {frame.sim_t.toFixed(2)} s</span> into the simulation</>
              )}. The engine spends its first several ticks attaching UEs, so early
              frames are legitimately empty. A run covers a fraction of a second at
              the default collection window, so &quot;not much data&quot; usually means
              &quot;not much simulated time&quot; rather than a silent engine.
            </p>
          )}

          {/* Per-UE and per-cell tables */}
          <div className="grid gap-3 lg:grid-cols-2">
            <div className="overflow-x-auto rounded-lg border border-zinc-800 bg-zinc-950">
              <div className="flex items-center justify-between px-3 py-2">
                <span className="text-[10px] font-bold uppercase tracking-wide text-zinc-400">
                  UEs at this tick
                </span>
                {pinnedUe !== null && (
                  <button onClick={() => setPinnedUe(null)}
                          className="flex items-center gap-1 text-[10px] text-teal-400 hover:text-teal-300">
                    <PinOff className="h-3 w-3" /> unpin IMSI {pinnedUe}
                  </button>
                )}
              </div>
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="border-y border-zinc-900 text-[9px] uppercase text-zinc-500">
                    <th className="px-2 py-1 text-left">IMSI</th>
                    <th className="px-2 py-1 text-right">Cell</th>
                    <th className="px-2 py-1 text-right">SINR</th>
                    <th className="px-2 py-1 text-right">Mbps</th>
                    <th className="px-2 py-1 text-right">PRB</th>
                    <th className="px-2 py-1 text-right">Delay</th>
                    <th className="px-2 py-1 text-left">Slice</th>
                    <th className="px-2 py-1 text-right">x,y</th>
                  </tr>
                </thead>
                <tbody>
                  {(frame?.ues ?? []).map(u => (
                    <tr
                      key={u.imsi}
                      onClick={() => { setPinnedUe(u.imsi); setPinnedCell(null) }}
                      className={cn(
                        'cursor-pointer border-b border-zinc-900/60 hover:bg-white/[0.03]',
                        pinnedUe === u.imsi && 'bg-teal-500/10',
                      )}
                    >
                      <td className="px-2 py-1 font-mono text-zinc-300">
                        {pinnedUe === u.imsi && <Pin className="mr-1 inline h-2.5 w-2.5 text-teal-400" />}
                        {u.imsi}
                      </td>
                      <td className="px-2 py-1 text-right font-mono text-zinc-400">
                        {u.cell}
                        {/* ON THE LTE ANCHOR, not a mmWave cell. The data plane
                            and every DU metric live on mmWave, so a UE here is
                            one that lost its mmWave link — a coverage failure
                            the cell number alone does not show. */}
                        {u.on_lte && (
                          <span
                            className="ml-1 rounded border border-amber-500/40 bg-amber-500/10 px-1 text-[9px] uppercase text-amber-400"
                            title="This UE is on the LTE anchor, not a mmWave cell — it has lost its mmWave link"
                          >
                            lte
                          </span>
                        )}
                      </td>
                      <td className="px-2 py-1 text-right font-mono text-zinc-300">{num(u.sinr)}</td>
                      <td className="px-2 py-1 text-right font-mono text-teal-400">
                        {num(u.thp_kbps != null ? u.thp_kbps / 1000 : null)}
                      </td>
                      <td className="px-2 py-1 text-right font-mono text-zinc-400">{num(u.prb)}</td>
                      <td className="px-2 py-1 text-right font-mono text-zinc-400">{num(u.delay_ms)}</td>
                      <td className="px-2 py-1 text-zinc-500">{u.slice ?? '—'}</td>
                      <td className="px-2 py-1 text-right font-mono text-zinc-600">
                        {u.x != null ? `${u.x.toFixed(0)},${u.y?.toFixed(0)}` : '—'}
                      </td>
                    </tr>
                  ))}
                  {(frame?.ues?.length ?? 0) === 0 && (
                    <tr><td colSpan={8} className="px-3 py-3 text-center text-[10px] text-zinc-600">
                      no UE rows in this window
                    </td></tr>
                  )}
                </tbody>
              </table>
            </div>

            <div className="overflow-x-auto rounded-lg border border-zinc-800 bg-zinc-950">
              <div className="flex items-center justify-between px-3 py-2">
                <span className="text-[10px] font-bold uppercase tracking-wide text-zinc-400">
                  Cells at this tick
                </span>
                {pinnedCell !== null && (
                  <button onClick={() => setPinnedCell(null)}
                          className="flex items-center gap-1 text-[10px] text-teal-400 hover:text-teal-300">
                    <PinOff className="h-3 w-3" /> unpin cell {pinnedCell}
                  </button>
                )}
              </div>
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="border-y border-zinc-900 text-[9px] uppercase text-zinc-500">
                    <th className="px-2 py-1 text-left">Cell</th>
                    <th className="px-2 py-1 text-right">UEs</th>
                    <th className="px-2 py-1 text-right">DL Mbps</th>
                    <th className="px-2 py-1 text-right">UL kbps</th>
                    <th className="px-2 py-1 text-right">PRB</th>
                    <th className="px-2 py-1 text-right">SINR</th>
                    <th className="px-2 py-1 text-right">Power W</th>
                    <th className="px-2 py-1 text-left">Sleep</th>
                  </tr>
                </thead>
                <tbody>
                  {(frame?.cells ?? []).map(c => (
                    <tr
                      key={c.cell}
                      onClick={() => { setPinnedCell(c.cell); setPinnedUe(null) }}
                      className={cn(
                        'cursor-pointer border-b border-zinc-900/60 hover:bg-white/[0.03]',
                        pinnedCell === c.cell && 'bg-teal-500/10',
                      )}
                    >
                      <td className="px-2 py-1 font-mono text-zinc-300">
                        {pinnedCell === c.cell && <Pin className="mr-1 inline h-2.5 w-2.5 text-teal-400" />}
                        {c.cell}
                      </td>
                      <td className="px-2 py-1 text-right font-mono text-zinc-400">{c.ues}</td>
                      <td className="px-2 py-1 text-right font-mono text-teal-400">
                        {num(c.thp_kbps != null ? c.thp_kbps / 1000 : null)}
                      </td>
                      <td className="px-2 py-1 text-right font-mono text-zinc-400">{num(c.ul_kbps)}</td>
                      <td className="px-2 py-1 text-right font-mono text-zinc-400">{num(c.prb)}</td>
                      <td className="px-2 py-1 text-right font-mono text-zinc-300">{num(c.sinr)}</td>
                      <td className="px-2 py-1 text-right font-mono text-amber-400/80">{num(c.power_w, 0)}</td>
                      <td className="px-2 py-1 text-zinc-500">
                        {c.sleep_depth ?? c.sleep_state ?? '—'}
                      </td>
                    </tr>
                  ))}
                  {(frame?.cells?.length ?? 0) === 0 && (
                    <tr><td colSpan={8} className="px-3 py-3 text-center text-[10px] text-zinc-600">
                      no cell rows in this window
                    </td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          {/* Trajectory of whatever is pinned */}
          <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-3">
            <p className="mb-2 text-[10px] uppercase tracking-wide text-zinc-500">
              {pinnedUe !== null ? `IMSI ${pinnedUe} across the run`
                : pinnedCell !== null ? `Cell ${pinnedCell} across the run`
                : 'Network across the run — click a UE or a cell row to pin it'}
            </p>
            <ResponsiveContainer width="100%" height={240}>
              <LineChart data={series} margin={{ top: 5, right: 12, bottom: 5, left: 0 }}>
                <CartesianGrid stroke="#27272a" strokeDasharray="3 3" />
                {/* Labelled by tick because that is what the scrubber moves,
                    with the simulated second underneath it so the axis is not
                    mistaken for seconds. */}
                <XAxis
                  dataKey="tick"
                  stroke="#52525b"
                  fontSize={10}
                  tickFormatter={(t: number) => {
                    const row = series.find(r => r.tick === t);
                    const st = row?.['t (s)'];
                    return st != null ? `${t} (${st.toFixed(1)}s)` : String(t);
                  }}
                />
                <YAxis stroke="#52525b" fontSize={10} width={48} />
                <Tooltip
                  contentStyle={{ background: '#09090b', border: '1px solid #27272a', fontSize: 11 }}
                  labelStyle={{ color: '#a1a1aa' }}
                />
                <Legend wrapperStyle={{ fontSize: 10 }} />
                {/* Where the tick cursor is, and where handovers happened */}
                <ReferenceLine x={frame?.tick} stroke="#14b8a6" strokeDasharray="4 2" />
                {/* Where a staged fault actually fired — the step in the line
                    above it is the thing the scenario was built to show. */}
                {faults.filter(f => f.tick >= 0).map((f, i) => (
                  <ReferenceLine key={`fault-${i}`} x={f.tick} stroke="#f43f5e" strokeOpacity={0.8} />
                ))}
                {hoTicks.map(t => (
                  <ReferenceLine key={t} x={t} stroke="#fbbf24" strokeOpacity={0.7} />
                ))}
                {seriesKeys.map((k, i) => (
                  <Line key={k} type="monotone" dataKey={k} stroke={COLORS[i % COLORS.length]}
                        strokeWidth={1.8} dot={false} connectNulls />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* Every measurement the engine reported in this window */}
          <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-3">
            <p className="mb-2 text-[10px] uppercase tracking-wide text-zinc-500">
              Raw E2SM-KPM measurements at tick {frame?.tick} ·{' '}
              {Object.keys(frame?.measurements ?? {}).length} reported
            </p>
            <div className="grid gap-x-6 gap-y-1 sm:grid-cols-2 lg:grid-cols-3">
              {Object.entries(frame?.measurements ?? {}).map(([name, s]) => (
                <div key={name} className="flex items-center justify-between gap-2 border-b border-zinc-900 py-0.5">
                  <span className="truncate font-mono text-[9px] text-zinc-500">{name}</span>
                  <span className="shrink-0 font-mono text-[10px] text-zinc-300">
                    {num(s.mean, 2)}
                    <span className="ml-1 text-zinc-600">({num(s.min, 1)}–{num(s.max, 1)})</span>
                  </span>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
