/**
 * Campaign test report (PDF).
 *
 * The scenario report (report.ts) describes a design. This one describes a
 * VERDICT: what was asserted, what was measured, which runs failed and by how
 * much, and — when a baseline is selected — what moved since last time. It is
 * the artefact a campaign exists to produce; everything else on screen is a
 * view of data that disappears when the tab closes.
 *
 * jsPDF is imported dynamically so it stays out of the main bundle until the
 * user actually exports (same rule as report.ts).
 */
import type {
  CampaignRecord, CampaignDiff, CampaignRun, NSGSimulationConfig,
} from '@/types/nsg';

const INK: [number, number, number] = [39, 39, 42];
const GREY: [number, number, number] = [113, 113, 122];
const LINE: [number, number, number] = [212, 212, 216];

const VERDICT_COLOUR: Record<string, [number, number, number]> = {
  pass:          [16, 133, 96],
  fail:          [190, 42, 42],
  error:         [180, 120, 20],
  no_thresholds: [82, 82, 91],
  empty:         [82, 82, 91],
};

const VERDICT_TEXT: Record<string, string> = {
  pass: 'PASS', fail: 'FAIL', error: 'ERROR',
  no_thresholds: 'RECORDED (no pass criteria)', empty: 'EMPTY',
};

/** Pull a dotted path out of a run summary — same address space as thresholds. */
function metricOf(run: CampaignRun, path: string): number | null {
  let node: unknown = run.summary;
  for (const part of path.split('.')) {
    if (typeof node !== 'object' || node === null || !(part in node)) return null;
    node = (node as Record<string, unknown>)[part];
  }
  return typeof node === 'number' ? node : null;
}

/** The scenario in one block — a report nobody can reproduce is an anecdote. */
function scenarioRows(cfg: NSGSimulationConfig | undefined): [string, string][] {
  if (!cfg) return [];
  const totalUe = (cfg.ue_groups ?? []).reduce((a, g) => a + (g.count ?? 0), 0);
  const bands = Array.from(new Set((cfg.base_stations ?? []).map(b => b.radio?.band?.label)))
    .filter(Boolean);
  const powers = (cfg.base_stations ?? []).map(b => b.radio?.tx_power_dbm).filter(v => v != null);
  const slices = Array.from(new Set(
    (cfg.ue_groups ?? []).flatMap(g => (g.services ?? []).map(s => s.slice_type))));
  const s = cfg.settings ?? ({} as NSGSimulationConfig['settings']);
  const rows: [string, string][] = [
    ['Cells', `${(cfg.base_stations ?? []).length}` +
      (powers.length ? `  (${Math.min(...powers as number[])}–${Math.max(...powers as number[])} dBm)` : '')],
    ['Band', bands.join(', ') || '—'],
    ['UE groups', `${(cfg.ue_groups ?? []).length}  (${totalUe} UEs)`],
    ['Slices', slices.join(', ') || '—'],
    ['Buildings', `${(cfg.buildings ?? []).length}`],
    ['Propagation', `${s.scenario ?? '—'}  ·  shadowing ${s.shadowing_enabled ? 'on' : 'off'}`],
    ['Handover', `${s.handover_mode ?? '—'}  ·  check ${s.ho_check_period_ms ?? '—'} ms` +
      `  ·  A3 ${s.ho_a3_entry ? 'on' : 'off'}  ·  margin ${s.ho_sinr_margin_db ?? '—'} dB`],
    ['Seed', `${s.random_seed ?? '—'}`],
  ];
  const anomalies = cfg.anomalies ?? [];
  if (anomalies.length) {
    rows.push(['Anomalies', anomalies
      .map(a => `${a.type} on ${a.cell_id} @ ${a.start_s}s`).join('; ')]);
  }
  return rows;
}

/** Every number the run produced, not only the ones a threshold happened to name. */
function summaryRows(run: CampaignRun): [string, string][] {
  const s = (run.summary ?? {}) as Record<string, unknown>;
  const energy = (s.energy_final ?? {}) as { total_w?: number };
  const rows: [string, string][] = [
    ['KPM windows', fmt(s.ticks as number)],
    ['Downlink mean', `${fmt(s.dl_mbps_mean as number)} Mbps`],
    ['Downlink peak', `${fmt(s.dl_mbps_max as number)} Mbps`],
    ['UEs (mean)', fmt(s.ue_count_mean as number)],
    ['Handovers', fmt(s.handovers_total as number)],
    ['Power (total)', energy.total_w != null ? `${fmt(energy.total_w)} W` : '—'],
  ];
  const configured = s.anomalies_configured as number | undefined;
  if (configured) {
    rows.push(['Anomalies fired',
      `${fmt(s.anomalies_fired as number)} of ${configured} configured`]);
  }
  const slices = (s.slices ?? {}) as Record<string, { mean_mbps?: number }>;
  for (const [name, v] of Object.entries(slices)) {
    rows.push([`Slice ${name}`, `${fmt(v.mean_mbps)} Mbps`]);
  }
  return rows;
}

function fmt(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return Math.abs(v) >= 1000 ? v.toFixed(0) : v.toFixed(2).replace(/\.00$/, '');
}

function duration(record: CampaignRecord): string {
  if (!record.finished_at) return 'in progress';
  const ms = new Date(record.finished_at).getTime() - new Date(record.created_at).getTime();
  if (!Number.isFinite(ms) || ms < 0) return '—';
  const m = Math.round(ms / 60000);
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m} min`;
}

export async function exportCampaignReport(
  record: CampaignRecord,
  diff: CampaignDiff | null = null,
): Promise<void> {
  const { jsPDF } = await import('jspdf');
  const doc = new jsPDF({ unit: 'mm', format: 'a4' });
  const pageW = 210;
  const pageH = 297;
  const margin = 14;
  const contentW = pageW - margin * 2;

  const accent = VERDICT_COLOUR[record.verdict ?? 'empty'] ?? VERDICT_COLOUR.empty;
  let page = 0;
  let y = 0;

  const footer = () => {
    doc.setDrawColor(...LINE);
    doc.setLineWidth(0.2);
    doc.line(margin, pageH - 10, pageW - margin, pageH - 10);
    doc.setTextColor(...GREY);
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(7.5);
    doc.text('Generated by NSG AI Platform', margin, pageH - 5);
    doc.text(`Page ${page}`, pageW - margin, pageH - 5, { align: 'right' });
  };

  const newPage = (first = false) => {
    if (!first) doc.addPage();
    page += 1;
    doc.setFillColor(...accent);
    doc.rect(0, 0, pageW, first ? 24 : 12, 'F');
    doc.setTextColor(255, 255, 255);
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(first ? 15 : 10);
    doc.text('NSG AI Platform', margin, first ? 11 : 8);
    if (first) {
      doc.setFont('helvetica', 'normal');
      doc.setFontSize(10);
      doc.text('Campaign Test Report', margin, 18);
      doc.setFontSize(8);
      doc.text(new Date().toLocaleString(), pageW - margin, 11, { align: 'right' });
    } else {
      doc.setFont('helvetica', 'normal');
      doc.setFontSize(8);
      doc.text(record.name, pageW - margin, 8, { align: 'right' });
    }
    footer();
    y = first ? 34 : 20;
  };

  /** Start a new page when the next block would not fit. Returns true if it did,
   *  so a table can repeat its column headings on the new page. */
  const need = (h: number): boolean => {
    if (y + h > pageH - 16) { newPage(); return true }
    return false;
  };

  const sectionTitle = (title: string) => {
    need(12);
    doc.setTextColor(...accent);
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(9);
    doc.text(title.toUpperCase(), margin, y);
    y += 2;
    doc.setDrawColor(...accent);
    doc.setLineWidth(0.4);
    doc.line(margin, y, pageW - margin, y);
    y += 6;
  };

  newPage(true);

  // ── Title + verdict ────────────────────────────────────────────────────────
  doc.setTextColor(...INK);
  doc.setFont('helvetica', 'bold');
  doc.setFontSize(16);
  doc.text(doc.splitTextToSize(record.name || 'Untitled campaign', contentW - 40), margin, y);

  const verdictLabel = VERDICT_TEXT[record.verdict ?? 'empty'] ?? String(record.verdict ?? '—');
  doc.setFillColor(...accent);
  doc.roundedRect(pageW - margin - 38, y - 6, 38, 9, 1.5, 1.5, 'F');
  doc.setTextColor(255, 255, 255);
  doc.setFontSize(record.verdict === 'no_thresholds' ? 6 : 10);
  doc.text(verdictLabel, pageW - margin - 19, y, { align: 'center' });
  y += 9;

  const counts = record.runs.reduce(
    (a, r) => { a[r.verdict ?? 'running'] = (a[r.verdict ?? 'running'] ?? 0) + 1; return a },
    {} as Record<string, number>,
  );
  doc.setTextColor(...GREY);
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(9);
  doc.text(
    `${record.runs.length}/${(record.planned?.length ?? record.runs.length)} runs · ` +
    `${counts.pass ?? 0} passed · ${counts.fail ?? 0} failed · ${counts.error ?? 0} errored · ` +
    `base seed ${record.base_seed} · ${duration(record)}`,
    margin, y,
  );
  y += 5;
  doc.setFontSize(8);
  doc.text(
    `Started ${new Date(record.created_at).toLocaleString()}` +
    (record.status === 'stopped' ? ' · stopped early, partial results'
      : record.status === 'interrupted' ? ' · interrupted, partial results' : ''),
    margin, y,
  );
  y += 9;

  // ── Pass criteria ──────────────────────────────────────────────────────────
  sectionTitle('Pass criteria');
  if (record.thresholds.length === 0) {
    doc.setTextColor(...GREY);
    doc.setFont('helvetica', 'italic');
    doc.setFontSize(9);
    doc.text('None declared — this campaign recorded results without asserting anything.', margin, y);
    y += 8;
  } else {
    doc.setFontSize(9);
    for (const t of record.thresholds) {
      need(6);
      doc.setTextColor(...GREY);
      doc.setFont('helvetica', 'normal');
      doc.text(t.label || t.metric, margin, y);
      doc.setTextColor(...INK);
      doc.setFont('helvetica', 'bold');
      doc.text(`${t.op} ${t.value}`, margin + 70, y);
      doc.setTextColor(...GREY);
      doc.setFont('helvetica', 'normal');
      doc.setFontSize(7.5);
      doc.text(t.metric, pageW - margin, y, { align: 'right' });
      doc.setFontSize(9);
      y += 5.5;
    }
    y += 4;
  }

  // ── Run results ────────────────────────────────────────────────────────────
  sectionTitle('Run results');

  // One column per threshold metric, so the table shows what was asserted —
  // a results table full of metrics nobody bounded is a data dump, not a report.
  const metricCols = record.thresholds.slice(0, 3);
  const colX = [margin, margin + 78, margin + 96];
  const metricX = metricCols.map((_, i) => margin + 118 + i * 30);

  const header = () => {
    doc.setTextColor(...GREY);
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(7.5);
    doc.text('SCENARIO', colX[0], y);
    doc.text('SEED', colX[1], y);
    doc.text('VERDICT', colX[2], y);
    metricCols.forEach((t, i) => {
      doc.text(
        doc.splitTextToSize((t.label || t.metric).toUpperCase(), 28)[0],
        metricX[i] + 26, y, { align: 'right' },
      );
    });
    y += 2;
    doc.setDrawColor(...LINE);
    doc.setLineWidth(0.2);
    doc.line(margin, y, pageW - margin, y);
    y += 4.5;
  };
  header();

  for (const run of record.runs) {
    if (need(7)) header();  // broke to a new page — repeat the column titles
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(8.5);
    doc.setTextColor(...INK);
    doc.text(doc.splitTextToSize(run.label, 74)[0], colX[0], y);
    doc.setTextColor(...GREY);
    doc.text(String(run.seed), colX[1], y);

    const rv = run.verdict ?? 'running';
    doc.setTextColor(...(VERDICT_COLOUR[rv] ?? GREY));
    doc.setFont('helvetica', 'bold');
    doc.text((VERDICT_TEXT[rv] ?? rv).split(' ')[0], colX[2], y);

    doc.setFont('helvetica', 'normal');
    doc.setTextColor(...INK);
    metricCols.forEach((t, i) => {
      doc.text(fmt(metricOf(run, t.metric)), metricX[i] + 26, y, { align: 'right' });
    });
    y += 6;
  }
  y += 3;

  // ── The scenario itself ────────────────────────────────────────────────────
  // Without it the report says a verdict about something the reader cannot
  // identify, let alone reproduce. Taken from the first run's stored config —
  // every run in a campaign item shares it apart from the seed.
  const firstCfg = (record.runs.find(r => (r as { config?: unknown }).config) as
    { config?: NSGSimulationConfig } | undefined)?.config;
  const scenario = scenarioRows(firstCfg);
  if (scenario.length) {
    sectionTitle('Scenario under test');
    doc.setFontSize(9);
    for (const [k, v] of scenario) {
      need(6);
      doc.setTextColor(...GREY);
      doc.setFont('helvetica', 'normal');
      doc.text(k, margin, y);
      doc.setTextColor(...INK);
      const lines = doc.splitTextToSize(v, contentW - 46);
      doc.text(lines, margin + 44, y);
      y += Math.max(5.5, lines.length * 4.5);
    }
    y += 4;
  }

  // ── Everything each run measured ───────────────────────────────────────────
  // The results table above shows only the metrics a threshold named. This is
  // the rest of what the engine reported — the reason to keep the PDF at all
  // when there is nowhere else to read the numbers.
  const measured = record.runs.filter(r => r.summary);
  if (measured.length) {
    sectionTitle('Measured');
    for (const run of measured) {
      need(14);
      doc.setTextColor(...INK);
      doc.setFont('helvetica', 'bold');
      doc.setFontSize(9);
      doc.text(run.label, margin, y);
      y += 5;

      const rows = summaryRows(run);
      const colW = contentW / 2;
      for (let i = 0; i < rows.length; i += 2) {
        need(5.5);
        for (const [j, cell] of [rows[i], rows[i + 1]].entries()) {
          if (!cell) continue;
          const x = margin + j * colW;
          doc.setTextColor(...GREY);
          doc.setFont('helvetica', 'normal');
          doc.setFontSize(8.5);
          doc.text(cell[0], x + 3, y);
          doc.setTextColor(...INK);
          doc.setFont('helvetica', 'bold');
          doc.text(cell[1], x + colW - 4, y, { align: 'right' });
        }
        y += 5;
      }

      // The raw E2SM-KPM set, three to a row.
      const meas = ((run.summary ?? {}) as {
        measurements?: Record<string, { mean?: number; min?: number; max?: number }>;
      }).measurements ?? {};
      const names = Object.keys(meas).sort();
      if (names.length) {
        need(8);
        y += 1;
        doc.setTextColor(...GREY);
        doc.setFont('helvetica', 'italic');
        doc.setFontSize(7.5);
        doc.text(`${names.length} E2SM-KPM measurements (mean, min–max)`, margin + 3, y);
        y += 4;
        const mColW = contentW / 3;
        for (let i = 0; i < names.length; i += 3) {
          need(4.5);
          for (const [j, name] of names.slice(i, i + 3).entries()) {
            const st = meas[name] ?? {};
            const x = margin + j * mColW;
            doc.setTextColor(...GREY);
            doc.setFont('helvetica', 'normal');
            doc.setFontSize(6.5);
            doc.text(doc.splitTextToSize(name, mColW - 26)[0], x + 3, y);
            doc.setTextColor(...INK);
            doc.text(`${fmt(st.mean)} (${fmt(st.min)}–${fmt(st.max)})`,
                     x + mColW - 4, y, { align: 'right' });
          }
          y += 4;
        }
      }
      y += 4;
    }
  }

  // ── Violations ─────────────────────────────────────────────────────────────
  const failing = record.runs.filter(r => r.violations.length > 0 || r.error);
  if (failing.length > 0) {
    // "Why runs failed" is the wrong heading when a run produced nothing to
    // judge — a report that says "measured 0" about a run that never measured
    // is worse than one that says nothing.
    sectionTitle(failing.every(r => r.verdict === 'error')
      ? 'Why runs could not be scored' : 'Why runs failed');
    for (const run of failing) {
      need(10);
      doc.setTextColor(...INK);
      doc.setFont('helvetica', 'bold');
      doc.setFontSize(9);
      doc.text(run.label, margin, y);
      y += 5;

      if (run.error) {
        need(6);
        doc.setTextColor(...VERDICT_COLOUR.error);
        doc.setFont('helvetica', 'normal');
        doc.setFontSize(8);
        const label = run.verdict === 'error' ? 'Not scored' : 'Engine error';
        const lines = doc.splitTextToSize(`${label}: ${run.error}`, contentW - 4);
        doc.text(lines, margin + 4, y);
        y += lines.length * 4 + 2;
      }

      for (const v of run.violations) {
        need(6);
        doc.setTextColor(...GREY);
        doc.setFont('helvetica', 'normal');
        doc.setFontSize(8.5);
        doc.text(`expected  ${v.expected}`, margin + 4, y);
        doc.setTextColor(...VERDICT_COLOUR.fail);
        doc.setFont('helvetica', 'bold');
        doc.text(
          v.reason === 'missing' ? 'never measured' : `measured ${fmt(v.measured)}`,
          pageW - margin, y, { align: 'right' },
        );
        y += 5;
      }
      y += 3;
    }
  }

  // ── Regression against a baseline ──────────────────────────────────────────
  if (diff) {
    sectionTitle(`Change since ${diff.baseline.name}`);

    doc.setTextColor(...GREY);
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(8.5);
    doc.text(
      `Baseline run ${new Date(diff.baseline.created_at).toLocaleDateString()} · overall: ${diff.overall}`,
      margin, y,
    );
    y += 5;

    // The same caveat the screen shows. A report that omits it turns a
    // methodology difference into an apparent finding.
    if (!diff.seed_match || !diff.threshold_match) {
      need(10);
      doc.setTextColor(...VERDICT_COLOUR.error);
      doc.setFont('helvetica', 'italic');
      doc.setFontSize(8);
      const caveats = [
        !diff.seed_match
          ? `Base seeds differ (${diff.baseline.base_seed} vs ${diff.current.base_seed}) — part of every delta is seed noise.`
          : '',
        !diff.threshold_match
          ? 'Pass criteria changed between the two campaigns, so verdict changes may reflect the criteria rather than the network.'
          : '',
      ].filter(Boolean).join(' ');
      const lines = doc.splitTextToSize(caveats, contentW);
      doc.text(lines, margin, y);
      y += lines.length * 4 + 3;
    }

    for (const item of diff.items) {
      need(12);
      doc.setTextColor(...INK);
      doc.setFont('helvetica', 'bold');
      doc.setFontSize(9);
      doc.text(item.label, margin, y);

      const changeColour =
        item.change === 'regressed' ? VERDICT_COLOUR.fail
        : item.change === 'fixed' ? VERDICT_COLOUR.pass
        : GREY;
      doc.setTextColor(...changeColour);
      doc.setFontSize(8);
      doc.text(
        item.status !== 'matched'
          ? (item.status === 'added' ? 'new in this campaign' : 'not run this time')
          : item.change === 'unchanged'
            ? `${item.current_verdict} (unchanged)`
            : `${item.baseline_verdict} → ${item.current_verdict}`,
        pageW - margin, y, { align: 'right' },
      );
      y += 5;

      const rows = item.metrics.filter(m => m.is_threshold);
      for (const m of rows) {
        need(6);
        doc.setTextColor(...GREY);
        doc.setFont('helvetica', 'normal');
        doc.setFontSize(8.5);
        doc.text(m.metric, margin + 4, y);
        doc.setTextColor(...INK);
        doc.text(`${fmt(m.baseline)}  →  ${fmt(m.current)}`, margin + 92, y);

        const deltaColour = m.within_noise ? GREY
          : m.direction === 'worse' ? VERDICT_COLOUR.fail
          : m.direction === 'better' ? VERDICT_COLOUR.pass
          : INK;
        doc.setTextColor(...deltaColour);
        doc.setFont('helvetica', 'bold');
        const pct = m.pct === null ? '' : ` (${m.pct > 0 ? '+' : ''}${m.pct}%)`;
        const qualifier = m.delta === 0 ? ''
          : m.within_noise ? '  within noise'
          : !m.noise_known ? '  no spread'
          : '';
        doc.text(
          `${m.delta > 0 ? '+' : ''}${fmt(m.delta)}${pct}${qualifier}`,
          pageW - margin, y, { align: 'right' },
        );
        y += 5;
      }
      y += 2;
    }

    need(10);
    doc.setTextColor(...GREY);
    doc.setFont('helvetica', 'italic');
    doc.setFontSize(7.5);
    doc.text(
      doc.splitTextToSize(
        '"Within noise" means the change is no larger than the baseline\'s own seed-to-seed spread, '
        + 'so it is not evidence of a real difference. "No spread" means the baseline ran that '
        + 'scenario once and the question cannot be answered — use 2+ repeats.', contentW),
      margin, y,
    );
  }

  const slug = (record.name || 'campaign')
    .toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'campaign';
  doc.save(`${slug}-campaign-report.pdf`);
}
