'use client';

/**
 * RicRealTopology — live network map for the REAL engine's Data tab.
 *
 * Unlike the sandbox map (UE *groups* at spawn areas), the real ns-3 RAN places
 * every UE as an individual node, so each UE reports its own (x, y) in config
 * coordinates plus its serving cell. This draws them as a lightweight SVG scatter
 * (no React Flow / coordinate coupling): gNBs from the config, each live UE as a
 * dot coloured by SINR, and a faint line to the cell it's attached to — so you
 * can see exactly which cell every UE is on and watch handovers move the lines.
 */
import { useMemo } from 'react';
import type { NsOranLiveUe, NSGSimulationConfig } from '@/types/nsg';

function ueColor(sinr?: number): string {
  if (sinr == null || !Number.isFinite(sinr)) return '#ef4444';
  if (sinr >= 15) return '#22c55e';
  if (sinr >= 3) return '#eab308';
  return '#f97316';
}

const VB_W = 1000;
const VB_H = 560;

/** A fixed extent in config metres, so the view does not rescale under moving UEs. */
export interface MapBounds { minX: number; maxX: number; minY: number; maxY: number; }

/**
 * A cell name short enough to sit over a map marker.
 *
 * Config names are written for the reports table — "Site-A / N77 / S3
 * (South-West)" — and at 13px over a 22px circle the whole string collides
 * with its neighbours. The parenthetical is the part a reader scanning a map
 * does not need; the site and sector are what they are looking for.
 */
function shortName(name: string): string {
  const base = name.split('(')[0].trim().replace(/\s*\/\s*/g, ' / ');
  return base.length > 26 ? `${base.slice(0, 25)}\u2026` : base;
}

export default function RicRealTopology({
  ues, config, bounds, trails, title = 'Live map — UEs & serving cells',
}: {
  ues: NsOranLiveUe[];
  config: NSGSimulationConfig | null;
  /**
   * Optional fixed extent. The live RIC view leaves this out and the map frames
   * itself around whatever is on screen, which is right when points arrive one
   * at a time. Scrubbing a finished run is the opposite case: the extent would
   * be recomputed at every tick and the cells would jump around the UE instead
   * of the UE moving between the cells. Pass an extent covering the WHOLE run
   * and the background holds still.
   */
  bounds?: MapBounds | null;
  title?: string;
  /**
   * Where each UE has already been, up to the tick being shown. A walking UE
   * covers a few metres between cells a hundred apart, so a lone dot reads as
   * stationary however carefully you scrub; the trail is what makes the motion —
   * and where on it the serving cell changed — legible at that scale.
   */
  trails?: Map<number, { x: number; y: number }[]> | null;
}) {
  const view = useMemo(() => {
    // gNB cells: config base stations, numbered 2..n+1 (cell 1 = LTE hub, hidden).
    //
    // CO-SITED SECTORS SHARE A POSITION. A three-sector site puts three
    // cells on one coordinate, so three markers and three labels land on
    // the same pixel and only the last one drawn is legible — six cells
    // read as four on the energy-saving scenario. They are fanned out
    // around their shared point instead, far enough to read and near
    // enough to still say "these are one site".
    const raw = (config?.base_stations ?? []).map((b, i) => ({
      cell: i + 2, name: b.name, x: b.position.x, y: b.position.y,
    }));
    const atPoint = new Map<string, number[]>();
    raw.forEach((b, i) => {
      const k = `${Math.round(b.x)},${Math.round(b.y)}`;
      atPoint.set(k, [...(atPoint.get(k) ?? []), i]);
    });
    const bases = raw.map((b, i) => {
      const group = atPoint.get(`${Math.round(b.x)},${Math.round(b.y)}`) ?? [i];
      if (group.length < 2) return b;
      const k = group.indexOf(i);
      const ang = (2 * Math.PI * k) / group.length - Math.PI / 2;
      return { ...b, x: b.x + 26 * Math.cos(ang), y: b.y + 26 * Math.sin(ang) };
    });
    const uePts = ues.filter(u => u.x != null && u.y != null)
      .map(u => ({ imsi: u.imsi, x: u.x as number, y: u.y as number, sinr: u.sinr, cell: u.cell, slice: u.slice }));

    if (!bases.length && !uePts.length) return null;

    const uesPerCell = new Map<number, number>();
    for (const u of uePts) if (u.cell != null) uesPerCell.set(u.cell, (uesPerCell.get(u.cell) ?? 0) + 1);

    let minX: number, maxX: number, minY: number, maxY: number;
    if (bounds) {
      ({ minX, maxX, minY, maxY } = bounds);
    } else {
      const xs = [...bases.map(b => b.x), ...uePts.map(u => u.x)];
      const ys = [...bases.map(b => b.y), ...uePts.map(u => u.y)];
      minX = Math.min(...xs); maxX = Math.max(...xs);
      minY = Math.min(...ys); maxY = Math.max(...ys);
      const dx = (maxX - minX) || 1, dy = (maxY - minY) || 1;
      minX -= dx * 0.1; maxX += dx * 0.1; minY -= dy * 0.1; maxY += dy * 0.1;
    }
    if (maxX === minX) { minX -= 1; maxX += 1; }
    if (maxY === minY) { minY -= 1; maxY += 1; }
    const sx = (x: number) => ((x - minX) / (maxX - minX)) * VB_W;
    const sy = (y: number) => VB_H - ((y - minY) / (maxY - minY)) * VB_H; // flip: North up

    const baseByCell = new Map(bases.map(b => [b.cell, b]));
    return { bases, uePts, uesPerCell, baseByCell, sx, sy };
  }, [ues, config, bounds]);

  if (!view) {
    return (
      <div className="h-80 flex items-center justify-center text-sm text-zinc-500 bg-zinc-950 border border-zinc-800 rounded-xl">
        Waiting for live UE positions…
      </div>
    );
  }

  const { bases, uePts, uesPerCell, baseByCell, sx, sy } = view;

  return (
    <div className="bg-zinc-950 border border-zinc-800 rounded-xl p-2">
      <div className="flex items-center justify-between px-1 pb-1">
        <div className="text-[10px] font-bold text-zinc-500 uppercase">{title}</div>
        <div className="flex items-center gap-2 text-[9px] text-zinc-500">
          <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full" style={{ background: '#22c55e' }} />≥15</span>
          <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full" style={{ background: '#eab308' }} />≥3</span>
          <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full" style={{ background: '#f97316' }} />&lt;3 dB</span>
        </div>
      </div>
      <svg viewBox={`0 0 ${VB_W} ${VB_H}`} preserveAspectRatio="xMidYMid meet" className="w-full h-72">
        {/* where each UE has been so far */}
        {trails && [...trails.entries()].map(([imsi, pts]) => (
          pts.length < 2 ? null : (
            <polyline key={`t-${imsi}`} fill="none" stroke="#71717a" strokeOpacity={0.5}
              strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round"
              points={pts.map(pt => `${sx(pt.x)},${sy(pt.y)}`).join(' ')} />
          )
        ))}
        {/* serving-cell attachment lines */}
        {uePts.map(u => {
          const b = u.cell != null ? baseByCell.get(u.cell) : undefined;
          if (!b) return null;
          return (
            <line key={`l-${u.imsi}`} x1={sx(u.x)} y1={sy(u.y)} x2={sx(b.x)} y2={sy(b.y)}
              stroke={ueColor(u.sinr)} strokeOpacity={0.28} strokeWidth={1.5} strokeDasharray="5 4" />
          );
        })}
        {/* gNB cells */}
        {bases.map(b => {
          const served = uesPerCell.get(b.cell) ?? 0;
          const color = served === 0 ? '#52525b' : '#38bdf8';
          return (
            <g key={`c-${b.cell}`}>
              <circle cx={sx(b.x)} cy={sy(b.y)} r={11} fill="#09090b" stroke={color} strokeWidth={3} />
              <circle cx={sx(b.x)} cy={sy(b.y)} r={4} fill={color} />
              {/* THE CELL'S OWN NAME, the one every other surface uses.
                  This said "cell 7" while the KPM tables beside it said
                  "Site-C / N28 / S1 (South Coverage — Deep Sleep)", so a
                  reader deciding which cell to put to sleep had no way to
                  match the two. The index stays as a subscript because the
                  per-UE rows still carry it. */}
              <text x={sx(b.x)} y={sy(b.y) - 30} textAnchor="middle" className="fill-zinc-200"
                style={{ fontSize: 13, fontWeight: 600 }}>{shortName(b.name)}</text>
              <text x={sx(b.x)} y={sy(b.y) - 17} textAnchor="middle" className="fill-zinc-500"
                style={{ fontSize: 10.5 }}>cell {b.cell}</text>
              <text x={sx(b.x)} y={sy(b.y) + 26} textAnchor="middle" className="fill-zinc-500" style={{ fontSize: 12 }}>
                {served} UE
              </text>
            </g>
          );
        })}
        {/* UEs */}
        {uePts.map(u => (
          <circle key={`u-${u.imsi}`} cx={sx(u.x)} cy={sy(u.y)} r={6} fill={ueColor(u.sinr)} stroke="#09090b" strokeWidth={1.5}>
            <title>UE {u.imsi} · {u.slice ?? '—'} · cell {u.cell ?? '—'} · SINR {u.sinr != null ? `${u.sinr.toFixed(1)} dB` : '—'}</title>
          </circle>
        ))}
      </svg>
    </div>
  );
}
