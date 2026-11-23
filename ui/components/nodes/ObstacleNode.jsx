import React, { memo } from 'react';
import { Handle, Position } from 'reactflow';

const MATERIAL = {
  'Glass':    { bg: 'rgba(14,165,233,0.12)', border: '#0ea5e9' },
  'Wood':     { bg: 'rgba(180,83,9,0.22)',   border: '#b45309' },
  'Foliage':  { bg: 'rgba(22,163,74,0.22)',  border: '#16a34a' },
  'Concrete': { bg: 'rgba(82,82,91,0.30)',   border: '#52525b' },
};

const ObstacleNode = ({ data, selected }) => {
  const m     = MATERIAL[data.material] ?? MATERIAL['Concrete'];
  const angle = data.angle || 0;

  return (
    <div
      style={{
        width: '100%', height: '100%',
        transform: `rotate(${angle}deg)`,
        background: m.bg,
        border: `2px ${selected ? 'solid' : 'dashed'} ${selected ? '#fff' : m.border}`,
        borderRadius: 2,
        boxShadow: selected ? `0 0 0 1px rgba(255,255,255,0.3), 0 0 12px ${m.border}55` : 'none',
        position: 'relative',
        overflow: 'hidden',
        cursor: 'move',
        boxSizing: 'border-box',
      }}
    >
      {/* Subtle grid pattern to suggest floor grid */}
      <div style={{
        position: 'absolute', inset: 0,
        backgroundImage: [
          `repeating-linear-gradient(0deg,   ${m.border}18 0, ${m.border}18 1px, transparent 1px, transparent 20px)`,
          `repeating-linear-gradient(90deg,  ${m.border}18 0, ${m.border}18 1px, transparent 1px, transparent 20px)`,
        ].join(', '),
        pointerEvents: 'none',
      }} />

      {/* Label bar pinned to top */}
      <div style={{
        position: 'absolute', top: 0, left: 0, right: 0,
        background: `${m.border}cc`,
        padding: '2px 5px',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 4,
      }}>
        <span style={{ fontSize: 9, fontWeight: 700, color: '#fff', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {data.label}
        </span>
        <span style={{ fontSize: 9, color: '#ffffffbb', whiteSpace: 'nowrap', flexShrink: 0 }}>
          {data.height}m
        </span>
      </div>

      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
      <Handle type="target" position={Position.Top}    style={{ opacity: 0 }} />
    </div>
  );
};

export default memo(ObstacleNode);
