import React, { memo } from 'react';
import { Handle, Position } from 'reactflow';

const UENode = ({ data }) => {
  // 'count' yerine 'ues' kullanıyoruz (Backend ile tam uyum)
  const uesCount = data.ues !== undefined ? Number(data.ues) : 1;
  const dotCount = Math.min(uesCount, 24);

  return (
    <div 
      className="relative flex items-center justify-center border-2 border-dashed border-blue-500/40 bg-blue-500/5 rounded-md hover:border-blue-400 hover:bg-blue-500/10 transition-colors cursor-move" 
      style={{ width: '160px', height: '120px' }}
    >
      <div className="absolute -top-3 -left-2 bg-zinc-950 text-blue-400 text-[10px] px-2 py-0.5 rounded font-mono border border-blue-500/50 shadow-md flex flex-col whitespace-nowrap z-10">
        <span className="font-bold text-zinc-200">{data.label}</span>
        <span className="text-[9px] opacity-80">{uesCount} Active UEs</span>
      </div>

      <div className="flex flex-wrap gap-2 p-3 w-full h-full justify-center items-center opacity-60 pointer-events-none">
        {Array.from({ length: dotCount }).map((_, i) => (
          <div key={i} className="text-[10px] font-mono text-zinc-500 font-bold">b</div>
        ))}
      </div>

      <Handle type="target" position={Position.Top} className="opacity-0" />
      <Handle type="source" position={Position.Bottom} className="opacity-0" />
    </div>
  );
};

export default memo(UENode);