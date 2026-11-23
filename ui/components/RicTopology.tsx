'use client';

/**
 * RicTopology — read-only live network map for the RIC data view.
 *
 * Reuses the scenario editor's positioning (flowMapper.configToFlowNodes places
 * gNBs / UE groups at their real sim coords) but swaps in live-data node types:
 * cells are tinted by load/status and show throughput + PRB, UE groups by SINR,
 * and edges are the live UE→serving-cell attachments from the KPM report. Read
 * only (no drag/edit) — this is a dashboard, not the editor.
 */
import { useMemo } from 'react';
import ReactFlow, { Background, Controls, Handle, Position, type Node, type Edge, type NodeProps } from 'reactflow';
import 'reactflow/dist/style.css';
import { RadioTower, Users } from 'lucide-react';
import { configToFlowNodes } from '@/utils/flowMapper';
import { useNSGStore, selectConfig, selectRicKpm } from '@/store/useNSGStore';

function cellColor(prb: number | undefined, active: boolean | undefined, faulted: boolean | undefined): string {
  if (faulted) return '#ef4444';
  if (active === false) return '#3f3f46';
  return (prb ?? 0) >= 75 ? '#f59e0b' : '#14b8a6';
}

function ueColor(sinr: number | null | undefined): string {
  if (sinr == null) return '#ef4444';
  if (sinr >= 15) return '#22c55e';
  if (sinr >= 3) return '#eab308';
  return '#f97316';
}

function RicCellNode({ data }: NodeProps) {
  const color = cellColor(data.prb, data.active, data.faulted);
  const sectors: { power?: number; azimuth?: number }[] = data.sectors?.length ? data.sectors : [{}];
  return (
    <div className="relative flex items-center justify-center w-10 h-10">
      {sectors.map((s, i) => {
        const scale = Math.max(0.3, Math.pow((s.power ?? 35) / 30, 1.5));
        return (
          <div key={i} className="absolute origin-bottom pointer-events-none z-0"
            style={{
              height: `${70 * scale}px`, width: `${50 * scale}px`, bottom: '50%',
              background: `linear-gradient(to top, ${color}55, ${color}00)`,
              clipPath: 'polygon(50% 100%, 0 0, 100% 0)',
              transform: `rotate(${s.azimuth ?? 0}deg)`, transformOrigin: 'bottom center',
            }} />
        );
      })}
      <div className="relative z-10 p-2 rounded-full border-2 bg-zinc-950" style={{ borderColor: color }}>
        <RadioTower className="w-5 h-5" style={{ color }} />
      </div>
      <div className="absolute top-full mt-1.5 px-2 py-0.5 bg-zinc-950 border border-zinc-800 rounded text-[9px] font-mono text-center whitespace-nowrap z-20">
        <div className="text-zinc-300">{data.label}</div>
        {data.thp != null && <div style={{ color }}>{data.thp.toFixed(1)} Mb · {(data.prb ?? 0).toFixed(0)}%</div>}
      </div>
      <Handle type="source" position={Position.Bottom} className="opacity-0" />
      <Handle type="target" position={Position.Top} className="opacity-0" />
    </div>
  );
}

function RicUeNode({ data }: NodeProps) {
  const color = ueColor(data.sinr);
  return (
    <div className="relative flex items-center justify-center">
      <div className="relative z-10 p-1.5 rounded-full border-2 bg-zinc-950" style={{ borderColor: color }}>
        <Users className="w-3.5 h-3.5" style={{ color }} />
      </div>
      <div className="absolute top-full mt-1 px-1.5 py-0.5 bg-zinc-950 border border-zinc-800 rounded text-[8px] font-mono text-center whitespace-nowrap z-20">
        <div className="text-zinc-400">{data.label}{data.count ? ` ×${data.count}` : ''}</div>
        <div style={{ color }}>{data.sinr != null ? `${data.sinr.toFixed(0)} dB` : 'outage'}</div>
      </div>
      <Handle type="target" position={Position.Top} className="opacity-0" />
    </div>
  );
}

const nodeTypes = { ricCell: RicCellNode, ricUe: RicUeNode };

export default function RicTopology() {
  const config = useNSGStore(selectConfig);
  const kpm = useNSGStore(selectRicKpm);

  const { nodes, edges } = useMemo<{ nodes: Node[]; edges: Edge[] }>(() => {
    if (!config) return { nodes: [], edges: [] };
    const base = configToFlowNodes(config).nodes;
    const cellById = new Map((kpm?.cells ?? []).map(c => [c.id, c]));
    const ueById = new Map((kpm?.ue_groups ?? []).map(u => [u.id, u]));

    const nodes: Node[] = base
      .filter(n => n.type === 'cellNode' || n.type === 'ueNode')
      .map(n => {
        if (n.type === 'cellNode') {
          const c = cellById.get(n.id);
          return { ...n, type: 'ricCell', draggable: false, data: {
            label: n.data.label, sectors: n.data.sectors,
            thp: c?.throughput_mbps, prb: c?.prb_used_pct, active: c?.active, faulted: c?.faulted,
          } };
        }
        const u = ueById.get(n.id);
        return { ...n, type: 'ricUe', draggable: false, data: {
          label: n.data.label, count: n.data.count, sinr: u?.sinr_db ?? null,
        } };
      });

    const edges: Edge[] = (kpm?.ue_groups ?? [])
      .filter(u => u.serving_cell != null)
      .map(u => ({
        id: `e-${u.id}`, source: u.serving_cell as string, target: u.id,
        animated: true, style: { stroke: '#3f6b8b', strokeWidth: 1, strokeDasharray: '4 4' },
      }));

    return { nodes, edges };
  }, [config, kpm]);

  if (!config) {
    return (
      <div className="h-64 flex items-center justify-center text-sm text-zinc-500 bg-zinc-950 border border-zinc-800 rounded-xl">
        No scenario to map.
      </div>
    );
  }

  return (
    <div className="h-96 bg-zinc-950 border border-zinc-800 rounded-xl overflow-hidden">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.3 }}
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnDrag
        zoomOnDoubleClick
        zoomOnScroll={false}
        minZoom={0.2}
        maxZoom={4}
      >
        <Background color="#27272a" gap={22} size={1} />
        <Controls showInteractive={false} position="bottom-right" />
      </ReactFlow>
    </div>
  );
}
