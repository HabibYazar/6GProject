/* Map "Import buildings" — the sparse-area cases.
 *
 * The button had ONE catch-all message, "Import failed — pan/zoom and retry", which was
 * advice that cannot work: the Overpass query is bound to config.area, so panning the map
 * changes nothing about it. Remote areas hit several distinct causes (a bbox the far-north
 * cos(lat) transform pushes past ±180, a busy public server, footprints the size filter
 * eats, or genuinely nothing mapped) and each has a different remedy.
 *
 * This tests the REAL NetworkMap.tsx, not a copy: it reads the file off disk, slices out
 * the pure helpers by name, strips the TS types with the project's own tsc, and runs them,
 * so the test sees the file change. The fetch path needs a browser and is not covered —
 * run the app and click the button for that.
 *
 *   node tools/test_map_import.mjs
 */
import fs from 'fs';
import ts from '/home/chancelor/nsg_ai_platform/ui/node_modules/typescript/lib/typescript.js';

const src = fs.readFileSync('/home/chancelor/nsg_ai_platform/ui/components/NetworkMap.tsx', 'utf8');

// Slice from a declaration's first line to the line that closes it at column 0.
function slice(name) {
  const lines = src.split('\n');
  const i = lines.findIndex(l => l.startsWith(name));
  if (i < 0) throw new Error(`not found in the real file: ${name}`);
  if (/^(const|type)/.test(name)) {              // single- or multi-line const/type
    let j = i;
    while (!lines[j].trimEnd().endsWith(';')) j++;
    return lines.slice(i, j + 1).join('\n');
  }
  let j = i;
  while (lines[j] !== '}') j++;
  return lines.slice(i, j + 1).join('\n');
}

const parts = [
  'const M_PER_DEG_LAT',
  'const OVERPASS_ENDPOINTS',
  'function metersToLatLon',
  'function latLonToMeters',
  'function simAreaBbox',
  'function osmBuildingType',
  'function osmElementsToBuildings',
].map(slice).join('\n\n');

const js = ts.transpileModule(
  parts + '\nexport { metersToLatLon, simAreaBbox, osmElementsToBuildings, OVERPASS_ENDPOINTS };',
  { compilerOptions: { target: 'ES2020', module: 'ESNext' } }).outputText;
const mod = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));
const { simAreaBbox, osmElementsToBuildings, OVERPASS_ENDPOINTS } = mod;

let fails = 0;
const check = (name, cond, detail = '') => {
  console.log(`${cond ? '  ok  ' : 'FAIL  '}${name}${detail ? '   ' + detail : ''}`);
  if (!cond) fails++;
};

// ---- bbox validity -------------------------------------------------------
const AREA = { x_min: -500, y_min: -500, x_max: 500, y_max: 500 };
console.log('== simAreaBbox ==');
for (const [name, anchor, want] of [
  ['Istanbul',            { lat: 41.0082, lon: 28.9784 }, true],
  ['Konya farmland',      { lat: 38.20,   lon: 32.90   }, true],
  ['Sahara',              { lat: 23.00,   lon: 12.00   }, true],
  ['Svalbard 78N',        { lat: 78.22,   lon: 15.63   }, true],
  ['far north 89.999N',   { lat: 89.999,  lon: 0       }, false],
  ['north pole',          { lat: 90,      lon: 0       }, false],
  ['near antimeridian',   { lat: 65,      lon: 179.999 }, false],
]) {
  const bb = simAreaBbox(AREA, anchor);
  check(name, (bb !== null) === want, bb ? `S${bb.south.toFixed(3)} W${bb.west.toFixed(3)} N${bb.north.toFixed(3)} E${bb.east.toFixed(3)}` : 'rejected');
}
const degen = simAreaBbox({ x_min: 0, y_min: 0, x_max: 0, y_max: 0 }, { lat: 41, lon: 29 });
check('degenerate zero-size area rejected', degen === null);

// ---- parser on sparse-area payloads --------------------------------------
console.log('\n== osmElementsToBuildings ==');
const anchor = { lat: 38.20, lon: 32.90 };
const M = 111320, cos = Math.cos(anchor.lat * Math.PI / 180);
const sq = (id, sideM, tags = { building: 'yes' }) => {
  const dLat = sideM / M, dLon = sideM / (M * cos);
  return { type: 'way', id, tags, geometry: [
    { lat: anchor.lat, lon: anchor.lon }, { lat: anchor.lat + dLat, lon: anchor.lon },
    { lat: anchor.lat + dLat, lon: anchor.lon + dLon }, { lat: anchor.lat, lon: anchor.lon + dLon }] };
};

let r = osmElementsToBuildings([], anchor);
check('empty response -> no crash, nothing dropped', r.buildings.length === 0 && r.tooSmall === 0 && r.tooBig === 0);

r = osmElementsToBuildings([sq(1, 3), sq(2, 2.5)], anchor);
check('rural sheds under 4 m -> counted as tooSmall', r.buildings.length === 0 && r.tooSmall === 2, JSON.stringify(r).slice(0, 60));

r = osmElementsToBuildings([sq(3, 600), sq(4, 450)], anchor);
check('greenhouses over 400 m -> counted as tooBig', r.buildings.length === 0 && r.tooBig === 2, JSON.stringify(r).slice(0, 60));

r = osmElementsToBuildings([sq(5, 20), sq(6, 2), sq(7, 900)], anchor);
check('mixed -> 1 kept, 1 small, 1 big', r.buildings.length === 1 && r.tooSmall === 1 && r.tooBig === 1);

r = osmElementsToBuildings([{ type: 'relation', id: 8, geometry: [] }, { type: 'way', id: 9 },
                            { type: 'way', id: 10, geometry: [{ lat: 38.2, lon: 32.9 }] }], anchor);
check('relations / missing or short geometry -> skipped, not crashed', r.buildings.length === 0);

r = osmElementsToBuildings([sq(11, 20, { building: 'yes', height: '0' }), sq(12, 20, { building: 'barn' })], anchor);
const zs = r.buildings.map(b => b.bounds.z_max);
check('no usable height -> non-zero default, never a flat building', zs.length === 2 && zs.every(z => z > 0), `z_max=${zs}`);

check('a mirror is configured', OVERPASS_ENDPOINTS.length >= 2, OVERPASS_ENDPOINTS.join(' '));

console.log(fails ? `\n${fails} FAILED` : '\nall passed');
process.exit(fails ? 1 : 0);
