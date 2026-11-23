import React, { useState, useEffect } from 'react';
import { ShieldAlert, Clock, Layers, Zap, Wifi, Check, Shuffle, Radio } from 'lucide-react';
import PartitionAdvice from './PartitionAdvice';

/**
 * Sim Options — the run-shaping settings, grouped by WHICH ENGINE HONOURS THEM.
 *
 * The platform has two tiers and this panel used to blur them: it put an
 * "analytical tier only" badge over shadowing (a real ns-O-RAN lever), told the
 * user the real engine reports KPM "at a fixed 0.5 s" (it does not — this panel
 * sets that period), and offered a Duration field that does not reach the real
 * engine at all. Every field now says which tier reads it, because a setting the
 * user changes and the engine ignores is worse than no setting.
 */

const SCENARIOS = [
  { value: 'UMa-Buildings', label: 'UMa-Buildings', desc: 'Urban macro + drawn building geometry (recommended)' },
  { value: 'UMa',           label: 'UMa',           desc: 'Urban macro — open, statistical LOS/NLOS' },
  { value: 'UMi',           label: 'UMi',           desc: 'Urban micro street canyon — small cells' },
  { value: 'RMa',           label: 'RMa',           desc: 'Rural macro — low density, long range' },
];

/**
 * Which tier reads a field. `analytical` matches the Config editor's badge so
 * the same fact looks the same in both places.
 */
// WHICH CONSUMER READS IT, measured by tools/tier_scoped_audit.py. The badge
// used to say "analytical" for everything, and there is no analytical tier
// any more — it meant the Shannon sandbox behind the RIC console, which is
// gone. A reader seeing "analytical" learned nothing about whether their
// change would do anything; "coverage map" and "fast tier" say exactly that.
const TIER_BADGE = {
  coverage: ['coverage map', 'Coverage map only — measured. Neither tier reads this.'],
  slow: ['slow tier', 'Slow tier (ns-3) only — measured. The fast tier has no such lever.'],
  partial: ['partly on fast', 'Some of its values reach the fast tier and some do not — see the reason.'],
  fast: ['fast tier', 'The fast tier reads this — measured. ns-3 ignores it.'],
  label: ['label', 'A label, not a lever — measured, changing it moves nothing.'],
  // kept so an un-migrated call site is visible rather than silently blank
  analytical: ['coverage map', 'Coverage map only — measured. Neither tier reads this.'],
};

const Tier = ({ kind }) => TIER_BADGE[kind]
  ? (
    <span
      title={TIER_BADGE[kind][1]}
      className="text-[9px] text-sky-400/60 uppercase tracking-wide shrink-0 border border-sky-500/25 rounded px-1"
    >
      {TIER_BADGE[kind][0]}
    </span>
  )
  : (
    <span
      title="Reaches the real ns-O-RAN engine (ns-3 mmWave + E2)."
      className="text-[9px] text-teal-400/70 uppercase tracking-wide shrink-0 border border-teal-500/25 rounded px-1"
    >
      real engine
    </span>
  );

/* Measured on nsg-engine:1.8/1.9: a run delivers roughly 12 KPM windows per
 * simulated second (8.2-17.5 across runs), so the window budget — not any
 * duration field — is what buys simulated time on a live or campaign run. */
const WINDOWS_PER_SIM_S = 12;

/* The BATCH path's horizon, per second of TOTAL WALL CLOCK — not per second of
 * the collection window. A run spends roughly 40 s starting up before collection
 * begins, and the sim is already advancing during it, which is why span tracks
 * total wall time rather than the window alone.
 *
 * 0.10 is the BARE-topology figure (windows of 10/30/60/120 s gave 4.1/5.8/9.2/
 * 13.9 s over walls of 40/59/90/150 — 0.103/0.098/0.102/0.093, flat). A real
 * product config runs about three times slower: measured on the same 3-cell,
 * 6-UE scenario through the product path, 1.1 s over 39 s of wall, 3.0-3.5 over
 * 96, 3.6-4.0 over 149 — 0.028, 0.033, 0.026.
 *
 * The estimate below uses the PRODUCT figure. Quoting the bare one made the
 * panel promise about three times the network time a drawn scenario gets. */
const BATCH_SIM_S_PER_WALL_S = 0.03;
const BATCH_STARTUP_S = 40;

const SimulationRules = ({ currentConfig, onUpdateRules }) => {
  const s = currentConfig?.settings ?? {};
  const a = currentConfig?.area    ?? {};

  const [duration,    setDuration   ] = useState(s.duration_s          ?? 180);
  const [reportMs,    setReportMs   ] = useState(s.report_interval_ms  ?? 1000);
  const [collectS,    setCollectS   ] = useState(s.kpm_collect_s       ?? '');
  const [scenario,    setScenario   ] = useState(s.scenario            ?? 'UMa-Buildings');
  const [shadowing,   setShadowing  ] = useState(s.shadowing_enabled   ?? true);
  const [seed,        setSeed       ] = useState(s.random_seed         ?? '');
  const [resolution,  setResolution ] = useState(a.resolution_m        ?? 37);

  // Sync if config changes (e.g. AI proposal accepted)
  useEffect(() => {
    const s2 = currentConfig?.settings ?? {};
    const a2 = currentConfig?.area    ?? {};
    setDuration(   s2.duration_s          ?? 180);
    setReportMs(   s2.report_interval_ms  ?? 1000);
    setCollectS(   s2.kpm_collect_s       ?? '');
    setScenario(   s2.scenario            ?? 'UMa-Buildings');
    setShadowing(  s2.shadowing_enabled   ?? true);
    setSeed(       s2.random_seed         ?? '');
    setResolution( a2.resolution_m        ?? 37);
  }, [currentConfig]);

  const computeNote = () => {
    const area = currentConfig?.area;
    if (!area) return null;
    const cells = Math.ceil((area.x_max - area.x_min) / resolution) *
                  Math.ceil((area.y_max - area.y_min) / resolution);
    return cells > 100_000
      ? `⚠ ~${(cells / 1000).toFixed(0)}k REM sample points — raise the resolution for faster runs`
      : `~${(cells / 1000).toFixed(1)}k REM sample points`;
  };

  /* What the chosen KPM period costs on the wall clock, which is what the user
   * watches on the live map. The RAN reports once per `reportMs` of SIMULATED
   * time, and a simulated second takes tens of wall seconds, so the live view
   * updates in bursts with a visible pause between them.
   *
   * Measured 2026-09-12, same 3-cell / 3-UE live session for 300 s, changing
   * only this field: 1000 ms gave 132 windows and pauses up to 27 s; 250 ms gave
   * 518 windows and pauses up to 9 s. A simulated second cost ~28 wall seconds
   * at 3 UE and ~73 at 6 UE, which is where these two multipliers come from. */
  const liveGapNote = () => {
    const ms = Number(reportMs);
    if (!Number.isFinite(ms) || ms <= 0) return null;
    const simS = ms / 1000;
    return `Longest pause on the live map: ≈ ${Math.round(simS * 28)} s at 3 UE, ` +
           `≈ ${Math.round(simS * 73)} s at 6 UE. Lower this to smooth it — measured, ` +
           `1000 → 250 ms took the pause from 27 s to 9 s and returned 3.9× the windows.`;
  };

  const handleSave = () => {
    onUpdateRules({
      duration:    parseInt(duration,   10),
      reportMs:    parseInt(reportMs,   10),
      collectS:    collectS === '' ? null : parseInt(collectS, 10),
      scenario,
      shadowing,
      seed:        seed === '' ? null : parseInt(seed, 10),
      resolution:  parseFloat(resolution),
    });
  };

  const note    = computeNote();
  const gapNote = liveGapNote();
  const frames  = Math.max(1, Math.round(Number(duration) || 0) * WINDOWS_PER_SIM_S);

  const cardCls  = "bg-zinc-900/40 border border-zinc-800/80 rounded-xl p-4 space-y-4";
  const inputCls = "w-full bg-zinc-950/80 border border-zinc-800 rounded-md p-2.5 text-sm focus:outline-none focus:border-teal-500 text-zinc-200 transition-colors";

  return (
    <div className="p-6 h-full overflow-y-auto space-y-6 text-zinc-300">

      <div className="flex items-center gap-3 pb-4 border-b border-zinc-800">
        <ShieldAlert className="w-5 h-5 text-teal-500" />
        <div>
          <h2 className="text-lg font-semibold text-zinc-100">Sim Options</h2>
          <p className="text-xs text-zinc-500">
            Applied on the next run. Each field says which tier reads it — the real
            ns-O-RAN engine, or the fast analytical tier that draws coverage maps.
          </p>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-6">

        {/* How long a run covers */}
        <div className={cardCls}>
          <h3 className="text-sm font-semibold text-zinc-200 flex items-center gap-2">
            <Clock className="w-4 h-4 text-amber-400" /> Run horizon
          </h3>
          <div>
            <label className="text-xs text-zinc-400 mb-1 flex items-center justify-between gap-2">
              <span>Duration (seconds)</span> <Tier kind="slow" />
            </label>
            <input type="number" min="1" value={duration} onChange={e => setDuration(e.target.value)}
              className={inputCls} />
            <p className="text-[10px] text-zinc-600 mt-1">
              Simulated length for the analytical tier. It does <span className="text-zinc-400">not</span> reach
              the ns-O-RAN engine, so raising it buys no extra simulated time on a real run.
            </p>
          </div>
          <div>
            <label className="text-xs text-zinc-400 mb-1 flex items-center justify-between gap-2">
              <span>Collection window (wall seconds)</span>
              <Tier kind="slow" />
            </label>
            <input type="number" min="10" max="14400" step="10" value={collectS}
              placeholder="auto — sized from the UE count"
              onChange={e => setCollectS(e.target.value)} className={inputCls} />
            <p className="text-[10px] text-zinc-600 mt-1">
              How long the engine collects KPM on a <span className="text-zinc-400">validation</span>{' '}
              run — the one the <span className="text-zinc-400">Compare</span> tab starts when you
              re-validate a saved run on the real engine — and therefore how much simulated time that
              run covers. Leave empty unless the scenario turns on elapsed time.
            </p>
            <p className="text-[10px] text-zinc-500 mt-1 leading-relaxed">
              <span className="text-amber-300/90">It does nothing on Campaign or RIC Test.</span>{' '}
              Those two drive the engine through its live session bridge, where the horizon is the{' '}
              <span className="text-zinc-400">KPM window budget</span> — a count of windows, roughly
              12 to the simulated second — set where the run is launched. All three tabs are the real
              ns-O-RAN engine; they just reach it two different ways, so the same number means
              different things: 10 here is ten seconds of wall clock, 10 frames on a campaign is ten
              KPM windows.
            </p>
            {collectS !== '' && Number(collectS) > 0 && (
              <p className="text-[10px] text-amber-300/80 mt-1">
                ≈ {((Number(collectS) + BATCH_STARTUP_S) * BATCH_SIM_S_PER_WALL_S).toFixed(1)} s of
                simulated time, and the run will take about{' '}
                {Math.round(Number(collectS) + BATCH_STARTUP_S)} s of wall clock. A planning figure —
                the run reports the span it actually reached.
              </p>
            )}
          </div>
          <div className="rounded-md bg-amber-500/5 border border-amber-500/20 p-2.5">
            <p className="text-[10px] text-amber-200/80 leading-relaxed">
              A batch run at the automatic window covers well
              <span className="font-semibold"> under a second</span> of network time, which is why a
              fault scheduled at 5 s never fires. Measured: the window buys simulated time
              <span className="font-semibold"> linearly</span> — a 3-cell, 6-UE config covered 1.1 s
              automatically and 3.6 s at a 120 s window, and only then did a cell outage at 2.0 s
              actually happen.
            </p>
            <p className="text-[10px] text-amber-200/60 leading-relaxed mt-2">
              On a <span className="font-semibold">live</span> or campaign run the equivalent lever is the
              KPM window budget set where the run is launched — roughly {WINDOWS_PER_SIM_S} windows per
              simulated second, so about {duration || 0} s needs ≈ {frames} windows.
            </p>
            <p className="text-[10px] text-amber-200/60 leading-relaxed mt-2">
              The other way to buy network time is a smaller gNB panel: measured,
              an 8×8 array down to 4×4 gives <span className="font-semibold">1.43×</span> more
              simulated time per run and costs about 35% of peak throughput, with
              handovers and mean throughput unchanged. Set it per cell under the
              antenna&apos;s massive-MIMO rows and columns.
            </p>
          </div>
        </div>

        {/* KPM reporting cadence */}
        <div className={cardCls}>
          <h3 className="text-sm font-semibold text-zinc-200 flex items-center gap-2">
            <Radio className="w-4 h-4 text-teal-400" /> KPM reporting <Tier kind="real" />
          </h3>
          <div>
            <label className="block text-xs text-zinc-400 mb-1">E2 indication period (ms of simulated time)</label>
            <input type="number" min="100" max="5000" step="50" value={reportMs}
              onChange={e => setReportMs(e.target.value)} className={inputCls} />
            <p className="text-[10px] text-zinc-600 mt-1">
              How often the RAN reports KPM over E2 (100–5000 ms). This is the engine&apos;s
              real period — the platform always sends it, so the engine defaults never apply.
            </p>
            {gapNote && <p className="text-[10px] text-teal-300/70 mt-2 leading-relaxed">{gapNote}</p>}
          </div>
        </div>

        {/* Propagation */}
        <div className={cardCls}>
          <h3 className="text-sm font-semibold text-zinc-200 flex items-center gap-2">
            <Wifi className="w-4 h-4 text-emerald-400" /> Propagation model <Tier kind="partial" />
          </h3>
          <div className="space-y-2">
            {SCENARIOS.map(sc => (
              <label key={sc.value} className="flex items-start gap-2.5 cursor-pointer group">
                <input type="radio" name="scenario" value={sc.value} checked={scenario === sc.value}
                  onChange={() => setScenario(sc.value)}
                  className="mt-0.5 h-3.5 w-3.5 accent-teal-500 cursor-pointer" />
                <span className="flex flex-col">
                  <span className={`text-xs font-semibold ${scenario === sc.value ? 'text-teal-400' : 'text-zinc-300 group-hover:text-zinc-100'} transition-colors`}>{sc.label}</span>
                  <span className="text-[10px] text-zinc-600">{sc.desc}</span>
                </span>
              </label>
            ))}
          </div>
          <p className="text-[10px] text-zinc-600">
            Selects the path-loss model and the matching LOS/NLOS condition model.
            Drawn buildings override that condition model with real geometry.
          </p>
        </div>

        {/* Fading */}
        <div className={cardCls}>
          <h3 className="text-sm font-semibold text-zinc-200 flex items-center gap-2">
            <Zap className="w-4 h-4 text-purple-400" /> Channel effects
          </h3>
          <div className="space-y-4 pt-1">
            <label className="flex items-center justify-between cursor-pointer group gap-3">
              <span className="flex flex-col">
                <span className="text-xs text-zinc-300 group-hover:text-zinc-100 transition-colors flex items-center gap-2">
                  Log-normal shadowing <Tier kind="real" />
                </span>
                <span className="text-[10px] text-zinc-600">
                  Large-scale variation in the 3GPP loss model. Turning it off does not
                  calm a scenario — measured, it made handovers noisier.
                </span>
              </span>
              <button type="button" onClick={() => setShadowing(v => !v)}
                className={`shrink-0 relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${shadowing ? 'bg-teal-600' : 'bg-zinc-700'}`}>
                <span className="inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform" style={{ transform: shadowing ? 'translateX(18px)' : 'translateX(2px)' }} />
              </button>
            </label>
          </div>
        </div>

        {/* Reproducibility */}
        <div className={cardCls}>
          <h3 className="text-sm font-semibold text-zinc-200 flex items-center gap-2">
            <Shuffle className="w-4 h-4 text-rose-400" /> Reproducibility <Tier kind="real" />
          </h3>
          <div>
            <label className="block text-xs text-zinc-400 mb-1">Random seed (blank = engine default)</label>
            <input type="number" min="0" value={seed} onChange={e => setSeed(e.target.value)}
              placeholder="blank = run 1"
              className={`${inputCls} placeholder:text-zinc-700`} />
            <p className="text-[10px] text-zinc-600 mt-1">
              Drives the engine&apos;s RNG run number, so mobility, positions and channel all
              repeat. Vary it to build a multi-seed dataset from one config.
            </p>
          </div>
        </div>

        {/* Coverage map resolution */}
        <div className={cardCls}>
          <h3 className="text-sm font-semibold text-zinc-200 flex items-center gap-2">
            <Layers className="w-4 h-4 text-blue-400" /> Coverage map (REM) <Tier kind="coverage" />
          </h3>
          <div className="space-y-3">
            <div>
              <label className="text-xs text-zinc-400 mb-2 flex justify-between">
                <span>Grid resolution (m)</span>
                <span className="text-blue-400 font-mono">{resolution} m</span>
              </label>
              <input type="range" min="5" max="200" step="5" value={resolution}
                onChange={e => setResolution(e.target.value)}
                className="w-full accent-teal-500" />
              <div className="flex justify-between text-[10px] text-zinc-600 mt-1">
                <span>5 m (fine, slow)</span>
                <span>200 m (coarse, fast)</span>
              </div>
            </div>
            {note && (
              <p className={`text-[10px] ${note.startsWith('⚠') ? 'text-amber-500' : 'text-zinc-600'}`}>{note}</p>
            )}
            <p className="text-[10px] text-zinc-600">
              Grid spacing for the heatmap only. The ns-O-RAN engine places UEs at real
              coordinates and never samples this grid.
            </p>
          </div>
        </div>

      </div>

      {/* How the run is EXECUTED, not what it contains. One ns-3 process uses one
          core, so a scenario is made faster by running it as several processes of
          whole cells — and the cost of that is not uniform, which is why the table
          is computed for this scenario instead of being a fixed recommendation.
          Kept out of the save payload above: it shapes the run, not the network. */}
      {currentConfig && <PartitionAdvice config={currentConfig} />}

      <div className="pt-4 flex justify-end">
        <button
          onClick={handleSave}
          className="flex items-center gap-2 px-6 py-2.5 rounded-lg font-medium bg-teal-600 hover:bg-teal-500 text-white shadow-lg shadow-teal-900/20 transition-all"
        >
          <Check className="w-4 h-4" /> Apply to Next Run
        </button>
      </div>

    </div>
  );
};

export default SimulationRules;
