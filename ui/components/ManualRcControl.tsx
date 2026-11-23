'use client';

/**
 * ManualRcControl — the one manual-xApp control widget, shared by BOTH the
 * analytic sandbox and the real ns-O-RAN engine so the two consoles look and
 * feel identical (the user's "tek dizayn, iki motor").
 *
 * It is purely presentational + self-contained selection state: every action is
 * a normalized callback the parent maps to its own command vocabulary (sandbox
 * `action`/slice-name vs real `cmd`/QCI). Four dropdown-driven panels — Cell
 * power, Handover, Slice PRB quota, Fault/reset — plus an optional traffic-surge
 * panel (sandbox) and an optional graded-dBm Set (real engine).
 */
import { useState } from 'react';
import { Antenna, ArrowRightLeft, Layers, AlertTriangle, RotateCcw, TrendingUp } from 'lucide-react';

export interface RcOption { id: string; label: string; }

export interface ManualRcControlProps {
  cells: RcOption[];
  ues: RcOption[];
  slices: RcOption[];
  disabled?: boolean;
  /** Real engine: expose a dBm field + Set (graded TX power). Sandbox: on/off only. */
  gradedPower?: boolean;
  onSleep: (cellId: string) => void;
  onWake: (cellId: string) => void;
  onSetPower?: (cellId: string, dbm: number) => void;
  onHandover: (ueId: string, targetCellId: string) => void;
  onSliceQuota: (sliceId: string, pct: number) => void;
  onFault: (cellId: string) => void;
  /** Sandbox: clear all overrides/anomalies. Omitted → Reset button hidden. */
  onReset?: () => void;
  /** Sandbox: currently-faulted cells the operator can repair one by one. */
  faultedCells?: RcOption[];
  onClearFault?: (cellId: string) => void;
  /** Sandbox only: demand-multiplier controls. */
  trafficSurge?: { current: number; onSet: (m: number) => void; onClear: () => void };
}

const selCls =
  'w-full bg-zinc-900 border border-zinc-700 text-[11px] text-white px-2 py-1 rounded outline-none focus:border-teal-500 disabled:opacity-40';

export default function ManualRcControl({
  cells, ues, slices, disabled = false, gradedPower = false,
  onSleep, onWake, onSetPower, onHandover, onSliceQuota, onFault, onReset,
  faultedCells = [], onClearFault, trafficSurge,
}: ManualRcControlProps) {
  const [selCell, setSelCell]     = useState('');
  const [dbm, setDbm]             = useState('30');
  const [selUe, setSelUe]         = useState('');
  const [selTarget, setSelTarget] = useState('');
  const [selSlice, setSelSlice]   = useState('');
  const [quota, setQuota]         = useState(50);
  const [selFault, setSelFault]   = useState('');

  // Derive an always-valid selection: fall back to the first option whenever the
  // remembered id is gone (cell slept, UE churned) — no effects, no stale ids.
  const valid = (opts: RcOption[], id: string) => (opts.some(o => o.id === id) ? id : (opts[0]?.id ?? ''));
  const cell   = valid(cells, selCell);
  const ue     = valid(ues, selUe);
  const target = valid(cells, selTarget);
  const slice  = valid(slices, selSlice);
  const faultCell = valid(cells.filter(c => !faultedCells.some(f => f.id === c.id)), selFault);

  const btn = (extra: string) =>
    `text-[11px] font-bold py-1 rounded border transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${extra}`;
  const teal = btn('bg-teal-600/20 border-teal-600/40 text-teal-400 hover:bg-teal-600/30');

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-3">
      <div className="text-[10px] font-bold text-zinc-400 uppercase flex items-center gap-1.5 mb-2.5">
        <Antenna className="w-3.5 h-3.5 text-teal-400" /> Manual RC Control — or drive it yourself
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-2.5">
        {/* ── Cell power ─────────────────────────────────────────────── */}
        <div className="bg-zinc-950/50 border border-zinc-800 rounded-lg p-2.5 flex flex-col gap-2">
          <div className="text-[10px] font-bold text-zinc-400 flex items-center gap-1"><Antenna className="w-3 h-3" /> Cell power</div>
          <select value={cell} onChange={e => setSelCell(e.target.value)} disabled={disabled || !cells.length} className={selCls}>
            {cells.length ? cells.map(c => <option key={c.id} value={c.id}>{c.label}</option>) : <option value="">cell…</option>}
          </select>
          <div className="flex items-center gap-1.5">
            <button disabled={disabled || !cell} onClick={() => onSleep(cell)} className={`${btn('bg-rose-600/15 border-rose-600/40 text-rose-300 hover:bg-rose-600/25')} flex-1`}>Sleep</button>
            <button disabled={disabled || !cell} onClick={() => onWake(cell)} className={`${btn('bg-emerald-600/15 border-emerald-600/40 text-emerald-300 hover:bg-emerald-600/25')} flex-1`}>Wake</button>
          </div>
          {gradedPower && onSetPower && (
            <div className="flex items-center gap-1.5">
              <input type="number" min={0} max={46} value={dbm} onChange={e => setDbm(e.target.value)} title="Target TX power in dBm (0 = sleep)"
                className="w-12 bg-zinc-900 border border-zinc-700 text-[11px] text-white px-1.5 py-1 rounded outline-none focus:border-teal-500 font-mono" />
              <span className="text-[10px] text-zinc-500">dBm</span>
              <button disabled={disabled || !cell} onClick={() => onSetPower(cell, Number(dbm))} className={`${btn('bg-amber-500/15 border-amber-500/40 text-amber-300 hover:bg-amber-500/25')} flex-1`}>Set</button>
            </div>
          )}
        </div>

        {/* ── Handover ───────────────────────────────────────────────── */}
        <div className="bg-zinc-950/50 border border-zinc-800 rounded-lg p-2.5 flex flex-col gap-2">
          <div className="text-[10px] font-bold text-zinc-400 flex items-center gap-1"><ArrowRightLeft className="w-3 h-3" /> Handover</div>
          <select value={ue} onChange={e => setSelUe(e.target.value)} disabled={disabled || !ues.length} className={selCls}>
            {ues.length ? ues.map(u => <option key={u.id} value={u.id}>{u.label}</option>) : <option value="">UE…</option>}
          </select>
          <div className="flex items-center gap-1.5">
            <select value={target} onChange={e => setSelTarget(e.target.value)} disabled={disabled || !cells.length} className={selCls}>
              {cells.length ? cells.map(c => <option key={c.id} value={c.id}>→ {c.label}</option>) : <option value="">target…</option>}
            </select>
            <button disabled={disabled || !ue || !target} onClick={() => onHandover(ue, target)} className={`${teal} px-2.5 shrink-0`}>Steer</button>
          </div>
        </div>

        {/* ── Slice PRB quota ────────────────────────────────────────── */}
        <div className="bg-zinc-950/50 border border-zinc-800 rounded-lg p-2.5 flex flex-col gap-2">
          <div className="text-[10px] font-bold text-zinc-400 flex items-center gap-1"><Layers className="w-3 h-3" /> Slice PRB quota</div>
          <select value={slice} onChange={e => setSelSlice(e.target.value)} disabled={disabled || !slices.length} className={selCls}>
            {slices.length ? slices.map(s => <option key={s.id} value={s.id}>{s.label}</option>) : <option value="">slice…</option>}
          </select>
          <div className="flex items-center gap-2">
            <input type="range" min={0} max={100} step={5} value={quota} onChange={e => setQuota(Number(e.target.value))} disabled={disabled} className="flex-1 accent-teal-500" />
            <span className="text-[11px] font-mono text-zinc-300 w-9 text-right">{quota}%</span>
            <button disabled={disabled || !slice} onClick={() => onSliceQuota(slice, quota)} className={`${teal} px-2.5 shrink-0`}>Set</button>
          </div>
        </div>

        {/* ── Fault / reset ──────────────────────────────────────────── */}
        <div className="bg-zinc-950/50 border border-zinc-800 rounded-lg p-2.5 flex flex-col gap-2">
          <div className="text-[10px] font-bold text-zinc-400 flex items-center gap-1"><AlertTriangle className="w-3 h-3" /> Fault / reset</div>
          <select value={faultCell} onChange={e => setSelFault(e.target.value)} disabled={disabled || !cells.length} className={selCls}>
            {cells.length ? cells.map(c => <option key={c.id} value={c.id}>{c.label}</option>) : <option value="">cell…</option>}
          </select>
          <div className="flex items-center gap-1.5">
            <button disabled={disabled || !faultCell} onClick={() => onFault(faultCell)} className={`${btn('bg-red-600/20 border-red-600/40 text-red-400 hover:bg-red-600/30')} flex-1`}>Inject</button>
            {onReset && <button disabled={disabled} onClick={onReset} className={`${btn('bg-zinc-800 border-zinc-700 text-zinc-400 hover:text-zinc-200')} flex-1 flex items-center justify-center gap-1`}><RotateCcw className="w-3 h-3" /> Reset</button>}
          </div>
          {faultedCells.length > 0 && onClearFault && (
            <div className="flex flex-wrap gap-1">
              {faultedCells.map(f => (
                <button key={f.id} onClick={() => onClearFault(f.id)} title="Repair this cell"
                  className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-red-950/40 border border-red-900/60 text-red-300 hover:border-teal-700 hover:text-teal-300 transition-colors">
                  {f.label} ✕
                </button>
              ))}
            </div>
          )}
        </div>

        {/* ── Traffic surge (sandbox only) ───────────────────────────── */}
        {trafficSurge && (
          <div className="bg-zinc-950/50 border border-zinc-800 rounded-lg p-2.5 flex flex-col gap-2 sm:col-span-2 xl:col-span-1">
            <div className="text-[10px] font-bold text-zinc-400 flex items-center gap-1">
              <TrendingUp className="w-3 h-3" /> Traffic surge {trafficSurge.current !== 1 && <span className="text-red-400 font-mono">×{trafficSurge.current}</span>}
            </div>
            <div className="flex gap-1">
              {[2, 3, 5].map(m => (
                <button key={m} disabled={disabled} onClick={() => trafficSurge.onSet(m)}
                  className={`${btn(trafficSurge.current === m ? 'bg-red-600/30 border-red-600/60 text-red-300' : 'bg-zinc-900 border-zinc-700 text-zinc-300 hover:border-red-700')} flex-1`}>
                  ×{m}
                </button>
              ))}
            </div>
            <button disabled={disabled || trafficSurge.current === 1} onClick={trafficSurge.onClear}
              className={btn('bg-zinc-800 border-zinc-700 text-zinc-400 hover:text-zinc-200')}>
              Clear surge
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
