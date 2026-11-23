/**
 * Client-side data export — download the session's measured data as a file.
 * Used by the RIC Test (real engine) and Live Traffic panels; the Analytics
 * tab already has its own PDF export.
 */

function trigger(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

const stamp = () => new Date().toISOString().replace(/[:T]/g, '-').slice(0, 19);

export function downloadJson(name: string, data: unknown): void {
  trigger(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }),
          `${name}-${stamp()}.json`);
}

/** Flat array-of-objects → CSV. Columns = union of keys, in first-seen order. */
export function downloadCsv(name: string, rows: Record<string, unknown>[]): void {
  const cols: string[] = [];
  for (const r of rows) for (const k of Object.keys(r)) if (!cols.includes(k)) cols.push(k);
  const esc = (v: unknown) => {
    if (v == null) return '';
    const s = typeof v === 'object' ? JSON.stringify(v) : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csv = [cols.join(','), ...rows.map(r => cols.map(c => esc(r[c])).join(','))].join('\n');
  trigger(new Blob([csv], { type: 'text/csv' }), `${name}-${stamp()}.csv`);
}
