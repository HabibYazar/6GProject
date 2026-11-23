/**
 * Shareable scenario report (PDF). Builds a one-page A4 summary of the current
 * scenario — configuration, coverage KPIs, and the RSRP heatmap — and downloads
 * it. jsPDF is imported dynamically so it stays out of the main bundle until the
 * user actually exports.
 *
 * The heatmap image reuses buildHeatmapBitmap (the exact renderer the Analytics
 * tab uses), upscaled onto an offscreen canvas, so the PDF matches the on-screen
 * coverage map — no DOM capture, no CORS.
 */
import type { NSGSimulationConfig, RemPoint } from '@/types/nsg';
import { buildHeatmapBitmap, rsrpToRgb } from './heatmapRender';

// ── Coverage KPIs ────────────────────────────────────────────────────────────

export interface CoverageKpis {
  points: number;
  mean: number | null;
  median: number | null;
  min: number | null;
  max: number | null;
  pctExcellent: number; // ≥ -85 dBm
  pctGood: number; //      ≥ -100 dBm
  pctUsable: number; //    ≥ -115 dBm
}

export function coverageKpis(data: RemPoint[]): CoverageKpis {
  if (!data || data.length === 0) {
    return { points: 0, mean: null, median: null, min: null, max: null, pctExcellent: 0, pctGood: 0, pctUsable: 0 };
  }
  const r = data.map(p => p.rsrp).sort((a, b) => a - b);
  const n = r.length;
  const sum = r.reduce((a, b) => a + b, 0);
  const pctAbove = (t: number) => Math.round((100 * r.filter(v => v >= t).length) / n);
  return {
    points: n,
    mean: sum / n,
    median: r[Math.floor(n / 2)],
    min: r[0],
    max: r[n - 1],
    pctExcellent: pctAbove(-85),
    pctGood: pctAbove(-100),
    pctUsable: pctAbove(-115),
  };
}

// ── Heatmap → data URL (upscaled for print) ──────────────────────────────────

function heatmapDataUrl(heatmap: RemPoint[]): { url: string; wpx: number; hpx: number } | null {
  const bmp = buildHeatmapBitmap(heatmap);
  if (!bmp) return null;
  const W = 900;
  const H = Math.max(1, Math.round((W * bmp.canvas.height) / bmp.canvas.width));
  const c = document.createElement('canvas');
  c.width = W;
  c.height = H;
  const ctx = c.getContext('2d');
  if (!ctx) return null;
  // JPEG has no alpha, so paint an explicit "no data / out of coverage" background
  // (cells below the noise floor are transparent in the bitmap) before drawing.
  ctx.fillStyle = '#f4f4f5';
  ctx.fillRect(0, 0, W, H);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(bmp.canvas, 0, 0, W, H);
  return { url: c.toDataURL('image/jpeg', 0.92), wpx: W, hpx: H };
}

// ── Config summary helpers ───────────────────────────────────────────────────

function configRows(cfg: NSGSimulationConfig): [string, string][] {
  const totalUe = cfg.ue_groups.reduce((a, g) => a + g.count, 0);
  const bands = Array.from(new Set(cfg.base_stations.map(b => b.radio.band.label)));
  const slices = Array.from(new Set(cfg.ue_groups.flatMap(g => g.services.map(s => s.slice_type))));
  const areaW = Math.round(cfg.area.x_max - cfg.area.x_min);
  const areaH = Math.round(cfg.area.y_max - cfg.area.y_min);
  return [
    ['Base stations', `${cfg.base_stations.length}`],
    ['Bands', bands.join(', ') || '—'],
    ['UE groups', `${cfg.ue_groups.length} (${totalUe} UEs)`],
    ['Slices', slices.join(', ') || '—'],
    ['Buildings', `${cfg.buildings.length}`],
    ['Area', `${areaW} × ${areaH} m @ ${cfg.area.resolution_m} m`],
    ['Propagation', cfg.settings.scenario],
    ['Shadowing', cfg.settings.shadowing_enabled ? 'enabled' : 'disabled'],
  ];
}

function coverageRows(k: CoverageKpis): [string, string][] {
  const dbm = (v: number | null) => (v === null ? '—' : `${v.toFixed(1)} dBm`);
  return [
    ['REM points', k.points ? k.points.toLocaleString() : '—'],
    ['Mean RSRP', dbm(k.mean)],
    ['Median RSRP', dbm(k.median)],
    ['Range', k.min === null ? '—' : `${k.min.toFixed(0)} … ${k.max!.toFixed(0)} dBm`],
    ['Excellent (≥ -85)', `${k.pctExcellent}%`],
    ['Good (≥ -100)', `${k.pctGood}%`],
    ['Usable (≥ -115)', `${k.pctUsable}%`],
  ];
}

// ── Main export ──────────────────────────────────────────────────────────────

const TEAL: [number, number, number] = [13, 148, 136];
const INK: [number, number, number] = [39, 39, 42];
const GREY: [number, number, number] = [113, 113, 122];

export async function exportScenarioReport(cfg: NSGSimulationConfig, heatmap: RemPoint[]): Promise<void> {
  const { jsPDF } = await import('jspdf');
  const doc = new jsPDF({ unit: 'mm', format: 'a4' });
  const pageW = 210;
  const margin = 14;
  const contentW = pageW - margin * 2;
  const now = new Date();

  // Header band
  doc.setFillColor(...TEAL);
  doc.rect(0, 0, pageW, 24, 'F');
  doc.setTextColor(255, 255, 255);
  doc.setFont('helvetica', 'bold');
  doc.setFontSize(15);
  doc.text('NSG AI Platform', margin, 11);
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(10);
  doc.text('Scenario Report', margin, 18);
  doc.setFontSize(8);
  doc.text(now.toLocaleString(), pageW - margin, 11, { align: 'right' });

  let y = 34;

  // Scenario name + description
  doc.setTextColor(...INK);
  doc.setFont('helvetica', 'bold');
  doc.setFontSize(16);
  doc.text(doc.splitTextToSize(cfg.name || 'Untitled scenario', contentW), margin, y);
  y += 8;
  if (cfg.description) {
    doc.setTextColor(...GREY);
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(9);
    const desc = doc.splitTextToSize(cfg.description, contentW);
    doc.text(desc, margin, y);
    y += desc.length * 4.5 + 4;
  } else {
    y += 2;
  }

  // Two columns: Configuration | Coverage
  const colGap = 10;
  const colW = (contentW - colGap) / 2;
  const leftX = margin;
  const rightX = margin + colW + colGap;
  const startY = y;

  const kpis = coverageKpis(heatmap);
  const drawColumn = (x: number, title: string, rows: [string, string][], note?: string) => {
    let cy = startY;
    doc.setTextColor(...TEAL);
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(9);
    doc.text(title.toUpperCase(), x, cy);
    cy += 2;
    doc.setDrawColor(...TEAL);
    doc.setLineWidth(0.4);
    doc.line(x, cy, x + colW, cy);
    cy += 5;
    doc.setFontSize(9.5);
    for (const [k, v] of rows) {
      doc.setTextColor(...GREY);
      doc.setFont('helvetica', 'normal');
      doc.text(k, x, cy);
      doc.setTextColor(...INK);
      doc.setFont('helvetica', 'bold');
      doc.text(v, x + colW, cy, { align: 'right' });
      cy += 6;
    }
    if (note) {
      doc.setTextColor(...GREY);
      doc.setFont('helvetica', 'italic');
      doc.setFontSize(8);
      doc.text(doc.splitTextToSize(note, colW), x, cy + 1);
    }
    return cy;
  };

  const leftEnd = drawColumn(leftX, 'Configuration', configRows(cfg));
  const rightEnd = drawColumn(
    rightX,
    'Coverage',
    coverageRows(kpis),
    kpis.points === 0 ? 'No coverage data — run EXECUTE to include the REM heatmap.' : undefined,
  );
  y = Math.max(leftEnd, rightEnd) + 8;

  // Coverage heatmap
  const img = heatmapDataUrl(heatmap);
  if (img) {
    doc.setTextColor(...TEAL);
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(9);
    doc.text('RSRP COVERAGE MAP', margin, y);
    y += 4;
    const imgW = contentW;
    const imgH = Math.min((imgW * img.hpx) / img.wpx, 150);
    doc.addImage(img.url, 'JPEG', margin, y, imgW, imgH);
    doc.setDrawColor(...GREY);
    doc.setLineWidth(0.2);
    doc.rect(margin, y, imgW, imgH);
    y += imgH + 6;

    // Legend gradient (weak red → strong green)
    const legW = 70;
    const legH = 3.5;
    const segs = 60;
    for (let i = 0; i < segs; i++) {
      const rsrp = -120 + (60 * i) / (segs - 1); // -120 … -60
      const [r, g, b] = rsrpToRgb(rsrp);
      doc.setFillColor(Math.round(r), Math.round(g), Math.round(b));
      doc.rect(margin + (legW / segs) * i, y, legW / segs + 0.3, legH, 'F');
    }
    doc.setTextColor(...GREY);
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(7);
    doc.text('-120 dBm (weak)', margin, y + legH + 3.5);
    doc.text('-60 dBm (strong)', margin + legW, y + legH + 3.5, { align: 'right' });
  }

  // Footer
  doc.setDrawColor(...GREY);
  doc.setLineWidth(0.2);
  doc.line(margin, 287, pageW - margin, 287);
  doc.setTextColor(...GREY);
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(7.5);
  doc.text('Generated by NSG AI Platform', margin, 292);
  doc.text('Page 1', pageW - margin, 292, { align: 'right' });

  const slug = (cfg.name || 'scenario').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'scenario';
  doc.save(`${slug}-report.pdf`);
}
