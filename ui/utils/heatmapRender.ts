/**
 * Shared REM heatmap rendering: turns a flat list of {x, y, rsrp} points into
 * a small off-screen bitmap, one pixel per REM grid cell, with no overlap or
 * blending. The caller scales that bitmap up with a single drawImage() call.
 *
 * Why a bitmap instead of drawing one gradient blob per point (the original
 * approach):
 *   - Performance: drawImage of a small bitmap is one GPU-accelerated call,
 *     regardless of point count. Drawing thousands of radial gradients per
 *     frame (re-triggered on every pan/zoom) was the source of the lag.
 *   - Correctness: with `lighter` (additive) blending, heavily overlapping
 *     semi-transparent circles sum toward white/cyan instead of showing the
 *     intended red→yellow→green gradient — exactly the washed-out look in
 *     the original screenshot. One pixel per grid cell has no overlap, so
 *     colors render exactly as intended.
 */

import type { RemPoint } from '@/types/nsg';

// ── RSRP → color (red = weak, yellow = mid, green = strong) ───────────────

const STRONG = { dbm: -60,  rgb: [34, 197, 94]  as const }; // green-500
const MID    = { dbm: -100, rgb: [234, 179, 8]  as const }; // yellow-500
const WEAK   = { dbm: -120, rgb: [239, 68, 68]  as const }; // red-500

function lerp(a: number, b: number, t: number) {
  return a + (b - a) * t;
}

export function rsrpToRgb(rsrp: number): readonly [number, number, number] {
  const clamped = Math.max(WEAK.dbm, Math.min(STRONG.dbm, rsrp));
  if (clamped >= MID.dbm) {
    const t = (clamped - MID.dbm) / (STRONG.dbm - MID.dbm);
    return [
      lerp(MID.rgb[0], STRONG.rgb[0], t),
      lerp(MID.rgb[1], STRONG.rgb[1], t),
      lerp(MID.rgb[2], STRONG.rgb[2], t),
    ];
  }
  const t = (clamped - WEAK.dbm) / (MID.dbm - WEAK.dbm);
  return [
    lerp(WEAK.rgb[0], MID.rgb[0], t),
    lerp(WEAK.rgb[1], MID.rgb[1], t),
    lerp(WEAK.rgb[2], MID.rgb[2], t),
  ];
}

export interface HeatmapBitmap {
  canvas: HTMLCanvasElement;
  /** Sorted ascending unique X (sim meters) values found in the data. */
  xs: number[];
  /** Sorted ascending unique Y (sim meters) values found in the data. */
  ys: number[];
  /** Bounding box in sim coordinates. */
  bounds: { xMin: number; xMax: number; yMin: number; yMax: number };
}

/**
 * Reconstruct the REM grid from a flat point list and rasterize it into a
 * small bitmap — one pixel per (x, y) grid cell, exact RGBA color, no blend.
 * Cells missing from `data` (filtered out backend-side as below the noise
 * floor) are left fully transparent.
 */
export function buildHeatmapBitmap(data: RemPoint[]): HeatmapBitmap | null {
  if (!data || data.length === 0) return null;

  const xSet = new Set<number>();
  const ySet = new Set<number>();
  for (const p of data) { xSet.add(p.x); ySet.add(p.y); }

  const xs = Array.from(xSet).sort((a, b) => a - b);
  const ys = Array.from(ySet).sort((a, b) => a - b);
  if (xs.length === 0 || ys.length === 0) return null;

  const xIndex = new Map(xs.map((v, i) => [v, i]));
  const yIndex = new Map(ys.map((v, i) => [v, i]));

  const canvas = document.createElement('canvas');
  canvas.width = xs.length;
  canvas.height = ys.length;
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;

  const img = ctx.createImageData(xs.length, ys.length);
  // Row 0 = largest sim Y (canvas Y increases downward, sim Y increases "up").
  for (const p of data) {
    const col = xIndex.get(p.x);
    const yi  = yIndex.get(p.y);
    if (col === undefined || yi === undefined) continue;
    const row = ys.length - 1 - yi;
    const [r, g, b] = rsrpToRgb(p.rsrp);
    const idx = (row * xs.length + col) * 4;
    img.data[idx]     = r;
    img.data[idx + 1] = g;
    img.data[idx + 2] = b;
    img.data[idx + 3] = 255;
  }
  ctx.putImageData(img, 0, 0);

  return {
    canvas,
    xs,
    ys,
    bounds: { xMin: xs[0], xMax: xs[xs.length - 1], yMin: ys[0], yMax: ys[ys.length - 1] },
  };
}
