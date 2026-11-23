import React, { memo } from 'react';

// A REAL UE from the running ns-O-RAN sim: position streams live over
// /ws/e2-real ([NSG-POS] bridge tail), so these dots MOVE with the actual
// random-walk motion. Read-only overlay — not part of the editable design.
const CELL_COLORS = ['#14b8a6', '#38bdf8', '#a78bfa', '#fbbf24', '#f43f5e', '#34d399', '#f97316'];

function LiveUeNode({ data }) {
  const color = CELL_COLORS[(data.cell ?? 0) % CELL_COLORS.length];
  return (
    // Zero-size anchor: React Flow positions a node by its top-left corner, so a
    // normal box would put the dot half a box south-east of the UE's real place —
    // and the label makes that box a text-dependent WIDTH, so the error changed
    // with the IMSI. Both children are absolutely positioned off a 0x0 origin, so
    // the dot's centre sits exactly on the streamed coordinate no matter what the
    // label says. That matters here more than on the design nodes: this overlay is
    // what you read to judge which cell a UE is actually near.
    <div className="pointer-events-none relative w-0 h-0" title={`UE ${data.imsi} — cell ${data.cell ?? '?'}`}>
      <div
        className="absolute w-3 h-3 rounded-full border-2 border-zinc-950 animate-pulse"
        style={{ background: color, boxShadow: `0 0 8px ${color}`,
                 left: '-6px', top: '-6px' }}
      />
      <div className="absolute text-[8px] font-mono leading-tight px-1 rounded bg-zinc-950/80 whitespace-nowrap"
           style={{ color, top: '8px', left: '50%', transform: 'translateX(-50%)' }}>
        {data.imsi}{data.slice ? `·${String(data.slice).slice(0, 4)}` : ''}
      </div>
    </div>
  );
}

export default memo(LiveUeNode);
