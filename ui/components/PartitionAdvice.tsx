'use client';

/**
 * Split this scenario across processes — and what each shape costs.
 *
 * One ns-3 process uses one core; a scenario is made faster by running it as
 * SEVERAL processes of whole cells, not by making one faster (every
 * single-process lever stacked is about 2.2x). The cost is not uniform: at
 * twelve cells, two, three and four groups cost nothing in handovers while
 * speed goes 2.2x to 6.7x, and only below three cells per group does handover
 * start to disappear.
 *
 * The table is deliberately honest in three ways a speed feature usually is not:
 *
 *   · shapes that need more processes than the machine has PHYSICAL cores are
 *     SHOWN and greyed, not hidden — otherwise the fastest row silently missing
 *     looks like a bug
 *   · the loss column leads, because a shape that deletes handover is not a
 *     faster version of the scenario, it is a different scenario
 *   · the gain column includes MORE UEs SERVED, which is counter-intuitive and
 *     real: partitions cover more simulated time, so more UEs finish attaching
 *
 * Numbers are estimates from a fitted law, and the component says so where the
 * user reads them rather than in a tooltip.
 */
import { useEffect, useState } from 'react';
import { Cpu, AlertTriangle, Loader2, Info } from 'lucide-react';
import type { NSGSimulationConfig } from '@/types/nsg';
import { useNSGStore } from '@/store/useNSGStore';
import { fetchPartitionAdvice, type PartitionAdvice as Advice } from '@/lib/partition';

const HANDOVER_TONE: Record<string, string> = {
  intact: 'text-emerald-400',
  reduced: 'text-amber-400',
  none: 'text-rose-400',
};
const handoverTone = (v: string) => HANDOVER_TONE[v] ?? 'text-zinc-500';

// Whether a split result still describes the SAME network. This is the column
// that decides whether the feature applies at all — everything else is speed.
const FIDELITY_TONE: Record<string, string> = {
  kept: 'text-emerald-400',
  approximate: 'text-amber-400',
  'different network': 'text-rose-400',
};
const FIDELITY_LABEL: Record<string, string> = {
  kept: 'same network',
  approximate: 'approximate',
  'different network': 'different network',
  unknown: '—',
};

export default function PartitionAdvice({ config }: { config: NSGSimulationConfig }) {
  const [advice, setAdvice] = useState<Advice | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // The choice lives in the STORE, not here. Measured with a mount/unmount log:
  // this panel is torn down and rebuilt on a tab change and again on the
  // re-render after Apply, so component state loses the selection and it snaps
  // back to the recommendation — which reads as "Apply reset my choice" even
  // though Apply never touched it.
  const picked = useNSGStore(s => s.partitionGroups);
  const setPicked = useNSGStore(s => s.setPartitionGroups);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    fetchPartitionAdvice(config)
      // Seed the selection from the recommendation ONCE. "Apply to Next Run"
      // writes the config, which re-runs this effect, and re-seeding here threw
      // away whatever the user had just chosen — the selection appeared to snap
      // back to 1 the moment they applied.
      .then(a => {
        if (!live) return;
        setAdvice(a);
        // Fill in a choice that has never been made, and REPLACE one the new
        // scenario cannot offer: a stored 6 is meaningless once the network has
        // three cells, and leaving it selects no row at all. Anything else the
        // user chose outlives this component — the recommendation is a default,
        // not a correction.
        const valid = a.options.some(o => o.groups === picked && !o.over_cores);
        if (!valid) setPicked(a.recommended_groups);
      })
      .catch(e => { if (live) setError(e instanceof Error ? e.message : 'failed'); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [config]);

  if (loading && !advice) {
    return (
      <div className="flex items-center gap-2 text-[12px] text-zinc-500 p-4">
        <Loader2 className="w-3.5 h-3.5 animate-spin" /> working out how this scenario can be split…
      </div>
    );
  }
  if (error) {
    return <div className="text-[12px] text-rose-400 p-4">Could not get partition advice: {error}</div>;
  }
  if (!advice) return null;

  const chosen = advice.options.find(o => o.groups === picked);
  const single = advice.options.length <= 1;

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-4 space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-zinc-100 flex items-center gap-2">
            <Cpu className="w-4 h-4 text-teal-400" /> Split across processes
          </h3>
          <p className="text-[11px] text-zinc-500 mt-0.5">
            {advice.cells} cell{advice.cells === 1 ? '' : 's'} · {advice.ues} UEs ·{' '}
            {advice.physical_cores} physical core{advice.physical_cores === 1 ? '' : 's'}
          </p>
        </div>
        {chosen && (
          <div className="text-right shrink-0">
            <div className="text-2xl font-bold font-mono tabular-nums text-teal-400 leading-none">
              {chosen.speed_x}×
            </div>
            <div className="text-[10px] text-zinc-500 mt-0.5">estimated</div>
          </div>
        )}
      </div>

      {single ? (
        <p className="text-[12px] text-zinc-400">
          A one-cell scenario cannot be split — a cell is the smallest unit a process can hold.
        </p>
      ) : (
        <>
          <table className="w-full text-[11px]">
            <thead>
              <tr className="text-zinc-600 text-left">
                <th className="font-medium pb-1.5">Groups</th>
                <th className="font-medium pb-1.5 text-right">Cells each</th>
                <th className="font-medium pb-1.5 text-right" title="The busiest partition — a parallel run is paced by its slowest member">UEs busiest</th>
                <th className="font-medium pb-1.5 text-right">Speed</th>
                <th className="font-medium pb-1.5 text-right">Handover</th>
                <th className="font-medium pb-1.5 text-right"
                    title="How much of each UE's interference this cut leaves in place. Splitting removes every cell outside a group from that group's world — whether that matters is geometry, and this is it.">
                  Interference
                </th>
              </tr>
            </thead>
            <tbody>
              {advice.options.map(o => {
                const disabled = o.over_cores;
                return (
                  <tr
                    key={o.groups}
                    onClick={() => !disabled && setPicked(o.groups)}
                    className={`border-t border-zinc-800/70 ${
                      disabled ? 'opacity-40 cursor-not-allowed'
                               : 'cursor-pointer hover:bg-zinc-800/50'
                    } ${picked === o.groups ? 'bg-teal-500/10' : ''}`}
                    title={disabled
                      ? `Needs ${o.groups} processes on ${advice.physical_cores} physical cores. `
                        + 'Past the core count the wall clock keeps improving while UEs stop being '
                        + 'served — the run still reports ok, so this is blocked rather than warned about.'
                      : undefined}
                  >
                    <td className="py-1.5 text-zinc-200 font-medium">
                      {o.groups}
                      {o.recommended && (
                        <span className="ml-1.5 text-[9px] font-bold text-teal-400 uppercase">best</span>
                      )}
                    </td>
                    <td className="py-1.5 text-right text-zinc-400">{o.cells_per_group}</td>
                    <td className="py-1.5 text-right">
                      {/* The BUSIEST partition, not the average. The average
                          never runs anywhere, and the gap is not small: a real
                          six-cell scenario split three ways gives 75 / 25 / 50
                          against an advertised 50 each. */}
                      <span className={o.over_density ? 'text-rose-400 font-medium' : 'text-zinc-400'}>
                        {o.ues_per_group}
                      </span>
                      {o.ues_split.length > 1 && !o.balanced && (
                        <span className="ml-1 text-[9px] text-amber-500/80"
                              title={`Actual split: ${o.ues_split.join(' / ')}`}>
                          {o.ues_split.join('/')}
                        </span>
                      )}
                    </td>
                    <td className="py-1.5 text-right font-mono tabular-nums text-zinc-200">
                      {o.speed_x}×
                    </td>
                    <td className={`py-1.5 text-right ${handoverTone(o.handover)}`}>
                      {o.handover}
                      {disabled && <span className="ml-1.5 text-[9px] text-zinc-500 uppercase">no cores</span>}
                    </td>
                    <td className={`py-1.5 text-right ${FIDELITY_TONE[o.fidelity] ?? 'text-zinc-500'}`}>
                      {o.interference_kept_pct !== null && (
                        <span className="font-mono tabular-nums">{o.interference_kept_pct}% </span>
                      )}
                      <span className="text-[9px] uppercase">
                        {FIDELITY_LABEL[o.fidelity] ?? o.fidelity}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          {chosen && chosen.fidelity === 'different network' && (
            <div className="flex gap-2 text-[11px] text-rose-300/90 bg-rose-500/5 border border-rose-500/20 rounded-lg px-3 py-2">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              <span>
                This cut removes <strong>{(100 - (chosen.interference_kept_pct ?? 0)).toFixed(0)}%
                of every UE&rsquo;s interference</strong>. Splitting is not an approximation —
                a cell outside a group leaves that group&rsquo;s world completely — so this
                is a <strong>different network that runs faster</strong>, not a faster run of
                yours. Measured on six cells in one cluster at this shape, over a matched
                window: <strong>2.8% of transport blocks in error against 9.6%</strong> whole,
                UE throughput 51% higher, and half the handovers. Use it for capacity and
                per-cell questions, not for coverage, interference or mobility.
              </span>
            </div>
          )}
          {chosen && chosen.fidelity === 'approximate' && (
            <div className="flex gap-2 text-[11px] text-amber-300/90 bg-amber-500/5 border border-amber-500/20 rounded-lg px-3 py-2">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              <span>
                This cut keeps {chosen.interference_kept_pct}% of the interference. Some of
                what a UE hears is gone, so radio-quality numbers will read better than the
                whole network&rsquo;s. Fine for throughput and load questions; check any
                coverage or SINR claim against a whole run.
              </span>
            </div>
          )}
          {chosen && chosen.groups > 1 && chosen.fidelity === 'kept' && (
            <div className="flex gap-2 text-[11px] text-emerald-300/90 bg-emerald-500/5 border border-emerald-500/20 rounded-lg px-3 py-2">
              <Info className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              <span>
                This cut falls on a natural separation — it keeps{' '}
                {chosen.interference_kept_pct}% of the interference, so the cells on either
                side were barely reaching each other anyway. Here splitting really is the
                same network, run faster.
              </span>
            </div>
          )}
          {chosen && chosen.over_density && (
            <div className="flex gap-2 text-[11px] text-rose-300/90 bg-rose-500/5 border border-rose-500/20 rounded-lg px-3 py-2">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              <span>
                The busiest partition would carry <strong>{chosen.ues_per_cell_max} UEs
                per cell</strong>
                {chosen.density_vs_whole > 1 && <> — {chosen.density_vs_whole}× what
                running whole puts on a cell</>}. Above ~20 there is an unfixed engine
                abort at t=0, and the run dies during startup rather than returning
                anything. {chosen.ues_split.length > 1 && <>This shape's real split is{' '}
                <strong>{chosen.ues_split.join(' / ')}</strong>, not{' '}
                {Math.round(advice.ues / chosen.groups)} each.</>}
              </span>
            </div>
          )}
          {chosen && chosen.empty_groups > 0 && (
            <div className="flex gap-2 text-[11px] text-amber-300/90 bg-amber-500/5 border border-amber-500/20 rounded-lg px-3 py-2">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              <span>
                {chosen.empty_groups} of {chosen.groups} processes would hold no UEs at
                all — they cost a core and simulate an empty cell while the others carry
                the whole load. The speed estimate assumes the work divides; it does not
                here.
              </span>
            </div>
          )}
          {chosen && chosen.handover === 'none' && (
            <div className="flex gap-2 text-[11px] text-rose-300/90 bg-rose-500/5 border border-rose-500/20 rounded-lg px-3 py-2">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              <span>
                One cell per process means a UE has nowhere to hand over to. This shape
                <strong> deletes</strong> handover, load-balancing and coverage-compensated cell
                sleep — the scenario still runs and still reports numbers, they just no longer
                describe the test you designed.
              </span>
            </div>
          )}
          {chosen && chosen.handover === 'reduced' && (
            <div className="flex gap-2 text-[11px] text-amber-300/90 bg-amber-500/5 border border-amber-500/20 rounded-lg px-3 py-2">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              <span>
                Two cells per group keeps roughly {chosen.handover_keep_pct}% of
                handovers — the ones crossing a group boundary are lost. Measured
                twice and it MOVED: 68% on a twelve-cell network, 47% on a six-cell
                one over a matched window. The figure here is the pessimistic end,
                because a smaller network puts more of each cell&rsquo;s neighbours
                outside its own group.
              </span>
            </div>
          )}
        </>
      )}

      <details className="text-[11px] text-zinc-500">
        <summary className="cursor-pointer hover:text-zinc-300 flex items-center gap-1.5">
          <Info className="w-3 h-3" /> What these numbers are, and are not
        </summary>
        <div className="mt-2 space-y-1.5 pl-4">
          <p>
            Speed is <code className="text-zinc-400">{advice.model.speed_law}</code>, fitted to
            measured runs at ±{advice.model.fit_error_pct}% ({advice.model.measured_on}). It is an
            estimate for planning, not a prediction for this scenario.
          </p>
          <ul className="list-disc pl-4 space-y-1">
            {advice.caveats.map((c, i) => <li key={i}>{c}</li>)}
          </ul>
        </div>
      </details>

      {/* ONE choice, THREE paths that start a real engine run. They behave
          differently and the panel says so rather than implying a single
          uniform effect:
            · Compare's "Validate on ns-O-RAN" is a sequential batch loop, so
              splitting each run is pure gain
            · a Campaign already parallelises across configs on the same cores,
              so it trades that for a longer horizon per run and goes sequential
            · the RIC Test real engine is a LIVE session — the streams are
              merged as they arrive, and control is routed to the process that
              owns the cell
          A scenario whose cell count the shape does not divide runs whole. */}
      <div className="space-y-1.5 border-t border-zinc-800 pt-2.5 text-[11px] text-zinc-500">
        <div className="flex items-start gap-1.5">
          <Info className="w-3 h-3 mt-0.5 shrink-0 text-zinc-600" />
          <span>
            {chosen && chosen.groups > 1
              ? <>Every real-engine run started from this app will use{' '}
                  <strong className="text-zinc-400">{chosen.groups} processes of{' '}
                  {chosen.cells_per_group} cell(s)</strong>, merged into one result — and the
                  result carries this shape with it, so a saved or exported run still says
                  what it cost.</>
              : <>Real-engine runs will execute each scenario{' '}
                  <strong className="text-zinc-400">whole</strong> — one process, nothing lost.</>}
          </span>
        </div>
        <ul className="list-disc space-y-1 pl-7 text-zinc-600">
          <li><span className="text-zinc-500">Compare → Validate on ns-O-RAN</span>: a
            sequential batch loop, so splitting is pure gain.</li>
          <li><span className="text-zinc-500">Campaign</span>: one scenario&rsquo;s
            processes run at the same time, one per core — what stops overlapping is the
            SCENARIOS, which were already using those cores. The suite takes about as
            long, each run covers more simulated time. Applied only when every scenario
            in the suite has a cell count the shape divides.</li>
          <li><span className="text-zinc-500">RIC Test → Real engine</span>: a live
            session, merged as it streams; control commands are routed to the process
            holding the cell. The panel there shows the shape the engine confirmed.</li>
        </ul>
        <p className="pl-7 text-zinc-600">
          The processes run <em>concurrently</em>, one ns-3 per physical core — that is
          where the speed comes from, and a partition that gets no UEs leaves its core
          idle and buys nothing. A scenario whose cell count this does not divide runs
          whole rather than failing. Starting the extra engine instances costs about
          15 s per run.
        </p>
      </div>
    </div>
  );
}
