'use client';

/**
 * AI-vs-baseline comparison — the standout tab that quantifies what an AI xApp
 * actually bought you on the SAME scenario. Runs are captured in RIC Test
 * (Save run); this view ranks them, headlines the best-vs-baseline gain, and
 * overlays their score / QoS trajectories. Sequential by design: you run the
 * baseline (no control) and each AI one at a time — never simultaneously.
 */
import { useMemo, useEffect, useState } from 'react';
import { motion, useMotionValue, useTransform, animate } from 'framer-motion';
import {
  Trophy, Trash2, TrendingUp, TrendingDown, Zap, Gauge, Signal, FlaskConical, Medal,
  Radio, Loader2, Rocket, AlertTriangle,
} from 'lucide-react';
import {
  ResponsiveContainer, LineChart, Line, BarChart, Bar, Cell,
  XAxis, YAxis, Tooltip, Legend, CartesianGrid,
} from 'recharts';
import { useNSGStore, selectRicSavedRuns, selectNsOranRuns } from '@/store/useNSGStore';
import type { SavedRun } from '@/utils/ricScore';
import type { NsOranKpm } from '@/types/nsg';

const RUN_COLORS = ['#14b8a6', '#f43f5e', '#a78bfa', '#fbbf24', '#38bdf8', '#f97316'];

function CountUp({ value, decimals = 0 }: { value: number; decimals?: number }) {
  const mv = useMotionValue(0);
  const text = useTransform(mv, v => v.toFixed(decimals));
  useEffect(() => {
    const controls = animate(mv, value, { duration: 0.5, ease: 'easeOut' });
    return () => controls.stop();
  }, [value, mv]);
  return <motion.span>{text}</motion.span>;
}

/** The baseline is the run whose label says so, else the lowest-scoring run. */
function pickBaseline(runs: SavedRun[]): SavedRun | null {
  if (!runs.length) return null;
  const named = runs.find(r => /baseline|no ?control|manual|off|none/i.test(r.label));
  return named ?? [...runs].sort((a, b) => a.avgScore - b.avgScore)[0];
}

export default function RicComparison() {
  const savedRuns = useNSGStore(selectRicSavedRuns);
  const clearRuns = useNSGStore(s => s.clearRicSavedRuns);
  const nsOranRuns = useNSGStore(selectNsOranRuns);
  const validateOnNsOran = useNSGStore(s => s.validateSavedRunsOnNsOran);

  const ranked = useMemo(() => [...savedRuns].sort((a, b) => b.avgScore - a.avgScore), [savedRuns]);
  const baseline = useMemo(() => pickBaseline(savedRuns), [savedRuns]);
  const best = ranked[0] ?? null;
  const showGain = !!best && !!baseline && best.id !== baseline.id;

  // Colour is stable per run (its index in savedRuns), independent of ranking.
  const colorOf = (r: SavedRun) => RUN_COLORS[savedRuns.indexOf(r) % RUN_COLORS.length];

  const trend = useMemo(() => {
    const maxLen = Math.max(0, ...savedRuns.map(r => r.series.length));
    return Array.from({ length: maxLen }, (_, i) => {
      const row: Record<string, number> = { tick: i };
      savedRuns.forEach(r => { if (r.series[i]) row[r.label] = r.series[i].score; });
      return row;
    });
  }, [savedRuns]);

  const metricBars = useMemo(
    () => savedRuns.map(r => ({ label: r.label, qos: r.avgQos })),
    [savedRuns],
  );

  if (savedRuns.length === 0) {
    return (
      <div className="h-full flex flex-col items-center justify-center text-center px-6">
        <div className="w-14 h-14 rounded-2xl bg-teal-500/10 border border-teal-500/20 flex items-center justify-center mb-4">
          <FlaskConical className="w-7 h-7 text-teal-400" />
        </div>
        <h3 className="text-zinc-200 font-semibold mb-1">Compare AI vs baseline</h3>
        <p className="text-sm text-zinc-500 max-w-md leading-relaxed">
          In <span className="text-zinc-300">RIC Test</span>, run the scenario once with{' '}
          <span className="text-zinc-300">no control (baseline)</span>, then again with an{' '}
          <span className="text-teal-400">AI xApp</span> — hit <span className="text-zinc-300">Save run</span> each time.
          They land here, ranked, with the AI&apos;s gain quantified.
        </p>
        <p className="text-[11px] text-zinc-600 mt-3">Sequential by design — one run at a time, never simultaneously.</p>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto p-4 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-zinc-100 font-semibold flex items-center gap-2">
            <Trophy className="w-4 h-4 text-amber-400" /> AI Performance Comparison
          </h2>
          <p className="text-[11px] text-zinc-500 mt-0.5">
            Same scenario · {savedRuns.length} run{savedRuns.length > 1 ? 's' : ''}. Score = QoS × coverage × energy bonus.
          </p>
        </div>
        <button onClick={clearRuns} className="text-[11px] text-zinc-500 hover:text-red-400 flex items-center gap-1 transition-colors">
          <Trash2 className="w-3.5 h-3.5" /> Clear
        </button>
      </div>

      {/* HERO — best vs baseline gain */}
      {showGain ? (
        <motion.div
          initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }}
          className="rounded-2xl border border-teal-500/25 bg-gradient-to-br from-teal-500/10 to-zinc-900 p-4"
        >
          <div className="flex items-center gap-2 text-[11px] font-bold uppercase text-teal-400 mb-2">
            <TrendingUp className="w-4 h-4" /> Best run vs baseline
          </div>
          <div className="flex items-baseline gap-2 mb-3 text-sm">
            <span className="text-zinc-200 font-semibold">{best!.label}</span>
            <span className="text-zinc-600 text-xs">vs</span>
            <span className="text-zinc-500">{baseline!.label}</span>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <GainStat label="Score" from={baseline!.avgScore} to={best!.avgScore} icon={Trophy} higherBetter />
            <GainStat label="QoS" from={baseline!.avgQos} to={best!.avgQos} icon={Gauge} higherBetter />
            <GainStat label="Energy used" from={baseline!.avgEnergyPct} to={best!.avgEnergyPct} icon={Zap} unit="%" higherBetter={false} />
          </div>
        </motion.div>
      ) : (
        <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-3 text-[11px] text-zinc-500">
          Save a second run (e.g. a <span className="text-zinc-300">baseline with no AI</span>) to see the head-to-head gain.
        </div>
      )}

      {/* leaderboard */}
      <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-3">
        <div className="text-[10px] font-bold uppercase text-zinc-500 mb-2 flex items-center gap-1.5">
          <Medal className="w-3.5 h-3.5" /> Leaderboard
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-[11px]">
            <thead><tr className="text-zinc-600 text-left">
              <th className="font-medium pb-1">Run</th>
              <th className="font-medium pb-1 text-right">Score</th>
              <th className="font-medium pb-1 text-right">QoS</th>
              <th className="font-medium pb-1 text-right">Energy%</th>
              <th className="font-medium pb-1 text-right">Outage ticks</th>
            </tr></thead>
            <tbody className="font-mono">
              {ranked.map((r, i) => (
                <motion.tr
                  key={r.id} initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: i * 0.05 }}
                  className={`border-t border-zinc-800/60 ${baseline && r.id === baseline.id ? 'opacity-70' : ''}`}
                >
                  <td className="py-1.5 font-sans">
                    <span className="flex items-center gap-1.5">
                      <span className="w-2 h-2 rounded-full shrink-0" style={{ background: colorOf(r) }} />
                      {i === 0 && <Trophy className="w-3 h-3 text-amber-400 shrink-0" />}
                      <span className="text-zinc-300">{r.label}</span>
                      {r.source === 'real' && (
                        <span className="text-[9px] text-sky-300 border border-sky-500/40 rounded px-1"
                          title="Scored from a LIVE run on the real ns-O-RAN engine">real engine</span>
                      )}
                      {baseline && r.id === baseline.id && (
                        <span className="text-[9px] text-zinc-500 border border-zinc-700 rounded px-1">baseline</span>
                      )}
                    </span>
                  </td>
                  <td className="py-1.5 text-right font-bold text-teal-400">{r.avgScore}</td>
                  <td className="py-1.5 text-right text-zinc-300">{r.avgQos}</td>
                  <td className="py-1.5 text-right text-amber-500">{r.avgEnergyPct}%</td>
                  <td className="py-1.5 text-right text-zinc-400">{r.totalOutageTicks}</td>
                </motion.tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* REAL-ENGINE leaderboard — each AI's decision replayed on ns-O-RAN */}
      <RealEngineLeaderboard
        runs={savedRuns}
        results={nsOranRuns}
        colorOf={colorOf}
        onValidate={validateOnNsOran}
      />

      {/* score over time */}
      <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-3">
        <div className="text-[10px] font-bold uppercase text-zinc-500 mb-2 flex items-center gap-1.5">
          <TrendingUp className="w-3.5 h-3.5" /> Score over time
        </div>
        <div className="h-52">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={trend} margin={{ top: 4, right: 8, bottom: 0, left: -18 }}>
              <CartesianGrid stroke="#27272a" strokeDasharray="3 3" />
              <XAxis dataKey="tick" stroke="#52525b" fontSize={10} tickLine={false} axisLine={false} />
              <YAxis stroke="#52525b" fontSize={10} tickLine={false} axisLine={false} />
              <Tooltip contentStyle={{ backgroundColor: '#18181b', borderColor: '#27272a', borderRadius: 8, fontSize: 11 }} />
              <Legend wrapperStyle={{ fontSize: 10 }} />
              {savedRuns.map(r => (
                <Line key={r.id} type="monotone" dataKey={r.label} stroke={colorOf(r)} strokeWidth={2} dot={false} isAnimationActive={false} />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* per-run average QoS bars */}
      <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-3">
        <div className="text-[10px] font-bold uppercase text-zinc-500 mb-2 flex items-center gap-1.5">
          <Signal className="w-3.5 h-3.5" /> Average QoS by run
        </div>
        <div className="h-40">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={metricBars} margin={{ top: 4, right: 8, bottom: 0, left: -18 }}>
              <CartesianGrid stroke="#27272a" strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="label" stroke="#52525b" fontSize={10} tickLine={false} axisLine={false} />
              <YAxis stroke="#52525b" fontSize={10} tickLine={false} axisLine={false} />
              <Tooltip contentStyle={{ backgroundColor: '#18181b', borderColor: '#27272a', borderRadius: 8, fontSize: 11 }} cursor={{ fill: '#ffffff08' }} />
              <Bar dataKey="qos" radius={[4, 4, 0, 0]}>
                {metricBars.map((_, i) => <Cell key={i} fill={RUN_COLORS[i % RUN_COLORS.length]} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}

// ── Real-engine leaderboard ───────────────────────────────────────────────────

interface NsOranRunResult { validating: boolean; kpm: NsOranKpm | null; error: string | null; }

/** Pull the comparable KPIs out of a real ns-O-RAN result. DRB.UEThpDl is per-UE
 *  DL throughput in kbps; L3servingSINR is serving SINR (dB); BLER comes from the
 *  engine's TB counters. `finalPowers` gives the AI's active-cell count. */
function realMetrics(kpm: NsOranKpm | null, finalPowers?: number[]) {
  if (!kpm) return null;
  const thp = kpm.measurements?.['DRB.UEThpDl']?.mean;
  const sinr = kpm.measurements?.['L3servingSINR']?.mean;
  const ul = kpm.measurements?.['DRB.PdcpSduBitRateUl']?.mean;
  const delay = kpm.measurements?.['DRB.PdcpSduDelayDl']?.mean;   // 0.1 ms units
  return {
    thpMbps: typeof thp === 'number' ? thp / 1000 : undefined,
    sinr: typeof sinr === 'number' ? sinr : undefined,
    bler: kpm.derived?.bler_pct,
    ulKbps: typeof ul === 'number' ? ul : undefined,
    delayMs: typeof delay === 'number' ? delay / 10 : undefined,
    handovers: kpm.handovers,
    // MEASURED draw from the sim's radio energy model, not the config-derived
    // estimate the analytical tier shows. WATTS, not total Joules: each replay
    // reaches a slightly different simulated span inside its fixed wall-clock
    // window, so comparing totals ranked the run that simulated longest.
    powerW: kpm.energy?.mean_power_w,
    // How much of that saving came from RU sleep states (Phase 2a).
    sleepPct: kpm.sleep?.layer_sleep_pct,
    active: finalPowers?.filter(p => p > 0).length,
    total: finalPowers?.length,
    ueCount: kpm.ue_count,
  };
}

/** When the engine can't measure, it says why (busy / ns-3 aborted / RAN never
 *  scheduled). Show that — a row of "—" reads as a broken Re-validate button. */
function engineStatusText(kpm: NsOranKpm): string {
  switch (kpm.status) {
    case 'busy': return 'engine busy — stop the live session, then retry';
    case 'crashed': return 'ns-3 aborted on startup — retry';
    case 'partial': return 'RAN never scheduled — no DU metrics';
    case 'no_data': return 'no KPM received from the engine';
    // Not a failure: the numbers are real, they just cover less simulated time
    // than the scenario asked for. Saying so beats a row that looks clean —
    // before this, a mid-run ns-3 death with data reported as a clean pass.
    case 'truncated':
      return kpm.ended_by === 'crash'
        ? 'ns-3 died part way — data up to the crash only'
        : 'stopped at the wall budget — shorter run than asked';
    default: return (kpm.error || kpm.status).slice(0, 52);
  }
}

/** The headline the whole session was building toward: rank the SAME AIs by what
 *  the REAL engine measured, by replaying each one's final cell on/off + power. */
function RealEngineLeaderboard({
  runs, results, colorOf, onValidate,
}: {
  runs: SavedRun[];
  results: Record<string, NsOranRunResult>;
  colorOf: (r: SavedRun) => string;
  onValidate: (ids?: string[]) => Promise<void>;
}) {
  const eligible = runs.filter(r => r.finalPowers && r.config);
  const anyValidating = Object.values(results).some(r => r?.validating);
  const done = eligible.filter(r => results[r.id]?.kpm || results[r.id]?.error).length;

  // Ranking axis: throughput for eMBB-style goals, measured PDCP delay
  // (ascending) for uRLLC-style goals — both are real engine measurements.
  const [rankBy, setRankBy] = useState<'thp' | 'delay'>('thp');

  const metricOf = (r: SavedRun) => realMetrics(results[r.id]?.kpm ?? null, r.finalPowers);
  // Best on the chosen axis = the winner on real data.
  const bestThp = Math.max(0, ...eligible.map(r => metricOf(r)?.thpMbps ?? 0));
  const bestDelay = Math.min(Infinity,
    ...eligible.map(r => metricOf(r)?.delayMs ?? Infinity));

  // Completed runs sort on the chosen axis; pending/failed keep list order.
  const ordered = [...eligible].sort((a, b) => {
    if (rankBy === 'delay') {
      const da = metricOf(a)?.delayMs ?? Infinity;
      const db = metricOf(b)?.delayMs ?? Infinity;
      return da - db;
    }
    const ta = metricOf(a)?.thpMbps ?? -1;
    const tb = metricOf(b)?.thpMbps ?? -1;
    return tb - ta;
  });

  return (
    <div className="rounded-xl border border-sky-500/25 bg-gradient-to-br from-sky-500/[0.07] to-zinc-900 p-3">
      <div className="flex items-start justify-between gap-3 mb-2">
        <div>
          <div className="text-[10px] font-bold uppercase text-sky-400 flex items-center gap-1.5">
            <Radio className="w-3.5 h-3.5" /> Real-engine leaderboard · ns-O-RAN
          </div>
          <p className="text-[11px] text-zinc-500 mt-0.5 leading-snug max-w-md">
            Each AI&apos;s final cell on/off + power, replayed on the real{' '}
            <span className="text-zinc-400">FlexRIC + ns-3 mmWave</span> engine over real E2AP.
            The winner is decided by the <span className="text-zinc-400">measured</span> axis you pick — throughput (eMBB) or delay (uRLLC).
          </p>
        </div>
        <select
          value={rankBy}
          onChange={e => setRankBy(e.target.value as 'thp' | 'delay')}
          className="shrink-0 text-[11px] bg-zinc-800 border border-zinc-700 rounded-lg px-2 py-1.5 text-zinc-300"
          title="Ranking axis — both are real engine measurements"
        >
          <option value="thp">Rank: DL throughput</option>
          <option value="delay">Rank: delay (uRLLC)</option>
        </select>
        <button
          onClick={() => onValidate()}
          disabled={anyValidating || eligible.length === 0}
          className="shrink-0 text-[11px] font-semibold px-2.5 py-1.5 rounded-lg flex items-center gap-1.5 transition-colors
            bg-sky-500/15 text-sky-300 border border-sky-500/30 hover:bg-sky-500/25
            disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {anyValidating
            ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Validating {done}/{eligible.length}</>
            : <><Rocket className="w-3.5 h-3.5" /> {done > 0 ? 'Re-validate' : 'Validate on ns-O-RAN'}</>}
        </button>
      </div>

      {eligible.length === 0 ? (
        <p className="text-[11px] text-zinc-500 py-3 text-center">
          Save a run <span className="text-zinc-400">while a KPM stream is live</span> to enable real-engine replay
          {runs.length > 0 && <> — the runs above were saved without a captured end-state</>}.
        </p>
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-[11px]">
              <thead><tr className="text-zinc-600 text-left">
                <th className="font-medium pb-1">Run</th>
                <th className="font-medium pb-1 text-right">DL Mbps/UE</th>
                <th className="font-medium pb-1 text-right">UL kbps</th>
                <th className="font-medium pb-1 text-right">SINR dB</th>
                <th className="font-medium pb-1 text-right">BLER%</th>
                <th className="font-medium pb-1 text-right" title="Mean PDCP DL delay — the uRLLC ranking axis">Delay ms</th>
                <th className="font-medium pb-1 text-right" title="Mean draw measured by the sim's radio energy model. Watts, not Joules: each replay reaches a slightly different simulated span, so totals are not comparable">Power W</th>
                <th className="font-medium pb-1 text-right" title="Share of the cell layer's airtime spent in an RU sleep state">Asleep %</th>
                <th className="font-medium pb-1 text-right" title="Observed in the sampled KPM (control-driven mobility)">HO</th>
                <th className="font-medium pb-1 text-right">Cells on</th>
              </tr></thead>
              <tbody className="font-mono">
                {ordered.map(r => {
                  const res = results[r.id];
                  const m = realMetrics(res?.kpm ?? null, r.finalPowers);
                  const isWinner = rankBy === 'delay'
                    ? m?.delayMs != null && isFinite(bestDelay) && Math.abs(m.delayMs - bestDelay) < 1e-6
                    : !!m?.thpMbps && bestThp > 0 && Math.abs(m.thpMbps - bestThp) < 1e-6;
                  return (
                    <tr key={r.id} className="border-t border-zinc-800/60">
                      <td className="py-1.5 font-sans">
                        <span className="flex items-center gap-1.5">
                          <span className="w-2 h-2 rounded-full shrink-0" style={{ background: colorOf(r) }} />
                          {isWinner && <Trophy className="w-3 h-3 text-amber-400 shrink-0" />}
                          <span className="text-zinc-300">{r.label}</span>
                        </span>
                      </td>
                      {res?.validating ? (
                        <td colSpan={9} className="py-1.5 text-right text-sky-400">
                          <span className="inline-flex items-center gap-1.5"><Loader2 className="w-3 h-3 animate-spin" /> running on real engine…</span>
                        </td>
                      ) : res?.error ? (
                        <td colSpan={9} className="py-1.5 text-right text-rose-400">
                          <span className="inline-flex items-center gap-1.5"><AlertTriangle className="w-3 h-3" /> {res.error.slice(0, 48)}</span>
                        </td>
                      ) : res?.kpm && res.kpm.status !== 'ok' ? (
                        <td colSpan={9} className="py-1.5 text-right text-amber-400">
                          <span className="inline-flex items-center gap-1.5"><AlertTriangle className="w-3 h-3" /> {engineStatusText(res.kpm)}</span>
                        </td>
                      ) : m ? (
                        <>
                          <td className={`py-1.5 text-right font-bold ${isWinner ? 'text-amber-400' : 'text-sky-300'}`}>
                            {m.thpMbps != null ? m.thpMbps.toFixed(2) : '—'}
                          </td>
                          <td className="py-1.5 text-right text-cyan-400">{m.ulKbps != null ? m.ulKbps.toFixed(0) : '—'}</td>
                          <td className="py-1.5 text-right text-zinc-300">{m.sinr != null ? m.sinr.toFixed(1) : '—'}</td>
                          <td className="py-1.5 text-right text-zinc-400">{m.bler != null ? m.bler.toFixed(1) : '—'}</td>
                          <td className={`py-1.5 text-right ${rankBy === 'delay' && isWinner ? 'text-amber-400 font-bold' : 'text-violet-300'}`}>
                            {m.delayMs != null ? m.delayMs.toFixed(1) : '—'}
                          </td>
                          <td className="py-1.5 text-right text-amber-300/90">{m.powerW != null ? m.powerW.toFixed(0) : '—'}</td>
                          <td className="py-1.5 text-right text-sky-300/90">{m.sleepPct != null ? m.sleepPct.toFixed(0) : '—'}</td>
                          <td className="py-1.5 text-right text-zinc-400">{m.handovers ?? '—'}</td>
                          <td className="py-1.5 text-right text-emerald-400 inline-flex items-center gap-1 justify-end w-full">
                            <Zap className="w-3 h-3" />{m.active ?? '—'}/{m.total ?? '—'}
                          </td>
                        </>
                      ) : (
                        <td colSpan={9} className="py-1.5 text-right text-zinc-600">not validated yet</td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="text-[10px] text-zinc-600 mt-2">
            Real E2AP/SCTP · one run at a time · ~1 min each. DL throughput is the mean per-UE DRB rate the engine reported.
          </p>
        </>
      )}
    </div>
  );
}

/** One before→after stat in the hero card, with a signed delta chip. */
function GainStat({
  label, from, to, icon: Icon, unit = '', higherBetter,
}: {
  label: string; from: number; to: number; unit?: string;
  icon: React.ComponentType<{ className?: string }>; higherBetter: boolean;
}) {
  const delta = to - from;
  const improved = higherBetter ? delta > 0 : delta < 0;
  const pctChange = from === 0 ? 0 : (delta / from) * 100;
  const Arrow = delta === 0 ? null : delta > 0 ? TrendingUp : TrendingDown;
  const chipTone = delta === 0
    ? 'text-zinc-500 bg-zinc-800'
    : improved ? 'text-teal-400 bg-teal-500/10' : 'text-rose-400 bg-rose-500/10';
  return (
    <div>
      <div className="flex items-center gap-1.5 text-[10px] text-zinc-500 uppercase font-bold mb-1">
        <Icon className="w-3.5 h-3.5" /> {label}
      </div>
      <div className="text-2xl font-bold font-mono tabular-nums leading-none text-zinc-100">
        <CountUp value={to} decimals={unit === '%' ? 0 : 1} />
        {unit && <span className="text-xs text-zinc-500 font-normal ml-0.5">{unit}</span>}
      </div>
      <div className={`inline-flex items-center gap-1 mt-1.5 px-1.5 py-0.5 rounded text-[10px] font-bold ${chipTone}`}>
        {Arrow && <Arrow className="w-3 h-3" />}
        {delta > 0 ? '+' : ''}{Math.abs(pctChange) < 0.05 ? '0' : pctChange.toFixed(0)}%
        <span className="text-[9px] font-normal opacity-70">vs base</span>
      </div>
    </div>
  );
}
