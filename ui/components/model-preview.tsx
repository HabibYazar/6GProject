"use client"

import { useState, useEffect } from "react"
import dynamic from "next/dynamic"
import { Button } from "@/components/ui/button"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Spinner } from "@/components/ui/spinner"
import { toast } from "@/hooks/use-toast"
import { cn } from "@/lib/utils"
import { Play, Eye, Radio, Server, Activity, Square, ShieldAlert, Waves, Trophy, PanelLeft, Waypoints, Map as MapIcon, FileDown, ListChecks, History, Clock } from "lucide-react"
import NetworkCanvas from './NetworkCanvas'

// maplibre touches window at import — load the map view client-only.
const NetworkMap = dynamic(() => import('./NetworkMap'), { ssr: false })
import ConfigEditor from './ConfigEditor'
import SimulationRules from './SimulationRules'
import CoverageMap from './CoverageMap'
import RicTestConsole from './RicTestConsole'
import RicComparison from './RicComparison'
import CampaignConsole from './CampaignConsole'
import CampaignTrend from './CampaignTrend'
import RunTimeline from './RunTimeline'

import { useNSGStore, selectConfig, selectIsSimulating, selectHeatmapData, selectRicStatus, selectActiveTab, selectConfigReviewSignal } from '@/store/useNSGStore'
import { canvasNodeToConfigPatch, canvasToSim, simFromNodeAnchor } from '@/utils/flowMapper'
import { stopSimulation as stopSim } from '@/lib/api'
import type { NSGSimulationConfig } from '@/types/nsg'

// The canvas historically offered display names ("Manhattan", "short range",
// "waypoint") and wrote them VERBATIM into mobility.pattern — the schema only
// accepts its enum, so any UI-edited mobility 422'd the engines. Normalise at
// the write boundary; legacy names map to their closest real pattern.
type MobilityPatternValue = 'static' | 'pedestrian' | 'vehicle' | 'drone' | 'random_walk'
const MOBILITY_PATTERNS: readonly MobilityPatternValue[] = ['static', 'pedestrian', 'vehicle', 'drone', 'random_walk']
const MOBILITY_ALIAS: Record<string, MobilityPatternValue> = {
  'Manhattan': 'vehicle', 'short range': 'pedestrian', 'waypoint': 'drone',
}
const normalizeMobility = (v: string): MobilityPatternValue =>
  (MOBILITY_PATTERNS as readonly string[]).includes(v)
    ? (v as MobilityPatternValue)
    : (MOBILITY_ALIAS[v] ?? 'pedestrian')

const TAB_LABELS: Record<string, string> = {
  visual: 'Visual', config: 'Config', guardrails: 'Sim Options',
  analytics: 'Analytics', ric: 'RIC Test', compare: 'Compare',
  campaign: 'Campaign',
}

export function ModelPreview({ isChatOpen, onToggleChat }: { isChatOpen?: boolean; onToggleChat?: () => void }) {
  const config        = useNSGStore(selectConfig)
  const isSimulating  = useNSGStore(selectIsSimulating)
  const heatmapData   = useNSGStore(selectHeatmapData)
  const ricStatus     = useNSGStore(selectRicStatus)
  const savedRunCount = useNSGStore(s => s.ricSavedRuns.length)
  const setConfig     = useNSGStore(s => s.setConfig)
  const runSimulation = useNSGStore(s => s.runSimulation)
  // Tab lives in the store: the chat's "Review parameters" card has to be able
  // to bring the config editor forward from the other pane.
  const activeTab     = useNSGStore(selectActiveTab)
  const setActiveTab  = useNSGStore(s => s.setActiveTab)
  const reviewSignal  = useNSGStore(selectConfigReviewSignal)
  const reviewPending = useNSGStore(s => s.configReviewPending)

  const [isRunning,          setIsRunning         ] = useState(false)
  const [isSimulationActive, setIsSimulationActive] = useState(false)
  const [visualView,         setVisualView         ] = useState<'abstract' | 'map'>('abstract')
  const [exporting,          setExporting          ] = useState(false)
  const [compareView,        setCompareView        ] = useState<'ai' | 'suite'>('ai')
  const [campaignView,       setCampaignView       ] = useState<'console' | 'timeline'>('console')

  // Remember the last-viewed tab across reloads.
  useEffect(() => {
    const saved = typeof window !== 'undefined' ? localStorage.getItem('nsg-active-tab') : null
    if (saved && saved in TAB_LABELS) setActiveTab(saved)
  }, [setActiveTab])
  useEffect(() => {
    if (typeof window !== 'undefined') localStorage.setItem('nsg-active-tab', activeTab)
  }, [activeTab])

  const handleExportReport = async () => {
    if (!config || exporting) return
    setExporting(true)
    try {
      // Import here (not at module top) so jsPDF stays out of the SSR graph + main bundle.
      const { exportScenarioReport } = await import("@/utils/report")
      await exportScenarioReport(config, heatmapData)
    } catch (e) {
      console.error('PDF export failed', e)
    } finally {
      setExporting(false)
    }
  }

  const handleRunScenario = async () => {
    if (!config) return
    setIsRunning(true)
    try {
      const result = await runSimulation()
      const pointCount = result.heatmap?.length ?? 0
      toast({
        title: "Simulation Complete",
        description: pointCount > 0
          ? `NS-3 coverage map generated — ${pointCount.toLocaleString()} REM points.`
          : "NS-3 run completed, but no REM points were produced.",
      })
      if (pointCount > 0) {
        setActiveTab('analytics')
      }
    } catch (error) {
      toast({ title: "Simulation Error", description: String(error), variant: "destructive" })
    } finally {
      setIsRunning(false)
    }
  }

  const handleStopScenario = async () => {
    setIsSimulationActive(false)
    toast({ title: "Simulation Stopped", description: "Engine halted." })
    try { await stopSim() } catch (e) { console.error(e) }
  }

  const handleNetworkUpdate = (updates: Array<{
    action?:   string
    id?:       string
    nodeType?: string
    label?:    string
    coords?:   { x: number; y: number }
    props?:    any
  }>) => {
    // Read the LATEST committed config (not the render-time `config` closure) so rapid
    // successive edits build on each other instead of overwriting (stale-closure race).
    const latest = useNSGStore.getState().config
    if (!latest) return
    let cfg = { ...latest }

    updates.forEach(({ action, id, coords, props }) => {
      if (action === 'add_site') {
        const sim = canvasToSim(coords?.x ?? 1050, coords?.y ?? 950)
        cfg = { ...cfg, base_stations: [...cfg.base_stations, {
          id: `gnb-${Date.now()}`, name: `New Site ${cfg.base_stations.length + 1}`,
          position: { x: sim.x, y: sim.y, z: 25 },
          radio: { band: { label: 'NR-N77', frequency_mhz: 3600, bandwidth_mhz: 100, numerology: 1 }, tx_power_dbm: 43, antenna: { type: 'cosine', azimuth_deg: 0, tilt_deg: 3, gain_dbi: 15, horizontal_beamwidth_deg: 65, vertical_beamwidth_deg: 7.5 } },
        }] }
      } else if (action === 'add_ue') {
        const sim = canvasToSim(coords?.x ?? 1150, coords?.y ?? 850)
        cfg = { ...cfg, ue_groups: [...cfg.ue_groups, {
          id: `ue-${Date.now()}`, name: `UE Group ${cfg.ue_groups.length + 1}`, count: 10,
          spawn_position: { x: sim.x, y: sim.y }, mobility: { pattern: 'pedestrian', speed_mps: 3 },
          services: [{ slice_type: 'eMBB', qos_id: 8, target_throughput_gbps: 0.01 }],
        }] }
      } else if (action === 'add_obstacle') {
        const sim = canvasToSim(coords?.x ?? 1200, coords?.y ?? 900)
        cfg = { ...cfg, buildings: [...cfg.buildings, {
          id: `bldg-${Date.now()}`,
          bounds: { x_min: sim.x, x_max: sim.x + 100, y_min: sim.y - 50, y_max: sim.y, z_min: 0, z_max: 20 },
          building_type: 'office', wall_material: 'concrete_with_windows', floors: 4, rooms_x: 4, rooms_y: 4,
        }] }

      } else if (action === 'delete' && id) {
        cfg = { ...cfg,
          base_stations: cfg.base_stations.filter(bs => bs.id !== id),
          ue_groups:     cfg.ue_groups.filter(ue => ue.id !== id),
          buildings:     cfg.buildings.filter(b  => b.id  !== id),
        }
      } else if (action === 'duplicate' && id) {
        const bs = cfg.base_stations.find(b => b.id === id)
        if (bs) {
          // Copy ALL co-located sectors (same site) together
          const suffix = `-copy${Date.now()}`
          const colocated = cfg.base_stations.filter(b =>
            Math.abs(b.position.x - bs.position.x) < 1 &&
            Math.abs(b.position.y - bs.position.y) < 1
          )
          const copies = colocated.map(b => ({
            ...b,
            id:       `${b.id}${suffix}`,
            name:     `${b.name} (Copy)`,
            position: { ...b.position, x: b.position.x + 80, y: b.position.y + 80 },
          }))
          cfg = { ...cfg, base_stations: [...cfg.base_stations, ...copies] }
        }
        const ue = cfg.ue_groups.find(u => u.id === id)
        if (ue) cfg = { ...cfg, ue_groups: [...cfg.ue_groups, { ...ue, id: `${ue.id}-copy${Date.now()}`, name: `${ue.name} (Copy)`, spawn_position: { x: ue.spawn_position.x + 80, y: ue.spawn_position.y + 80 } }] }
      } else if (action === 'set_setting' && props?.key) {
        // A SCENARIO-level setting, not a per-site one. antenna_element is the
        // only thing the site panel edits this way, and it belongs there anyway:
        // it is what decides whether the azimuth sitting next to it does
        // anything at all.
        cfg = { ...cfg, settings: { ...cfg.settings, [String(props.key)]: props.value } }

      } else if (action === 'add_sector' && id) {
        const bs = cfg.base_stations.find(b => b.id === id)
        if (bs) {
          const colocated = cfg.base_stations.filter(b => b.position.x === bs.position.x && b.position.y === bs.position.y)
          const newAz = (Math.max(...colocated.map(b => b.radio.antenna.azimuth_deg)) + 120) % 360
          cfg = { ...cfg, base_stations: [...cfg.base_stations, { ...bs, id: `${bs.id}-s${colocated.length}`, name: `${bs.name} S${colocated.length + 1}`, radio: { ...bs.radio, antenna: { ...bs.radio.antenna, azimuth_deg: newAz } } }] }
        }
      } else if (action === 'delete_sector' && id) {
        cfg = { ...cfg, base_stations: cfg.base_stations.filter(bs => bs.id !== id) }

      } else if (id && coords && !action) {
        // Drag: move all co-located base stations together as a single site
        const draggedBS = cfg.base_stations.find(bs => bs.id === id)
        if (draggedBS) {
          const newSim = simFromNodeAnchor('cellNode', coords.x, coords.y)
          const deltaX = newSim.x - draggedBS.position.x
          const deltaY = newSim.y - draggedBS.position.y
          const updatedBS = cfg.base_stations.map(bs => {
            const samePos = Math.abs(bs.position.x - draggedBS.position.x) < 1 &&
                            Math.abs(bs.position.y - draggedBS.position.y) < 1
            return samePos
              ? { ...bs, position: { ...bs.position, x: bs.position.x + deltaX, y: bs.position.y + deltaY } }
              : bs
          })
          cfg = { ...cfg, base_stations: updatedBS }
        } else {
          const patch = canvasNodeToConfigPatch(id, coords, cfg)
          cfg = { ...cfg, ...patch }
        }

      } else if (id && props && !action) {
        // Property update from properties panel
        const bsIdx = cfg.base_stations.findIndex(bs => bs.id === id)
        if (bsIdx !== -1) {
          let bs = { ...cfg.base_stations[bsIdx] }
          if (props.azimuth !== undefined) bs = { ...bs, radio: { ...bs.radio, antenna: { ...bs.radio.antenna, azimuth_deg: Number(props.azimuth) } } }
          if (props.tilt    !== undefined) bs = { ...bs, radio: { ...bs.radio, antenna: { ...bs.radio.antenna, tilt_deg:    Number(props.tilt)    } } }
          if (props.power   !== undefined) bs = { ...bs, radio: { ...bs.radio, tx_power_dbm: Number(props.power) } }
          if (props.height  !== undefined) bs = { ...bs, position: { ...bs.position, z: Number(props.height) } }
          if (props.name    !== undefined) bs = { ...bs, name: String(props.name) }
          if (props.band    !== undefined) {
            const bm: Record<string, { frequency_mhz: number; bandwidth_mhz: number; numerology: number }> = {
              'NR-3600': { frequency_mhz: 3600, bandwidth_mhz: 100, numerology: 1 },
              'NR-2100': { frequency_mhz: 2100, bandwidth_mhz: 50,  numerology: 1 },
              'NR-700':  { frequency_mhz: 700,  bandwidth_mhz: 20,  numerology: 0 },
              'LTE-1800':{ frequency_mhz: 1800, bandwidth_mhz: 20,  numerology: 0 },
            }
            const bd = bm[props.band] ?? bm['NR-3600']
            bs = { ...bs, radio: { ...bs.radio, band: { ...bs.radio.band, ...bd, label: props.band } } }
          }
          const list = [...cfg.base_stations]; list[bsIdx] = bs; cfg = { ...cfg, base_stations: list }
        }
        const ueIdx = cfg.ue_groups.findIndex(ue => ue.id === id)
        if (ueIdx !== -1) {
          let ue = { ...cfg.ue_groups[ueIdx] }
          if (props.ues          !== undefined) ue = { ...ue, count: Number(props.ues) }
          if (props.description  !== undefined) ue = { ...ue, name: String(props.description) }
          if (props.mobilityType !== undefined) ue = { ...ue, mobility: { ...ue.mobility, pattern: normalizeMobility(String(props.mobilityType)) } }
          if (props.speed        !== undefined) ue = { ...ue, mobility: { ...ue.mobility, speed_mps: Number(props.speed) } }
          const list = [...cfg.ue_groups]; list[ueIdx] = ue; cfg = { ...cfg, ue_groups: list }
        }
        const bldIdx = cfg.buildings.findIndex(b => b.id === id)
        if (bldIdx !== -1) {
          let bld = { ...cfg.buildings[bldIdx] }
          if (props.name     !== undefined) bld = { ...bld, id: String(props.name) }
          if (props.material !== undefined) {
            const mm: Record<string, 'wood' | 'concrete' | 'concrete_with_windows'> = { 'Wood': 'wood', 'Glass': 'concrete', 'Foliage': 'wood', 'Concrete': 'concrete_with_windows' }
            bld = { ...bld, wall_material: mm[props.material] ?? 'concrete_with_windows' }
          }
          if (props.height       !== undefined) bld = { ...bld, bounds: { ...bld.bounds, z_max: Number(props.height) } }
          if (props.width        !== undefined) bld = { ...bld, bounds: { ...bld.bounds, x_max: bld.bounds.x_min + Number(props.width) } }
          if (props.depth        !== undefined) bld = { ...bld, bounds: { ...bld.bounds, y_max: bld.bounds.y_min + Number(props.depth) } }
          if (props.floors       !== undefined) bld = { ...bld, floors: Number(props.floors) }
          if (props.buildingType !== undefined) bld = { ...bld, building_type: props.buildingType as 'residential' | 'office' | 'industrial' }
          const list = [...cfg.buildings]; list[bldIdx] = bld; cfg = { ...cfg, buildings: list }
        }
      }
    })

    setConfig(cfg)
  }

  const handleUpdateRules = (newRules: any) => {
    if (!config) return
    setConfig({
      ...config,
      settings: {
        ...config.settings,
        ...(newRules.duration   !== undefined && { duration_s:           Number(newRules.duration)         }),
        ...(newRules.reportMs   !== undefined && { report_interval_ms:   Number(newRules.reportMs)         }),
        ...(newRules.collectS   !== undefined && { kpm_collect_s:        newRules.collectS                 }),
        ...(newRules.scenario   !== undefined && { scenario:             newRules.scenario                 }),
        ...(newRules.shadowing  !== undefined && { shadowing_enabled:    Boolean(newRules.shadowing)       }),
        ...(newRules.seed       !== undefined && { random_seed:          newRules.seed                     }),
      },
      area: {
        ...config.area,
        ...(newRules.resolution !== undefined && { resolution_m: Number(newRules.resolution) }),
      },
    })
    toast({ title: "Rules Applied", description: "Simulation settings updated." })
  }

  return (
    <div className="flex h-full flex-col bg-zinc-950 rounded-lg overflow-hidden border border-white/10 relative">
      <Tabs value={activeTab} onValueChange={setActiveTab} className="flex-1 flex flex-col min-h-0">
        <div className="flex items-center justify-between border-b border-zinc-800 bg-zinc-950/80 px-4 py-2 backdrop-blur-sm z-10">
          <div className="flex items-center gap-2">
            {onToggleChat && (
              <button
                onClick={onToggleChat}
                title={isChatOpen ? 'Hide AI Assistant' : 'Show AI Assistant'}
                className="p-1.5 rounded-lg bg-zinc-800/50 hover:bg-zinc-800 text-zinc-400 hover:text-white transition-colors border border-zinc-700/50"
              >
                <PanelLeft className={`w-4 h-4 transition-transform ${isChatOpen ? '' : 'rotate-180'}`} />
              </button>
            )}
            <h2 className="text-sm font-semibold text-zinc-200 flex items-center gap-2">
              <Server className="w-4 h-4 text-teal-600" />
              {TAB_LABELS[activeTab] ?? 'Model Preview'}
            </h2>
            {activeTab === 'visual' && config && (
              <div className="flex items-center gap-0.5 p-0.5 bg-zinc-900 border border-zinc-800 rounded-lg ml-1">
                {(['abstract', 'map'] as const).map(v => (
                  <button
                    key={v}
                    onClick={() => setVisualView(v)}
                    className={cn(
                      "px-2 py-1 text-[11px] font-bold rounded-md transition-colors flex items-center gap-1.5",
                      visualView === v ? "bg-teal-500/15 text-teal-400" : "text-zinc-500 hover:text-zinc-300"
                    )}
                  >
                    {v === 'abstract' ? <Waypoints className="w-3.5 h-3.5" /> : <MapIcon className="w-3.5 h-3.5" />}
                    {v === 'abstract' ? 'Abstract' : 'Map'}
                  </button>
                ))}
              </div>
            )}
          </div>
          <TabsList className="bg-zinc-900 border border-zinc-800">
            <TabsTrigger value="visual"     className="text-xs text-zinc-400 data-[state=active]:bg-teal-500/10 data-[state=active]:text-teal-400 transition-colors"><Eye        className="w-3 h-3 mr-2"/> Visual</TabsTrigger>
            <TabsTrigger value="config"     className="text-xs text-zinc-400 data-[state=active]:bg-teal-500/10 data-[state=active]:text-teal-400 transition-colors"><Radio      className="w-3 h-3 mr-2"/> Config{reviewPending && <span title="Key parameters are still on their defaults" className="ml-1.5 w-1.5 h-1.5 rounded-full bg-amber-400" />}</TabsTrigger>
            <TabsTrigger value="guardrails" className="text-xs text-zinc-400 data-[state=active]:bg-teal-500/10 data-[state=active]:text-teal-400 transition-colors"><ShieldAlert className="w-3 h-3 mr-2"/> Sim Options</TabsTrigger>
            <TabsTrigger value="analytics"  className="text-xs text-zinc-400 data-[state=active]:bg-teal-500/10 data-[state=active]:text-teal-400 transition-colors"><Activity   className="w-3 h-3 mr-2"/> Analytics</TabsTrigger>
            <TabsTrigger value="ric"        className="text-xs text-zinc-400 data-[state=active]:bg-teal-500/10 data-[state=active]:text-teal-400 transition-colors"><ShieldAlert className="w-3 h-3 mr-2"/> RIC Test{ricStatus === 'connected' && <span className="ml-1.5 w-1.5 h-1.5 rounded-full bg-teal-400 animate-pulse" />}</TabsTrigger>
            <TabsTrigger value="compare"    className="text-xs text-zinc-400 data-[state=active]:bg-amber-500/10 data-[state=active]:text-amber-400 transition-colors"><Trophy      className="w-3 h-3 mr-2"/> Compare{savedRunCount > 0 && <span className="ml-1.5 text-[9px] font-bold text-amber-400/80">{savedRunCount}</span>}</TabsTrigger>
            <TabsTrigger value="campaign"   className="text-xs text-zinc-400 data-[state=active]:bg-amber-500/10 data-[state=active]:text-amber-400 transition-colors"><ListChecks  className="w-3 h-3 mr-2"/> Campaign</TabsTrigger>
          </TabsList>
        </div>

        <TabsContent value="visual" className="flex-1 mt-0">
          <div className="w-full h-full relative bg-zinc-950">
            {config ? (
              <>
                {visualView === 'abstract'
                  ? <NetworkCanvas currentConfig={config} onConfigUpdate={handleNetworkUpdate} isSimulationActive={isSimulationActive} heatmapData={heatmapData} />
                  : <NetworkMap config={config} />}
              </>
            ) : (
              <div className="w-full h-full flex flex-col items-center justify-center text-zinc-600">
                <Activity className="h-10 w-10 mb-4 opacity-50" />
                <p>Waiting for network topology...</p>
              </div>
            )}
          </div>
        </TabsContent>

        <TabsContent value="config" className="flex-1 mt-0 border-none p-0 overflow-hidden bg-black relative data-[state=active]:flex flex-col min-h-0 h-full">
          {/* Rendered even with no scenario loaded: the editor is driven by the
              backend schema, so it can show every parameter at its default
              before anything exists. "No config loaded" was a dead end on the
              one tab whose job is to show what can be configured. */}
          <ConfigEditor
            currentConfig={config}
            reviewSignal={reviewSignal}
            onConfigSave={(newConfig: NSGSimulationConfig) => setConfig(newConfig)}
          />
        </TabsContent>

        <TabsContent value="guardrails" className="flex-1 mt-0 p-0 overflow-hidden bg-black relative data-[state=active]:flex flex-col min-h-0 h-full border-none">
          {config ? (
            <SimulationRules currentConfig={config} onUpdateRules={handleUpdateRules} />
          ) : (
            <div className="flex h-full items-center justify-center text-zinc-600">No network data loaded to apply rules.</div>
          )}
        </TabsContent>

        <TabsContent value="analytics" className="flex-1 mt-0 p-4 bg-zinc-950 overflow-y-auto flex flex-col min-h-0">
          <div className="flex items-center justify-between gap-2 mb-4 shrink-0">
          {/* Live Traffic used to sit beside this as a second view. Campaign covers
              what it showed — a run's traffic over time, with a verdict — so the
              switcher went with it rather than being left with one option. */}
          <div className="flex items-center gap-1.5 px-1 text-xs font-bold text-teal-400">
            <Waves className="w-3.5 h-3.5" /> Coverage Map
            {heatmapData.length > 0 && <span className="w-1.5 h-1.5 rounded-full bg-teal-400" />}
          </div>
            <button
              onClick={handleExportReport}
              disabled={!config || exporting}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-bold rounded-lg border border-zinc-700 text-zinc-300 hover:border-teal-500/60 hover:text-teal-400 disabled:opacity-40 transition-colors"
              title="Export this scenario as a PDF report"
            >
              <FileDown className="w-3.5 h-3.5" /> {exporting ? 'Exporting…' : 'Export PDF'}
            </button>
          </div>

          <div className="flex-1 min-h-0">
            <CoverageMap heatmapData={heatmapData} config={config} />
          </div>
        </TabsContent>

        <TabsContent value="ric" className="flex-1 mt-0 p-4 bg-zinc-950 overflow-y-auto flex flex-col min-h-0">
          <div className="flex-1 min-h-0">
            <RicTestConsole />
          </div>
        </TabsContent>

        {/* Compare holds the two "compared to what?" questions, kept apart on
            purpose: an AI xApp against a baseline ON ONE SCENARIO, and a
            scenario suite against its own history. Merging them would put runs
            with no common axis on one screen. */}
        <TabsContent value="compare" className="flex-1 mt-0 bg-zinc-950 min-h-0 overflow-hidden data-[state=active]:flex flex-col">
          <div className="flex items-center gap-1 border-b border-zinc-800 px-4 py-2">
            {([
              ['ai',    'AI runs',      Trophy],
              ['suite', 'Suite trend',  History],
            ] as const).map(([key, label, Icon]) => (
              <button
                key={key}
                onClick={() => setCompareView(key)}
                className={cn(
                  "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-bold transition-colors",
                  compareView === key ? "bg-amber-500/15 text-amber-400" : "text-zinc-500 hover:text-zinc-300",
                )}
              >
                <Icon className="h-3.5 w-3.5" /> {label}
              </button>
            ))}
          </div>
          {/* Kept mounted for the same reason as the Campaign sub-tabs. */}
          <div className="min-h-0 flex-1 overflow-hidden">
            <div className={cn("h-full", compareView !== 'ai' && "hidden")}>
              <RicComparison />
            </div>
            <div className={cn("h-full", compareView !== 'suite' && "hidden")}>
              <CampaignTrend />
            </div>
          </div>
        </TabsContent>

        {/* Campaign holds the two halves of an unattended run: the verdict, and
            the per-tick evidence behind it. A campaign of one item IS a single
            unattended run, so this needs no separate "Runs" tab — what was
            missing was a way to look INSIDE a run, which the summary cannot show. */}
        {/* forceMount + hide-when-inactive: Radix unmounts inactive tab content,
            which threw away the selected campaign, its diff and its polling every
            time the user looked at another tab and came back. Only this tab gets
            it — the canvas and map tabs are expensive to keep alive and hold no
            state worth preserving. */}
        <TabsContent forceMount value="campaign" className="flex-1 mt-0 bg-zinc-950 min-h-0 overflow-hidden data-[state=inactive]:hidden data-[state=active]:flex flex-col">
          <div className="flex items-center gap-1 border-b border-zinc-800 px-4 py-2">
            {([
              ['console',  'Campaigns', ListChecks],
              ['timeline', 'Timeline',  Clock],
            ] as const).map(([key, label, Icon]) => (
              <button
                key={key}
                onClick={() => setCampaignView(key)}
                className={cn(
                  "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-bold transition-colors",
                  campaignView === key ? "bg-amber-500/15 text-amber-400" : "text-zinc-500 hover:text-zinc-300",
                )}
              >
                <Icon className="h-3.5 w-3.5" /> {label}
              </button>
            ))}
          </div>
          {/* Both stay MOUNTED and are hidden with CSS. Swapping them unmounted
              the console, which threw away the selected campaign, its diff and —
              worst — its polling: switching to Timeline during a run made the
              right-hand panel look like no campaign had ever been started, while
              the engine was still working. */}
          <div className="min-h-0 flex-1 overflow-hidden">
            <div className={cn("h-full", campaignView !== 'console' && "hidden")}>
              <CampaignConsole />
            </div>
            <div className={cn("h-full", campaignView !== 'timeline' && "hidden")}>
              <RunTimeline />
            </div>
          </div>
        </TabsContent>
      </Tabs>

      {/* This runs the ns-3 REM sim and produces the coverage heatmap — nothing
          else. It used to sit under the Visual canvas too, labelled "EXECUTE
          SCENARIO", which is the first big button a new viewer meets and reads
          as "run my scenario": it is not, and a campaign is. So it lives on
          Analytics, where the map it makes appears, and says what it does. It
          stays visible while a sim runs so STOP is reachable from anywhere. */}
      {(activeTab === 'analytics' || isSimulationActive) && (
      <div className="px-4 py-2 border-t border-zinc-800 bg-zinc-950 z-10 relative">
        {isSimulationActive ? (
          <Button onClick={handleStopScenario} className="w-full h-9 rounded-lg font-bold transition-all bg-red-950/40 text-red-500 border border-red-900/50 hover:bg-red-900 hover:text-white">
            <Square className="h-4 w-4 mr-2 fill-current" /> STOP SIMULATION
          </Button>
        ) : (
          <Button
            onClick={handleRunScenario}
            disabled={!config || isRunning || isSimulating}
            className={cn("w-full h-9 rounded-lg font-bold transition-all", config && !isRunning && !isSimulating ? "bg-teal-600 text-white hover:bg-teal-500" : "bg-zinc-800 text-zinc-500")}
          >
            {isRunning || isSimulating ? <Spinner className="h-4 w-4 mr-2 text-white" /> : <Play className="h-4 w-4 mr-2" />}
            {isRunning || isSimulating ? "GENERATING COVERAGE MAP…" : "GENERATE COVERAGE MAP"}
          </Button>
        )}
      </div>
      )}
    </div>
  )
}
