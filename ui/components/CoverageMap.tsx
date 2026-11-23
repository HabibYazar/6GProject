'use client';

/**
 * CoverageMap
 *
 * Dedicated, standalone RSRP heatmap view for the Analytics tab — separate
 * from the interactive topology canvas (HeatmapOverlay), so reading the
 * coverage map doesn't compete with editing the network for screen space
 * or render budget. No React Flow pan/zoom coupling; just a flat canvas
 * sized to its panel, with cell-site markers for spatial orientation,
 * axis ticks in meters, and a cursor tooltip showing exact RSRP.
 */

import { useEffect, useRef, useState } from 'react';
import { buildHeatmapBitmap, rsrpToRgb } from '@/utils/heatmapRender';
import type { RemPoint, NSGSimulationConfig } from '@/types/nsg';

interface CoverageMapProps {
  heatmapData: RemPoint[];
  config: NSGSimulationConfig | null;
}

const PADDING = 36; // px reserved for axis ticks

export default function CoverageMap({ heatmapData, config }: CoverageMapProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef     = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [hover, setHover] = useState<{ x: number; y: number; rsrp: number; px: number; py: number } | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const { width, height } = entries[0].contentRect;
      setSize({ width, height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const bitmap = heatmapData.length > 0 ? buildHeatmapBitmap(heatmapData) : null;

  // Sim-space bounds: prefer the configured area (consistent framing even if
  // the REM happens to cover a subset), fall back to the data's own bbox.
  const bounds = config?.area
    ? { xMin: config.area.x_min, xMax: config.area.x_max, yMin: config.area.y_min, yMax: config.area.y_max }
    : bitmap?.bounds ?? null;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || size.width === 0 || size.height === 0) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width  = size.width * dpr;
    canvas.height = size.height * dpr;
    canvas.style.width  = `${size.width}px`;
    canvas.style.height = `${size.height}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size.width, size.height);

    if (!bounds) {
      ctx.fillStyle = '#52525b';
      ctx.font = '13px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('Run a scenario to generate a coverage map', size.width / 2, size.height / 2);
      return;
    }

    const plotW = size.width - PADDING * 1.5;
    const plotH = size.height - PADDING * 1.5;
    const xSpan = bounds.xMax - bounds.xMin || 1;
    const ySpan = bounds.yMax - bounds.yMin || 1;

    const toPx = (x: number, y: number) => ({
      px: PADDING + ((x - bounds.xMin) / xSpan) * plotW,
      // Y flip: sim North (larger y) renders toward the top of the panel.
      py: PADDING + (1 - (y - bounds.yMin) / ySpan) * plotH,
    });

    // Background plot area
    ctx.fillStyle = '#09090b';
    ctx.fillRect(PADDING, PADDING, plotW, plotH);

    // Heatmap bitmap, scaled to the plot rect
    if (bitmap) {
      const tl = toPx(bitmap.bounds.xMin, bitmap.bounds.yMax);
      const br = toPx(bitmap.bounds.xMax, bitmap.bounds.yMin);
      ctx.imageSmoothingEnabled = true;
      ctx.drawImage(bitmap.canvas, tl.px, tl.py, br.px - tl.px, br.py - tl.py);
    }

    // Axis ticks (5 divisions each)
    ctx.strokeStyle = '#27272a';
    ctx.fillStyle = '#71717a';
    ctx.font = '10px monospace';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const xv = bounds.xMin + (xSpan * i) / 4;
      const { px } = toPx(xv, bounds.yMin);
      ctx.textAlign = 'center';
      ctx.fillText(`${xv.toFixed(0)}m`, px, size.height - PADDING * 0.4);
      const yv = bounds.yMin + (ySpan * i) / 4;
      const { py } = toPx(bounds.xMin, yv);
      ctx.textAlign = 'right';
      ctx.fillText(`${yv.toFixed(0)}m`, PADDING - 6, py + 3);
    }

    // Plot border
    ctx.strokeStyle = '#3f3f46';
    ctx.strokeRect(PADDING, PADDING, plotW, plotH);

    // Cell site markers, for spatial orientation ("what's where")
    if (config) {
      const seen = new Set<string>();
      for (const bs of config.base_stations) {
        const key = `${bs.position.x.toFixed(0)},${bs.position.y.toFixed(0)}`;
        if (seen.has(key)) continue; // co-located sectors -> one marker per site
        seen.add(key);
        const { px, py } = toPx(bs.position.x, bs.position.y);
        ctx.beginPath();
        ctx.arc(px, py, 4, 0, Math.PI * 2);
        ctx.fillStyle = '#ffffff';
        ctx.fill();
        ctx.strokeStyle = '#000000';
        ctx.lineWidth = 1;
        ctx.stroke();
      }

      // Building footprints, faint outline only — orientation, not detail
      ctx.strokeStyle = '#52525b88';
      ctx.lineWidth = 1;
      for (const b of config.buildings) {
        const tl = toPx(b.bounds.x_min, b.bounds.y_max);
        const br = toPx(b.bounds.x_max, b.bounds.y_min);
        ctx.strokeRect(tl.px, tl.py, br.px - tl.px, br.py - tl.py);
      }
    }

    // Hover crosshair + readout
    if (hover) {
      ctx.strokeStyle = '#ffffff55';
      ctx.beginPath();
      ctx.moveTo(hover.px, PADDING); ctx.lineTo(hover.px, PADDING + plotH);
      ctx.moveTo(PADDING, hover.py); ctx.lineTo(PADDING + plotW, hover.py);
      ctx.stroke();
    }
  }, [bitmap, bounds, size, hover, config]);

  const handleMouseMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (!bounds || !bitmap) { setHover(null); return; }
    const rect = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;

    const plotW = size.width - PADDING * 1.5;
    const plotH = size.height - PADDING * 1.5;
    if (px < PADDING || px > PADDING + plotW || py < PADDING || py > PADDING + plotH) { setHover(null); return; }

    const xSpan = bounds.xMax - bounds.xMin || 1;
    const ySpan = bounds.yMax - bounds.yMin || 1;
    const simX = bounds.xMin + ((px - PADDING) / plotW) * xSpan;
    const simY = bounds.yMin + (1 - (py - PADDING) / plotH) * ySpan;

    // Nearest grid cell lookup
    let nearest = bitmap.xs[0], nd = Infinity;
    for (const xv of bitmap.xs) { const d = Math.abs(xv - simX); if (d < nd) { nd = d; nearest = xv; } }
    let nearestY = bitmap.ys[0], ndY = Infinity;
    for (const yv of bitmap.ys) { const d = Math.abs(yv - simY); if (d < ndY) { ndY = d; nearestY = yv; } }
    const point = heatmapData.find(p => p.x === nearest && p.y === nearestY);

    setHover(point ? { x: nearest, y: nearestY, rsrp: point.rsrp, px, py } : null);
  };

  return (
    <div className="flex flex-col h-full gap-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-zinc-200">RSRP Coverage Map</h3>
          <p className="text-[11px] text-zinc-500">
            Signal strength across the simulated area — green is strong near cell sites (white dots), red is weak or blocked by buildings.
          </p>
        </div>
        {heatmapData.length > 0 && (
          <div className="flex items-center gap-2 shrink-0">
            <span className="text-[9px] text-zinc-600 font-mono">-120</span>
            <div className="w-24 h-2 rounded-full" style={{ background: 'linear-gradient(90deg, #ef4444, #eab308, #22c55e)' }} />
            <span className="text-[9px] text-zinc-600 font-mono">-60 dBm</span>
          </div>
        )}
      </div>

      <div ref={containerRef} className="flex-1 relative bg-zinc-950 border border-zinc-800 rounded-xl overflow-hidden min-h-0">
        <canvas
          ref={canvasRef}
          onMouseMove={handleMouseMove}
          onMouseLeave={() => setHover(null)}
          className="absolute inset-0"
        />
        {hover && (
          <div
            className="absolute pointer-events-none bg-zinc-900 border border-zinc-700 rounded-md px-2 py-1 text-[10px] font-mono text-zinc-200 shadow-lg"
            style={{ left: hover.px + 12, top: hover.py + 12 }}
          >
            <div>({hover.x.toFixed(0)}m, {hover.y.toFixed(0)}m)</div>
            <div style={{ color: `rgb(${rsrpToRgb(hover.rsrp).join(',')})` }}>{hover.rsrp.toFixed(1)} dBm</div>
          </div>
        )}
        {heatmapData.length === 0 && !config?.area && (
          <div className="absolute inset-0 flex items-center justify-center text-zinc-600 text-sm">
            No coverage data yet.
          </div>
        )}
      </div>
    </div>
  );
}
