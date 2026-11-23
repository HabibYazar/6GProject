/**
 * What a run MEASURED, in words rather than field paths.
 *
 * The campaign threshold picker had a short list of these; the trend chart had
 * none and showed raw keys like `measurements.RRU.PrbUsedDl.mean`, which does
 * not say what it is or what it is in. Both now read from here, so a metric is
 * named once and named the same way everywhere.
 *
 * Three rules the list follows:
 *
 *   · the LABEL says what it is, the UNIT says what it is in, and neither
 *     repeats the field path — a reader who knows the 3GPP counter name can
 *     still see it in the hint
 *   · metrics are GROUPED by the question they answer, because a flat list of
 *     twenty is not more useful than a flat list of nine
 *   · a metric that cannot be trusted says so in its own label. PDCP delay
 *     swings 14x across repeats of the SAME config and seed (41-574 ms,
 *     against 1.1x for throughput) because it is four or five samples and the
 *     first carries attach-time buffering. It stays selectable — it is real
 *     data — but nothing should be judged on it, and the list says that where
 *     the choice is made rather than in a document nobody opens.
 */
export interface MetricPreset {
  /** Dotted path into a run summary. */
  path: string;
  label: string;
  unit?: string;
  hint: string;
  group: string;
}

export const METRIC_PRESETS: MetricPreset[] = [
  // ── What the network carried ───────────────────────────────────────────────
  { path: 'dl_mbps_mean', label: 'Downlink, whole network', unit: 'Mbps',
    hint: 'summed over every UE, averaged over the run', group: 'Throughput' },
  { path: 'dl_mbps_max', label: 'Downlink, best window', unit: 'Mbps',
    hint: 'the highest single KPM window', group: 'Throughput' },
  { path: 'measurements.DRB.UEThpDl.mean', label: 'Downlink per UE', unit: 'kbps',
    hint: 'DRB.UEThpDl — the per-UE counter, not the network total. The engine '
        + 'computes it as mean PDU size over mean RLC DELAY, so it is bits over a '
        + 'latency rather than over time: it reads 13 Mbps on a link whose delivered '
        + 'volume is 36. Use Bytes delivered for a rate.', group: 'Throughput' },
  { path: 'measurements.DRB.PdcpSduBitRateDl.mean', label: 'PDCP bitrate, downlink', unit: 'kbps',
    hint: 'DRB.PdcpSduBitRateDl — measured at PDCP, above the MAC', group: 'Throughput' },
  { path: 'measurements.DRB.PdcpSduBitRateUl.mean', label: 'PDCP bitrate, uplink', unit: 'kbps',
    hint: 'DRB.PdcpSduBitRateUl', group: 'Throughput' },
  { path: 'measurements.DRB.PdcpSduVolumeDl.mean', label: 'Data delivered, downlink', unit: 'kB',
    hint: 'DRB.PdcpSduVolumeDl per window', group: 'Throughput' },

  // ── How good the radio was ─────────────────────────────────────────────────
  { path: 'measurements.L3servingSINR.mean', label: 'Serving-cell SINR', unit: 'dB',
    hint: 'L3servingSINR — the serving link only, not the best neighbour', group: 'Radio quality' },
  { path: 'measurements.TB.TotNbrDlInitial.64Qam.mean', label: 'Blocks sent at 64QAM', unit: 'count',
    hint: 'a good link earns a high modulation; this is how often it did', group: 'Radio quality' },
  { path: 'measurements.TB.TotNbrDlInitial.Qpsk.mean', label: 'Blocks sent at QPSK', unit: 'count',
    hint: 'the fallback modulation — rises when the link degrades', group: 'Radio quality' },
  { path: 'measurements.TB.ErrTotalNbrDl.1.mean', label: 'Transport blocks in error', unit: 'count',
    hint: 'TB.ErrTotalNbrDl — against the total below, this is the block error rate',
    group: 'Radio quality' },
  { path: 'measurements.TB.TotNbrDl.1.mean', label: 'Transport blocks sent', unit: 'count',
    hint: 'TB.TotNbrDl — the denominator for the error count', group: 'Radio quality' },

  // ── How hard the cells were working ────────────────────────────────────────
  { path: 'measurements.RRU.PrbUsedDl.mean', label: 'Spectrum in use, downlink', unit: 'share',
    hint: 'RRU.PrbUsedDl — NOT resource blocks. The engine reports a share of the '
        + "cell's OFDM symbols scaled by 139, a block count hardcoded for 100 MHz at "
        + '60 kHz ("TODO fix this for different numerologies", its own comment). It '
        + 'also under-reports: the bytes it delivered need more symbols than it says '
        + 'it used, by up to 1.57x. Read it as a relative load, not a count.',
    group: 'Cell load' },
  { path: 'measurements.DRB.BufferSize.Qos.mean', label: 'Data waiting in the queue', unit: 'bytes',
    hint: 'DRB.BufferSize.Qos — a rising queue means the cell cannot keep up',
    group: 'Cell load' },
  { path: 'ue_count_mean', label: 'UEs connected', unit: 'count',
    hint: 'averaged over the run — a partitioned run can serve MORE, because it '
        + 'covers more simulated time and more UEs finish attaching', group: 'Cell load' },
  { path: 'energy_final.total_w', label: 'Power, whole network', unit: 'W',
    hint: 'measured from the radio energy model, not derived from the config',
    group: 'Cell load' },

  // ── Mobility and control ───────────────────────────────────────────────────
  { path: 'handovers_total', label: 'Handovers', unit: 'count',
    hint: 'serving-cell changes. Splitting below three cells per group DELETES '
        + 'some of these — compare it against a whole run before reading it',
    group: 'Mobility & control' },
  { path: 'anomalies_fired', label: 'Faults that actually fired', unit: 'count',
    hint: 'a fault scheduled past the run’s simulated horizon never happens, '
        + 'and the run then measures a healthy network and passes',
    group: 'Mobility & control' },
  { path: 'anomalies_configured', label: 'Faults scheduled', unit: 'count',
    hint: 'compare with the line above', group: 'Mobility & control' },

  // ── What the run actually covered ──────────────────────────────────────────
  { path: 'sim_span_s', label: 'Network time covered', unit: 's',
    hint: 'SIMULATED seconds, which is the horizon every other number here is '
        + 'limited to — usually a small fraction of the wall clock',
    group: 'What the run covered' },
  { path: 'ticks', label: 'KPM windows captured', unit: 'count',
    hint: 'about 12 to the simulated second. A PARTITIONED run reports far '
        + 'fewer, because duplicates at one instant collapse when the streams '
        + 'merge — do not compare this across the two', group: 'What the run covered' },

  // ── Real, but do not judge on it ───────────────────────────────────────────
  { path: 'measurements.DRB.PdcpSduDelayDl.mean',
    label: 'PDCP delay, downlink — NOISY', unit: 'ms',
    hint: 'measured to swing 14x across repeats of the SAME config and seed '
        + '(41-574 ms, against 1.1x for throughput): four or five samples, the '
        + 'first carrying attach-time buffering. Judge on throughput instead',
    group: 'Not safe to judge on' },
  { path: 'measurements.DRB.PdcpSduDelayUl.mean',
    label: 'PDCP delay, uplink — NOISY', unit: 'ms',
    hint: 'same sample-count problem as the downlink figure above',
    group: 'Not safe to judge on' },
];

export const METRIC_GROUPS = [...new Set(METRIC_PRESETS.map(m => m.group))];

const BY_PATH = new Map(METRIC_PRESETS.map(m => [m.path, m]));

export function metricPreset(path: string): MetricPreset | undefined {
  return BY_PATH.get(path);
}

/** "Serving-cell SINR (dB)", or the raw path when the metric is not in the list. */
export function metricLabel(path: string): string {
  const m = BY_PATH.get(path);
  if (!m) return path;
  return m.unit ? `${m.label} (${m.unit})` : m.label;
}
