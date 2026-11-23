import React, { useState, useEffect, useCallback, useMemo } from 'react';
import ReactFlow, { applyNodeChanges, Background, Controls, Panel, ReactFlowProvider } from 'reactflow';
import 'reactflow/dist/style.css';

import { configToFlowNodes, simToCanvas, nodeCentrePx, nodeAnchorPx } from '../utils/flowMapper';
import { getEnvelope } from '../lib/api';
import CellNode from './nodes/CellNode';
import UENode from './nodes/UENode';
import ObstacleNode from './nodes/ObstacleNode';
import LiveUeNode from './nodes/LiveUeNode';
import HeatmapOverlay from './HeatmapOverlay';
import { useNSGStore } from '../store/useNSGStore';

// SCALE must match flowMapper.ts SCALE (= 1.0 → 1 sim metre = 1 px). Was 0.25
// (a stale 1px=4m assumption) which made the Link Editor report 4x distances.
const CANVAS_SCALE = 1.0;
const getRealMeters    = (pixels) => Number((pixels / CANVAS_SCALE).toFixed(2));
const getPixelsFromMeters = (meters) => Number((meters * CANVAS_SCALE).toFixed(2));

function NetworkCanvasContent({ currentConfig, onConfigUpdate, isSimulationActive, heatmapData }) {
  // THE ENGINE'S MEASURED LIMITS, fetched once. Drawing is where a scenario
  // quietly gets too big — a few clicks add UE groups of 40 and 80 — and the
  // backend's clamp then caps the run without saying so. This is what lets
  // someone see it before the clamp has to.
  const [envelope, setEnvelope] = useState(null);

  // What is on the canvas decides two of the limits, so the fetch carries it and
  // repeats when it changes. Bandwidth and cell count are read the way the
  // backend reads them (first cell's band) so the warning and the clamp agree.
  const drawnShape = useMemo(() => {
    const bss = currentConfig?.base_stations ?? [];
    const band = bss[0]?.radio?.band;
    return {
      n_gnb: bss.length,
      bw_mhz: Number(band?.bandwidth_mhz) || 0,
      numerology: Number(band?.numerology) || 2,
    };
  }, [currentConfig]);

  useEffect(() => {
    getEnvelope(drawnShape.n_gnb ? drawnShape : undefined)
      .then(setEnvelope).catch(() => {});
  }, [drawnShape.n_gnb, drawnShape.bw_mhz, drawnShape.numerology]);

  // Two kinds of trouble, kept apart because they need different reactions.
  // `capped` means the backend will quietly run a SMALLER network than the one
  // drawn. `breaks` means the run aborts or comes back with nothing to read —
  // measured failures, not guesses.
  const { capped, breaks } = useMemo(() => {
    if (!envelope || !currentConfig) return { capped: [], breaks: [] };
    const cells = (currentConfig.base_stations ?? []).length;
    const ues = (currentConfig.ue_groups ?? []).reduce((a, g) => a + (Number(g.count) || 0), 0);
    const capped = [];
    const breaks = [];
    if (cells > envelope.gnb_max) capped.push(`${cells} cells — the engine runs ${envelope.gnb_max}`);
    if (ues > envelope.ue_max) capped.push(`${ues} UEs — the engine runs ${envelope.ue_max}`);
    if (cells > 0 && ues / cells > envelope.ue_per_cell_max) {
      capped.push(`${Math.round(ues / cells)} UEs per cell — the measured ceiling is ${envelope.ue_per_cell_max}`);
    }
    // Bandwidth per UE: below the measured floor the scheduler's transport-block
    // split rounds to zero and ns-3 aborts mid-run.
    const perCellBw = envelope.ue_per_cell_max_for_bw;
    if (perCellBw && cells > 0 && ues / cells > perCellBw) {
      breaks.push(
        `${Math.round(ues / cells)} UEs per cell at ${drawnShape.bw_mhz} MHz — above ` +
        `${perCellBw} the scheduler aborts the run (transport block splits to zero)`,
      );
    }
    return { capped, breaks };
  }, [envelope, currentConfig, drawnShape]);

  const nodeTypes = useMemo(() => ({ cellNode: CellNode, tower: CellNode, ueNode: UENode, user: UENode, obstacleNode: ObstacleNode, liveUeNode: LiveUeNode }), []);

  // REAL UEs from a running ns-O-RAN live session: their positions stream in
  // config coordinates, so they map onto the design canvas with the same
  // transform as the design nodes. Derived (not in node state) — read-only
  // overlay that appears while a live session runs and vanishes with it.
  const liveKpm = useNSGStore(s => s.ricRealKpm);
  const liveUeNodes = useMemo(() => {
    const ues = liveKpm?.ues ?? [];
    return ues.filter(u => u.x != null && u.y != null).map(u => {
      const p = simToCanvas(u.x, u.y);
      return {
        id: `live-ue-${u.imsi}`, type: 'liveUeNode',
        position: { x: p.x, y: p.y },
        data: { imsi: u.imsi, cell: u.cell, slice: u.slice },
        draggable: false, selectable: false, zIndex: 40,
      };
    });
  }, [liveKpm]);
  const [nodes, setNodes] = useState([]);
  const [edges, setEdges] = useState([]);
  const [showHeatmap, setShowHeatmap] = useState(true);
  
  // Tüm yeni özellikleri (isim, açıklama, hız vb.) tutan hafıza
  const [localProps, setLocalProps] = useState({
    power: 35, ues: 1, azimuth: 0, mobilityType: "pedestrian",
    speed: 30, name: "", description: "", tilt: 2, height: 20, band: "NR-3600",
    material: "Concrete", loss: 20, obsWidth: 120, obsDepth: 80, angle: 0,
    floors: 4, buildingType: "office",
  });
  
  const [editDistance, setEditDistance] = useState("");
  const [anchorMode, setAnchorMode] = useState("node1");

  const selectedNodes = nodes.filter(node => node.selected);
  const calculateRawPixelDistance = (n1, n2) => Math.sqrt(Math.pow(n2.position.x - n1.position.x, 2) + Math.pow(n2.position.y - n1.position.y, 2));

  useEffect(() => {
    if (selectedNodes.length === 1) {
      const d = selectedNodes[0].data;

      // Collect ALL co-located sectors from config so adding/removing shows immediately
      let sectors = [{ id: selectedNodes[0].id, power: d.txPower || 35, azimuth: d.azimuth || 0, tilt: d.tilt || 2, height: d.height || 20 }];
      if (currentConfig && selectedNodes[0].type === 'cellNode') {
        const thisBs = currentConfig.base_stations.find(bs => bs.id === selectedNodes[0].id);
        if (thisBs) {
          const colocated = currentConfig.base_stations.filter(bs =>
            Math.abs(bs.position.x - thisBs.position.x) < 1 &&
            Math.abs(bs.position.y - thisBs.position.y) < 1
          );
          if (colocated.length > 0) {
            sectors = colocated.map(bs => ({
              id:      bs.id,
              power:   bs.radio.tx_power_dbm,
              azimuth: bs.radio.antenna.azimuth_deg,
              tilt:    bs.radio.antenna.tilt_deg,
              height:  bs.position.z,
            }));
          }
        }
      }

      setLocalProps({
        name:         d.label        || "",
        description:  d.description  || "",
        height:       d.height       || 20,
        band:         d.band         || "NR-3600",
        sectors,
        ues:          d.ues          || 1,
        mobilityType: d.mobilityType || "pedestrian",
        speed:        d.speed        || 3,
        material:     d.material     || "Concrete",
        loss:         d.loss         || 20,
        obsWidth:     d.width        || 120,
        obsDepth:     d.depth        || d.heightM || 80,
        floors:       d.floors       || 4,
        buildingType: d.buildingType || 'office',
        angle:        d.angle        || 0,
      });
    } else if (selectedNodes.length === 2) {
      const rawPix = calculateRawPixelDistance(selectedNodes[0], selectedNodes[1]);
      setEditDistance(getRealMeters(rawPix).toFixed(1));
      setAnchorMode("node1"); 
    }
  }, [selectedNodes.length === 1 ? selectedNodes[0].id : null, selectedNodes.length, currentConfig, nodes.length]);

 const applyNewDistance = () => {
    const targetDistMeters = parseFloat(editDistance);
    if (isNaN(targetDistMeters) || targetDistMeters < 0 || selectedNodes.length !== 2) return;
    
    const n1 = selectedNodes[0], n2 = selectedNodes[1];

    // Ölçüm DÜĞÜM MERKEZLERİ arasında yapılır, React Flow'un tuttuğu sol üst
    // köşeler arasında değil: bir hücre kutusu 40x40, bir UE grubu kutusu
    // 160x120, yani köşeden köşeye ölçmek bir hücre ile bir UE arasında 60 m
    // yatay / 40 m dikey sabit hata verirdi — tam da bu aracın düzeltmek için
    // var olduğu türden bir hata.
    const c1 = nodeCentrePx(n1), c2 = nodeCentrePx(n2);
    const dx = c2.x - c1.x;
    const dy = c2.y - c1.y;
    const currentRawPix = Math.sqrt(dx * dx + dy * dy);

    if (currentRawPix === 0) return;

    // Hedef piksel uzaklığı
    const targetPixelDist = getPixelsFromMeters(targetDistMeters);

    // Oran: Hedefin mevcut mesafeye oranı
    const scale = targetPixelDist / currentRawPix;

    // AnchorMode'a göre yeni MERKEZLER, sonra her düğümün kendi kutusuna göre
    // tekrar çapaya çevrilir.
    let k1 = { x: c1.x, y: c1.y }, k2 = { x: c2.x, y: c2.y };

    if (anchorMode === 'node1') {
       // n1 sabit, n2 hareketli
       k2 = { x: c1.x + dx * scale, y: c1.y + dy * scale };
    } else if (anchorMode === 'node2') {
       // n2 sabit, n1 hareketli
       k1 = { x: c2.x - dx * scale, y: c2.y - dy * scale };
    } else {
       // İkisi de hareketli (merkeze göre)
       const midX = (c1.x + c2.x) / 2;
       const midY = (c1.y + c2.y) / 2;
       k1 = { x: midX - (dx * scale) / 2, y: midY - (dy * scale) / 2 };
       k2 = { x: midX + (dx * scale) / 2, y: midY + (dy * scale) / 2 };
    }

    const newN1 = nodeAnchorPx(n1.type, k1.x, k1.y);
    const newN2 = nodeAnchorPx(n2.type, k2.x, k2.y);

    // Nodes güncelleme
    setNodes(nds => nds.map(n => {
      if (n.id === n1.id) return { ...n, position: newN1 };
      if (n.id === n2.id) return { ...n, position: newN2 };
      return n;
    }));

    // Pass canvas pixel positions — model-preview converts to sim coords via canvasNodeToConfigPatch
    if (onConfigUpdate) {
      onConfigUpdate([
        { id: n1.id, coords: newN1 },
        { id: n2.id, coords: newN2 },
      ]);
    }
  };

  const onNodeDragStop = (event, node) => {
    // Pass canvas pixel position directly; model-preview applies canvasNodeToConfigPatch
    if (onConfigUpdate) {
      onConfigUpdate([{ id: node.id, nodeType: node.type, label: node.data?.label, coords: node.position }]);
    }
  };

  // 2. Sağ panelden açı/güç değiştirme fonksiyonunu güncelle:
  const handlePropertyChange = (nodeId, field, value, sectorIndex = undefined) => {
    const dataField = (field === 'name' || field === 'description') ? 'label' : field;
    const node = nodes.find(n => n.id === nodeId); 
    
    setNodes(nds => nds.map(n => {
      if (n.id === nodeId) {
         const newData = { ...n.data };
         if (sectorIndex !== undefined && newData.sectors) {
            const newSectors = [...newData.sectors];
            newSectors[sectorIndex] = { ...newSectors[sectorIndex], [field]: Number(value) || value };
            newData.sectors = newSectors;
         } else {
            newData[dataField] = value;
            newData[field] = value;
         }
         return { ...n, data: newData };
      }
      return n;
    }));
    
    if (onConfigUpdate && node) {
      onConfigUpdate([{ 
        id: nodeId, 
        nodeType: node.type, 
        label: node.data?.label, 
        props: { [field]: value, sectorIndex: sectorIndex } 
      }]);
    }
  };

  useEffect(() => {
    if (!currentConfig) return;
    const { nodes: newNodes } = configToFlowNodes(currentConfig);
    setNodes(prev => newNodes.map(n => ({ ...n, selected: !!prev.find(p => p.id === n.id)?.selected })));
    setEdges([]);
  }, [currentConfig]);

  const onNodesChange = useCallback((changes) => setNodes((nds) => applyNodeChanges(changes, nds)), []);

  return (
    <>
      <style>{`
        .react-flow__node.selected { outline: 3px solid #3b82f6 !important; outline-offset: 6px; box-shadow: 0 0 30px rgba(59, 130, 246, 0.6) !important; transition: all 0.2s ease; z-index: 50; }
        .react-flow__node-cellNode.selected { border-radius: 50%; }
        .react-flow__node-ueNode.selected { border-radius: 8px; }
      `}</style>
      <ReactFlow
        nodes={liveUeNodes.length ? [...nodes, ...liveUeNodes] : nodes}
        edges={edges} onNodesChange={onNodesChange} onNodeDragStop={onNodeDragStop} nodeTypes={nodeTypes}
        nodesConnectable={false} elementsSelectable={true} multiSelectionKeyCode={['Control', 'Shift', 'Meta']}
        nodesDraggable={!isSimulationActive}
        fitView fitViewOptions={{ padding: 0.2 }} minZoom={0.05} maxZoom={2} className="bg-zinc-950 border border-zinc-800"
      >
        <Background color="#27272a" gap={25} />
        <HeatmapOverlay data={heatmapData} visible={showHeatmap} />
        <Controls className="bg-zinc-900 border-zinc-800 fill-zinc-400" />

        {/* SOL ÜST TOOLBOX (ARAÇ KUTUSU VE ÖLÇEK) */}
        <Panel position="top-left" className="mt-2 ml-2 w-48 flex flex-col gap-2 p-3 bg-zinc-900/90 border border-zinc-800 rounded-lg shadow-xl backdrop-blur-md">
        {/* EĞER SİMÜLASYON ÇALIŞIYORSA KULLANICIYA UYARI VER */}
           {isSimulationActive && (
             <div className="bg-red-950/40 border border-red-900/50 text-red-500 text-[10px] font-bold text-center py-1.5 rounded animate-pulse uppercase tracking-wider">
               Simulation Running<br/>(Editing Locked)
             </div>
           )}
           {/* THESE ARE THE SLOW TIER'S LIMITS AND ONLY ITS.
               Every one was measured on ns-3: where the mmWave scheduler
               splits a transport block to zero, where its heap corrupts,
               where the envelope caps a run. The FAST TIER has none of that
               code — it computes a window in closed form, so it has no abort
               to hit. Measured (tools/fast_tier_envelope.py): 48 cells and
               5,760 UEs run clean with every UE served and 44 MB of memory;
               it beats real time to 48 cells / 1,440 UEs and past that it is
               merely slower, still producing every window.
               Saying "the run" without saying WHICH run told someone their
               scenario was impossible when one of the two tiers runs it
               comfortably. */}
           {breaks.length > 0 && (
             <div className="bg-rose-950/40 border border-rose-800/60 rounded px-2 py-1.5">
               <div className="text-[9px] font-bold text-rose-400 uppercase tracking-wide mb-1">
                 Slow tier (ns-3) will break
               </div>
               <ul className="text-[9px] leading-relaxed text-rose-200/80 list-disc pl-3">
                 {breaks.map((m, i) => <li key={i}>{m}</li>)}
               </ul>
               <div className="text-[9px] text-rose-200/50 mt-1">
                 Widen the band or thin the cell — ns-3 aborts here, it is not capped.
                 <span className="text-teal-400/70"> The fast tier runs this.</span>
               </div>
             </div>
           )}
           {capped.length > 0 && (
             <div className="bg-amber-950/30 border border-amber-800/50 rounded px-2 py-1.5">
               <div className="text-[9px] font-bold text-amber-400 uppercase tracking-wide mb-1">
                 Past the slow tier's envelope
               </div>
               <ul className="text-[9px] leading-relaxed text-amber-200/80 list-disc pl-3">
                 {capped.map((m, i) => <li key={i}>{m}</li>)}
               </ul>
               <div className="text-[9px] text-amber-200/50 mt-1">
                 A slow-tier run will be capped to these.
                 <span className="text-teal-400/70"> The fast tier runs it as drawn — measured to 48 cells / 5,760 UEs.</span>
               </div>
             </div>
           )}
           {heatmapData && heatmapData.length > 0 && (
             <button
               onClick={() => setShowHeatmap(v => !v)}
               className={`text-[10px] w-full px-3 py-1.5 rounded font-bold transition-colors border ${
                 showHeatmap
                   ? 'bg-teal-600/20 border-teal-600/50 text-teal-400 hover:bg-teal-600/30'
                   : 'bg-zinc-800 border-zinc-700 text-zinc-500 hover:bg-zinc-700'
               }`}
             >
               {showHeatmap ? '◉' : '○'} RSRP HEATMAP
             </button>
           )}

           <fieldset disabled={isSimulationActive} className="flex flex-col gap-2 disabled:opacity-40 transition-opacity">
             <button onClick={() => { if(onConfigUpdate) onConfigUpdate([{ action: 'add_site',     coords: { x: 1200, y: 800 } }])}} className="text-[10px] w-full px-3 py-1.5 bg-teal-600 text-black hover:bg-teal-500 rounded font-bold transition-colors shadow-[0_0_10px_rgba(20,184,166,0.3)]">+ ADD SITE</button>
             <button onClick={() => { if(onConfigUpdate) onConfigUpdate([{ action: 'add_ue',       coords: { x: 1300, y: 900 } }])}} className="text-[10px] w-full px-3 py-1.5 bg-blue-600 text-white hover:bg-blue-500 rounded font-bold transition-colors shadow-[0_0_10px_rgba(59,130,246,0.3)]">+ ADD AREA</button>
             <button onClick={() => { if(onConfigUpdate) onConfigUpdate([{ action: 'add_obstacle', coords: { x: 1100, y: 700 } }])}} className="text-[10px] w-full px-3 py-1.5 bg-zinc-600 text-white hover:bg-zinc-500 rounded font-bold transition-colors shadow-[0_0_10px_rgba(82,82,91,0.3)]">+ ADD OBSTACLE</button>
             
             <div className="w-full h-px bg-zinc-700 my-1"></div>

             <div className="flex gap-1">
               <button onClick={() => { if(onConfigUpdate) selectedNodes.forEach(n => onConfigUpdate([{ action: 'duplicate', id: n.id }]))}} disabled={selectedNodes.length === 0} className="flex-1 text-[9px] py-1.5 bg-zinc-800 text-zinc-300 hover:bg-zinc-700 disabled:opacity-50 disabled:cursor-not-allowed rounded font-bold transition-colors">COPY</button>
               <button onClick={() => { if(onConfigUpdate) selectedNodes.forEach(n => onConfigUpdate([{ action: 'delete', id: n.id }]))}} disabled={selectedNodes.length === 0} className="flex-1 text-[9px] py-1.5 bg-red-950/40 border border-red-900/50 text-red-500 hover:bg-red-900 hover:text-white disabled:opacity-50 disabled:cursor-not-allowed rounded font-bold transition-colors">DEL</button>
             </div>
           </fieldset>
        </Panel>

        {/* SAĞ ÖZELLİKLER PANELİ */}
        {selectedNodes.length === 1 && (
          <Panel position="top-right" className="mt-12 mr-2 w-72 bg-zinc-950/95 border border-zinc-800 rounded-xl shadow-2xl backdrop-blur-md animate-in slide-in-from-right-5 overflow-hidden">
            <div className="bg-zinc-900 px-4 py-3 border-b border-zinc-800 flex justify-between items-center">
              <span className="text-xs text-teal-400 font-bold uppercase tracking-widest">Properties</span>
              {/* Kilitleme İkonu */}
              {isSimulationActive && <span className="text-[10px] text-red-400 font-bold animate-pulse">LOCKED</span>}
            </div>
            
            {/* ÇÖZÜM: BÜTÜN İÇERİK FIELDSET İÇİNE ALINDI */}
            <fieldset disabled={isSimulationActive} className="p-4 flex flex-col gap-4 disabled:opacity-50 transition-opacity">
              
              {/* EĞER SEÇİLEN OBJE BİR KULE İSE: */}
              {selectedNodes[0].type === 'cellNode' && (
                <>
                  <div className="flex flex-col gap-1.5 mb-2">
                    <label className="text-[10px] font-bold text-zinc-500 uppercase">Site Name</label>
                    <input type="text" value={localProps.name} onChange={(e) => { setLocalProps(prev => ({ ...prev, name: e.target.value })); handlePropertyChange(selectedNodes[0].id, 'name', e.target.value); }} className="bg-zinc-900 border border-zinc-700 text-sm text-white px-2 py-1.5 rounded focus:border-teal-500 outline-none" />
                  </div>
                  
                  {/* FREKANS BANDI (DROPDOWN) */}
                  <div className="flex flex-col gap-1.5 mb-4">
                    <label className="text-[10px] font-bold text-zinc-500 uppercase">Frequency Band</label>
                    <select value={localProps.band} onChange={(e) => { setLocalProps(prev => ({ ...prev, band: e.target.value })); handlePropertyChange(selectedNodes[0].id, 'band', e.target.value); }} className="bg-zinc-900 border border-zinc-700 text-sm text-white px-2 py-1.5 rounded focus:border-teal-500 outline-none">
                      <option value="NR-3600">NR-3600 (C-Band / 3.6 GHz)</option>
                      <option value="NR-2100">NR-2100 (Mid-Band / 2.1 GHz)</option>
                      <option value="NR-700">NR-700 (Low-Band / 700 MHz)</option>
                      <option value="LTE-1800">LTE-1800 (Legacy 4G)</option>
                    </select>
                  </div>

                  {/* ANTEN MODELİ — senaryo geneli (settings.antenna_element),
                      ama yeri burası: yanındaki azimutun bir şey yapıp
                      yapmadığına karar veren ayar bu. */}
                  <div className="flex flex-col gap-1.5 mb-4">
                    <label className="text-[10px] font-bold text-zinc-500 uppercase">
                      Antenna Model <span className="text-zinc-600 normal-case font-normal">· whole scenario</span>
                    </label>
                    <select
                      value={selectedNodes[0].data?.antennaElement ?? 'isotropic'}
                      onChange={(e) => onConfigUpdate && onConfigUpdate([
                        { action: 'set_setting', props: { key: 'antenna_element', value: e.target.value } },
                      ])}
                      className="bg-zinc-900 border border-zinc-700 text-sm text-white px-2 py-1.5 rounded focus:border-teal-500 outline-none"
                    >
                      <option value="isotropic">Isotropic — no direction (azimuth inert)</option>
                      <option value="3gpp">3GPP TR 38.901 — 65° sector, fixed pattern</option>
                      <option value="cosine">Cosine — beamwidth per cell</option>
                      <option value="parabolic">Parabolic — dish / point-to-point</option>
                      <option value="circular">Circular aperture — beamwidth from radius</option>
                    </select>
                    {(selectedNodes[0].data?.antennaElement ?? 'isotropic') === 'isotropic' && (
                      <span className="text-[10px] text-amber-400/80">
                        Isotropic radiates equally in every direction, so the azimuth below
                        changes nothing. Pick 3GPP or Cosine to make orientation real.
                      </span>
                    )}
                  </div>

                  <div className="flex justify-between items-center mb-2 border-b border-zinc-800 pb-2">
                    <label className="text-[10px] font-bold text-teal-400 uppercase">Antenna Sectors</label>
                    <span
                      className="ml-1 text-[8px] font-bold px-1 py-0.5 rounded bg-violet-500/15 text-violet-300 border border-violet-500/25 align-middle"
                      title="Azimut/tilt kapsama haritasını ve analitik katmanı her zaman şekillendirir. GERÇEK ns-O-RAN motorunda ise yalnızca anten modeli 'isotropic' DIŞINDA bir şeyse etkilidir: izotropik elemanda dizinin dönüşü tam olarak sadeleşir. Bu rozet eskiden 'gerçek motor sektörleri yok sayar' diyordu; ölçüldü, yanlış."
                    >
                      needs a directional antenna model
                    </span>
                    <button 
                      onClick={() => onConfigUpdate && onConfigUpdate([{ action: 'add_sector', id: selectedNodes[0].id }])}
                      className="text-xs bg-zinc-800 hover:bg-zinc-700 text-white px-2 py-1 rounded"
                    >
                      + Add
                    </button>
                  </div>

                  <div className="flex flex-col gap-3 max-h-[300px] overflow-y-auto custom-scrollbar pr-1">
                    {localProps.sectors && localProps.sectors.map((sector, idx) => {
                      // Each sector is its own BS in the config — use sector.id to target it directly
                      const sectorId = sector.id || selectedNodes[0].id;
                      return (
                        <div key={sectorId} className="bg-zinc-950/50 border border-zinc-800 p-2 rounded flex flex-col gap-2">
                          <div className="flex justify-between items-center mb-1">
                            <span className="text-xs font-bold text-zinc-300">Sector {idx + 1}</span>
                            {localProps.sectors.length > 1 && (
                              <button
                                onClick={() => onConfigUpdate && onConfigUpdate([{ action: 'delete', id: sectorId }])}
                                className="text-[10px] text-red-400 hover:text-red-300"
                              >
                                Remove
                              </button>
                            )}
                          </div>

                          <div className="flex gap-2">
                            <div className="flex flex-col gap-1 flex-1">
                              <label className="text-[9px] text-zinc-500">Power (dBm)</label>
                              <input type="number" min="1" max="70" value={sector.power} onChange={(e) => {
                                const newSectors = [...localProps.sectors];
                                newSectors[idx] = { ...newSectors[idx], power: e.target.value };
                                setLocalProps(prev => ({ ...prev, sectors: newSectors }));
                                handlePropertyChange(sectorId, 'power', e.target.value);
                              }} className="bg-zinc-900 border border-zinc-700 text-xs text-white px-1.5 py-1 rounded focus:border-teal-500 outline-none" />
                            </div>

                            <div className="flex flex-col gap-1 flex-1">
                              <label className="text-[9px] text-zinc-500">Azimuth (°)</label>
                              <input type="number" min="0" max="359" value={sector.azimuth} onChange={(e) => {
                                const newSectors = [...localProps.sectors];
                                newSectors[idx] = { ...newSectors[idx], azimuth: e.target.value };
                                setLocalProps(prev => ({ ...prev, sectors: newSectors }));
                                handlePropertyChange(sectorId, 'azimuth', e.target.value);
                              }} className="bg-zinc-900 border border-zinc-700 text-xs text-white px-1.5 py-1 rounded focus:border-teal-500 outline-none" />
                            </div>
                          </div>

                          <div className="flex gap-2">
                            <div className="flex flex-col gap-1 flex-1">
                              <label className="text-[9px] text-zinc-500">Tilt (°)</label>
                              <input type="number" min="0" max="15" value={sector.tilt} onChange={(e) => {
                                const newSectors = [...localProps.sectors];
                                newSectors[idx] = { ...newSectors[idx], tilt: e.target.value };
                                setLocalProps(prev => ({ ...prev, sectors: newSectors }));
                                handlePropertyChange(sectorId, 'tilt', e.target.value);
                              }} className="bg-zinc-900 border border-zinc-700 text-xs text-white px-1.5 py-1 rounded focus:border-teal-500 outline-none" />
                            </div>

                            <div className="flex flex-col gap-1 flex-1">
                              <label className="text-[9px] text-zinc-500">Height (m)</label>
                              <input type="number" min="1" max="150" value={sector.height || localProps.height} onChange={(e) => {
                                const newSectors = [...localProps.sectors];
                                newSectors[idx] = { ...newSectors[idx], height: e.target.value };
                                setLocalProps(prev => ({ ...prev, sectors: newSectors }));
                                handlePropertyChange(sectorId, 'height', e.target.value);
                              }} className="bg-zinc-900 border border-zinc-700 text-xs text-white px-1.5 py-1 rounded focus:border-teal-500 outline-none" />
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </>
              )}

              {/* EĞER SEÇİLEN OBJE BİR KULLANICI İSE: */}
              {selectedNodes[0].type === 'ueNode' && (
                <>
                  <div className="flex flex-col gap-1.5">
                    <label className="text-[10px] font-bold text-zinc-500 uppercase">Area Description</label>
                    <input type="text" value={localProps.description} onChange={(e) => { setLocalProps(prev => ({ ...prev, description: e.target.value })); handlePropertyChange(selectedNodes[0].id, 'description', e.target.value); }} className="bg-zinc-900 border border-zinc-700 text-sm text-white px-2 py-1.5 rounded focus:border-teal-500 outline-none" />
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <label className="text-[10px] font-bold text-zinc-500 uppercase">Active UEs</label>
                    <input type="number" min="1" max="1000" value={localProps.ues} onChange={(e) => { setLocalProps(prev => ({ ...prev, ues: e.target.value })); handlePropertyChange(selectedNodes[0].id, 'ues', e.target.value); }} className="bg-zinc-900 border border-zinc-700 text-sm text-white px-2 py-1.5 rounded focus:border-teal-500 outline-none" />
                  </div>
                  <div className="flex flex-col gap-1.5 mt-1">
                    <label className="text-[10px] font-bold text-zinc-500 uppercase">Mobility Pattern</label>
                    {/* Values ARE the schema enum — display names used to be
                        written verbatim into mobility.pattern and 422'd both engines. */}
                    <select value={localProps.mobilityType} onChange={(e) => { setLocalProps(prev => ({ ...prev, mobilityType: e.target.value })); handlePropertyChange(selectedNodes[0].id, 'mobilityType', e.target.value); }} className="bg-zinc-900 border border-zinc-700 text-sm text-white px-2 py-1.5 rounded focus:border-teal-500 outline-none">
                      <option value="static">Static</option>
                      <option value="pedestrian">Pedestrian (~1.4 m/s)</option>
                      <option value="vehicle">Vehicle</option>
                      <option value="drone">Drone</option>
                      <option value="random_walk">Random Walk</option>
                    </select>
                  </div>
                  <div className="flex flex-col gap-1.5">
                    {/* METRE/SANİYE — kilometre/saat DEĞİL. Bu alan doğrudan
                        mobility.speed_mps'e yazıyor (model-preview.tsx), yani
                        km/h etiketi hem 1.389 m/s'yi "1,389" diye absürt
                        gösteriyordu hem de 5 km/h sanıp "5" yazan birinin
                        hızını 5 m/s = 18 km/h yapıyordu. Şema baştan sona m/s;
                        doğrusu dönüştürmek değil, birimi doğru yazmak.
                        Sınırlar motorun kırpma aralığı (main.py:596). */}
                    <label className="text-[10px] font-bold text-zinc-500 uppercase">Movement Speed (m/s)</label>
                    <input type="number" min="0.5" max="40" step="0.1" value={localProps.speed} onChange={(e) => { setLocalProps(prev => ({ ...prev, speed: e.target.value })); handlePropertyChange(selectedNodes[0].id, 'speed', e.target.value); }} className="bg-zinc-900 border border-zinc-700 text-sm text-white px-2 py-1.5 rounded focus:border-teal-500 outline-none" />
                    <span className="text-[10px] text-zinc-500">
                      {Number.isFinite(Number(localProps.speed)) && Number(localProps.speed) > 0
                        ? `= ${(Number(localProps.speed) * 3.6).toFixed(1)} km/h`
                        : 'engine range 0.5 – 40 m/s'}
                    </span>
                  </div>
                </>
              )}

              {/* BUILDING / OBSTACLE PROPERTIES */}
              {selectedNodes[0].type === 'obstacleNode' && (
                <>
                  <div className="flex flex-col gap-1.5">
                    <label className="text-[10px] font-bold text-zinc-500 uppercase">Building ID</label>
                    <input type="text" value={localProps.name} onChange={(e) => { setLocalProps(prev => ({ ...prev, name: e.target.value })); handlePropertyChange(selectedNodes[0].id, 'name', e.target.value); }} className="bg-zinc-900 border border-zinc-700 text-sm text-white px-2 py-1.5 rounded focus:border-teal-500 outline-none" />
                  </div>

                  <div className="flex gap-2">
                    <div className="flex flex-col gap-1.5 flex-1">
                      <label className="text-[10px] font-bold text-zinc-500 uppercase">Type</label>
                      <select value={localProps.buildingType} onChange={(e) => { setLocalProps(prev => ({ ...prev, buildingType: e.target.value })); handlePropertyChange(selectedNodes[0].id, 'buildingType', e.target.value); }} className="bg-zinc-900 border border-zinc-700 text-sm text-white px-2 py-1.5 rounded focus:border-teal-500 outline-none">
                        <option value="office">Office</option>
                        <option value="residential">Residential</option>
                        <option value="industrial">Industrial</option>
                      </select>
                    </div>
                    <div className="flex flex-col gap-1.5 flex-1">
                      <label className="text-[10px] font-bold text-zinc-500 uppercase">Floors</label>
                      <input type="number" min="1" max="100" value={localProps.floors} onChange={(e) => { setLocalProps(prev => ({ ...prev, floors: e.target.value })); handlePropertyChange(selectedNodes[0].id, 'floors', e.target.value); }} className="bg-zinc-900 border border-zinc-700 text-sm text-white px-2 py-1.5 rounded focus:border-teal-500 outline-none" />
                    </div>
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <label className="text-[10px] font-bold text-zinc-500 uppercase">Wall Material (RF Loss)</label>
                    <select value={localProps.material} onChange={(e) => {
                      const v = e.target.value;
                      const loss = v === 'Glass' ? 3 : v === 'Wood' ? 5 : v === 'Foliage' ? 10 : 20;
                      setLocalProps(prev => ({ ...prev, material: v, loss }));
                      setNodes(nds => nds.map(n => n.id === selectedNodes[0].id ? { ...n, data: { ...n.data, material: v, loss } } : n));
                      if (onConfigUpdate) onConfigUpdate([{ id: selectedNodes[0].id, props: { material: v, loss } }]);
                    }} className="bg-zinc-900 border border-zinc-700 text-sm text-white px-2 py-1.5 rounded focus:border-teal-500 outline-none">
                      <option value="Concrete">Concrete Block (-20 dB)</option>
                      <option value="Wood">Wood / Drywall (-5 dB)</option>
                      <option value="Glass">Glass Window (-3 dB)</option>
                      <option value="Foliage">Foliage / Tree (-10 dB)</option>
                    </select>
                  </div>

                  <div className="flex gap-2">
                    <div className="flex flex-col gap-1.5 flex-1">
                      <label className="text-[10px] font-bold text-zinc-500 uppercase">Z Height (m)</label>
                      <input type="number" min="1" max="500" value={localProps.height} onChange={(e) => { setLocalProps(prev => ({ ...prev, height: e.target.value })); handlePropertyChange(selectedNodes[0].id, 'height', e.target.value); }} className="bg-zinc-900 border border-zinc-700 text-sm text-white px-2 py-1.5 rounded focus:border-teal-500 outline-none" />
                    </div>
                    <div className="flex flex-col gap-1.5 flex-1">
                      <label className="text-[10px] font-bold text-zinc-500 uppercase">Width X (m)</label>
                      <input type="number" min="5" max="2000" value={localProps.obsWidth} onChange={(e) => { setLocalProps(prev => ({ ...prev, obsWidth: e.target.value })); handlePropertyChange(selectedNodes[0].id, 'width', e.target.value); }} className="bg-zinc-900 border border-zinc-700 text-sm text-white px-2 py-1.5 rounded focus:border-teal-500 outline-none" />
                    </div>
                  </div>

                  <div className="flex gap-2">
                    <div className="flex flex-col gap-1.5 flex-1">
                      <label className="text-[10px] font-bold text-zinc-500 uppercase">Depth Y (m)</label>
                      <input type="number" min="5" max="2000" value={localProps.obsDepth} onChange={(e) => { setLocalProps(prev => ({ ...prev, obsDepth: e.target.value })); handlePropertyChange(selectedNodes[0].id, 'depth', e.target.value); }} className="bg-zinc-900 border border-zinc-700 text-sm text-white px-2 py-1.5 rounded focus:border-teal-500 outline-none" />
                    </div>
                    <div className="flex flex-col gap-1.5 flex-1">
                      <label className="text-[10px] font-bold text-zinc-500 uppercase">Rotation (°)</label>
                      <input type="number" min="0" max="359" value={localProps.angle} onChange={(e) => {
                        const v = parseInt(e.target.value) || 0;
                        setLocalProps(prev => ({ ...prev, angle: v }));
                        // Rotation is visual-only — not in schema. Update node data directly without going through config.
                        setNodes(nds => nds.map(n => n.id === selectedNodes[0].id ? { ...n, data: { ...n.data, angle: v } } : n));
                      }} className="bg-zinc-900 border border-zinc-700 text-sm text-white px-2 py-1.5 rounded focus:border-teal-500 outline-none" />
                    </div>
                  </div>
                </>
              )}

            </fieldset>
          </Panel>
        )}

        {/* ALT MESAFE ÖLÇÜM PANELİ */}
        {selectedNodes.length === 2 && (
          <Panel position="bottom-center" className="mb-4 p-4 bg-zinc-900/95 border border-teal-500/50 rounded-xl shadow-[0_0_20px_rgba(20,184,166,0.2)] backdrop-blur-md animate-in slide-in-from-bottom-5 w-[350px]">
             <div className="flex flex-col items-center gap-3">
              <span className="text-xs text-zinc-400 font-bold uppercase tracking-widest">Link Editor</span>
              <div className="flex items-center gap-2">
                <input type="number" value={editDistance} onChange={(e) => setEditDistance(e.target.value)} className="w-24 bg-zinc-950 border border-teal-500/50 text-teal-400 text-2xl font-light text-center rounded outline-none focus:border-teal-400 transition-colors" />
                <span className="text-sm text-zinc-500 font-bold uppercase mt-1">Meters</span>
              </div>
              <div className="flex items-center gap-1 mt-2 p-1 bg-zinc-950 rounded-lg border border-zinc-800 w-full">
                <button onClick={() => setAnchorMode('node2')} className={`flex-1 px-2 py-1.5 text-[9px] font-bold uppercase rounded transition-colors ${anchorMode === 'node2' ? 'bg-teal-500/20 text-teal-400' : 'text-zinc-500 hover:text-zinc-300'}`}> Move {selectedNodes[0].data?.label}</button>
                <button onClick={() => setAnchorMode('center')} className={`flex-1 px-2 py-1.5 text-[9px] font-bold uppercase rounded transition-colors ${anchorMode === 'center' ? 'bg-teal-500/20 text-teal-400' : 'text-zinc-500 hover:text-zinc-300'}`}> Move Both</button>
                <button onClick={() => setAnchorMode('node1')} className={`flex-1 px-2 py-1.5 text-[9px] font-bold uppercase rounded transition-colors ${anchorMode === 'node1' ? 'bg-teal-500/20 text-teal-400' : 'text-zinc-500 hover:text-zinc-300'}`}> Move {selectedNodes[1].data?.label}</button>
              </div>
              <button onClick={applyNewDistance} className="w-full mt-1 bg-teal-600 hover:bg-teal-500 text-black font-bold py-2 rounded text-xs transition-colors">APPLY DISTANCE</button>
            </div>
          </Panel>
        )}
      </ReactFlow>
    </>
  );
}

export default function NetworkCanvas(props) {
  return <ReactFlowProvider><NetworkCanvasContent {...props} /></ReactFlowProvider>;
}