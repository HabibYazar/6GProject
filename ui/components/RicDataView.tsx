'use client';

/**
 * RicDataView — the "Veri" (data) view of the RIC test.
 *
 * Turns the live KPM report (cells / UE groups / slices) into a clean, modern
 * dashboard instead of dense tables: headline system metrics, then per-cell and
 * per-slice bars. Reads the same shared RicSimulation snapshot the control view
 * uses, so it updates live as the AI/operator drives the RAN.
 */
import { useEffect, useState } from 'react';
import { wattParts } from '@/utils/power';
import { motion, useMotionValue, useTransform, animate } from 'framer-motion';
import { Activity, Clock, Gauge, Radio, Send, Signal, Users, Zap } from 'lucide-react';
import { ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, CartesianGrid } from 'recharts';
import { useNSGStore, selectRicKpm, selectRicRunSeries } from '@/store/useNSGStore';
import RicRealTopology from './RicRealTopology';
import { selectConfig } from '@/store/useNSGStore';

/** Trend metrics the live chart can plot (all already accumulated per tick). */
const TREND: Record<'tput' | 'qos' | 'score', { label: string; unit: string; color: string }> = {
  tput:  { label: 'Throughput', unit: 'Mbps', color: '#14b8a6' },
  qos:   { label: 'QoS',        unit: '',      color: '#38bdf8' },
  score: { label: 'Score',      unit: '',      color: '#f59e0b' },
};

/** Smoothly rolls a number from its current display to `value` on every change. */
function CountUp({ value, decimals = 0 }: { value: number; decimals?: number }) {
  const mv = useMotionValue(0);
  const text = useTransform(mv, v => v.toFixed(decimals));
  useEffect(() => {
    const controls = animate(mv, value, { duration: 0.4, ease: 'easeOut' });
    return () => controls.stop();
  }, [value, mv]);
  return <motion.span>{text}</motion.span>;
}

function Metric({
  label, value, unit, decimals = 0, icon: Icon, tone = 'text-zinc-100', delay = 0,
}: {
  label: string; value: number | string; unit?: string; decimals?: number;
  icon: React.ComponentType<{ className?: string }>; tone?: string; delay?: number;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, delay }}
      className="bg-zinc-900 border border-zinc-800 rounded-xl p-3"
    >
      <div className="flex items-center gap-1.5 text-[10px] text-zinc-500 uppercase font-bold mb-1.5">
        <Icon className="w-3.5 h-3.5" /> {label}
      </div>
      <div className={`text-2xl font-bold font-mono tabular-nums leading-none ${tone}`}>
        {typeof value === 'number' ? <CountUp value={value} decimals={decimals} /> : value}
        {unit && <span className="text-xs text-zinc-500 font-normal ml-1">{unit}</span>}
      </div>
    </motion.div>
  );
}

/** Load-graded bar colour: teal (healthy) → amber (busy); red when faulted, dim when off. */
function cellColor(prbPct: number, active: boolean, faulted?: boolean): string {
  if (faulted) return '#ef4444';
  if (!active) return '#3f3f46';
  if (prbPct >= 75) return '#f59e0b';
  return '#14b8a6';
}

export default function RicDataView() {
  const kpm = useNSGStore(selectRicKpm);
  const config = useNSGStore(selectConfig);
  const runSeries = useNSGStore(selectRicRunSeries);
  const [metric, setMetric] = useState<'tput' | 'qos' | 'score'>('tput');

  if (!kpm || kpm.cells.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center text-sm text-zinc-500 py-16">
        Connect E2 and start a run to see live data.
      </div>
    );
  }

  const { cells, ue_groups: ues, slices } = kpm;

  const totalThp = cells.reduce((s, c) => s + c.throughput_mbps, 0);
  const sinrs = ues.map(u => u.sinr_db).filter((x): x is number => x != null);
  const avgSinr = sinrs.length ? sinrs.reduce((a, b) => a + b, 0) / sinrs.length : null;
  const prbUtil = cells.length ? cells.reduce((s, c) => s + c.prb_used_pct, 0) / cells.length : 0;
  const activeCells = cells.filter(c => c.active).length;
  const connUEs = ues.reduce((s, u) => s + u.count, 0);
  const totalPower = cells.reduce((s, c) => s + c.power_w, 0);

  const maxCellThp = Math.max(1, ...cells.map(c => c.throughput_mbps));
  const maxSliceThp = Math.max(1, ...slices.map(s => s.throughput_mbps));

  return (
    <div className="flex flex-col gap-3">
      {/* Headline system metrics.
          THE SAME SEVEN THE SLOW TIER SHOWS, in the same order. They used to
          be six different ones, so switching tier rearranged the dashboard
          and the two could not be read against each other — which is the
          only thing a two-tier product is for. */}
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-2">
        <Metric label="Sim time" value={kpm.sim_t != null ? kpm.sim_t : '—'} decimals={2} unit={`s · tick ${kpm.tick ?? 0}`} icon={Clock} tone="text-amber-300" delay={0} />
        <Metric label="Throughput" value={totalThp} decimals={1} unit="Mbps" icon={Activity} tone="text-teal-300" delay={0.04} />
        <Metric label="PRB util" value={prbUtil} decimals={0} unit="%" icon={Gauge} tone="text-blue-300" delay={0.08} />
        <Metric label="Avg SINR" value={avgSinr ?? '—'} decimals={1} unit="dB" icon={Signal} tone="text-violet-300" delay={0.12} />
        <Metric label="Power" value={Number(wattParts(totalPower).value)}
                decimals={totalPower >= 1000 ? (totalPower >= 10000 ? 1 : 2) : 0}
                unit={wattParts(totalPower).unit} icon={Zap} tone="text-amber-300" delay={0.16} />
        <Metric label="Handovers" value={kpm.handovers ?? 0} icon={Send} tone="text-sky-300" delay={0.2} />
        <Metric label="UEs" value={connUEs} unit="attached" icon={Users} delay={0.24} />
      </div>

      {/* THE SAME MAP THE SLOW TIER DRAWS. This used to be `RicTopology`, a
          React Flow SCHEMATIC laid out on canvas coordinates, while the slow
          tier drew a real metric plot of the identical scenario — so an edge
          here meant nothing and an edge there meant distance. The fast tier
          resolves every UE's own position and serving cell now, so it can
          feed the real one. */}
      {kpm.ues && kpm.ues.length > 0 && (
        <RicRealTopology ues={kpm.ues} config={config} />
      )}

      {/* Live trend — pick a metric, watch it move as the RAN is driven */}
      {runSeries.length > 1 && (
        <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-3">
          <div className="flex items-center justify-between mb-2">
            <div className="text-[10px] font-bold text-zinc-500 uppercase">Live trend</div>
            <div className="flex items-center gap-0.5 bg-zinc-800 rounded-lg p-0.5">
              {(['tput', 'qos', 'score'] as const).map(m => (
                <button
                  key={m}
                  onClick={() => setMetric(m)}
                  className={`px-2 py-0.5 text-[10px] rounded-md transition-colors ${metric === m ? 'bg-zinc-700 text-zinc-100' : 'text-zinc-500 hover:text-zinc-300'}`}
                >
                  {TREND[m].label}
                </button>
              ))}
            </div>
          </div>
          <div className="h-40">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={runSeries} margin={{ top: 4, right: 6, bottom: 0, left: -18 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#27272a" vertical={false} />
                <XAxis dataKey="tick" tick={{ fontSize: 9, fill: '#71717a' }} axisLine={false} tickLine={false} />
                <YAxis tick={{ fontSize: 9, fill: '#71717a' }} axisLine={false} tickLine={false} width={38} />
                <Tooltip
                  contentStyle={{ background: '#18181b', border: '1px solid #3f3f46', borderRadius: 8, fontSize: 11 }}
                  labelStyle={{ color: '#a1a1aa' }}
                  formatter={(v: number) => [`${v}${TREND[metric].unit ? ' ' + TREND[metric].unit : ''}`, TREND[metric].label]}
                />
                <Area type="monotone" dataKey={metric} stroke={TREND[metric].color} fill={TREND[metric].color} fillOpacity={0.12} strokeWidth={2} isAnimationActive={false} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* Per-cell bars */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-3">
        <div className="text-[10px] font-bold text-zinc-500 uppercase mb-3">Cells — throughput &amp; load</div>
        <div className="flex flex-col gap-2">
          {cells.map(c => (
            <div key={c.id} className="flex items-center gap-2">
              <span className="w-32 truncate text-[11px] text-zinc-300" title={c.name}>{c.name}</span>
              <div className="flex-1 h-2.5 bg-zinc-800 rounded-full overflow-hidden">
                <div
                  className="h-full rounded-full transition-all duration-500"
                  style={{ width: `${Math.max(2, (c.throughput_mbps / maxCellThp) * 100)}%`, backgroundColor: cellColor(c.prb_used_pct, c.active, c.faulted) }}
                />
              </div>
              <span className="w-16 text-right text-[11px] font-mono text-zinc-400 tabular-nums">{c.throughput_mbps.toFixed(1)}</span>
              <span className="w-14 text-right text-[11px] font-mono text-blue-400 tabular-nums">{c.prb_used_pct.toFixed(0)}% PRB</span>
              {c.faulted
                ? <span className="w-12 text-[9px] text-red-400 font-bold uppercase">fault</span>
                : !c.active
                  ? <span className="w-12 text-[9px] text-zinc-600 font-bold uppercase">off</span>
                  : <span className="w-12 text-[9px] text-teal-500/70 font-mono">{c.connected_ues} ue</span>}
            </div>
          ))}
        </div>
      </div>

      {/* Per-slice breakdown */}
      {slices.length > 0 && (
        <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-3">
          <div className="text-[10px] font-bold text-zinc-500 uppercase mb-3">Slices — throughput &amp; QoS</div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3">
            {slices.map(s => (
              <div key={s.slice}>
                <div className="flex items-center justify-between mb-1">
                  <span className="text-[11px] text-zinc-300 font-medium">{s.slice}</span>
                  <span className="text-[11px] font-mono text-zinc-400 tabular-nums">{s.throughput_mbps.toFixed(1)} Mbps</span>
                </div>
                <div className="h-2 bg-zinc-800 rounded-full overflow-hidden">
                  <div
                    className="h-full rounded-full bg-teal-500 transition-all duration-500"
                    style={{ width: `${Math.max(2, (s.throughput_mbps / maxSliceThp) * 100)}%` }}
                  />
                </div>
                <div className="flex items-center justify-between mt-1 text-[9px] text-zinc-600">
                  <span>PRB {s.prb_pct.toFixed(0)}%</span>
                  <span className={s.qos_score >= 80 ? 'text-teal-500/80' : s.qos_score >= 50 ? 'text-amber-500/80' : 'text-red-400/80'}>
                    QoS {s.qos_score.toFixed(0)}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
