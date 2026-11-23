/**
 * One way to print a radio unit's power draw.
 *
 * `power_w` is the RU's real consumption off the engine's own state currents
 * — `GetStateA(state) * 5 V` — so a transmitting cell is about 3 kW, an idle
 * one 680 W, asleep 450-580 W and switched off 0 W. A six-cell network is
 * therefore 17,000-odd watts, and four separate places printed that as
 * "17246 W".
 *
 * The fast tier used to report the RADIATED power here instead (10 W at
 * 40 dBm), so only the slow tier's cards ever showed the kilowatt scale and
 * the two consoles disagreed by a factor of three hundred. They agree now,
 * which is exactly why the formatting has to be shared rather than repeated.
 */
export function formatWatts(w: number | null | undefined, decimals = 0): string {
  if (w === null || w === undefined || !Number.isFinite(w)) return '—';
  if (Math.abs(w) >= 1000) return `${(w / 1000).toFixed(w >= 10000 ? 1 : 2)} kW`;
  return `${w.toFixed(decimals)} W`;
}

/** The pieces, for cards that lay the value and the unit out themselves. */
export function wattParts(w: number | null | undefined): { value: string; unit: string } {
  if (w === null || w === undefined || !Number.isFinite(w)) return { value: '—', unit: 'W' };
  if (Math.abs(w) >= 1000) {
    return { value: (w / 1000).toFixed(w >= 10000 ? 1 : 2), unit: 'kW' };
  }
  return { value: w.toFixed(0), unit: 'W' };
}
