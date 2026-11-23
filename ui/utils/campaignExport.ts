/**
 * Campaign data export — the machine-readable half.
 *
 * The PDF is evidence for a person: verdict, criteria, what moved. It is the
 * wrong container for the numbers themselves, and until now there was nowhere
 * else to get them — a run's per-tick series lived only in the backend and in
 * one chart. These two exports close that:
 *
 *   CSV   one row per KPM window per run: the time series, ready for a
 *         spreadsheet or pandas. This is what "give me the data" means.
 *   JSON  the whole campaign — thresholds, verdicts, violations, every run's
 *         scenario config and full frame list. Enough to reproduce the run and
 *         to re-derive every number in the PDF.
 */
import type { CampaignRecord, SimRunDataset, SimFrame } from '@/types/nsg';

function download(name: string, mime: string, body: string): void {
  const url = URL.createObjectURL(new Blob([body], { type: mime }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  // Revoke on the next tick: revoking synchronously can cancel the download in
  // some browsers before it has read the blob.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function slug(s: string): string {
  return (s || 'campaign').toLowerCase().replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '').slice(0, 40) || 'campaign';
}

const csvCell = (v: unknown): string => {
  if (v === null || v === undefined) return '';
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

/**
 * Per-tick rows across every run in the campaign.
 *
 * Long format (one row per tick per run) rather than one file per run: it is the
 * shape every analysis tool wants, and it keeps the run label and seed on each
 * row so the runs stay distinguishable after a filter.
 */
export function campaignFramesCsv(
  record: CampaignRecord,
  datasets: Record<string, SimRunDataset>,
): string {
  // The measurement set varies per tick, so the columns are the union across
  // everything present — a fixed guess would silently drop whatever the engine
  // reported that day.
  const measNames = new Set<string>();
  for (const d of Object.values(datasets)) {
    for (const f of d.frames ?? []) {
      for (const n of Object.keys(f.measurements ?? {})) measNames.add(n);
    }
  }
  const meas = [...measNames].sort();

  const head = [
    'campaign', 'run_label', 'seed', 'run_id', 'tick',
    'ues_reporting', 'cells_reporting', 'dl_mbps', 'handovers', 'power_w',
    ...meas.map(m => `${m}.mean`),
  ];
  const rows: string[] = [head.map(csvCell).join(',')];

  for (const run of record.runs) {
    const d = run.run_id ? datasets[run.run_id] : undefined;
    for (const f of (d?.frames ?? []) as SimFrame[]) {
      const dl = (f.ues ?? []).reduce((a, u) => a + (u.thp_kbps ?? 0), 0) / 1000;
      rows.push([
        record.name, run.label, run.seed, run.run_id, f.tick,
        (f.ues ?? []).length, (f.cells ?? []).length, dl.toFixed(3),
        f.handovers ?? '', f.energy?.total_w ?? '',
        ...meas.map(m => f.measurements?.[m]?.mean ?? ''),
      ].map(csvCell).join(','));
    }
  }
  return rows.join('\n');
}

export function exportCampaignCsv(
  record: CampaignRecord,
  datasets: Record<string, SimRunDataset>,
): void {
  download(`${slug(record.name)}-timeseries.csv`, 'text/csv;charset=utf-8',
           campaignFramesCsv(record, datasets));
}

export function exportCampaignJson(
  record: CampaignRecord,
  datasets: Record<string, SimRunDataset>,
): void {
  const bundle = {
    exported_at: new Date().toISOString(),
    campaign: record,
    // Keyed by run id so a row in the campaign record points straight at its
    // dataset without a scan.
    runs: datasets,
  };
  download(`${slug(record.name)}-campaign.json`, 'application/json',
           JSON.stringify(bundle, null, 1));
}
