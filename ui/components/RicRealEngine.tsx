'use client';

/**
 * Real-engine LIVE session (/ws/e2-real). The ns-3 mmWave RAN runs for real
 * inside the ns-O-RAN container while an xApp drives real E2SM-RC control over
 * E2AP. Here the browser is the xApp — but the whole point is that a CUSTOMER'S
 * own trained xApp connects the exact same way. Live KPM frames stream in; each
 * control command fires a real RIC CONTROL on the running RAN.
 *
 * Same Control/Data split + shared dropdown ManualRcControl as the analytic
 * sandbox, so the two consoles are one design. Control = KPM tables + manual
 * levers + Save run + connector + log; Data = the live dashboard.
 */
import { useState, useMemo, useEffect } from 'react';
import { wattParts } from '@/utils/power';
import { authHeader } from '@/lib/auth';
import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip as ChartTooltip, Legend,
} from 'recharts';
import {
  Radio, Zap, Signal, Gauge, Play, Square, Loader2, Send,
  Users, Terminal, Download, Power, Grid3x3, Save, Trophy, Layers, BarChart3, Clock, Cpu,
} from 'lucide-react';
import {
  useNSGStore, selectConfig, selectRicRealStatus, selectRicRealKpm, selectRicRealMetrics,
  selectRicRealUes, selectRicRealLog, selectRicRealControlCount, selectRicRealSeries,
} from '@/store/useNSGStore';
import { apiUrl } from '@/lib/api';
import { downloadJson } from '@/utils/exportData';
import ManualRcControl, { type RcOption } from './ManualRcControl';
import RicRealTopology from './RicRealTopology';

/** slice name → the QCI its bearers are created with (mirrors the backend's
 *  _SLICE_QCI_DEFAULT + config qos_id override) so a slice-name dropdown drives
 *  the real per-QCI MAC quota control. */
const SLICE_QCI_DEFAULT: Record<string, number> = { eMBB: 9, uRLLC: 3, mMTC: 8, voice: 1 };

/** Where the session is in SIMULATED time, which is the number that explains a thin
 *  table. A tick is a KPM window, not a second, and the engine runs at a few percent
 *  of real time — half a minute of watching can be well under one simulated second.
 *  Without this on screen an early session reads as "the engine produced nothing"
 *  when it means "almost no time has passed yet". */
function SimClock({ simT, tick }: { simT?: number | null; tick?: number }) {
  if (simT == null && tick == null) return null;
  return (
    <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-400 tabular-nums"
      title="Simulated time reached, from the RAN's own position telemetry. Ticks are KPM windows, not seconds.">
      {simT != null ? `t = ${simT.toFixed(2)} s sim` : 't = — s sim'}{tick != null ? ` · tick ${tick}` : ''}
    </span>
  );
}

/** The first simulated seconds genuinely carry little: UEs are still attaching and the
 *  KPM windows that have closed are few. Say so, rather than letting an empty-looking
 *  table be read as a broken engine. */
const EARLY_SIM_S = 2.0;
function EarlySimNote({ simT }: { simT?: number | null }) {
  if (simT == null || simT >= EARLY_SIM_S) return null;
  return (
    <div className="text-[11px] text-amber-300/90 bg-amber-500/5 border border-amber-500/20 rounded-lg px-3 py-2">
      First <span className="font-semibold tabular-nums">{simT.toFixed(2)} s</span> of simulated time —
      UEs are still attaching and few KPM windows have closed. Sparse rows here are the clock, not missing data.
    </div>
  );
}

/** Fetch a file from the backend and trigger a browser download. */
async function downloadFile(path: string, filename: string) {
  try {
    const res = await fetch(apiUrl(path), { headers: { ...authHeader() } });
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    console.error('download failed', path, e);
  }
}

function StatCard({
  icon: Icon, label, value, unit, tone = 'teal',
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string; value: string; unit?: string; tone?: 'teal' | 'sky' | 'amber' | 'violet';
}) {
  const tones: Record<string, string> = {
    teal: 'text-teal-400', sky: 'text-sky-400', amber: 'text-amber-400', violet: 'text-violet-400',
  };
  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-3">
      <div className="flex items-center gap-1.5 text-[10px] font-bold uppercase text-zinc-500 mb-1">
        <Icon className={`w-3.5 h-3.5 ${tones[tone]}`} /> {label}
      </div>
      <div className="text-2xl font-bold font-mono tabular-nums leading-none text-zinc-100">
        {value}
        {unit && <span className="text-xs text-zinc-500 font-normal ml-1">{unit}</span>}
      </div>
    </div>
  );
}

export default function RicRealEngine() {
  const config       = useNSGStore(selectConfig);
  const status       = useNSGStore(selectRicRealStatus);
  const kpm          = useNSGStore(selectRicRealKpm);
  const metrics      = useNSGStore(selectRicRealMetrics);
  const uesMap       = useNSGStore(selectRicRealUes);
  const log          = useNSGStore(selectRicRealLog);
  const controlCount = useNSGStore(selectRicRealControlCount);
  const series       = useNSGStore(selectRicRealSeries);
  const sessionId    = useNSGStore(s => s.ricRealSessionId);
  const clients      = useNSGStore(s => s.ricRealClients);
  // What the engine CONFIRMED it started, not what Sim Options asked for. A
  // shape the scenario's cell count does not divide runs whole, and a session
  // that quietly did something else is the failure this feature must not have.
  const partition    = useNSGStore(s => s.ricRealPartition);
  const start        = useNSGStore(s => s.startRicRealTest);
  const stop         = useNSGStore(s => s.stopRicRealTest);
  const sendControl  = useNSGStore(s => s.sendRicRealControl);
  const injectFault  = useNSGStore(s => s.injectRicRealFault);
  const saveRun      = useNSGStore(s => s.saveRicRealRun);

  const [ricView, setRicView] = useState<'control' | 'data'>('control');
  const [runLabel, setRunLabel] = useState('');
  const liveScore = series.length ? series[series.length - 1].score : null;
  const connected  = status === 'connected';
  const connecting = status === 'connecting';

  // Headline KPIs from the merged last-known map (steady across sparse frames);
  // tick + UE count reflect the latest indication.
  const thp  = metrics['DRB.UEThpDl']?.mean;
  const prb  = metrics['RRU.PrbUsedDl']?.mean;
  const sinr = metrics['L3servingSINR']?.mean;
  const ueCount = kpm?.ue_ids?.length;
  const fmt = (v: number | undefined, d = 0) => (typeof v === 'number' ? v.toFixed(d) : '—');

  // Per-UE rows (merged by IMSI) + a per-cell table derived from them, joined
  // with the frame's MEASURED per-cell power (bridge-tailed radio energy model).
  const ues = useMemo(() => Object.values(uesMap).sort((a, b) => a.imsi - b.imsi), [uesMap]);
  const cellRows = useMemo(() => {
    const by: Record<number, { cell: number; ues: number; thp: number; prb: number; ul: number; sinr: number[] }> = {};
    for (const u of ues) {
      if (u.cell == null) continue;
      const d = by[u.cell] ?? (by[u.cell] = { cell: u.cell, ues: 0, thp: 0, prb: 0, ul: 0, sinr: [] });
      d.ues += 1; d.thp += u.thp_kbps ?? 0; d.prb += u.prb ?? 0; d.ul += u.ul_kbps ?? 0;
      if (u.sinr != null) d.sinr.push(u.sinr);
    }
    const perCellW = kpm?.energy?.per_cell ?? {};
    const perCellSleep = kpm?.sleep?.per_cell ?? {};
    // A sleeping cell serves no UEs, so it would drop out of a UE-derived table
    // — losing exactly the row that explains the power saving. Seed those in.
    for (const k of Object.keys(perCellSleep)) {
      const c = Number(k);
      if (!Number.isNaN(c) && !by[c])
        by[c] = { cell: c, ues: 0, thp: 0, prb: 0, ul: 0, sinr: [] };
    }
    return Object.values(by).sort((a, b) => a.cell - b.cell).map(d => ({
      cell: d.cell, ues: d.ues, thp: d.thp, prb: d.prb, ul: d.ul,
      sinr: d.sinr.length ? d.sinr.reduce((x, y) => x + y, 0) / d.sinr.length : null,
      powerW: perCellW[String(d.cell)]?.w ?? null,
      sleepState: perCellSleep[String(d.cell)]?.state ?? null,
      sleepDepth: perCellSleep[String(d.cell)]?.depth ?? null,
      sleepPct: perCellSleep[String(d.cell)]?.sleep_pct ?? null,
    }));
  }, [ues, kpm]);
  const totalPowerW = kpm?.energy?.total_w;
  const liveHandovers = kpm?.handovers;
  // The live frame carries slices as a { name: kbps } dict (not an array) — flatten
  // it into rows with Mbps so the bars/cards/quota dropdown can consume it.
  const slices = useMemo(
    () => Object.entries(kpm?.slices ?? {})
      .map(([slice, kbps]) => ({ slice, throughput_mbps: (Number(kbps) || 0) / 1000 }))
      .sort((a, b) => a.slice.localeCompare(b.slice)),
    [kpm],
  );

  // slice name → QCI (config qos_id overrides, else sane default).
  const sliceQci = useMemo(() => {
    const out: Record<string, number> = { ...SLICE_QCI_DEFAULT };
    for (const g of config?.ue_groups ?? [])
      for (const svc of g.services ?? [])
        if (svc.slice_type && svc.qos_id != null) out[String(svc.slice_type)] = Number(svc.qos_id);
    return out;
  }, [config]);

  // ── Dropdown option lists for the shared control widget ──────────────────
  // Full cell list = config gNBs (numbered 2..n+1, cell 1 = LTE hub) ∪ any cell
  // seen live/in energy. This keeps a SLEPT cell (0 UEs → gone from cellRows)
  // selectable so it can still be woken.
  const cellList = useMemo(() => {
    const ids = new Set<number>();
    const nGnb = config?.base_stations?.length ?? 0;
    for (let i = 0; i < nGnb; i++) ids.add(2 + i);
    for (const c of (kpm?.cells ?? []) as { cell?: number }[]) if (typeof c.cell === 'number') ids.add(c.cell);
    for (const c of cellRows) ids.add(c.cell);
    for (const k of Object.keys(kpm?.energy?.per_cell ?? {})) { const n = Number(k); if (!Number.isNaN(n)) ids.add(n); }
    const rowByCell = new Map(cellRows.map(r => [r.cell, r]));
    const perCellW = kpm?.energy?.per_cell ?? {};
    return [...ids].sort((a, b) => a - b).map(cell => ({
      cell,
      ues: rowByCell.get(cell)?.ues ?? 0,
      powerW: rowByCell.get(cell)?.powerW ?? (perCellW[String(cell)]?.w ?? null),
    }));
  }, [config, kpm, cellRows]);

  const cellOpts: RcOption[] = cellList.map(c => ({
    id: String(c.cell),
    label: `cell ${c.cell} · ${c.ues} UE${c.powerW != null ? ` · ${c.powerW.toFixed(0)} W` : ''}`,
  }));
  const ueOpts: RcOption[] = ues.map(u => ({
    id: String(u.imsi),
    label: `UE ${u.imsi} · ${u.slice ?? '—'} · cell ${u.cell ?? '—'}`,
  }));
  const configSliceNames = useMemo(() => {
    const set = new Set<string>();
    for (const g of config?.ue_groups ?? [])
      for (const svc of g.services ?? []) if (svc.slice_type) set.add(String(svc.slice_type));
    return [...set];
  }, [config]);
  const sliceNames = configSliceNames.length ? configSliceNames : slices.map(s => s.slice);
  const sliceOpts: RcOption[] = sliceNames.map(name => ({
    id: name, label: `${name} · QCI ${sliceQci[name] ?? SLICE_QCI_DEFAULT[name] ?? '?'}`,
  }));

  // VIAVI-style time dimension: per-cell DL throughput history across ticks.
  const [cellHistory, setCellHistory] = useState<Record<string, number>[]>([]);
  useEffect(() => {
    if (!kpm?.cells?.length) return;
    setCellHistory(h => {
      const row: Record<string, number> = { tick: kpm.tick };
      for (const c of kpm.cells ?? []) row[`cell ${c.cell}`] = +((c.thp_kbps ?? 0) / 1000).toFixed(2);
      const next = [...h, row];
      return next.length > 90 ? next.slice(-90) : next;
    });
  }, [kpm]);
  useEffect(() => { if (connecting) setCellHistory([]); }, [connecting]);
  const cellSeriesKeys = useMemo(() => {
    const keys = new Set<string>();
    for (const row of cellHistory) for (const k of Object.keys(row)) if (k !== 'tick') keys.add(k);
    return [...keys].sort();
  }, [cellHistory]);
  const CELL_CHART_COLORS = ['#14b8a6', '#38bdf8', '#a78bfa', '#fbbf24', '#f43f5e', '#34d399', '#f97316'];

  const maxCellThp = Math.max(1, ...cellRows.map(c => c.thp));
  const maxSliceThp = Math.max(1, ...slices.map(s => s.throughput_mbps));

  const statusChip = {
    idle:       { t: 'Not connected', c: 'text-zinc-500 bg-zinc-800' },
    connecting: { t: 'Booting engine…', c: 'text-amber-400 bg-amber-500/10' },
    connected:  { t: 'Live', c: 'text-emerald-400 bg-emerald-500/10' },
    stopped:    { t: 'Stopped', c: 'text-zinc-500 bg-zinc-800' },
    error:      { t: 'Error', c: 'text-rose-400 bg-rose-500/10' },
  }[status];

  return (
    <div className="space-y-3">
      {/* Header + connect */}
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-zinc-100 flex items-center gap-2">
            <Radio className="w-4 h-4 text-sky-400" /> RIC Test — Closed-Loop Control
            <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${statusChip.c}`}>
              {connecting && <Loader2 className="w-3 h-3 animate-spin inline mr-1" />}{statusChip.t}
            </span>
            {/* In the header, so the simulated clock is on screen for BOTH tabs. */}
            {kpm && <SimClock simT={kpm.sim_t} tick={kpm.tick} />}
          </h3>
          <p className="text-[11px] text-zinc-500 mt-0.5">
            ns-3 mmWave RAN + FlexRIC over E2AP. Drive control yourself or connect your own xApp — every command lands as E2SM-RC on the running RAN.
          </p>
        </div>
        <div className="shrink-0 flex items-center gap-2">
          <button
            onClick={() => downloadJson('nsg-ric-real-session', {
              exported_at: new Date().toISOString(),
              status,
              controls_sent: controlCount,
              score_series: series,
              latest_kpm: kpm ? {
                tick: kpm.tick, cells: kpm.cells, ues: Object.values(uesMap),
                slices: kpm.slices, handovers: kpm.handovers, energy: kpm.energy,
              } : null,
              measurements: metrics,
              session_log: log.map(l => ({ ts: l.ts, kind: l.kind, line: l.line })),
            })}
            disabled={!kpm && series.length === 0}
            title="Download this session's measured data (score series, cell/UE tables, KPM measurements, log) as JSON"
            className="text-[11px] font-semibold px-3 py-1.5 rounded-lg flex items-center gap-1.5
              bg-zinc-800 border border-zinc-700 text-zinc-300 hover:bg-zinc-700
              disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
            <Download className="w-3.5 h-3.5" /> Export
          </button>
          {connected || connecting ? (
            <button onClick={stop}
              className="text-[11px] font-semibold px-3 py-1.5 rounded-lg flex items-center gap-1.5
                bg-rose-500/15 text-rose-300 border border-rose-500/30 hover:bg-rose-500/25 transition-colors">
              <Square className="w-3.5 h-3.5" /> Stop
            </button>
          ) : (
            <button onClick={start}
              className="text-[11px] font-semibold px-3 py-1.5 rounded-lg flex items-center gap-1.5
                bg-sky-500/15 text-sky-300 border border-sky-500/30 hover:bg-sky-500/25 transition-colors">
              <Play className="w-3.5 h-3.5" /> Start live session
            </button>
          )}
        </div>
      </div>

      {partition && (
        <div className={`flex items-start gap-2 text-[11px] rounded-lg px-3 py-2 border ${
          partition.handover === 'none'
            ? 'text-rose-300/90 bg-rose-500/5 border-rose-500/20'
            : partition.handover === 'reduced'
              ? 'text-amber-300/90 bg-amber-500/5 border-amber-500/20'
              : 'text-teal-300/90 bg-teal-500/5 border-teal-500/20'}`}>
          <Cpu className="w-3.5 h-3.5 shrink-0 mt-0.5" />
          <span>
            This RAN is running as <strong>{partition.groups} processes of{' '}
            {partition.cellsPerGroup} cell(s)</strong> — the shape chosen in Sim Options.
            Handover <strong>{partition.handover}</strong>.{' '}
            {partition.handover === 'none'
              ? 'A UE has no target cell inside its process, so handover, load balancing '
                + 'and coverage-compensated sleep cannot happen here — control commands '
                + 'still land, but this is a capacity view, not a RIC control test.'
              : partition.handover === 'reduced'
                ? 'Handovers crossing a group boundary do not happen; measured, between '
                  + 'a third and a half of them go missing — and the smaller the network, '
                  + 'the larger the share of each cell\u2019s neighbours that sit outside '
                  + 'its group.'
                : 'Cells inside a group hand over normally. What is lost is interference '
                  + 'and mobility BETWEEN groups.'}
          </span>
        </div>
      )}

      {/* View toggle: control vs the live data dashboard (mirrors the sandbox) */}
      <div className="flex items-center gap-1 bg-zinc-900 border border-zinc-800 rounded-lg p-1 w-fit">
        <button onClick={() => setRicView('control')} className={`flex items-center gap-1.5 px-3 py-1 text-[11px] font-medium rounded-md transition-colors ${ricView === 'control' ? 'bg-zinc-800 text-teal-400' : 'text-zinc-500 hover:text-zinc-300'}`}>
          <Radio className="w-3.5 h-3.5" /> Control
        </button>
        <button onClick={() => setRicView('data')} className={`flex items-center gap-1.5 px-3 py-1 text-[11px] font-medium rounded-md transition-colors ${ricView === 'data' ? 'bg-zinc-800 text-teal-400' : 'text-zinc-500 hover:text-zinc-300'}`}>
          <BarChart3 className="w-3.5 h-3.5" /> Data
        </button>
      </div>

      {/* ══════════════════════ DATA TAB ══════════════════════ */}
      {ricView === 'data' && (
        <>
          {kpm && <EarlySimNote simT={kpm.sim_t} />}

          {/* Headline KPIs — all measured on the running RAN. "Sim time" leads because
              every other number on this row is only as meaningful as the time behind it. */}
          <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-2">
            <StatCard icon={Clock} label="Sim time"
              value={kpm?.sim_t != null ? kpm.sim_t.toFixed(2) : '—'}
              unit={kpm ? `s · tick ${kpm.tick}` : 's'} tone="amber" />
            <StatCard icon={Zap}   label="DL Thp / UE" value={fmt(thp)}  unit="kbps" tone="teal" />
            <StatCard icon={Gauge} label="PRB used"    value={fmt(prb)}  unit="PRB"  tone="sky" />
            <StatCard icon={Signal} label="Serving SINR" value={fmt(sinr, 1)} unit="dB" tone="violet" />
            <StatCard icon={Power} label="Power (measured)"
              value={wattParts(totalPowerW).value}
              unit={wattParts(totalPowerW).unit} tone="amber" />
            <StatCard icon={Send} label="Handovers"
              value={liveHandovers != null ? String(liveHandovers) : '—'} unit="live" tone="sky" />
            <StatCard icon={Users} label="UEs" value={`${ueCount ?? '—'}`} unit="attached" tone="teal" />
          </div>

          {!kpm && (
            <div className="text-[12px] text-zinc-500 py-16 text-center">
              {connecting ? 'Booting the real engine (RIC + ns-3, ~30–40s)…' : 'Start a live session to see live data.'}
            </div>
          )}

          {/* Live map — each real UE at its own position + serving-cell lines */}
          {kpm && <RicRealTopology ues={ues} config={config} />}

          {/* Cells — throughput & load bars (mirror of the sandbox dashboard) */}
          {cellRows.length > 0 && (
            <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-3">
              <div className="text-[10px] font-bold text-zinc-500 uppercase mb-3">Cells — throughput &amp; load</div>
              <div className="flex flex-col gap-2">
                {cellRows.map(c => (
                  <div key={c.cell} className="flex items-center gap-2">
                    <span className="w-20 truncate text-[11px] text-zinc-300">cell {c.cell}</span>
                    <div className="flex-1 h-2.5 bg-zinc-800 rounded-full overflow-hidden">
                      <div className="h-full rounded-full bg-teal-500 transition-all duration-500"
                        style={{ width: `${Math.max(2, (c.thp / maxCellThp) * 100)}%` }} />
                    </div>
                    <span className="w-20 text-right text-[11px] font-mono text-zinc-400 tabular-nums">{(c.thp / 1000).toFixed(1)} Mb</span>
                    <span className="w-14 text-right text-[11px] font-mono text-sky-400 tabular-nums">{c.prb.toFixed(0)} PRB</span>
                    <span className="w-12 text-right text-[10px] font-mono text-amber-300/80">{c.powerW != null ? `${c.powerW.toFixed(0)}W` : '—'}</span>
                    <span className="w-12 text-right text-[9px] text-teal-500/70 font-mono">{c.ues} ue</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Slices — throughput bars */}
          {slices.length > 0 && (
            <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-3">
              <div className="text-[10px] font-bold text-zinc-500 uppercase mb-3">Slices — throughput</div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3">
                {slices.map(s => (
                  <div key={s.slice}>
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-[11px] text-zinc-300 font-medium">{s.slice}</span>
                      <span className="text-[11px] font-mono text-zinc-400 tabular-nums">{s.throughput_mbps.toFixed(1)} Mbps</span>
                    </div>
                    <div className="h-2 bg-zinc-800 rounded-full overflow-hidden">
                      <div className="h-full rounded-full bg-teal-500 transition-all duration-500"
                        style={{ width: `${Math.max(2, (s.throughput_mbps / maxSliceThp) * 100)}%` }} />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Per-cell power tiles (VIAVI-style) — MEASURED by the radio energy model */}
          {cellRows.length > 0 && (
            <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-3">
              <div className="text-[10px] font-bold uppercase text-zinc-500 mb-2 flex items-center gap-1.5">
                <Power className="w-3.5 h-3.5 text-amber-400" /> Power per cell (measured)
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
                {cellRows.map(c => {
                  const maxW = Math.max(...cellRows.map(x => x.powerW ?? 0), 1);
                  const r = (c.powerW ?? 0) / maxW;
                  const down = c.sleepState === 'asleep' || c.sleepState === 'waking';
                  const tone = down
                    ? 'border-sky-500/30 bg-sky-500/15 text-sky-300'
                    : c.powerW == null
                    ? 'border-zinc-800 bg-zinc-800/40 text-zinc-500'
                    : r > 0.8 ? 'border-rose-500/30 bg-rose-500/15 text-rose-300'
                    : r > 0.5 ? 'border-amber-500/30 bg-amber-500/15 text-amber-300'
                    : 'border-emerald-500/30 bg-emerald-500/15 text-emerald-300';
                  return (
                    <div key={c.cell} className={`rounded-lg border p-2.5 ${tone}`}>
                      <div className="text-[10px] font-bold uppercase opacity-80">cell {c.cell} · {c.ues} UE</div>
                      <div className="text-lg font-mono">
                        {c.powerW != null ? c.powerW.toFixed(0) : '—'}
                        <span className="text-[10px] opacity-70 ml-1">W</span>
                      </div>
                      {/* Why the draw is low: the RU descended into a sleep depth. */}
                      {c.sleepState && c.sleepDepth !== 'none' && (
                        <div className="text-[9px] font-mono opacity-80 mt-0.5">
                          {c.sleepState === 'waking' ? 'waking' : c.sleepState === 'asleep'
                            ? `${c.sleepDepth} sleep` : `${c.sleepDepth} armed`}
                          {c.sleepPct != null && c.sleepPct > 0 && ` · ${c.sleepPct.toFixed(0)}%`}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* Per-cell DL throughput over time — measured, per tick */}
          {cellHistory.length > 1 && (
            <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-3">
              <div className="text-[10px] font-bold uppercase text-zinc-500 mb-2 flex items-center gap-1.5">
                <Zap className="w-3.5 h-3.5 text-teal-400" /> DL throughput per cell (Mbps · live)
              </div>
              <div className="h-44">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={cellHistory} margin={{ top: 4, right: 8, bottom: 0, left: -18 }}>
                    <XAxis dataKey="tick" tick={{ fill: '#52525b', fontSize: 10 }} stroke="#3f3f46" />
                    <YAxis tick={{ fill: '#52525b', fontSize: 10 }} stroke="#3f3f46" />
                    <ChartTooltip
                      contentStyle={{ background: '#18181b', border: '1px solid #3f3f46', borderRadius: 8, fontSize: 11 }}
                      labelStyle={{ color: '#a1a1aa' }}
                    />
                    <Legend wrapperStyle={{ fontSize: 10 }} />
                    {cellSeriesKeys.map((k, i) => (
                      <Line key={k} type="monotone" dataKey={k} dot={false} strokeWidth={2}
                        stroke={CELL_CHART_COLORS[i % CELL_CHART_COLORS.length]} isAnimationActive={false} />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}
        </>
      )}

      {/* ══════════════════════ CONTROL TAB ══════════════════════ */}
      {ricView === 'control' && (
        <>
          {kpm && <EarlySimNote simT={kpm.sim_t} />}

          {/* KPM tables (Control headline) */}
          {ues.length > 0 ? (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-2">
              <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-3 overflow-x-auto">
                <div className="text-[10px] font-bold uppercase text-zinc-500 mb-2 flex items-center gap-1.5">
                  <Grid3x3 className="w-3.5 h-3.5 text-sky-400" /> Cell reports (KPM)
                </div>
                <table className="w-full text-[11px]">
                  <thead><tr className="text-zinc-600 text-left">
                    <th className="font-medium pb-1">Cell</th>
                    <th className="font-medium pb-1 text-right">UEs</th>
                    <th className="font-medium pb-1 text-right">DL kbps</th>
                    <th className="font-medium pb-1 text-right">UL kbps</th>
                    <th className="font-medium pb-1 text-right">PRB</th>
                    <th className="font-medium pb-1 text-right">SINR</th>
                    <th className="font-medium pb-1 text-right" title="Measured by the radio energy model">W</th>
                  </tr></thead>
                  <tbody className="font-mono">
                    {cellRows.map(c => (
                      <tr key={c.cell} className="border-t border-zinc-800/60">
                        <td className="py-1 font-sans text-zinc-300">cell {c.cell}</td>
                        <td className="py-1 text-right text-amber-400">{c.ues}</td>
                        <td className="py-1 text-right text-teal-400">{c.thp.toFixed(0)}</td>
                        <td className="py-1 text-right text-cyan-400">{c.ul.toFixed(0)}</td>
                        <td className="py-1 text-right text-sky-400">{c.prb.toFixed(0)}</td>
                        <td className="py-1 text-right text-violet-300">{c.sinr != null ? c.sinr.toFixed(1) : '—'}</td>
                        <td className="py-1 text-right text-amber-300/90">{c.powerW != null ? c.powerW.toFixed(0) : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-3 overflow-x-auto">
                <div className="text-[10px] font-bold uppercase text-zinc-500 mb-2 flex items-center gap-1.5">
                  <Users className="w-3.5 h-3.5 text-amber-400" /> UE reports (KPM)
                </div>
                <table className="w-full text-[11px]">
                  <thead><tr className="text-zinc-600 text-left">
                    <th className="font-medium pb-1">UE (IMSI)</th>
                    <th className="font-medium pb-1">Slice</th>
                    <th className="font-medium pb-1 text-right">Cell</th>
                    <th className="font-medium pb-1 text-right">SINR</th>
                    <th className="font-medium pb-1 text-right">DL kbps</th>
                    <th className="font-medium pb-1 text-right">UL kbps</th>
                    <th className="font-medium pb-1 text-right">Delay ms</th>
                    <th className="font-medium pb-1 text-right">PRB</th>
                  </tr></thead>
                  <tbody className="font-mono">
                    {ues.map(u => (
                      <tr key={u.imsi} className="border-t border-zinc-800/60">
                        <td className="py-1 text-zinc-200">{u.imsi}</td>
                        <td className="py-1 font-sans text-[10px] text-zinc-400">{u.slice ?? '—'}</td>
                        <td className="py-1 text-right text-zinc-400">{u.cell ?? '—'}</td>
                        <td className="py-1 text-right text-violet-300">{u.sinr != null ? u.sinr.toFixed(1) : '—'}</td>
                        <td className="py-1 text-right text-teal-400">{u.thp_kbps != null ? u.thp_kbps.toFixed(0) : '—'}</td>
                        <td className="py-1 text-right text-cyan-400">{u.ul_kbps != null ? u.ul_kbps.toFixed(0) : '—'}</td>
                        <td className="py-1 text-right text-zinc-400">{u.delay_ms != null ? u.delay_ms.toFixed(1) : '—'}</td>
                        <td className="py-1 text-right text-sky-400">{u.prb != null ? u.prb.toFixed(0) : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : (
            <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-3 text-[12px] text-zinc-500 text-center py-8">
              {connecting ? 'Booting the real engine (RIC + ns-3, ~30–40s)…'
                : connected ? 'Live — waiting for the first KPM indication…'
                : 'Start a live session to stream real KPM and drive control.'}
            </div>
          )}

          {/* Slice reports (KPM) */}
          {slices.length > 0 && (
            <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-3">
              <div className="text-[10px] font-bold uppercase text-zinc-500 mb-2 flex items-center gap-1.5">
                <Layers className="w-3.5 h-3.5 text-violet-400" /> Slice reports (KPM)
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                {slices.map(s => (
                  <div key={s.slice} className="bg-zinc-950/50 border border-zinc-800 rounded-lg p-2">
                    <div className="text-[11px] font-bold text-zinc-300">{s.slice}</div>
                    <div className="text-[10px] text-zinc-500">Tput <span className="text-zinc-300 font-mono">{s.throughput_mbps.toFixed(1)}</span> Mbps</div>
                    <div className="text-[10px] text-zinc-600">QCI {sliceQci[s.slice] ?? '—'}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Manual RC control (shared widget, graded dBm on the real engine) */}
          <ManualRcControl
            cells={cellOpts}
            ues={ueOpts}
            slices={sliceOpts}
            disabled={!connected}
            gradedPower
            onSleep={id => sendControl({ cmd: 'cell_off', cell: Number(id) })}
            onWake={id => sendControl({ cmd: 'cell_on', cell: Number(id) })}
            onSetPower={(id, dbm) => sendControl({ cmd: 'cell_power', cell: Number(id), dbm })}
            onHandover={(ueId, targetId) => sendControl({ cmd: 'handover', ue: Number(ueId), cell: Number(targetId) })}
            onSliceQuota={(sliceId, pct) => sendControl({ cmd: 'slice_quota', qci: sliceQci[sliceId] ?? SLICE_QCI_DEFAULT[sliceId] ?? 9, pct })}
            onFault={id => injectFault(Number(id))}
          />
          <p className="text-[10px] text-zinc-600 -mt-1 px-1">
            Real E2SM-RC on the live gNBs · {controlCount} sent this session · Sleep/Wake/Set drive TX power (Energy_state, any dBm), Steer moves a UE (KPM id = IMSI), Inject fault kills a cell the xApp must route around.
          </p>

          {/* Save run for the Compare tab */}
          <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[11px] font-bold text-teal-400 uppercase flex items-center gap-1.5">
                <Trophy className="w-3.5 h-3.5" /> Save run
              </span>
              <span className="text-[11px] text-zinc-500 flex items-center gap-1.5">
                live score <span className="font-mono font-bold text-teal-400">{liveScore != null ? liveScore.toFixed(1) : '—'}</span>
                <span className="text-zinc-600">· {series.length} ticks</span>
              </span>
              <span className="flex-1" />
              <input
                value={runLabel} onChange={e => setRunLabel(e.target.value)} placeholder="run label (e.g. cell-2 asleep)"
                className="min-w-[160px] bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-[11px] text-zinc-200 placeholder:text-zinc-600 outline-none focus:border-teal-500"
              />
              <button
                onClick={() => { saveRun(runLabel.trim() || `real run #${series.length}`); setRunLabel(''); }}
                disabled={series.length === 0}
                className="text-[11px] font-bold px-3 py-1.5 rounded-lg flex items-center gap-1.5
                  bg-teal-600 hover:bg-teal-500 text-black disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
                <Save className="w-3.5 h-3.5" /> Save run
              </button>
            </div>
            <p className="text-[10px] text-zinc-600 mt-1.5">
              Drive the RAN (or connect an xApp), then Save — saved runs rank head-to-head on <span className="text-zinc-300">real data</span> in the <span className="text-amber-400 font-semibold">Compare</span> tab.
            </p>
          </div>

          {/* Session log */}
          <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-3">
            <div className="text-[10px] font-bold uppercase text-zinc-500 mb-2 flex items-center gap-1.5">
              <Terminal className="w-3.5 h-3.5" /> Live session log
            </div>
            {log.length === 0 ? (
              <p className="text-[11px] text-zinc-600 py-4 text-center">
                {connecting ? 'Booting the real engine (RIC + ns-3, ~30–40s)…' : 'Start a live session to see the real E2 handshake, KPM, and control echoes.'}
              </p>
            ) : (
              <div className="max-h-52 overflow-y-auto font-mono text-[11px] space-y-0.5">
                {[...log].reverse().map(e => (
                  <div key={e.id} className="flex gap-2">
                    <span className={
                      e.kind === 'control' ? 'text-teal-400'
                      : e.kind === 'error' ? 'text-rose-400'
                      : 'text-zinc-400'
                    }>
                      {e.kind === 'control' ? '▸ ctrl' : e.kind === 'error' ? '✕ err' : '·'}
                    </span>
                    <span className="text-zinc-400 break-all">{e.line}</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Connect your own xApp */}
          <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-3">
            <div className="text-[10px] font-bold uppercase text-zinc-500 mb-1.5 flex items-center gap-1.5">
              <Download className="w-3.5 h-3.5" /> Connect your own xApp
            </div>
            <p className="text-[11px] text-zinc-500 leading-snug mb-2.5">
              The browser is the xApp here. Your own trained model connects the same way —
              over <code className="text-zinc-400">ws://…/ws/e2-real</code>, plain JSON, no FlexRIC SDK. Grab the
              turnkey connector, drop your model into <code className="text-zinc-400">decide()</code>, run it.
            </p>
            {sessionId ? (
              <div className="mb-2.5 flex flex-wrap items-center gap-2 text-[11px]">
                <span className="text-zinc-500">Join this live session:</span>
                <code className="px-1.5 py-0.5 rounded bg-sky-500/15 text-sky-300 border border-sky-500/25 font-mono">
                  {sessionId}
                </code>
                {clients.length > 0 && (
                  <span className="text-zinc-500">· attached: {clients.join(', ')}</span>
                )}
              </div>
            ) : (
              <div className="mb-2.5 text-[11px] text-zinc-600">
                Start a live session to get a join id — the xApp then drives THIS run on your
                scenario instead of a default one.
              </div>
            )}
            <div className="flex flex-wrap gap-2">
              <button
                onClick={() => downloadFile(
                  `/ric/xapp-connector-real${sessionId ? `?session=${sessionId}` : ''}`,
                  'nsg_xapp_real.py')}
                title={sessionId
                  ? 'Connector pre-filled to JOIN this live session'
                  : 'No live session — the xApp will start its own on the default scenario'}
                className="text-[11px] font-medium px-2.5 py-1.5 rounded-lg flex items-center gap-1.5
                  border border-sky-500/30 text-sky-300 hover:bg-sky-500/15 transition-colors">
                <Download className="w-3.5 h-3.5" /> xApp connector (real engine)
              </button>
              <button
                onClick={() => downloadFile('/ric/training-kit', 'nsg_training_kit.zip')}
                className="text-[11px] font-medium px-2.5 py-1.5 rounded-lg flex items-center gap-1.5
                  border border-zinc-700 text-zinc-400 hover:text-teal-400 hover:border-teal-500/50 transition-colors">
                <Download className="w-3.5 h-3.5" /> Training kit
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
