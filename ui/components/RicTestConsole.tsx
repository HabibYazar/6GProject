'use client';

/**
 * RicTestConsole — the RIC surface, over either tier of the same engine.
 *
 * THERE IS ONE ENGINE AND TWO TIERS OF IT. This console used to offer an
 * "analytic sandbox" beside a "real engine", and the sandbox was a Shannon
 * capacity with a PRB throttle — a different model, not a faster one. It is
 * gone. What the toggle now selects is a TIER:
 *
 *   FAST   the per-window model (/ws/e2). Its scheduler, AMC and
 *          transport-block sizes are the engine's own tables, and since the
 *          tier-B integration its CHANNEL is too — 3GPP UMa/UMi with
 *          shadowing, per-cell carriers and the antenna pattern. It computes
 *          a KPM window in one step instead of stepping ns-3 4000 times for
 *          it, which is where the ~1000x comes from.
 *   SLOW   ns-3 / ns-O-RAN itself (/ws/e2-real), through the live bridge.
 *
 * Both keep the real RIC and real E2AP; only the RAN underneath changes.
 * That is the whole commercial claim, so the two have to sit behind one
 * switch on one screen — a customer compares them, and two separate tabs
 * would make the comparison a navigation exercise.
 *
 * Control = KPM tables + manual levers + Save run + connector;
 * Data = the RicDataView dashboard. Both tiers carry the identical split.
 */

import { useState } from 'react';
import { formatWatts } from '@/utils/power';
import { authHeader } from '@/lib/auth';
import {
  Play, Square, Radio, CheckCircle2, XCircle, Plug, Copy, Download, Trophy, Save,
  Zap, FlaskConical, Lightbulb, BarChart3,
} from 'lucide-react';
import { apiUrl } from '@/lib/api';
import { RIC_PRESETS } from '@/utils/ricPresets';
import RicDataView from './RicDataView';
import RicRealEngine from './RicRealEngine';
import ManualRcControl, { type RcOption } from './ManualRcControl';
import {
  useNSGStore,
  selectConfig,
  selectRicKpm,
  selectRicStatus,
  selectRicControlLog,
  selectRicSessionId,
  selectRicRoster,
  selectRicRunSeries,
  selectRicSavedRuns,
} from '@/store/useNSGStore';

const SLICE_COLORS: Record<string, string> = {
  eMBB: '#14b8a6', uRLLC: '#f43f5e', mMTC: '#a78bfa', voice: '#fbbf24',
};

function rsrpColor(rsrp: number | null): string {
  if (rsrp === null) return '#71717a';
  if (rsrp >= -80) return '#22c55e';
  if (rsrp >= -100) return '#eab308';
  return '#ef4444';
}

export default function RicTestConsole() {
  const config       = useNSGStore(selectConfig);
  const kpm          = useNSGStore(selectRicKpm);
  const status       = useNSGStore(selectRicStatus);
  const controlLog   = useNSGStore(selectRicControlLog);
  const sessionId    = useNSGStore(selectRicSessionId);
  const roster       = useNSGStore(selectRicRoster);
  const runSeries    = useNSGStore(selectRicRunSeries);
  const savedRuns    = useNSGStore(selectRicSavedRuns);
  const startRicTest = useNSGStore(s => s.startRicTest);
  const stopRicTest  = useNSGStore(s => s.stopRicTest);
  const setConfig    = useNSGStore(s => s.setConfig);
  const loadConfig   = useNSGStore(s => s.loadConfig);
  const sendRicControl = useNSGStore(s => s.sendRicControl);
  const injectAnomaly  = useNSGStore(s => s.injectAnomaly);
  const saveRicRun     = useNSGStore(s => s.saveRicRun);

  const isActive = status === 'connected' || status === 'connecting';
  const externalXapps = roster.filter(r => r !== 'browser' && r !== 'ai-xapp');

  const [ricView, setRicView] = useState<'control' | 'data'>('control');
  // Which TIER of the one engine. Default fast: it answers in milliseconds
  // where the slow tier needs a container, a session and minutes.
  const [engineMode, setEngineMode] = useState<'fast' | 'slow'>('fast');
  // Real time by default — see the note on the selector.
  const [rate, setRate] = useState<number>(1);

  // Run comparison
  const [runLabel, setRunLabel] = useState('');
  const liveScore = runSeries.length ? runSeries[runSeries.length - 1].score : null;
  const doSaveRun = () => {
    saveRicRun(runLabel.trim() || `run #${savedRuns.length + 1}`);
    setRunLabel('');
  };

  const downloadConnector = async () => {
    if (!sessionId) return;
    try {
      const res = await fetch(apiUrl(`/ric/xapp-connector?session=${sessionId}`), { headers: { ...authHeader() } });
      const text = await res.text();
      const blob = new Blob([text], { type: 'text/x-python' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'nsg_xapp.py';
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      console.error('connector download failed', e);
    }
  };

  const downloadTrainingKit = async () => {
    try {
      const res = await fetch(apiUrl('/ric/training-kit'), { headers: { ...authHeader() } });
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'nsg_training_kit.zip';
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      console.error('training kit download failed', e);
    }
  };

  const cells = kpm?.cells ?? [];
  const ueGroups = kpm?.ue_groups ?? [];
  // Per UE. The group rows above stay because the control levers act on a
  // group; the table below reports what the tier actually resolves.
  const perUe = kpm?.ues ?? [];
  const slices = kpm?.slices ?? [];
  const activePreset = RIC_PRESETS.find(p => p.config.name === config?.name);

  // Normalize the sandbox KPM into the shared ManualRcControl option shape.
  const cellOpts: RcOption[] = cells.map(c => ({
    id: c.id,
    label: `${c.name}${c.faulted ? ' · FAULT' : (!c.active ? ' · asleep' : '')}`,
  }));
  const ueOpts: RcOption[] = ueGroups.map(u => ({ id: u.id, label: u.name }));
  const sliceOpts: RcOption[] = slices.map(s => ({ id: s.slice, label: s.slice }));
  const faultedOpts: RcOption[] = cells.filter(c => c.faulted).map(c => ({ id: c.id, label: c.name }));
  const surge = kpm?.anomalies?.demand_multiplier ?? 1;

  // ONE engine, two tiers. The label says which RAN is underneath and what
  // it costs, because that is the only thing the switch changes.
  const engineToggle = (
    <div className="shrink-0 flex items-center gap-2">
      <div className="flex items-center gap-1 bg-zinc-900 border border-zinc-800 rounded-lg p-1 w-fit">
        <button
          onClick={() => setEngineMode('fast')}
          title="Per-window model: the engine's own AMC, transport-block and channel, computed one window at a time"
          className={`flex items-center gap-1.5 px-3 py-1 text-[11px] font-semibold rounded-md transition-colors ${engineMode === 'fast' ? 'bg-teal-500/15 text-teal-400' : 'text-zinc-500 hover:text-zinc-300'}`}
        >
          <Zap className="w-3.5 h-3.5" /> Fast tier
        </button>
        <button
          onClick={() => setEngineMode('slow')}
          title="ns-3 / ns-O-RAN itself, through the live bridge"
          className={`flex items-center gap-1.5 px-3 py-1 text-[11px] font-semibold rounded-md transition-colors ${engineMode === 'slow' ? 'bg-sky-600 text-white' : 'text-zinc-500 hover:text-zinc-300'}`}
        >
          <Radio className="w-3.5 h-3.5" /> Slow tier (ns-3)
        </button>
      </div>
      {engineMode === 'fast' && isActive && (
        /* HOW FAST SIMULATED TIME RUNS. 1x is real time and the default,
           because what a RIC test rig tests is an xApp, and an xApp is a
           control loop whose deadline O-RAN defines in WALL clock —
           near-real-time is 10 ms to 1 s. The fast tier could run this
           scene ~200x; the reason it does not is that at 100x a control
           computed from one report reaches a RAN a hundred simulated
           seconds past it, and the loop under test is then not the one
           that will ship. Above 1x is for watching, not for testing, and
           the warning under it says exactly that. The slow tier has no
           such control: ns-3 runs at a few percent of real time and
           cannot be sped up. */
        <label className="flex items-center gap-1.5 text-[10px] text-zinc-500">
          speed
          <select
            value={rate}
            onChange={e => { const r = Number(e.target.value); setRate(r);
                             sendRicControl({ action: 'set_rate', rate: r }); }}
            className="rounded border border-zinc-800 bg-zinc-900 px-1.5 py-0.5 font-mono text-[11px] text-teal-400 outline-none focus:border-teal-500"
          >
            {[1, 2, 5, 10, 25, 50].map(v => (
              <option key={v} value={v}>{v}x{v === 1 ? ' (real time)' : ''}</option>
            ))}
          </select>
        </label>
      )}
      <span className="text-[10px] text-zinc-600">
        {engineMode === 'slow'
          ? 'same RIC, same E2AP — ns-3 stepping every TTI, a few percent of real time'
          : rate === 1
            ? 'same RIC, same E2AP — real time, so an xApp\u2019s control loop is timed the way it will be in the field'
            : 'same RIC, same E2AP'}
      </span>
    </div>
  );

  const rateWarning = engineMode === 'fast' && isActive
    ? (kpm as { rate_warning?: string | null } | null)?.rate_warning : null;

  if (engineMode === 'slow') {
    return (
      <div className="flex flex-col h-full gap-3 overflow-y-auto">
        {engineToggle}
        <RicRealEngine />
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full gap-3 overflow-y-auto">
      {engineToggle}
      {rateWarning && (
        <div className="shrink-0 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-[11px] leading-snug text-amber-400/90">
          {rateWarning}
        </div>
      )}
      {/* Header + connect */}
      <div className="flex items-center justify-between shrink-0">
        <div>
          <h3 className="text-sm font-semibold text-zinc-200 flex items-center gap-2">
            RIC Test — Closed-Loop Control
            {status === 'connected' && (
              <span className="flex items-center gap-1 text-[10px] font-normal text-teal-400">
                <span className="w-1.5 h-1.5 rounded-full bg-teal-400 animate-pulse" /> E2 LIVE
              </span>
            )}
            {/* The simulated clock, in the header, on BOTH tiers. The slow
                tier has always shown it here and the fast tier did not, so
                switching tier moved it — and it is the number every other
                number on the screen depends on. */}
            {kpm?.sim_t != null && (
              <span className="text-[10px] font-normal text-amber-400/80 font-mono">
                t = {kpm.sim_t.toFixed(2)} s sim · tick {kpm.tick ?? 0}
              </span>
            )}
          </h3>
          <p className="text-[11px] text-zinc-500">
            Per-window RAN over the same RIC and the same E2AP. Drive control yourself or connect your own xApp — the scenario stays live so you can swap AIs and compare.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={downloadTrainingKit}
            className="flex items-center gap-1.5 px-2.5 py-1.5 text-[11px] font-medium rounded-lg border border-zinc-700 text-zinc-400 hover:text-teal-400 hover:border-teal-500/50 transition-colors"
            title="Download the local RL training kit to build/train your own AI"
          >
            <Download className="w-3.5 h-3.5" /> Training kit
          </button>
          {isActive ? (
            <button
              onClick={stopRicTest}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg border border-zinc-700 text-zinc-400 hover:text-zinc-200 hover:border-zinc-600 transition-colors"
            >
              <Square className="w-3.5 h-3.5" /> Disconnect
            </button>
          ) : (
            <button
              onClick={startRicTest}
              disabled={!config}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-bold rounded-lg bg-teal-600 hover:bg-teal-500 text-black disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              <Play className="w-3.5 h-3.5" /> Connect E2
            </button>
          )}
        </div>
      </div>

      {/* View toggle: control panels vs the live data dashboard */}
      <div className="shrink-0 flex items-center gap-1 bg-zinc-900 border border-zinc-800 rounded-lg p-1 w-fit">
        <button onClick={() => setRicView('control')} className={`flex items-center gap-1.5 px-3 py-1 text-[11px] font-medium rounded-md transition-colors ${ricView === 'control' ? 'bg-zinc-800 text-teal-400' : 'text-zinc-500 hover:text-zinc-300'}`}>
          <Radio className="w-3.5 h-3.5" /> Control
        </button>
        <button onClick={() => setRicView('data')} className={`flex items-center gap-1.5 px-3 py-1 text-[11px] font-medium rounded-md transition-colors ${ricView === 'data' ? 'bg-zinc-800 text-teal-400' : 'text-zinc-500 hover:text-zinc-300'}`}>
          <BarChart3 className="w-3.5 h-3.5" /> Data
        </button>
      </div>

      {ricView === 'data' && <RicDataView />}

      {ricView === 'control' && (
      <>
      {/* ── Scenario presets: ready challenges, no scenario-building needed ── */}
      {!isActive && (
        <div className="shrink-0 bg-zinc-900 border border-zinc-800 rounded-xl p-3">
          <div className="text-[10px] font-bold text-teal-400 uppercase mb-1 flex items-center gap-1.5">
            <FlaskConical className="w-3.5 h-3.5" /> Test scenario preset
          </div>
          <p className="text-[10px] text-zinc-600 mb-2.5">
            No AI or scenario-building needed — pick a ready challenge with a known right answer, then Connect E2. Each is deterministic, so two AIs are compared on identical physics.
          </p>
          <div className="grid grid-cols-3 gap-2">
            {RIC_PRESETS.map(p => {
              const active = config?.name === p.config.name;
              return (
                <button key={p.id} onClick={() => { void loadConfig(p.config) }}
                  className={`text-left p-2.5 rounded-lg border transition-colors ${
                    active ? 'bg-teal-600/15 border-teal-500/50' : 'bg-zinc-950/50 border-zinc-800 hover:border-zinc-600'
                  }`}>
                  <div className="flex items-center justify-between gap-1">
                    <span className="text-[11px] font-bold text-zinc-200">{p.label}</span>
                    {active && <CheckCircle2 className="w-3.5 h-3.5 text-teal-400 shrink-0" />}
                  </div>
                  <div className="text-[9px] text-teal-500/80 uppercase tracking-wide mt-0.5">{p.tag}</div>
                  <div className="text-[10px] text-zinc-500 mt-1 leading-snug">{p.challenge}</div>
                </button>
              );
            })}
          </div>
          {activePreset && (
            <div className="mt-2 flex items-start gap-1.5 text-[10px] text-amber-300/90 bg-amber-950/20 border border-amber-900/30 rounded-lg px-2 py-1.5">
              <Lightbulb className="w-3 h-3 mt-0.5 shrink-0" />
              <span><span className="font-bold">Right answer:</span> {activePreset.hint}</span>
            </div>
          )}
        </div>
      )}

      {/* ── Session banner: how to connect an external xApp ─────────────── */}
      {isActive && sessionId && (
        <div className="shrink-0 bg-zinc-900 border border-teal-500/25 rounded-xl p-3 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 min-w-0">
            <Plug className="w-4 h-4 text-teal-400 shrink-0" />
            <div className="min-w-0">
              <div className="text-[11px] text-zinc-300">
                Bring your own AI:
                <code className="mx-1.5 px-1.5 py-0.5 bg-zinc-950 border border-zinc-700 rounded text-teal-400 font-mono">{sessionId}</code>
              </div>
              <div className="text-[10px] text-zinc-600 truncate">
                Download the connector → drop your model into <code className="text-zinc-500">decide()</code> → run it on your machine. It joins this session over WebSocket.
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {externalXapps.length > 0 && (
              <span className="text-[10px] text-teal-400 flex items-center gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-teal-400 animate-pulse" />
                {externalXapps.length} xApp{externalXapps.length !== 1 ? 's' : ''} connected
              </span>
            )}
            <button
              onClick={downloadConnector}
              className="flex items-center gap-1.5 px-2.5 py-1 text-[10px] font-bold rounded-lg bg-zinc-800 border border-zinc-700 text-zinc-300 hover:border-teal-500/50 hover:text-teal-400 transition-colors"
              title="Download a ready-to-run Python connector pre-filled with this session"
            >
              <Download className="w-3.5 h-3.5" /> Download connector
            </button>
            <button
              onClick={() => navigator.clipboard?.writeText(sessionId)}
              className="text-zinc-500 hover:text-teal-400 transition-colors"
              title="Copy session id"
            >
              <Copy className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      )}

      {!kpm ? (
        <div className="flex-1 flex flex-col items-center justify-center text-zinc-600 text-sm gap-2 min-h-[300px]">
          <Radio className={`w-8 h-8 opacity-30 ${status === 'connecting' ? 'animate-pulse' : ''}`} />
          {status === 'error' ? 'Connection error — is the backend reachable?'
            : status === 'connecting' ? 'Connecting to E2…'
            : 'Connect E2 to start a RIC test session.'}
        </div>
      ) : (
        <>
          {/* ── KPM tables (Control headline) ──────────────────────────── */}
          <div className="grid grid-cols-2 gap-3">
            {/* Cells */}
            <div className="bg-zinc-950 border border-zinc-800 rounded-xl p-3">
              <div className="text-[10px] font-bold text-zinc-500 uppercase mb-2">Cell Reports (KPM)</div>
              <div className="overflow-x-auto">
                <table className="w-full text-[11px]">
                  <thead>
                    <tr className="text-zinc-600 text-left">
                      <th className="font-medium pb-1">Cell</th>
                      <th className="font-medium pb-1 text-right">Power</th>
                      <th className="font-medium pb-1 text-right">PRB</th>
                      <th className="font-medium pb-1 text-right">Mbps</th>
                      <th className="font-medium pb-1 text-right">UEs</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono">
                    {cells.map(c => (
                      <tr key={c.id} className={`border-t border-zinc-900 ${!c.active && !c.faulted ? 'opacity-40' : ''}`}>
                        <td className="py-1 text-zinc-300 font-sans">
                          {c.faulted ? (
                            <span className="text-red-400">
                              <span className="line-through">{c.name}</span>
                              <span className="ml-1.5 px-1 py-0.5 rounded bg-red-950 border border-red-800 text-[9px] font-bold tracking-wide">FAULT</span>
                            </span>
                          ) : c.active ? c.name : <span className="line-through">{c.name}</span>}
                        </td>
                        <td className="py-1 text-right text-amber-500">{formatWatts(c.power_w)}</td>
                        <td className="py-1 text-right text-blue-400">{c.prb_used_pct}%</td>
                        <td className="py-1 text-right text-zinc-300">{c.throughput_mbps}</td>
                        <td className="py-1 text-right text-zinc-400">{c.connected_ues}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {/* PER UE, the same columns the slow tier shows plus the three
                this tier can answer that ns-3's KPM set does not — RSRP,
                the MCS the AMC chose, and the block error rate at it.
                It used to be one row per GROUP with four columns, so the
                two tiers' control screens could not be read against each
                other and everything the uplink work added was invisible.
                The group fold is still there, above the table, because the
                control levers act on a group. */}
            <div className="bg-zinc-950 border border-zinc-800 rounded-xl p-3">
              <div className="flex items-baseline justify-between mb-2">
                <div className="text-[10px] font-bold text-zinc-500 uppercase">UE Reports (KPM)</div>
                <div className="text-[9px] text-zinc-600">{perUe.length} UEs · per UE, not per group</div>
              </div>
              <div className="overflow-x-auto max-h-72 overflow-y-auto">
                <table className="w-full text-[11px]">
                  <thead className="sticky top-0 bg-zinc-950">
                    <tr className="text-zinc-600 text-left">
                      <th className="font-medium pb-1">UE (IMSI)</th>
                      <th className="font-medium pb-1">Slice</th>
                      <th className="font-medium pb-1 text-right">Cell</th>
                      <th className="font-medium pb-1 text-right">RSRP</th>
                      <th className="font-medium pb-1 text-right">SINR</th>
                      <th className="font-medium pb-1 text-right">DL kbps</th>
                      <th className="font-medium pb-1 text-right">UL kbps</th>
                      <th className="font-medium pb-1 text-right">MCS</th>
                      <th className="font-medium pb-1 text-right">BLER</th>
                      <th className="font-medium pb-1 text-right">PRB</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono">
                    {perUe.map(u => (
                      <tr key={u.imsi} className="border-t border-zinc-900">
                        <td className="py-1 text-zinc-200">{u.imsi}</td>
                        <td className="py-1 text-zinc-500 font-sans">{u.slice ?? '—'}</td>
                        <td className="py-1 text-right text-zinc-400">{u.cell ?? <span className="text-red-500">out</span>}</td>
                        <td className="py-1 text-right" style={{ color: rsrpColor(u.rsrp ?? null) }}>{u.rsrp ?? '—'}</td>
                        <td className="py-1 text-right text-zinc-400">{u.sinr ?? '—'}</td>
                        <td className="py-1 text-right text-zinc-300">{u.thp_kbps != null ? Math.round(u.thp_kbps) : '—'}</td>
                        <td className="py-1 text-right text-zinc-400">{u.ul_kbps != null ? Math.round(u.ul_kbps) : <span className="text-zinc-700">—</span>}</td>
                        <td className="py-1 text-right text-zinc-500">{u.mcs ?? '—'}</td>
                        <td className="py-1 text-right text-zinc-500">{u.bler_pct != null ? `${u.bler_pct}%` : '—'}</td>
                        <td className="py-1 text-right text-sky-400/80">{u.prb ?? '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          {/* Slices */}
          <div className="bg-zinc-950 border border-zinc-800 rounded-xl p-3">
            <div className="text-[10px] font-bold text-zinc-500 uppercase mb-2">Slice Reports (KPM)</div>
            <div className="grid grid-cols-4 gap-2">
              {slices.map(s => (
                <div key={s.slice} className="bg-zinc-900/50 border border-zinc-800 rounded-lg p-2">
                  <div className="flex items-center gap-1.5 mb-1">
                    <span className="w-2 h-2 rounded-full" style={{ background: SLICE_COLORS[s.slice] }} />
                    <span className="text-[11px] font-bold text-zinc-300">{s.slice}</span>
                  </div>
                  <div className="text-[10px] text-zinc-500">Tput <span className="text-zinc-300 font-mono">{s.throughput_mbps}</span> Mbps</div>
                  <div className="text-[10px] text-zinc-500">PRB cap <span className="text-zinc-300 font-mono">{s.prb_pct}%</span> · QoS <span className="text-zinc-300 font-mono">{s.qos_score}</span></div>
                </div>
              ))}
            </div>
          </div>

          {/* ── Manual RC control (shared widget) ───────────────────────── */}
          <ManualRcControl
            cells={cellOpts}
            ues={ueOpts}
            slices={sliceOpts}
            onSleep={id => sendRicControl({ action: 'cell_power', cell: id, state: 'off' })}
            onWake={id => sendRicControl({ action: 'cell_power', cell: id, state: 'on' })}
            onHandover={(ueId, targetId) => sendRicControl({ action: 'handover', ue_group: ueId, target_cell: targetId })}
            onSliceQuota={(sliceId, pct) => sendRicControl({ action: 'slice_quota', slice: sliceId, prb_percent: pct })}
            onFault={id => injectAnomaly({ kind: 'cell_fault', cell: id })}
            onReset={() => { sendRicControl({ action: 'reset' }); injectAnomaly({ kind: 'clear_all' }); }}
            faultedCells={faultedOpts}
            onClearFault={id => injectAnomaly({ kind: 'clear_fault', cell: id })}
            trafficSurge={{
              current: surge,
              onSet: m => injectAnomaly({ kind: 'traffic_surge', multiplier: m }),
              onClear: () => injectAnomaly({ kind: 'clear_surge' }),
            }}
          />

          {/* ── Save run for the Compare tab ─────────────────────────────── */}
          <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-3">
            <div className="flex items-center justify-between gap-2">
              <div className="text-[10px] font-bold text-teal-400 uppercase flex items-center gap-1.5">
                <Trophy className="w-3.5 h-3.5" /> Save run
              </div>
              <div className="flex items-center gap-2">
                {liveScore !== null && (
                  <span className="text-[11px] text-zinc-400">score <span className="font-mono text-zinc-200">{liveScore}</span></span>
                )}
                <input value={runLabel} onChange={e => setRunLabel(e.target.value)} placeholder="run label…"
                  className="w-28 bg-zinc-950 border border-zinc-700 text-[11px] text-white px-2 py-1 rounded outline-none focus:border-teal-500" />
                <button onClick={doSaveRun} disabled={runSeries.length === 0}
                  className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-bold rounded bg-teal-600 hover:bg-teal-500 text-black disabled:opacity-40 transition-colors">
                  <Save className="w-3 h-3" /> Save
                </button>
              </div>
            </div>
            <p className="text-[10px] text-zinc-600 mt-1.5">
              Drive the network (or connect an xApp), then Save — ranked AI-vs-baseline in the <span className="text-amber-400 font-semibold">Compare</span> tab{savedRuns.length > 0 ? <> · {savedRuns.length} saved</> : null}.
            </p>
          </div>

          {/* ── Control log ─────────────────────────────────────────────── */}
          <div className="bg-zinc-950 border border-zinc-800 rounded-xl p-3">
            <div className="text-[10px] font-bold text-zinc-500 uppercase mb-2">Control Log</div>
            {controlLog.length === 0 ? (
              <div className="text-[11px] text-zinc-600 py-2">No control actions yet.</div>
            ) : (
              <div className="flex flex-col gap-1 max-h-[140px] overflow-y-auto font-mono text-[10px]">
                {[...controlLog].reverse().map(e => (
                  <div key={e.id} className="flex items-center gap-2 py-0.5 border-b border-zinc-900/60">
                    {e.ack.success ? <CheckCircle2 className="w-3 h-3 text-teal-500 shrink-0" />
                      : <XCircle className="w-3 h-3 text-red-500 shrink-0" />}
                    <span className={`px-1 rounded shrink-0 ${
                      e.source === 'fault' ? 'bg-red-950/60 text-red-400'
                      : e.source === 'browser' ? 'bg-zinc-800 text-zinc-400'
                      : 'bg-teal-950/60 text-teal-400'}`}>
                      {e.source}
                    </span>
                    <span className="text-zinc-400 truncate">{JSON.stringify(e.command)}</span>
                    <span className={e.ack.success ? 'text-zinc-600 shrink-0' : 'text-red-400 shrink-0'}>→ {e.ack.detail}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      )}
      </>
      )}
    </div>
  );
}
