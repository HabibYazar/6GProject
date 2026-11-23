"use client"

/**
 * Test campaign console.
 *
 * A single run answers "what happened?"; a campaign answers "is this still good
 * enough?" — a suite of scenarios at fixed seeds, scored against thresholds the
 * user declares BEFORE the run, ending in one verdict that can be compared to
 * last week's.
 *
 * The scenario source is the live config: a campaign is built by adding the
 * scenario currently on the canvas, so the thing being tested is the thing the
 * user designed. Runs execute sequentially on the backend (the engine serves one
 * session at a time), which is why this polls rather than streams.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import { fetchAuthState } from '@/lib/auth'
import { cn } from "@/lib/utils"
import {
  Trophy, Plus, Trash2, Play, RefreshCw, CheckCircle2, XCircle,
  AlertTriangle, Clock, ChevronRight, ListChecks, Square, GitCompare,
  TrendingDown, TrendingUp, Minus, FileDown, Table2, Braces,
} from "lucide-react"
import {
  createCampaign, listCampaigns, getCampaign, deleteCampaign, stopCampaign, getSimRun,
  getEnginePool,
  getCampaignDatasets,
  compareCampaigns,
} from "@/lib/api"
import { exportCampaignCsv, exportCampaignJson } from "@/utils/campaignExport"
import { fetchPartitionAdvice } from "@/lib/partition"
// One dictionary of metric names, shared with the trend chart — a metric
// should be named once and named the same way in both places.
import { METRIC_PRESETS, METRIC_GROUPS } from "@/lib/metrics"
import { useNSGStore, selectConfig } from "@/store/useNSGStore"
import type {
  CampaignBrief, CampaignRecord, CampaignRun, CampaignThreshold, CampaignItemSpec,
  CampaignViolation, CampaignDiff, CampaignDiffItem, CampaignDiffMetric, SimRunProgress,
  SimRunDataset,
  NSGSimulationConfig, ThresholdOp, Verdict,
} from "@/types/nsg"

// Metrics worth asserting on, as dotted paths into a run summary. Free text is
// still allowed — the backend resolves any path — but a dropdown of the ones the
// engine actually reports is the difference between a threshold that scores a
// run and one that fails it as "missing".
// Paths verified against a real ns-O-RAN run summary. Two traps live here and
// both cost a campaign before they were found: the E2SM-KPM measurement names
// are themselves dotted (`RRU.PrbUsedDl`), and `energy_final` is an object —
// `energy_final` alone resolves to a dict, so a threshold on it always failed
// as "never measured".
const OPS: ThresholdOp[] = ['>=', '<=', '>', '<']

const VERDICT_STYLE: Record<string, { cls: string; icon: typeof CheckCircle2; text: string }> = {
  pass:          { cls: 'text-emerald-400 border-emerald-500/30 bg-emerald-500/10', icon: CheckCircle2,  text: 'PASS' },
  fail:          { cls: 'text-red-400 border-red-500/30 bg-red-500/10',             icon: XCircle,       text: 'FAIL' },
  // "NOT SCORED", not "ERROR": most of these are a run that produced nothing to
  // judge (timed out, no UE ever attached), which is not the scenario's fault
  // and must not read like one.
  error:         { cls: 'text-amber-400 border-amber-500/30 bg-amber-500/10',       icon: AlertTriangle, text: 'NOT SCORED' },
  no_thresholds: { cls: 'text-zinc-400 border-zinc-600/40 bg-zinc-800/40',          icon: ListChecks,    text: 'RECORDED' },
  empty:         { cls: 'text-zinc-500 border-zinc-700/40 bg-zinc-900',             icon: ListChecks,    text: 'EMPTY' },
}

/** NO VERDICT IS NOT THE SAME AS RUNNING. This drew the pulsing "running" chip
 *  for anything without a verdict, so a run left `queued` by an interrupted
 *  campaign — one that will never start — showed as in flight for as long as the
 *  record existed. The status says which it is; use it. */
function VerdictChip({ verdict, running, status }: {
  verdict: Verdict | null; running?: boolean; status?: string;
}) {
  if (running || status === 'running') {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-md border border-teal-500/30 bg-teal-500/10 px-2 py-0.5 text-[10px] font-bold uppercase text-teal-300">
        <Clock className="h-3 w-3 animate-pulse" /> running
      </span>
    )
  }
  if (!verdict) {
    const label = status === 'queued' ? 'not run'
      : status === 'stopped' ? 'stopped'
      : status === 'interrupted' ? 'interrupted'
      : status === 'error' ? 'error'
      : 'no verdict'
    return (
      <span className="inline-flex items-center gap-1.5 rounded-md border border-zinc-700 bg-zinc-800/50 px-2 py-0.5 text-[10px] font-bold uppercase text-zinc-400">
        {label}
      </span>
    )
  }
  const s = VERDICT_STYLE[verdict] ?? VERDICT_STYLE.empty
  const Icon = s.icon
  return (
    <span className={cn("inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[10px] font-bold uppercase", s.cls)}>
      <Icon className="h-3 w-3" /> {s.text}
    </span>
  )
}

/**
 * Roughly how long one run will take, in wall-clock seconds.
 *
 * The horizon of a batch run is the KPM WINDOW BUDGET, not settings.duration_s
 * (which never reaches the ns-O-RAN engine), and what the wall clock scales with
 * is the UE count — more UEs cost wall time, not simulated time. Fitted to runs
 * measured 2026-09-09/10 on nsg-engine:1.8, seconds per window against UEs:
 *
 *      1 UE  0.85-0.98      9 UE  6.1
 *      4 UE  2.9-3.0       24 UE  13.5
 *
 * so about 0.2 s of overhead plus 0.62 s per UE, per window — fitted across
 * those points at 10% mean error, 20% worst case. A larger intercept looked
 * safer and was worse where it matters: it ran 46% long on the single-UE runs
 * that a handover demo actually uses. It is an ESTIMATE — the engine is a real
 * simulation and geometry moves it.
 */
function estimateRunSeconds(frames: number, ues: number): number {
  return Math.max(1, frames) * (0.2 + 0.62 * Math.max(1, ues));
}

/**
 * Bringing the extra engine instances up and taking them down again.
 *
 * Measured 2026-09-19: 8.6 s + 6.4 s for two instances, 9.5 s + 7.3 s for
 * three — near-flat in the count, because the script clears and waits rather
 * than doing per-instance work. It belongs in the estimate: on a short run it
 * is most of what the split gives back.
 */
const PARTITION_LEASE_S = 16

/**
 * The wall budget a run is allowed, and the horizon that actually follows from it.
 *
 * FRAMES ARE A CEILING, NOT A HORIZON. ns-3 runs at a few percent of real time,
 * so a run almost always ends when its wall budget runs out, and the frame
 * target is never reached: a six-cell scenario asked for 50 frames collected 39
 * and covered 1.1 simulated seconds — while this card promised 4.2. The card
 * now shows whichever of the two actually binds, and says which.
 */
const WALL_CAP_CHOICES = [900, 1800, 3600, 7200] as const

/**
 * Wall clock for a run that is SPLIT across processes.
 *
 * The partitions run concurrently, so the run is paced by the BUSIEST one — not
 * by the whole UE count, and not by the group count either. A six-cell scenario
 * split six ways came out 75 / 0 / 0 / 25 / 25 / 25: six processes, but one of
 * them still carrying half the network, so the run is barely faster than two.
 * Estimating from `ues / groups` would have promised six times the speed.
 */
function estimatePartitionedSeconds(frames: number, busiestUes: number): number {
  return estimateRunSeconds(frames, busiestUes) + PARTITION_LEASE_S
}

/**
 * How much SIMULATED time a window budget buys.
 *
 * This is the question the UI could not answer: frames are KPM windows, about
 * 13 to the simulated second, so "120 frames" is roughly nine seconds of network
 * time — not 120 of anything.
 *
 * NOT THE SAME LEVER as Sim Options' "collection window". That one is WALL
 * seconds on the VALIDATION path (`/ric/validate-nsoran` -> engine :8090), which
 * only the Compare tab starts; a campaign goes through the live session bridge
 * (:8091) and never sees it — `kpm_collect_s` does not exist in
 * live_bridge_server.py. So the two estimates disagree for one scenario because
 * they estimate different quantities that happen to share a number: ten FRAMES
 * here is ten KPM windows (~0.8 s simulated), ten there is ten seconds of wall
 * clock.
 *
 * MIND THE WORD "BATCH". In this codebase it means HEADLESS — `run_batch_simulation`
 * below is the campaign/live path, not the :8090 one. Saying "the batch path" for
 * :8090 reads as if a campaign were something else entirely, which it is not: both
 * are the real engine, reached two ways. There is no field that sets simulated seconds
 * directly on this path (settings.duration_s never reaches the ns-O-RAN engine),
 * so the window budget IS the horizon control, and it should say what it buys.
 * The windows-per-simulated-second is NOT a constant: measured on nsg-engine:1.8
 * it ran 8.2, 11.1, 11.4, 11.8, 11.9, 13.3 and 17.5 across runs, so ~12 is the
 * middle and any single run can land 40% either side. This is a planning figure,
 * not a promise — which is why the run itself now reports `sim_span_s`, the span
 * it actually covered, rather than leaving this estimate as the only answer.
 */
/**
 * The FAST tier's horizon, which is arithmetic rather than an estimate.
 *
 * A window IS the KPM report interval, so `frames` windows are exactly
 * `frames * report_interval_ms` of simulated time. There is nothing to
 * predict: the slow tier's `simSecondsFor` answers a different question —
 * how many of ns-3's windows fit in a simulated second — and using it here
 * read 40 frames as 0.3 s where the run actually covers 40.
 */
function fastSimSeconds(frames: number, reportIntervalMs = 1000): number {
  return Math.max(1, frames) * Math.max(1, reportIntervalMs) / 1000
}

/**
 * Wall clock for a fast-tier run. MEASURED, not assumed.
 *
 * There is no wall budget on this path — `_run_fast_tier` takes frames and
 * nothing else — so this is how long it takes, not how long it is allowed.
 *
 * Two terms, because one does not fit. Per window the tier costs a term
 * per LINK (cells x UEs) and a term that grows with the UEs PER CELL,
 * which is what the interference mixture walks. Fitted over seven sizes
 * from 3 cells / 12 UEs to 12 / 150, worst error 26%:
 *
 *    3c  12ue    3.7 ms      6c 150ue   87.6 ms
 *    6c  36ue   18.3 ms      3c  60ue   18.4 ms
 *    6c  72ue   36.1 ms     12c 150ue  134.6 ms
 *   12c  72ue   53.9 ms
 *
 * A MULTI-BAND SCENE COSTS MORE. With one carrier the model only prices
 * the winning cell's SINR; with several it prices every cell's, because
 * the serving choice can no longer ignore the ones it excludes. Measured
 * on the same 6 cells / 150 UEs: 87.6 ms single-band against 147 on the
 * two-band energy-saving scenario, so 1.7x.
 */
const FAST_A = 0.0000653   // s per window per (cell x UE)
const FAST_B = 0.00000774  // s per window per (UE^2 / cell)
const FAST_MULTIBAND = 1.7

function fastRunSeconds(frames: number, cells: number, ues: number,
                        carriers = 1): number {
  const c = Math.max(1, cells), u = Math.max(1, ues)
  const per = FAST_A * c * u + FAST_B * (u * u) / c
  return Math.max(0.05,
    Math.max(1, frames) * per * (carriers > 1 ? FAST_MULTIBAND : 1))
}

/** How many distinct carriers the scene puts on the air. */
function carrierCount(cfg: NSGSimulationConfig | null | undefined): number {
  const set = new Set((cfg?.base_stations ?? []).map(
    b => `${b.radio?.band?.frequency_mhz}/${b.radio?.band?.bandwidth_mhz}`))
  return Math.max(1, set.size)
}

function simSecondsFor(frames: number, cellsPerProcess = 2.4): number {
  // ~5 KPM windows per simulated second PER CELL. The old form divided by a
  // flat 12, which is only right at about two cells: every gNB sends its own
  // CU-CP report, so a six-cell network burns a frame budget three times
  // faster than a two-cell one. Measured on this engine: 6 cells gave 39
  // frames across 1.1 simulated seconds (5.9 per cell per second), a 2-cell
  // partition 50 across 6.1 (4.1). Five is the middle, and the old 12 came
  // from a scenario near the bottom of that range.
  //
  // This is also WHY splitting reaches further at the same budget: each
  // process holds fewer cells, so each of its windows covers more network time.
  //
  // Checked against every matched run measured so far — 39/6c/1.1s, 50/2c/6.1s,
  // 25/2c/3.0s — it lands within 18%, where the flat divisor was out by 195% on
  // the six-cell case. Still a planning figure: the run reports `sim_span_s`,
  // which is the only number that describes what actually happened.
  return Math.max(1, frames) / (5 * Math.max(1, cellsPerProcess));
}

/** "45 s" / "6 min" / "1 h 10 min" — never "3600 s". */
function humanDuration(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)} s`;
  const mins = Math.round(seconds / 60);
  if (mins < 90) return `${mins} min`;
  return `${Math.floor(mins / 60)} h ${mins % 60} min`;
}

export default function CampaignConsole() {
  // An admin can look beyond their own work, but only by asking: a list silently
  // full of other people's campaigns is indistinguishable from your own.
  const [allUsers, setAllUsers] = useState(false)
  // How many engines will actually serve this campaign. A configured instance
  // that is not running is skipped and the campaign silently serialises — the
  // only visible symptom was two runs that "started together" finishing minutes
  // apart, which is not something a user should have to infer.
  const [poolInfo, setPoolInfo] = useState<{ configured: number; alive: number; runs_in_flight?: number } | null>(null)
  useEffect(() => {
    let alive = true
    const read = () => { getEnginePool().then((p: { configured: number; alive: number }) => alive && setPoolInfo(p)).catch(() => {}) }
    read()
    const id = setInterval(read, 30000)
    return () => { alive = false; clearInterval(id) }
  }, [])
  const [isAdmin, setIsAdmin] = useState(false)
  useEffect(() => {
    let alive = true
    fetchAuthState()
      .then((a) => alive && setIsAdmin(a.authenticated && a.role === "admin"))
      .catch(() => {})
    return () => { alive = false }
  }, [])

  const config = useNSGStore(selectConfig)

  const [campaigns, setCampaigns] = useState<CampaignBrief[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<CampaignRecord | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [baselineId, setBaselineId] = useState<string>('')
  const [diff, setDiff] = useState<CampaignDiff | null>(null)
  const [exporting, setExporting] = useState(false)

  // ── Draft campaign ──────────────────────────────────────────────────────────
  const [name, setName] = useState('Baseline suite')
  const [baseSeed, setBaseSeed] = useState(42)
  const [wallCap, setWallCap] = useState<number>(900)
  // Which tier runs the suite. Default slow — it is the authority, and an
  // existing suite must rerun as the suite it was.
  const [tier, setTier] = useState<'fast' | 'slow'>('slow')
  const [items, setItems] = useState<CampaignItemSpec[]>([])
  // Sim Options' shape, applied to the WHOLE suite or not at all. A campaign
  // already runs its configs concurrently and partitioning uses every core for
  // one of them, so the two do not stack: with a shape set, scenarios run one
  // at a time, each split N ways. The suite takes about as long; each run covers
  // several times more simulated time, which is what a campaign is short of.
  const wantedGroups = useNSGStore(s => s.partitionGroups) ?? 1;
  // All-or-nothing on purpose. The backend refuses a shape that does not
  // divide a config's cells, and a suite where one scenario silently ran
  // whole while the rest ran split would compare results from two
  // different instruments.
  // The BUSIEST partition per item, from the backend's own nearest-cell split.
  // The estimate cannot be computed here: UE positions are derived by the engine
  // payload builder, not stored on the config, so asking the backend is the only
  // way to get the number the run will actually be paced by.
  const [busiest, setBusiest] = useState<Record<string, number>>({})
  const usableGroups =
    wantedGroups > 1 && items.length > 0 &&
    items.every(it => (it.config?.base_stations?.length ?? 0) > 0 &&
                      (it.config.base_stations.length % wantedGroups === 0))
      ? wantedGroups : 1

  // Ask once per item whenever the shape or the suite changes. Cheap: the
  // backend computes the split with the same nearest-cell pass the run would
  // do, and touches no engine.
  useEffect(() => {
    if (usableGroups <= 1) { setBusiest({}); return }
    let live = true
    void (async () => {
      const out: Record<string, number> = {}
      for (const it of items) {
        try {
          const a = await fetchPartitionAdvice(it.config)
          const o = a.options.find(x => x.groups === usableGroups)
          if (o) out[it.label] = o.ues_per_group
        } catch { /* leave it out; the estimate falls back to the whole count */ }
      }
      if (live) setBusiest(out)
    })()
    return () => { live = false }
  }, [usableGroups, items])
  const [thresholds, setThresholds] = useState<CampaignThreshold[]>([
    { metric: 'dl_mbps_mean', op: '>=', value: 10, label: 'Downlink (mean)' },
  ])

  const refreshList = useCallback(async () => {
    try {
      const { campaigns } = await listCampaigns(allUsers)
      setCampaigns(campaigns)
      setError(null)
      return campaigns
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      return []
    }
  }, [allUsers])

  useEffect(() => { void refreshList() }, [refreshList])

  // Poll only while something is actually running — a finished campaign is a
  // static record and re-fetching it every few seconds is pure noise.
  const anyRunning = campaigns.some(c => c.status === 'running')
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  useEffect(() => {
    const active = anyRunning || detail?.status === 'running'
    // Keep a slow heartbeat even when nothing here is running. The condition was
    // self-referential: it polled only while the ALREADY-LOADED list held a
    // running campaign, so one started anywhere else — another tab, another user,
    // a script — never appeared, because the list could not learn there was
    // anything to wait for. Seen exactly that: a campaign finished and its run
    // showed up under Timeline while this list still showed the previous, failed
    // one and looked like the only attempt.
    const every = active ? 4000 : 20000
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
    pollRef.current = setInterval(async () => {
      await refreshList()
      if (selectedId && active) {
        // Clear on success: a 404 from one poll (a campaign deleted elsewhere, or
      // a read that raced a backend restart) used to leave its banner up for the
      // rest of the session, over a panel that was showing fresh data.
      try { setDetail(await getCampaign(selectedId)); setError(null) } catch { /* keep last good */ }
      }
    }, every)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [anyRunning, detail?.status, selectedId, refreshList])

  const openCampaign = async (id: string) => {
    setSelectedId(id)
    // A diff belongs to the pair that produced it — carrying it onto another
    // campaign would show last campaign's regression under this one's name.
    setBaselineId('')
    setDiff(null)
    try { setDetail(await getCampaign(id)); setError(null) } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const runDiff = async (against: string) => {
    setBaselineId(against)
    if (!selectedId || !against) { setDiff(null); return }
    try {
      setDiff(await compareCampaigns(selectedId, against))
      setError(null)
    } catch (e) {
      setDiff(null)
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  /**
   * Fetch every run's full dataset once, then hand it to whichever exporter.
   * The frames are the bulk of a campaign, so they are pulled on demand rather
   * than carried in the campaign record.
   */
  const withDatasets = async (
    fn: (rec: CampaignRecord, sets: Record<string, SimRunDataset>) => void,
  ) => {
    if (!detail || exporting) return
    setExporting(true)
    try {
      // One request for the whole campaign. This used to loop the runs and await
      // a full dataset for each, which is the same bytes over twenty-one
      // serialised round-trips.
      const { datasets } = await getCampaignDatasets(detail.id)
      fn(detail, datasets)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setExporting(false)
    }
  }

  const exportReport = async () => {
    if (!detail || exporting) return
    setExporting(true)
    try {
      // Imported here, not at module top, so jsPDF stays out of the main bundle.
      const { exportCampaignReport } = await import("@/utils/campaignReport")
      // The diff goes into the report only when it belongs to THIS campaign —
      // a report carrying someone else's baseline comparison would be a lie.
      await exportCampaignReport(detail, diff?.current.id === detail.id ? diff : null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setExporting(false)
    }
  }

  const stop = async (id: string) => {
    try {
      await stopCampaign(id)
      await refreshList()
      if (selectedId === id) setDetail(await getCampaign(id))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const addCurrentScenario = () => {
    if (!config) return
    const base = config.name || `Scenario ${items.length + 1}`
    // Labels key every report and comparison, so two items may not share one.
    let label = base
    let n = 2
    while (items.some(i => i.label === label)) label = `${base} (${n++})`
    setItems([...items, { label, config: config as NSGSimulationConfig, frames: 40, repeats: 1 }])
  }

  const launch = async () => {
    if (items.length === 0) return
    setBusy(true)
    try {
      const { campaign_id } = await createCampaign({
        name, description: '', base_seed: baseSeed, items, thresholds,
        wall_cap_s: wallCap,
        tier,
        // The shape chosen in Sim Options. Only sent when every scenario in the
        // suite can take it: the backend refuses a shape that does not divide a
        // config's cells, and one odd scenario must not fail the campaign.
        // Partitioning is a SLOW-tier control — it buys ns-3 a longer horizon
        // per process — and the backend refuses it alongside the fast tier
        // rather than ignoring it, so do not send it there.
        partition_groups: tier === 'fast' ? 1 : usableGroups,
      })
      await refreshList()
      await openCampaign(campaign_id)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (id: string) => {
    try {
      await deleteCampaign(id)
      if (selectedId === id) { setSelectedId(null); setDetail(null) }
      await refreshList()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const plannedRuns = items.reduce((n, i) => n + (i.repeats ?? 1), 0)

  return (
    <div className="flex h-full min-h-0 gap-3 p-3">
      {/* ── Left: build + list ─────────────────────────────────────────────── */}
      <div className="flex w-[340px] shrink-0 flex-col gap-3 overflow-y-auto custom-scrollbar">

        <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-3">
          <div className="mb-2 flex items-center gap-2">
            <Trophy className="h-4 w-4 text-amber-400" />
            <h3 className="text-xs font-bold uppercase tracking-wide text-zinc-300">New campaign</h3>
          </div>

          <input
            value={name}
            onChange={e => setName(e.target.value)}
            placeholder="Campaign name"
            className="mb-2 w-full rounded border border-zinc-800 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-200 outline-none focus:border-teal-500"
          />

          {/* WHICH TIER. The only thing that changes is the RAN underneath;
              the thresholds, the scoring and the frames are the same, which
              is what lets one suite be run on both and compared. */}
          <div className="mb-2">
            <div className="mb-1 text-[10px] uppercase tracking-wide text-zinc-500">engine tier</div>
            <div className="flex items-center gap-1 rounded-lg border border-zinc-800 bg-zinc-900 p-1">
              <button
                onClick={() => setTier('fast')}
                title="The per-window model: the engine's own AMC, transport blocks and 3GPP channel, one KPM window per step. Seconds per run."
                className={`flex-1 rounded-md px-2 py-1 text-[11px] font-semibold transition-colors ${tier === 'fast' ? 'bg-teal-500/15 text-teal-400' : 'text-zinc-500 hover:text-zinc-300'}`}
              >Fast tier</button>
              <button
                onClick={() => setTier('slow')}
                title="ns-3 / ns-O-RAN itself. The authority, and minutes per run."
                className={`flex-1 rounded-md px-2 py-1 text-[11px] font-semibold transition-colors ${tier === 'slow' ? 'bg-sky-600 text-white' : 'text-zinc-500 hover:text-zinc-300'}`}
              >Slow tier (ns-3)</button>
            </div>
            <div className="mt-1 text-[10px] leading-snug text-zinc-600">
              {tier === 'fast'
                ? 'Seconds per run, and the horizon is exactly the frames asked for — no wall budget, no partitioning. Where it disagrees with ns-3 is published, not assumed away.'
                : 'ns-3 steps every TTI. The budget below is the real horizon, not the frame target.'}
            </div>
          </div>

          {/* NOT SHOWN ON THE FAST TIER. It was disabled and greyed, which
              still reads as "a setting you cannot reach" rather than "a
              setting that does not exist here" — and the copy two lines
              above already says there is no wall budget. `_run_fast_tier`
              takes frames and nothing else. */}
          <label hidden={tier === 'fast'}
                 className="mb-2 flex items-center justify-between text-[10px] uppercase tracking-wide text-zinc-500">
            time budget / run
            <select
              value={wallCap}
              disabled={tier === 'fast'}
              onChange={e => setWallCap(Number(e.target.value))}
              title="Wall-clock seconds each run may take. This is the REAL horizon: ns-3 runs at a few percent of real time, so a run ends here rather than at its frame target. Raising it is the only way to reach more simulated seconds."
              className="w-28 rounded border border-zinc-800 bg-zinc-900 px-2 py-1 text-right font-mono text-xs text-teal-400 outline-none focus:border-teal-500"
            >
              {WALL_CAP_CHOICES.map(v => (
                <option key={v} value={v}>{v < 3600 ? `${v / 60} min` : `${v / 3600} h`}</option>
              ))}
            </select>
          </label>

          <label className="mb-3 flex items-center justify-between text-[10px] uppercase tracking-wide text-zinc-500">
            base seed
            <input
              type="number"
              value={baseSeed}
              onChange={e => setBaseSeed(Number(e.target.value))}
              title="Repeat i of every scenario runs at base_seed + i, so the campaign is reproducible while its repeats are not identical."
              className="w-24 rounded border border-zinc-800 bg-zinc-900 px-2 py-1 text-right font-mono text-xs text-teal-400 outline-none focus:border-teal-500"
            />
          </label>

          {/* Scenarios */}
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[10px] font-bold uppercase tracking-wide text-zinc-500">
              Scenarios {items.length > 0 && <span className="text-zinc-600">· {plannedRuns} run{plannedRuns !== 1 ? 's' : ''}</span>}
            </span>
            <button
              onClick={addCurrentScenario}
              disabled={!config}
              title={config ? "Add the scenario currently loaded" : "Generate or load a scenario first"}
              className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-bold uppercase text-teal-400 hover:bg-teal-500/10 disabled:opacity-30"
            >
              <Plus className="h-3 w-3" /> current
            </button>
          </div>

          {items.length === 0 ? (
            <p className="mb-3 rounded border border-dashed border-zinc-800 px-2 py-3 text-center text-[10px] text-zinc-600">
              No scenarios yet. Load one on the canvas and press “current”.
            </p>
          ) : (
            <div className="mb-3 space-y-1">
              {items.map((it, i) => (
                <div key={i} className="group rounded border border-zinc-800 bg-zinc-900/60 px-2 py-1.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-[11px] text-zinc-300">{it.label}</span>
                    <button
                      onClick={() => setItems(items.filter((_, j) => j !== i))}
                      className="opacity-0 transition-opacity group-hover:opacity-100 text-zinc-500 hover:text-red-400"
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </div>
                  <div className="mt-1 flex items-center gap-3 text-[9px] uppercase tracking-wide text-zinc-600">
                    <label className="flex items-center gap-1">
                      repeats
                      <input
                        type="number" min={1} max={10} value={it.repeats ?? 1}
                        onChange={e => setItems(items.map((x, j) => j === i
                          ? { ...x, repeats: Math.max(1, Math.min(10, Number(e.target.value))) } : x))}
                        className="w-10 rounded border border-zinc-800 bg-zinc-950 px-1 text-right font-mono text-[10px] text-teal-400 outline-none"
                      />
                    </label>
                    <label className="flex items-center gap-1">
                      frames
                      <input
                        type="number" min={4} max={600} value={it.frames ?? 40}
                        onChange={e => setItems(items.map((x, j) => j === i
                          ? { ...x, frames: Math.max(4, Math.min(600, Number(e.target.value))) } : x))}
                        className="w-12 rounded border border-zinc-800 bg-zinc-950 px-1 text-right font-mono text-[10px] text-teal-400 outline-none"
                      />
                    </label>
                    {/* Frames and UEs are both right here, and together they are
                        what the wall clock depends on — so say how long this is
                        going to take before it is started, not after. */}
                    <span className="text-zinc-500" title={
                      "FRAMES are KPM windows, ~12 per simulated second — not seconds, and not the same "
                      + "lever as Sim Options' collection window, which is wall seconds on the validation "
                      + "path and does not reach a campaign. Left: network time this covers. Right: "
                      + "estimated wall clock, from measured runs. Both are planning figures; the run "
                      + "reports the span it actually reached."
                      + (usableGroups > 1
                        ? ` SPLIT ${usableGroups} WAYS: each partition collects its own ${it.frames ?? 40} `
                          + "windows, so the simulated span is unchanged — what the split buys is wall "
                          + "clock, and the estimate is paced by the BUSIEST partition"
                          + (busiest[it.label] !== undefined ? ` (${busiest[it.label]} UEs)` : "")
                          + `, plus ~${PARTITION_LEASE_S} s to start and stop the extra engines. The `
                          + "merged run comes back with FEWER frames than the partitions collected — "
                          + "duplicates at the same simulated instant collapse."
                        : "")}>
                      {(() => {
                        // WHICHEVER BINDS. The frame target implies one horizon,
                        // the wall budget implies another, and the run gets the
                        // smaller of the two. Promising the frame figure when the
                        // budget cuts it short is how a card offered 4.2 s and a
                        // run delivered 1.1.
                        const ues = (it.config?.ue_groups ?? []).reduce((a, g) => a + (g.count ?? 0), 0)
                        const nFrames = it.frames ?? 40
                        const cells = it.config?.base_stations?.length ?? 1
                        // Each PROCESS holds this many cells, and that is what
                        // sets how fast it spends its window budget.
                        const perProcess = usableGroups > 1 ? cells / usableGroups : cells
                        // THE FAST TIER HAS NO BUDGET TO BE LIMITED BY.
                        // Its horizon is arithmetic — frames times the
                        // report interval — and `_run_fast_tier` takes no
                        // wall cap at all. Reading the slow tier's model
                        // here put 40 frames at 0.3 simulated seconds and
                        // labelled it budget-limited at 15 minutes, on a
                        // run that covers 40 seconds in under a second.
                        if (tier === 'fast') {
                          const win = it.config?.settings?.report_interval_ms ?? 1000
                          return (
                            <>≈ {fastSimSeconds(nFrames, win).toFixed(0)} s simulated
                              <span className="ml-1 text-zinc-700">
                                ({nFrames} × {win} ms)
                              </span>
                            </>
                          )
                        }
                        const perRun = usableGroups > 1 && busiest[it.label] !== undefined
                          ? estimatePartitionedSeconds(nFrames, busiest[it.label])
                          : estimateRunSeconds(nFrames, ues)
                        const capped = perRun > wallCap
                        const full = simSecondsFor(nFrames, perProcess)
                        const reach = capped ? full * (wallCap / perRun) : full
                        return (
                          <>
                            ≈ {reach.toFixed(1)} s simulated
                            {capped && (
                              <span className="ml-1 text-amber-400/80"
                                    title={`The frame target implies ${full.toFixed(1)} s, but this run would need ${Math.round(perRun)} s of wall clock and the budget is ${wallCap} s. Raise the budget above to reach further.`}>
                                (budget-limited)
                              </span>
                            )}
                          </>
                        )
                      })()}
                      <span className="text-zinc-700"> · </span>
                      {humanDuration(
                          (tier === 'fast'
                            ? fastRunSeconds(
                                it.frames ?? 40,
                                it.config?.base_stations?.length ?? 1,
                                (it.config?.ue_groups ?? []).reduce((a, g) => a + (g.count ?? 0), 0),
                                carrierCount(it.config),
                              )
                            : Math.min(
                                usableGroups > 1 && busiest[it.label] !== undefined
                                  ? estimatePartitionedSeconds(it.frames ?? 40, busiest[it.label])
                                  : estimateRunSeconds(
                                      it.frames ?? 40,
                                      (it.config?.ue_groups ?? []).reduce((a, g) => a + (g.count ?? 0), 0),
                                    ),
                                wallCap,
                              )
                          ) * (it.repeats ?? 1),
                        )} to run
                      {/* No working shown. This number IS the estimate for the
                          run as it will actually execute, split or whole — the
                          split, the busiest partition's UE count and the engine
                          start-up cost are already inside it. A badge beside it
                          reading "3 · busiest 4 UE" looked like a sum the reader
                          was expected to finish. It lives in the tooltip. */}
                      {(it.repeats ?? 1) > 1 && ` · ${it.repeats} runs`}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Thresholds */}
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[10px] font-bold uppercase tracking-wide text-zinc-500">Pass criteria</span>
            <button
              onClick={() => setThresholds([...thresholds, { metric: 'dl_mbps_mean', op: '>=', value: 0, label: '' }])}
              className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-bold uppercase text-teal-400 hover:bg-teal-500/10"
            >
              <Plus className="h-3 w-3" /> add
            </button>
          </div>

          {thresholds.length === 0 && (
            <p className="mb-3 rounded border border-dashed border-zinc-800 px-2 py-2 text-[10px] text-zinc-600">
              No criteria — the campaign will record results without ever failing.
            </p>
          )}

          <div className="mb-3 space-y-1">
            {thresholds.map((t, i) => (
              <div key={i} className="group flex items-center gap-1">
                <select
                  value={METRIC_PRESETS.some(m => m.path === t.metric) ? t.metric : '__custom'}
                  onChange={e => {
                    const v = e.target.value
                    if (v === '__custom') return
                    const preset = METRIC_PRESETS.find(m => m.path === v)
                    setThresholds(thresholds.map((x, j) => j === i
                      ? { ...x, metric: v, label: preset?.label ?? '' } : x))
                  }}
                  className="min-w-0 flex-1 rounded border border-zinc-800 bg-zinc-900 px-1.5 py-1 text-[10px] text-zinc-300 outline-none focus:border-teal-500"
                >
                  {/* Grouped, because a flat list of twenty is no more usable
                      than a flat list of nine. The unit is on the option so the
                      reader does not have to know whether PRB is a count or a
                      percentage. */}
                  {METRIC_GROUPS.map(g => (
                    <optgroup key={g} label={g}>
                      {METRIC_PRESETS.filter(m => m.group === g).map(m => (
                        <option key={m.path} value={m.path} title={m.hint}>
                          {m.unit ? `${m.label} (${m.unit})` : m.label}
                        </option>
                      ))}
                    </optgroup>
                  ))}
                  <option value="__custom">custom…</option>
                </select>
                <select
                  value={t.op}
                  onChange={e => setThresholds(thresholds.map((x, j) => j === i
                    ? { ...x, op: e.target.value as ThresholdOp } : x))}
                  className="w-12 rounded border border-zinc-800 bg-zinc-900 px-1 py-1 text-center font-mono text-[10px] text-zinc-300 outline-none focus:border-teal-500"
                >
                  {OPS.map(o => <option key={o} value={o}>{o}</option>)}
                </select>
                <input
                  type="number"
                  value={t.value}
                  onChange={e => setThresholds(thresholds.map((x, j) => j === i
                    ? { ...x, value: Number(e.target.value) } : x))}
                  className="w-16 rounded border border-zinc-800 bg-zinc-900 px-1.5 py-1 text-right font-mono text-[10px] text-teal-400 outline-none focus:border-teal-500"
                />
                <button
                  onClick={() => setThresholds(thresholds.filter((_, j) => j !== i))}
                  className="text-zinc-600 opacity-0 transition-opacity hover:text-red-400 group-hover:opacity-100"
                >
                  <Trash2 className="h-3 w-3" />
                </button>
              </div>
            ))}
            {thresholds.some(t => !METRIC_PRESETS.some(m => m.path === t.metric)) && (
              <input
                value={thresholds.find(t => !METRIC_PRESETS.some(m => m.path === t.metric))?.metric ?? ''}
                onChange={e => {
                  const idx = thresholds.findIndex(t => !METRIC_PRESETS.some(m => m.path === t.metric))
                  setThresholds(thresholds.map((x, j) => j === idx ? { ...x, metric: e.target.value } : x))
                }}
                placeholder="custom metric path, e.g. slices.eMBB.mean_mbps"
                className="w-full rounded border border-zinc-800 bg-zinc-900 px-2 py-1 font-mono text-[10px] text-zinc-300 outline-none focus:border-teal-500"
              />
            )}
          </div>

          <button
            onClick={launch}
            disabled={items.length === 0 || busy}
            className={cn(
              "flex w-full items-center justify-center gap-2 rounded-lg py-2 text-xs font-bold transition-colors",
              items.length > 0 && !busy
                ? "bg-amber-600 text-white hover:bg-amber-500"
                : "bg-zinc-800 text-zinc-500",
            )}
          >
            <Play className="h-3.5 w-3.5" />
            {busy ? 'Starting…' : `Run campaign (${plannedRuns})`}
          </button>
          <p className="mt-1.5 text-center text-[9px] leading-relaxed text-zinc-600">
            {/* An engine that is mid-run refuses the health probe exactly as a
                stopped one does — the bridge serves one connection at a time with
                a backlog of 1. So a normal campaign used to report "0 of 1
                engines reachable", which reads as a broken engine. If the backend
                has a run in flight, that is what this is. */}
            {/* NONE OF THIS IS THE FAST TIER. No engine, no pool, no
                queue — it runs in the backend process and a window costs
                about 0.085 ms per cell per UE. Reporting "minutes each"
                under a suite that finishes in under a second is the same
                mistake as the wall budget above it. */}
            {tier === 'fast'
            ? 'Runs execute in the backend — no engine, no queue. A window costs about 0.085 ms per cell per UE.'
            : poolInfo && poolInfo.alive === 0 && (poolInfo.runs_in_flight ?? 0) > 0
            ? `Engine busy with ${poolInfo.runs_in_flight} run${poolInfo.runs_in_flight === 1 ? '' : 's'} — one at a time, minutes each.`
            : poolInfo && poolInfo.alive > 1
              ? `${poolInfo.alive} engines — ${poolInfo.alive} runs at a time, minutes each.`
              : poolInfo && poolInfo.configured > poolInfo.alive
                ? `${poolInfo.alive} of ${poolInfo.configured} engines reachable — runs execute ONE at a time, minutes each.`
                : 'Runs execute one at a time on the real engine — minutes each.'}
          </p>
          {/* Two ways to spend the same cores, and they do NOT stack — so the
              campaign picks one rather than letting both oversubscribe the
              machine. With a shape set in Sim Options, scenarios run ONE AT A
              TIME, each split across every core; without one, scenarios run
              concurrently, one per engine. Past the PHYSICAL core count the wall
              clock keeps improving while UEs stop being served and every run
              still reports ok, which is why this is a choice and not a sum. */}
          {tier === 'fast' ? (
            <p className="mt-1 text-center text-[9px] leading-relaxed text-zinc-700">
              The frame count IS the horizon here — that many windows of the
              report interval, reached exactly. Partitioning is a slow-tier
              lever and the backend refuses it on this tier.
            </p>
          ) : usableGroups > 1 ? (
            <p className="mt-1 rounded border border-teal-500/20 bg-teal-500/5 px-2 py-1.5 text-center text-[9px] leading-relaxed text-teal-300/80">
              {/* "Scenarios then run one at a time" was read as the PROCESSES
                  queueing, which is the opposite of what happens. Lead with the
                  concurrency, and only mention the queue when there is more
                  than one scenario for it to apply to. */}
              Sim Options splits each scenario into <strong>{usableGroups} processes that
              run at the same time</strong>, one per core.
              {items.length > 1
                ? <> The scenarios themselves queue instead of overlapping — they were
                    already using those cores. The suite takes about as long, and each run
                    covers several times more simulated time.</>
                : <> This run covers several times more simulated time than it would whole.</>}
            </p>
          ) : wantedGroups > 1 ? (
            <p className="mt-1 rounded border border-amber-500/20 bg-amber-500/5 px-2 py-1.5 text-center text-[9px] leading-relaxed text-amber-300/80">
              Sim Options asks for {wantedGroups} processes, but {items.length === 0
                ? 'this suite is empty'
                : 'not every scenario here has a cell count it divides'} — this
              campaign will run each scenario whole.
            </p>
          ) : (
            <p className="mt-1 text-center text-[9px] leading-relaxed text-zinc-700">
              A campaign runs configs in parallel, one per engine. Choose a shape in
              Sim Options to spend those cores inside each run instead, for a longer
              horizon.
            </p>
          )}
        </div>

        {/* History */}
        <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-3">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-xs font-bold uppercase tracking-wide text-zinc-300">History</h3>
            {isAdmin && (
              <label className="ml-3 flex cursor-pointer items-center gap-1.5 text-[10px] text-zinc-500 hover:text-zinc-300">
                <input type="checkbox" checked={allUsers} className="h-3 w-3 accent-emerald-500"
                       onChange={(e) => setAllUsers(e.target.checked)} />
                All users
              </label>
            )}
            <button onClick={() => void refreshList()} className="text-zinc-500 hover:text-teal-400">
              <RefreshCw className="h-3 w-3" />
            </button>
          </div>
          {campaigns.length === 0 ? (
            <p className="text-[10px] text-zinc-600">No campaigns yet.</p>
          ) : (
            <div className="space-y-1">
              {campaigns.map(c => (
                <div
                  key={c.id}
                  onClick={() => void openCampaign(c.id)}
                  className={cn(
                    "group cursor-pointer rounded border px-2 py-1.5 transition-colors",
                    selectedId === c.id
                      ? "border-teal-500/40 bg-teal-500/5"
                      : "border-zinc-800 hover:border-zinc-700",
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-[11px] text-zinc-300">{c.name}</span>
                    <VerdictChip verdict={c.verdict} running={c.status === 'running'} status={c.status} />
                  </div>
                  <div className="mt-0.5 flex items-center justify-between text-[9px] text-zinc-600">
                    {/* A split campaign and a whole one sit next to each other
                        in this list and score on the same axes. Without this
                        they are indistinguishable, and comparing them is the
                        mistake the shape-on-the-result work exists to stop. */}
                    <span>{new Date(c.created_at).toLocaleString()} · {c.run_count}/{c.planned?.length ?? c.run_count} runs{(c.partition_groups ?? 1) > 1 ? <> · <span className="text-teal-400/70" title={`Each scenario ran as ${c.partition_groups} processes of whole cells`}>÷{c.partition_groups}</span></> : null}{c.owner ? <> · <span className="text-zinc-500">{c.owner}</span></> : null}</span>
                    {c.status === 'running' ? (
                      <button
                        onClick={e => { e.stopPropagation(); void stop(c.id) }}
                        title="Stop after the current run"
                        className="text-zinc-500 transition-colors hover:text-red-400"
                      >
                        <Square className="h-2.5 w-2.5 fill-current" />
                      </button>
                    ) : (
                      <button
                        onClick={e => { e.stopPropagation(); void remove(c.id) }}
                        className="opacity-0 transition-opacity hover:text-red-400 group-hover:opacity-100"
                      >
                        <Trash2 className="h-3 w-3" />
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ── Right: detail ──────────────────────────────────────────────────── */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-lg border border-zinc-800 bg-zinc-950">
        {error && (
          <div className="border-b border-red-900/40 bg-red-950/20 px-3 py-2 text-[10px] text-red-300">
            {error}
          </div>
        )}

        {!detail ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-2 text-zinc-600">
            <Trophy className="h-8 w-8 opacity-40" />
            <p className="text-xs">Pick a campaign, or build one on the left.</p>
          </div>
        ) : (
          <div className="flex-1 overflow-y-auto custom-scrollbar">
            {/* Header */}
            <div className="border-b border-zinc-800 px-4 py-3">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="truncate text-sm font-semibold text-zinc-200">{detail.name}</h2>
                  <p className="mt-0.5 text-[10px] text-zinc-500">
                    seed {detail.base_seed} ·{' '}
                    {(detail.partition_groups ?? 1) > 1 && (
                      <span className="text-teal-400/80"
                            title="Each scenario ran as this many ns-3 processes of whole cells, merged into one result. Below three cells per group some handovers do not happen — the run's own notes say which.">
                        {detail.partition_groups} processes ·{' '}
                      </span>
                    )}
                    {detail.runs.length}/{detail.planned?.length ?? detail.runs.length} runs ·{' '}
                    {detail.status === 'interrupted'
                      ? 'interrupted (backend restarted mid-campaign)'
                      : detail.status === 'stopped'
                        ? 'stopped early — results so far are kept'
                        : detail.status}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {detail.runs.length > 0 && (
                    <>
                      <button
                        onClick={() => void exportReport()}
                        disabled={exporting}
                        title="Export this campaign as a PDF test report"
                        className="flex items-center gap-1.5 rounded border border-zinc-700 px-2 py-1 text-[10px] font-bold uppercase text-zinc-300 transition-colors hover:border-teal-500/60 hover:text-teal-400 disabled:opacity-40"
                      >
                        <FileDown className="h-3 w-3" /> {exporting ? '…' : 'PDF'}
                      </button>
                      {/* The data itself. A PDF is the wrong container for
                          numbers someone wants to analyse, and until now there
                          was nowhere else to get them. */}
                      <button
                        onClick={() => void withDatasets(exportCampaignCsv)}
                        disabled={exporting}
                        title="Per-tick time series for every run, as CSV"
                        className="flex items-center gap-1.5 rounded border border-zinc-700 px-2 py-1 text-[10px] font-bold uppercase text-zinc-300 transition-colors hover:border-teal-500/60 hover:text-teal-400 disabled:opacity-40"
                      >
                        <Table2 className="h-3 w-3" /> CSV
                      </button>
                      <button
                        onClick={() => void withDatasets(exportCampaignJson)}
                        disabled={exporting}
                        title="The whole campaign: verdicts, scenario configs and every frame"
                        className="flex items-center gap-1.5 rounded border border-zinc-700 px-2 py-1 text-[10px] font-bold uppercase text-zinc-300 transition-colors hover:border-teal-500/60 hover:text-teal-400 disabled:opacity-40"
                      >
                        <Braces className="h-3 w-3" /> JSON
                      </button>
                    </>
                  )}
                  {detail.status === 'running' && (
                    <button
                      onClick={() => void stop(detail.id)}
                      title="Stop after the current run finishes"
                      className="flex items-center gap-1.5 rounded border border-red-900/50 bg-red-950/30 px-2 py-1 text-[10px] font-bold uppercase text-red-400 transition-colors hover:bg-red-900/40 hover:text-red-300"
                    >
                      <Square className="h-3 w-3 fill-current" /> stop
                    </button>
                  )}
                  <VerdictChip verdict={detail.verdict} running={detail.status === 'running'} status={detail.status} />
                </div>
              </div>

              {/* Baseline picker — the campaign only becomes a bench when it can
                  be held against a previous one. */}
              <div className="mt-2 flex items-center gap-2">
                <GitCompare className="h-3 w-3 shrink-0 text-zinc-600" />
                <select
                  value={baselineId}
                  onChange={e => void runDiff(e.target.value)}
                  className="min-w-0 flex-1 rounded border border-zinc-800 bg-zinc-900 px-1.5 py-1 text-[10px] text-zinc-300 outline-none focus:border-teal-500"
                >
                  <option value="">Compare against a baseline…</option>
                  {campaigns
                    .filter(c => c.id !== detail.id && c.run_count > 0)
                    .map(c => (
                      <option key={c.id} value={c.id}>
                        {c.name} · {new Date(c.created_at).toLocaleDateString()} · {c.verdict ?? '—'}
                      </option>
                    ))}
                </select>
                {diff && (
                  <button
                    onClick={() => { setBaselineId(''); setDiff(null) }}
                    className="shrink-0 text-[10px] uppercase text-zinc-500 hover:text-zinc-300"
                  >
                    clear
                  </button>
                )}
              </div>

              {detail.thresholds.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {detail.thresholds.map((t, i) => (
                    <span key={i} className="rounded border border-zinc-800 bg-zinc-900 px-1.5 py-0.5 font-mono text-[9px] text-zinc-400">
                      {t.label || t.metric} {t.op} {t.value}
                    </span>
                  ))}
                </div>
              )}
            </div>

            {diff && <DiffPanel diff={diff} />}

            {/* Runs */}
            <div className="divide-y divide-zinc-900">
              {detail.runs.length === 0 && (
                <p className="px-4 py-6 text-center text-[11px] text-zinc-600">
                  Queued — the first run is starting.
                </p>
              )}
              {detail.runs.map((r, i) => (
                <RunRow key={i} run={r} />
              ))}
              {/* Runs not started yet, so the suite's full shape is visible up
                  front. "Queued" is only true while the campaign is alive — on a
                  stopped or interrupted one these will never execute, and saying
                  otherwise shows work that is not coming. */}
              {(detail.planned ?? []).slice(detail.runs.length).map((p, i) => {
                const alive = detail.status === 'running'
                return (
                  <div key={`p${i}`} className="flex items-center justify-between px-4 py-2 opacity-40">
                    <span className="text-[11px] text-zinc-400">{p.label}</span>
                    <span className="text-[9px] uppercase tracking-wide text-zinc-600">
                      {alive ? `queued · seed ${p.seed}`
                        : `not run — campaign ${detail.status}`}
                    </span>
                  </div>
                )
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

const OVERALL_STYLE: Record<string, string> = {
  regressed: 'text-red-400 border-red-500/30 bg-red-500/10',
  fixed:     'text-emerald-400 border-emerald-500/30 bg-emerald-500/10',
  unchanged: 'text-zinc-400 border-zinc-700/40 bg-zinc-900',
  changed:   'text-amber-400 border-amber-500/30 bg-amber-500/10',
}

function DiffPanel({ diff }: { diff: CampaignDiff }) {
  const [showAll, setShowAll] = useState(false)

  return (
    <div className="border-b border-zinc-800 bg-black/30 px-4 py-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className={cn("rounded border px-2 py-0.5 text-[10px] font-bold uppercase",
                            OVERALL_STYLE[diff.overall] ?? OVERALL_STYLE.changed)}>
          {diff.overall}
        </span>
        <span className="text-[10px] text-zinc-500">
          vs <span className="text-zinc-300">{diff.baseline.name}</span>{' '}
          ({new Date(diff.baseline.created_at).toLocaleDateString()})
        </span>
      </div>

      {/* Two campaigns that were not run the same way can still be compared —
          but the reader has to be told, or the diff reads as a finding. */}
      {(!diff.seed_match || !diff.threshold_match) && (
        <p className="mb-2 rounded border border-amber-500/25 bg-amber-500/5 px-2 py-1.5 text-[10px] leading-relaxed text-amber-300/80">
          {!diff.seed_match && <>Different base seeds ({diff.baseline.base_seed} → {diff.current.base_seed}) — part of every delta is seed noise. </>}
          {!diff.threshold_match && <>Pass criteria changed between the two, so verdict changes may reflect the criteria, not the network.</>}
        </p>
      )}

      <div className="space-y-2">
        {diff.items.map(item => (
          <DiffItem key={item.label} item={item} showAll={showAll} />
        ))}
      </div>

      <button
        onClick={() => setShowAll(v => !v)}
        className="mt-2 text-[10px] uppercase tracking-wide text-zinc-500 hover:text-teal-400"
      >
        {showAll ? 'show only pass criteria' : 'show every metric'}
      </button>
    </div>
  )
}

const CHANGE_STYLE: Record<string, string> = {
  regressed:    'text-red-400',
  fixed:        'text-emerald-400',
  unchanged:    'text-zinc-500',
  changed:      'text-amber-400',
  incomparable: 'text-zinc-600',
}

function DiffItem({ item, showAll }: { item: CampaignDiffItem; showAll: boolean }) {
  if (item.status !== 'matched') {
    return (
      <div className="flex items-center justify-between rounded border border-zinc-800 px-2 py-1.5">
        <span className="text-[11px] text-zinc-400">{item.label}</span>
        <span className="text-[9px] uppercase tracking-wide text-zinc-500">
          {item.status === 'added' ? 'new in this campaign' : 'not run this time'}
        </span>
      </div>
    )
  }

  const rows = showAll ? item.metrics : item.metrics.filter(m => m.is_threshold)

  return (
    <div className="rounded border border-zinc-800">
      <div className="flex items-center justify-between border-b border-zinc-800/60 px-2 py-1.5">
        <span className="truncate text-[11px] text-zinc-300">
          {item.label}
          {/* The names differed and we paired them anyway. Saying so is the
              difference between a comparison and an assumption. */}
          {item.matched_by === 'position' && (
            <span
              className="ml-1.5 rounded border border-amber-500/40 bg-amber-500/10 px-1 text-[9px] uppercase text-amber-400"
              title={`The two campaigns used different scenario names, and each holds exactly one scenario, so these were paired by position. Baseline side: "${item.baseline_label}".`}
            >
              vs {item.baseline_label}
            </span>
          )}
        </span>
        <span className={cn("text-[9px] font-bold uppercase tracking-wide", CHANGE_STYLE[item.change])}>
          {item.change === 'unchanged'
            ? `${item.current_verdict} (unchanged)`
            : `${item.baseline_verdict} → ${item.current_verdict}`}
        </span>
      </div>
      {rows.length === 0 ? (
        <p className="px-2 py-1.5 text-[10px] text-zinc-600">
          No pass criteria on this campaign — use “show every metric”.
        </p>
      ) : (
        <div className="divide-y divide-zinc-900">
          {rows.map(m => <DiffMetricRow key={m.metric} m={m} />)}
        </div>
      )}
      {/* WHAT CHANGED IN THE CONFIG. The rows above answer "did it get better";
          without this there is nothing to attribute the answer to, and tuning a
          baseline is exactly the activity that needs both halves. */}
      {item.config_delta && !item.config_delta.available && (
        <div className="border-t border-zinc-800/60 px-2 py-1.5">
          <p className="text-[10px] text-zinc-600">
            Config not recorded for one of these runs — campaigns before 14 Sep 2026
            did not persist it, so what differed cannot be shown. Re-run to get it.
          </p>
        </div>
      )}
      {item.config_delta?.available && (
        <div className="border-t border-zinc-800/60 px-2 py-1.5">
          {item.config_delta.changed.length === 0 ? (
            <p className="text-[10px] text-zinc-600">
              Same config on both sides — any difference above is run-to-run variation,
              not something that was changed.
            </p>
          ) : (
            <>
              <p className="mb-1 text-[9px] uppercase tracking-wide text-zinc-500">
                config changed ({item.config_delta.total})
              </p>
              <div className="space-y-0.5">
                {item.config_delta.changed.map(c => (
                  <div key={c.path} className="flex items-baseline gap-2 font-mono text-[10px]">
                    <span className="truncate text-zinc-400" title={c.path}>{c.path}</span>
                    <span className="ml-auto whitespace-nowrap text-zinc-600">
                      {String(c.baseline)}
                    </span>
                    <span className="text-zinc-700">→</span>
                    <span className="whitespace-nowrap text-teal-400">{String(c.current)}</span>
                  </div>
                ))}
              </div>
              {item.config_delta.truncated && (
                <p className="mt-1 text-[10px] text-amber-400/80">
                  showing the first {item.config_delta.changed.length} of{' '}
                  {item.config_delta.total} — too many differences to attribute a metric
                  change to any one of them
                </p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

function DiffMetricRow({ m }: { m: CampaignDiffMetric }) {
  const Icon = m.delta === 0 ? Minus : m.delta > 0 ? TrendingUp : TrendingDown
  // Colour follows MEANING, not sign: a drop in energy is good news, and only
  // the campaign's own thresholds know which way that is.
  const colour = m.within_noise ? 'text-zinc-500'
    : m.direction === 'worse' ? 'text-red-400'
    : m.direction === 'better' ? 'text-emerald-400'
    : 'text-zinc-300'

  return (
    <div className="flex items-center justify-between gap-3 px-2 py-1">
      <span className="flex min-w-0 items-center gap-1.5">
        <span className="truncate font-mono text-[10px] text-zinc-400">{m.metric}</span>
        {m.is_threshold && (
          <span className="shrink-0 rounded border border-zinc-700 px-1 text-[8px] uppercase text-zinc-500">
            criterion
          </span>
        )}
      </span>
      <span className="flex shrink-0 items-center gap-2 font-mono text-[10px]">
        <span className="text-zinc-600">{m.baseline}</span>
        <span className="text-zinc-700">→</span>
        <span className="text-zinc-300">{m.current}</span>
        <span className={cn("flex items-center gap-0.5", colour)}>
          <Icon className="h-3 w-3" />
          {m.delta > 0 ? '+' : ''}{m.delta}
          {m.pct !== null && <span className="opacity-70"> ({m.pct > 0 ? '+' : ''}{m.pct}%)</span>}
        </span>
        {m.delta !== 0 && m.within_noise && (
          <span
            title={`Within the baseline's own seed-to-seed spread (${m.baseline_spread}) — not evidence of a change.`}
            className="rounded border border-zinc-700 px-1 text-[8px] uppercase text-zinc-500"
          >
            noise
          </span>
        )}
        {m.delta !== 0 && !m.noise_known && (
          <span
            title="The baseline ran this scenario once, so there is no spread to judge this against. Use 2+ repeats to tell a real change from run-to-run variation."
            className="rounded border border-amber-500/30 px-1 text-[8px] uppercase text-amber-500/70"
          >
            no spread
          </span>
        )}
      </span>
    </div>
  )
}

/**
 * Live progress for a run that is still going.
 *
 * A batch run is minutes long and used to show nothing but a spinner — there was
 * no way to tell a working run from a stuck one, and no sense of how far along it
 * was. Everything here is data the engine was already sending and the platform
 * was discarding until the run finished.
 */
function RunProgress({ runId }: { runId: string }) {
  const [p, setP] = useState<SimRunProgress | null>(null)

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const d = await getSimRun(runId)
        if (alive && d.progress) setP(d.progress)
      } catch { /* the run may not be registered yet — keep polling */ }
    }
    void tick()
    const id = setInterval(tick, 3000)
    return () => { alive = false; clearInterval(id) }
  }, [runId])

  if (!p) {
    return (
      <p className="px-4 py-2 text-[10px] text-zinc-600">
        starting the engine…
      </p>
    )
  }

  // Before the first KPM window every metric is zero, which reads as "this run is
  // measuring nothing" when it means "the engine has not finished starting".
  // Starting is a real phase and a long one: the E2 registration budget alone is
  // 90s plus 4s per UE, so a 35-UE scenario needs nearly four minutes to produce
  // its first window. Say that, and show the two things that prove it is alive —
  // elapsed time and the bridge's own stage line.
  if (!p.n_frames) {
    return (
      <div className="border-t border-zinc-900 bg-black/30 px-4 py-2.5">
        <p className="text-[10px] text-zinc-500">
          starting the engine — no KPM window yet{' '}
          <span className="font-mono text-teal-400">{Math.round(p.elapsed_s)}s</span>
          <span className="text-zinc-600">
            {' '}· a large scenario takes minutes to register its UEs
          </span>
        </p>
        {p.stage && (
          <p className="mt-1 truncate font-mono text-[9px] text-zinc-600" title={p.stage}>
            {p.stage}
          </p>
        )}
      </div>
    )
  }

  const cells: [string, string][] = [
    ['KPM windows', `${p.n_frames} / ${p.frames_target}`],
    ['engine tick', p.last_tick != null ? String(p.last_tick) : '—'],
    ['elapsed', `${Math.round(p.elapsed_s)} s`],
    ['UEs reporting', String(p.ues_reporting)],
    ['cells reporting', String(p.cells_reporting)],
    ['downlink', `${p.dl_mbps} Mbps`],
    ['power', p.power_w != null ? `${Math.round(p.power_w)} W` : '—'],
    ['handovers', p.handovers != null ? String(p.handovers) : '—'],
  ]
  if (p.anomalies_fired > 0) cells.push(['anomalies fired', String(p.anomalies_fired)])

  return (
    <div className="border-t border-zinc-900 bg-black/30 px-4 py-2.5">
      <div className="mb-2 h-1 w-full overflow-hidden rounded bg-zinc-800">
        <div className="h-full bg-teal-500 transition-all duration-500"
             style={{ width: `${p.pct}%` }} />
      </div>
      <div className="grid grid-cols-2 gap-x-6 gap-y-0.5 sm:grid-cols-4">
        {cells.map(([k, v]) => (
          <div key={k} className="flex items-center justify-between gap-2">
            <span className="text-[9px] uppercase tracking-wide text-zinc-600">{k}</span>
            <span className="font-mono text-[10px] text-teal-400">{v}</span>
          </div>
        ))}
      </div>
      {p.stage && (
        <p className="mt-1.5 truncate font-mono text-[9px] text-zinc-600" title={p.stage}>
          {p.stage}
        </p>
      )}
    </div>
  )
}

function RunRow({ run }: { run: CampaignRun }) {
  const [open, setOpen] = useState(false)
  const summary = (run.summary ?? {}) as Record<string, number>
  const running = run.status === 'running'

  return (
    <div>
      <button
        onClick={() => setOpen(o => !o)}
        className="flex w-full items-center justify-between gap-3 px-4 py-2.5 text-left hover:bg-white/[0.02]"
      >
        <div className="flex min-w-0 items-center gap-2">
          <ChevronRight className={cn("h-3 w-3 shrink-0 text-zinc-600 transition-transform", open && "rotate-90")} />
          <span className="truncate text-[11px] text-zinc-300">{run.label}</span>
          <span className="shrink-0 font-mono text-[9px] text-zinc-600">seed {run.seed}</span>
        </div>
        <div className="flex shrink-0 items-center gap-3">
          {!running && run.summary && (
            <span className="font-mono text-[10px] text-zinc-500">
              {summary.dl_mbps_mean ?? '—'} Mbps · {summary.ticks ?? 0} ticks
            </span>
          )}
          <VerdictChip verdict={run.verdict} running={running} status={run.status} />
        </div>
      </button>

      {/* Progress shows without needing the row expanded: while a run is in
          flight it IS the content, and hiding it behind a click is hiding the
          only thing there is to see. */}
      {running && run.run_id && <RunProgress runId={run.run_id} />}

      {open && !running && (
        <div className="border-t border-zinc-900 bg-black/40 px-4 py-3">
          {run.error && (
            <p className="mb-2 rounded border border-amber-500/25 bg-amber-500/5 px-2 py-1.5 text-[10px] text-amber-300">
              {run.error}
            </p>
          )}

          {run.violations.length > 0 && (
            <div className="mb-2 space-y-1">
              {run.violations.map((v: CampaignViolation, i: number) => (
                <div key={i} className="flex items-center justify-between rounded border border-red-500/25 bg-red-500/5 px-2 py-1 text-[10px]">
                  <span className="font-mono text-red-300">{v.expected}</span>
                  <span className="text-red-400/80">
                    {v.reason === 'missing'
                      ? 'never measured'
                      : `measured ${v.measured}`}
                  </span>
                </div>
              ))}
            </div>
          )}

          {run.summary ? (
            <div className="grid grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-3">
              {Object.entries(run.summary)
                .filter(([, v]) => typeof v === 'number')
                .map(([k, v]) => (
                  <div key={k} className="flex items-center justify-between gap-2 border-b border-zinc-900 py-0.5">
                    <span className="truncate text-[9px] uppercase tracking-wide text-zinc-600">{k.replace(/_/g, ' ')}</span>
                    <span className="font-mono text-[10px] text-teal-400">{String(v)}</span>
                  </div>
                ))}
            </div>
          ) : (
            !run.error && <p className="text-[10px] text-zinc-600">No summary yet.</p>
          )}

          {run.run_id && (
            <p className="mt-2 font-mono text-[9px] text-zinc-700">dataset {run.run_id}</p>
          )}
        </div>
      )}
    </div>
  )
}
