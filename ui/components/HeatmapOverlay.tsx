'use client';

/**
 * HeatmapOverlay
 *
 * Renders NS-3 REM coverage points (x, y, rsrp) as a coverage map on a
 * <canvas> layered inside the React Flow renderer.
 *
 * Must be rendered as a direct child of <ReactFlow>, alongside <Background>
 * and <Controls> — it reads the live pan/zoom transform via React Flow's
 * internal store (the same mechanism <Background> uses) so the heatmap
 * tracks the canvas exactly, with no separate scroll/zoom logic of its own.
 *
 * Rendering: buildHeatmapBitmap() rasterizes the REM grid into a small
 * off-screen bitmap (one pixel per grid cell), which is drawn with a single
 * drawImage() call scaled to the world-space area — O(1) per frame
 * regardless of point count, and no color blending artifacts from
 * overlapping point draws.
 */

import { useEffect, useRef } from 'react';
import { useStore, Panel } from 'reactflow';
import { shallow } from 'zustand/shallow';
import { simToCanvas } from '@/utils/flowMapper';
import { buildHeatmapBitmap } from '@/utils/heatmapRender';
import type { RemPoint } from '@/types/nsg';

interface HeatmapOverlayProps {
  data: RemPoint[];
  visible?: boolean;
}

// Single combined selector (with shallow equality) instead of three separate
// useStore calls with inline selectors — avoids resubscribing on every
// render and keeps this component to one Zustand subscription.
const viewportSelector = (s: { transform: [number, number, number]; width: number; height: number }) => ({
  tx: s.transform[0], ty: s.transform[1], zoom: s.transform[2],
  width: s.width, height: s.height,
});

// Kept modest so buildings, towers, and labels stay legible underneath.
const HEATMAP_OPACITY = 0.45;

export default function HeatmapOverlay({ data, visible = true }: HeatmapOverlayProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const { tx, ty, zoom, width, height } = useStore(viewportSelector, shallow);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !width || !height) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width  = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width  = `${width}px`;
    canvas.style.height = `${height}px`;

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    if (!visible || !data || data.length === 0) return;

    const bitmap = buildHeatmapBitmap(data);
    if (!bitmap) return;

    const { canvas: bmp, bounds } = bitmap;

    ctx.translate(tx, ty);
    ctx.scale(zoom, zoom);
    ctx.imageSmoothingEnabled = true;
    ctx.globalAlpha = HEATMAP_OPACITY;

    // Bounding corners in world (pre-pan/zoom) canvas px — simToCanvas flips Y,
    // so the sim-space top-left (xMin, yMax) becomes the canvas-space top-left.
    const topLeft     = simToCanvas(bounds.xMin, bounds.yMax);
    const bottomRight = simToCanvas(bounds.xMax, bounds.yMin);

    ctx.drawImage(
      bmp,
      topLeft.x, topLeft.y,
      bottomRight.x - topLeft.x,
      bottomRight.y - topLeft.y,
    );
    ctx.globalAlpha = 1;
  }, [data, visible, tx, ty, zoom, width, height]);

  if (!data || data.length === 0) return null;

  return (
    <>
      <canvas
        ref={canvasRef}
        style={{
          position: 'absolute',
          top: 0,
          left: 0,
          zIndex: 0,
          pointerEvents: 'none',
        }}
      />
      {visible && (
        <Panel position="bottom-left" className="mb-4 ml-2 pointer-events-none">
          <div className="flex flex-col gap-1 bg-zinc-950/90 border border-zinc-800 rounded-lg px-3 py-2 backdrop-blur-sm">
            <span className="text-[9px] font-bold text-zinc-500 uppercase tracking-wider">RSRP Coverage</span>
            <div
              className="w-28 h-2 rounded-full"
              style={{ background: 'linear-gradient(90deg, #ef4444, #eab308, #22c55e)' }}
            />
            <div className="flex justify-between w-28">
              <span className="text-[8px] text-zinc-600 font-mono">-120 dBm</span>
              <span className="text-[8px] text-zinc-600 font-mono">-60 dBm</span>
            </div>
          </div>
        </Panel>
      )}
    </>
  );
}
