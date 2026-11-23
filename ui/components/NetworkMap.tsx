'use client';

/**
 * Digital-twin map view — renders the scenario's cells + UE groups on a real
 * OpenStreetMap base, anchoring the abstract (metre) coordinates to a chosen real
 * location. Pure design / visualisation layer: it reads the same NSGSimulationConfig
 * and the engines stay metre-based (x = East m, y = North m). Toggled from Visual.
 *
 * Uses Leaflet (raster tiles, no WebGL) so it renders on any browser.
 *
 * v3 "Trust & area": measure tool (click two points → metres), the sim area drawn
 * as a rectangle, anchor ANY location (map centre or place search, not just presets),
 * and co-located sectors grouped into one site marker so they don't stack or scatter.
 */
import { useEffect, useRef, useState } from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import { MapPin, Move, Building2, Loader2, Ruler, Crosshair, Search, X } from 'lucide-react';
import type { NSGSimulationConfig, Building, BuildingType, BaseStation } from '@/types/nsg';
import { useNSGStore } from '@/store/useNSGStore';

interface Anchor { name: string; lat: number; lon: number }

/** Anchor presets — quick jumps to a real neighbourhood. */
const ANCHORS: Anchor[] = [
  { name: 'İstanbul · Levent', lat: 41.0825, lon: 29.0100 },
  { name: 'London · City', lat: 51.5155, lon: -0.0922 },
  { name: 'New York · Midtown', lat: 40.7549, lon: -73.9840 },
  { name: 'Paris · La Défense', lat: 48.8918, lon: 2.2410 },
];

const M_PER_DEG_LAT = 111320;

// Sim-area size limits (metres per side). MIN ≈ a single small cell + its UE;
// MAX ≈ industry macro-network scale (e.g. a 3GPP ~19-site hex layout). Beyond MAX
// the REM grid (area ÷ resolution_m) gets heavy. Retune here — single source of truth.
const AREA_MIN_M = 300;
const AREA_MAX_M = 5000;

/** Clamp one axis of the box: keep the `fixed` edge, size the dragged edge into [min,max]. */
function clampEdge(dragged: number, fixed: number, min: number, max: number): [number, number] {
  const size = Math.max(min, Math.min(max, Math.abs(dragged - fixed)));
  return dragged < fixed ? [fixed - size, fixed] : [fixed, fixed + size];
}

/** (x East m, y North m) → [lat, lon] around an anchor (equirectangular, city-scale). */
function metersToLatLon(x: number, y: number, a: { lat: number; lon: number }): [number, number] {
  const lat = a.lat + y / M_PER_DEG_LAT;
  const lon = a.lon + x / (M_PER_DEG_LAT * Math.cos((a.lat * Math.PI) / 180));
  return [lat, lon];
}

/** Inverse of metersToLatLon: [lat, lon] → (x East m, y North m) around an anchor. */
function latLonToMeters(lat: number, lon: number, a: { lat: number; lon: number }): { x: number; y: number } {
  const y = (lat - a.lat) * M_PER_DEG_LAT;
  const x = (lon - a.lon) * M_PER_DEG_LAT * Math.cos((a.lat * Math.PI) / 180);
  return { x: Math.round(x), y: Math.round(y) };
}

const fmtDist = (m: number) => (m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`);

/** What the fine-placement panel is currently editing. `ids` covers all co-located sectors. */
type Selected = { kind: 'cell' | 'ue'; ids: string[]; label: string };

/** Distance (m) from a point to a building's axis-aligned footprint (0 if inside it). */
function distToBuilding(px: number, py: number, b: Building): number {
  const dx = Math.max(0, b.bounds.x_min - px, px - b.bounds.x_max);
  const dy = Math.max(0, b.bounds.y_min - py, py - b.bounds.y_max);
  return Math.hypot(dx, dy);
}
function nearestBuilding(px: number, py: number, buildings: Building[]): { d: number; b: Building } | null {
  let best: { d: number; b: Building } | null = null;
  for (const b of buildings) { const d = distToBuilding(px, py, b); if (!best || d < best.d) best = { d, b }; }
  return best;
}

/** Map an OSM `building=*` tag onto our coarse building_type. */
function osmBuildingType(v?: string): BuildingType {
  if (!v) return 'residential';
  if (/office|commercial|retail|hotel|hospital|university|school|public/.test(v)) return 'office';
  if (/industrial|warehouse|factory|manufacture/.test(v)) return 'industrial';
  return 'residential';
}

/** Public Overpass instances rate-limit and time out under load. A remote area is not a
 *  lighter query for them — it is the same queue — so a refusal there is the server's
 *  state, not the scenario's. Fall through the mirrors before reporting failure. */
const OVERPASS_ENDPOINTS = [
  'https://overpass-api.de/api/interpreter',
  'https://overpass.kumi.systems/api/interpreter',
];

/** The sim area as a lat/lon bbox, or null if it is not one Overpass will accept.
 *  `metersToLatLon` divides by cos(lat), so a far-north anchor (Svalbard, Greenland —
 *  exactly the empty places you click on a world map) can push `lon` past ±180 and the
 *  request comes back as a server error the UI then blames on the area. */
function simAreaBbox(a: { x_min: number; y_min: number; x_max: number; y_max: number },
                     anchor: { lat: number; lon: number }) {
  const [south, west] = metersToLatLon(a.x_min, a.y_min, anchor);
  const [north, east] = metersToLatLon(a.x_max, a.y_max, anchor);
  const latOk = (v: number) => Number.isFinite(v) && v >= -90 && v <= 90;
  const lonOk = (v: number) => Number.isFinite(v) && v >= -180 && v <= 180;
  if (!latOk(south) || !latOk(north) || !lonOk(west) || !lonOk(east)) return null;
  if (!(south < north) || !(west < east)) return null;
  return { south, west, north, east };
}

type OsmWay = { type: string; id: number; geometry?: { lat: number; lon: number }[]; tags?: Record<string, string> };
/** Footprints plus a tally of what was thrown away. In a sparse area the size filter is
 *  the usual reason nothing lands — farm sheds fall under 4 m and barns/greenhouses run
 *  past 400 m — and "0 imported" reads like a failure unless we say which filter ate them. */
function osmElementsToBuildings(elements: OsmWay[], anchor: { lat: number; lon: number }, max = 80):
    { buildings: Building[]; tooSmall: number; tooBig: number } {
  const out: Building[] = [];
  let tooSmall = 0, tooBig = 0;
  for (const el of elements) {
    if (el.type !== 'way' || !el.geometry || el.geometry.length < 3) continue;
    let minLat = Infinity, maxLat = -Infinity, minLon = Infinity, maxLon = -Infinity;
    for (const g of el.geometry) {
      minLat = Math.min(minLat, g.lat); maxLat = Math.max(maxLat, g.lat);
      minLon = Math.min(minLon, g.lon); maxLon = Math.max(maxLon, g.lon);
    }
    const p1 = latLonToMeters(minLat, minLon, anchor);
    const p2 = latLonToMeters(maxLat, maxLon, anchor);
    const x_min = Math.min(p1.x, p2.x), x_max = Math.max(p1.x, p2.x);
    const y_min = Math.min(p1.y, p2.y), y_max = Math.max(p1.y, p2.y);
    const w = x_max - x_min, d = y_max - y_min;
    if (w < 4 || d < 4) { tooSmall++; continue; }        // slivers
    if (w > 400 || d > 400) { tooBig++; continue; }      // mega-structures
    const tags = el.tags ?? {};
    const levels = parseInt(tags['building:levels'] ?? '', 10);
    const heightM = parseFloat(tags['height'] ?? '') || (levels > 0 ? levels * 3 : 9);
    out.push({
      id: `osm-${el.id}`,
      bounds: { x_min, x_max, y_min, y_max, z_min: 0, z_max: Math.round(heightM) },
      building_type: osmBuildingType(tags.building),
      wall_material: 'concrete',
      floors: levels > 0 ? levels : Math.max(1, Math.round(heightM / 3)),
      rooms_x: Math.max(1, Math.min(12, Math.round(w / 6))),
      rooms_y: Math.max(1, Math.min(12, Math.round(d / 6))),
    });
  }
  out.sort((a, b) =>
    (b.bounds.x_max - b.bounds.x_min) * (b.bounds.y_max - b.bounds.y_min) -
    (a.bounds.x_max - a.bounds.x_min) * (a.bounds.y_max - a.bounds.y_min));
  return { buildings: out.slice(0, max), tooSmall, tooBig };
}

const esc = (s: string) => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c] as string));

function cellIcon(label: string): L.DivIcon {
  const dot = '<div style="width:24px;height:24px;border-radius:50%;background:#14b8a6;border:2px solid #0f766e;box-shadow:0 2px 8px rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;font-size:12px;">📡</div>';
  const tag = `<span style="margin-top:2px;font-size:10px;font-weight:700;color:#fff;background:rgba(20,184,166,.92);padding:1px 5px;border-radius:4px;white-space:nowrap;">${esc(label)}</span>`;
  return L.divIcon({ html: `<div style="display:flex;flex-direction:column;align-items:center;transform:translate(-50%,-50%);">${dot}${tag}</div>`, className: '', iconSize: [0, 0] });
}

function ueIcon(label: string): L.DivIcon {
  const dot = '<div style="width:15px;height:15px;border-radius:50%;background:#a78bfa;border:2px solid #7c3aed;box-shadow:0 2px 6px rgba(0,0,0,.4);"></div>';
  const tag = `<span style="margin-top:2px;font-size:9px;font-weight:700;color:#fff;background:rgba(167,139,250,.92);padding:1px 5px;border-radius:4px;white-space:nowrap;">${esc(label)}</span>`;
  return L.divIcon({ html: `<div style="display:flex;flex-direction:column;align-items:center;transform:translate(-50%,-50%);">${dot}${tag}</div>`, className: '', iconSize: [0, 0] });
}

function measureLabelIcon(m: number): L.DivIcon {
  const html = `<div style="transform:translate(-50%,-50%);background:rgba(56,189,248,.96);color:#04283b;font-size:11px;font-weight:800;padding:2px 6px;border-radius:5px;white-space:nowrap;box-shadow:0 2px 6px rgba(0,0,0,.4);">${fmtDist(m)}</div>`;
  return L.divIcon({ html, className: '', iconSize: [0, 0] });
}

/** Small square corner handle for resizing the sim-area box. */
function boxHandleIcon(): L.DivIcon {
  return L.divIcon({ html: '<div style="width:12px;height:12px;background:#fff;border:2px solid #0d9488;border-radius:2px;transform:translate(-50%,-50%);box-shadow:0 1px 4px rgba(0,0,0,.45);"></div>', className: '', iconSize: [0, 0] });
}
/** Round move handle (top-centre of the box) for relocating the whole scenario. */
function boxMoveIcon(): L.DivIcon {
  const svg = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="5 9 2 12 5 15"/><polyline points="9 5 12 2 15 5"/><polyline points="15 19 12 22 9 19"/><polyline points="19 9 22 12 19 15"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="12" y1="2" x2="12" y2="22"/></svg>';
  return L.divIcon({ html: `<div style="width:26px;height:26px;background:rgba(20,184,166,.92);border:2px solid #0f766e;border-radius:50%;display:flex;align-items:center;justify-content:center;transform:translate(-50%,-50%);box-shadow:0 2px 6px rgba(0,0,0,.4);cursor:move;">${svg}</div>`, className: '', iconSize: [0, 0] });
}
function simAreaLabelIcon(): L.DivIcon {
  return L.divIcon({ html: '<span style="transform:translate(10px,-3px);display:inline-block;font-size:9px;font-weight:700;color:#5eead4;background:rgba(4,47,46,.8);padding:1px 4px;border-radius:3px;white-space:nowrap;">sim area · drag ✥ / corners</span>', className: '', iconSize: [0, 0] });
}

/** Common site name for a group of sectors, e.g. "Site-A / N77 / S1" → "Site-A". */
function siteName(grp: BaseStation[]): string {
  const first = grp[0].name.split('/')[0].trim();
  return first || grp[0].name;
}

export default function NetworkMap({ config }: { config: NSGSimulationConfig }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const layerRef = useRef<L.LayerGroup | null>(null);
  const measureLayerRef = useRef<L.LayerGroup | null>(null);

  // Anchor persists in localStorage so switching Abstract⇄Map (which unmounts this
  // component) or reloading keeps the chosen location instead of snapping to İstanbul.
  const [anchor, setAnchorRaw] = useState<Anchor>(() => {
    if (typeof window === 'undefined') return ANCHORS[0];
    try { const r = localStorage.getItem('nsg-map-anchor'); return r ? (JSON.parse(r) as Anchor) : ANCHORS[0]; } catch { return ANCHORS[0]; }
  });
  const setAnchor = (a: Anchor) => {
    setAnchorRaw(a);
    try { localStorage.setItem('nsg-map-anchor', JSON.stringify(a)); } catch { /* storage disabled */ }
  };
  const setConfig = useNSGStore(s => s.setConfig);
  const liveKpm = useNSGStore(s => s.ricRealKpm);   // REAL UEs (live session)

  // Latest config/anchor for handlers (avoids stale closures across redraws).
  const configRef = useRef(config); configRef.current = config;
  const anchorRef = useRef(anchor); anchorRef.current = anchor;
  // Fit the view on first draw + deliberate anchor jumps, never on a drag / "use centre".
  const fitRef = useRef(true);

  const [importing, setImporting] = useState(false);
  const [importMsg, setImportMsg] = useState<string | null>(null);

  // Measure tool
  const [measuring, setMeasuring] = useState(false);
  const [measureDist, setMeasureDist] = useState<number | null>(null);
  const measuringRef = useRef(false);
  const measurePtsRef = useRef<L.LatLng[]>([]);

  // Place search
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<{ name: string; lat: number; lon: number }[]>([]);
  const [searching, setSearching] = useState(false);

  // Fine placement: selection + live drag readout
  const [selected, setSelected] = useState<Selected | null>(null);
  const selectedRef = useRef<Selected | null>(null); selectedRef.current = selected;
  const selectionLayerRef = useRef<L.LayerGroup | null>(null);
  const liveLayerRef = useRef<L.LayerGroup | null>(null);   // REAL UEs from the live session
  const [dragging, setDragging] = useState(false);
  const readoutRef = useRef<HTMLDivElement>(null);

  const setAnchorAndFit = (a: Anchor, fit: boolean) => {
    fitRef.current = fit;
    setAnchor(a);
    // OSM-imported buildings (id "osm-…") belong to the OLD location — drop them on a
    // location change so they don't misrepresent the new area. Re-import fetches the new
    // location's buildings. Manually/AI-authored buildings are kept.
    const c = configRef.current;
    if (c && c.buildings.some(b => b.id.startsWith('osm-'))) {
      setConfig({ ...c, buildings: c.buildings.filter(b => !b.id.startsWith('osm-')) });
      setImportMsg('Location changed — old buildings cleared, re-import for here');
    }
  };

  const updateReadout = (ll: L.LatLng) => {
    const p = latLonToMeters(ll.lat, ll.lng, anchorRef.current);
    const nb = nearestBuilding(p.x, p.y, configRef.current?.buildings ?? []);
    const near = nb ? (nb.d < 1 ? 'on a building' : `nearest bldg ${Math.round(nb.d)} m`) : 'no buildings';
    if (readoutRef.current) readoutRef.current.textContent = `x ${p.x} · y ${p.y} m · ${near}`;
  };

  const nudge = (axis: 'x' | 'y', delta: number) => {
    const sel = selectedRef.current, c = configRef.current;
    if (!sel || !c) return;
    const idSet = new Set(sel.ids);
    if (sel.kind === 'cell') setConfig({ ...c, base_stations: c.base_stations.map(bs => idSet.has(bs.id) ? { ...bs, position: { ...bs.position, [axis]: bs.position[axis] + delta } } : bs) });
    else setConfig({ ...c, ue_groups: c.ue_groups.map(u => idSet.has(u.id) ? { ...u, spawn_position: { ...u.spawn_position, [axis]: u.spawn_position[axis] + delta } } : u) });
  };

  // Snap the selected cell onto the nearest building (footprint centre, rooftop height).
  const snapToNearestBuilding = () => {
    const sel = selectedRef.current, c = configRef.current;
    if (!sel || sel.kind !== 'cell' || !c) return;
    const bs0 = c.base_stations.find(b => b.id === sel.ids[0]);
    const nb = bs0 ? nearestBuilding(bs0.position.x, bs0.position.y, c.buildings) : null;
    if (!nb) return;
    const cx = Math.round((nb.b.bounds.x_min + nb.b.bounds.x_max) / 2);
    const cy = Math.round((nb.b.bounds.y_min + nb.b.bounds.y_max) / 2);
    const idSet = new Set(sel.ids);
    setConfig({ ...c, base_stations: c.base_stations.map(bs => idSet.has(bs.id) ? { ...bs, position: { x: cx, y: cy, z: nb.b.bounds.z_max } } : bs) });
  };

  // Init the map once.
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = L.map(containerRef.current, { zoomControl: false, attributionControl: true }).setView(
      [anchorRef.current.lat, anchorRef.current.lon], 15,
    );
    L.control.zoom({ position: 'bottomright' }).addTo(map);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap contributors' }).addTo(map);
    layerRef.current = L.layerGroup().addTo(map);
    measureLayerRef.current = L.layerGroup().addTo(map);
    selectionLayerRef.current = L.layerGroup().addTo(map);
    liveLayerRef.current = L.layerGroup().addTo(map);
    mapRef.current = map;

    // Measure: click two points → draw a line + real great-circle distance (metres).
    map.on('click', (e: L.LeafletMouseEvent) => {
      if (!measuringRef.current) { setSelected(null); return; } // click on empty map deselects
      const ml = measureLayerRef.current;
      if (!ml) return;
      const pts = measurePtsRef.current;
      const dot = (p: L.LatLng) => L.circleMarker(p, { radius: 5, color: '#0ea5e9', weight: 2, fillColor: '#38bdf8', fillOpacity: 1 });
      if (pts.length !== 1) {
        // start a fresh measurement
        ml.clearLayers();
        measurePtsRef.current = [e.latlng];
        dot(e.latlng).addTo(ml);
        setMeasureDist(null);
      } else {
        const a = pts[0], b = e.latlng;
        measurePtsRef.current = [a, b];
        dot(b).addTo(ml);
        L.polyline([a, b], { color: '#38bdf8', weight: 2, dashArray: '5,6' }).addTo(ml);
        const d = map.distance(a, b);
        L.marker(L.latLng((a.lat + b.lat) / 2, (a.lng + b.lng) / 2), { icon: measureLabelIcon(d), interactive: false }).addTo(ml);
        setMeasureDist(d);
      }
    });

    setTimeout(() => map.invalidateSize(), 0);
    return () => { map.remove(); mapRef.current = null; layerRef.current = null; measureLayerRef.current = null; selectionLayerRef.current = null; liveLayerRef.current = null; };
  }, []);

  // Draw sim area + buildings + cells + UEs whenever the config or anchor changes.
  useEffect(() => {
    const map = mapRef.current;
    const layer = layerRef.current;
    if (!map || !layer) return;
    layer.clearLayers();
    const pts: [number, number][] = [];

    const commitDrag = (ids: string[], kind: 'cell' | 'ue', marker: L.Marker) => {
      const { lat, lng } = marker.getLatLng();
      const { x, y } = latLonToMeters(lat, lng, anchorRef.current);
      const c = configRef.current;
      if (!c) return;
      const idSet = new Set(ids);
      if (kind === 'cell') {
        setConfig({ ...c, base_stations: c.base_stations.map(bs => idSet.has(bs.id) ? { ...bs, position: { ...bs.position, x, y } } : bs) });
      } else {
        setConfig({ ...c, ue_groups: c.ue_groups.map(u => idSet.has(u.id) ? { ...u, spawn_position: { x, y } } : u) });
      }
    };

    // Editable sim-area box: the dashed rectangle is config.area projected onto the map.
    // Drag the ✥ handle (top-centre) to relocate the whole scenario (re-anchors so cells
    // + UEs move with it); drag a corner to resize config.area.
    const area = config.area;
    const areaSW = metersToLatLon(area.x_min, area.y_min, anchor);
    const areaNE = metersToLatLon(area.x_max, area.y_max, anchor);
    const boxRect = L.rectangle([areaSW, areaNE], { color: '#14b8a6', weight: 1.5, opacity: 0.7, dashArray: '7,6', fill: false, interactive: false }).addTo(layer);
    pts.push(areaSW, areaNE);

    const cornersM: Record<string, [number, number]> = {
      sw: [area.x_min, area.y_min], se: [area.x_max, area.y_min],
      ne: [area.x_max, area.y_max], nw: [area.x_min, area.y_max],
    };
    const opposite: Record<string, string> = { sw: 'ne', se: 'nw', ne: 'sw', nw: 'se' };
    (['sw', 'se', 'ne', 'nw'] as const).forEach(k => {
      const oppM = cornersM[opposite[k]];
      const h = L.marker(metersToLatLon(cornersM[k][0], cornersM[k][1], anchor), { icon: boxHandleIcon(), draggable: true, zIndexOffset: 900 }).addTo(layer);
      // New box bounds from the dragged corner, each side clamped to [AREA_MIN_M, AREA_MAX_M].
      const clamped = () => {
        const p = latLonToMeters(h.getLatLng().lat, h.getLatLng().lng, anchorRef.current);
        const [x_min, x_max] = clampEdge(p.x, oppM[0], AREA_MIN_M, AREA_MAX_M);
        const [y_min, y_max] = clampEdge(p.y, oppM[1], AREA_MIN_M, AREA_MAX_M);
        return { x_min, x_max, y_min, y_max };
      };
      h.on('drag', () => {
        const b = clamped();
        boxRect.setBounds(L.latLngBounds(metersToLatLon(b.x_min, b.y_min, anchorRef.current), metersToLatLon(b.x_max, b.y_max, anchorRef.current)));
      });
      h.on('dragend', () => {
        const c = configRef.current; if (!c) return;
        const nb = clamped();
        const oldA = c.area;
        const sx = (nb.x_max - nb.x_min) / (oldA.x_max - oldA.x_min);
        const sy = (nb.y_max - nb.y_min) / (oldA.y_max - oldA.y_min);
        // Cells + UEs keep their RELATIVE position in the box; inter-object distance
        // scales with the area. Buildings stay put (real OSM footprints).
        const scale = (px: number, py: number) => ({
          x: Math.round(nb.x_min + (px - oldA.x_min) * sx),
          y: Math.round(nb.y_min + (py - oldA.y_min) * sy),
        });
        setConfig({
          ...c,
          area: { ...c.area, ...nb },
          base_stations: c.base_stations.map(bs => ({ ...bs, position: { ...bs.position, ...scale(bs.position.x, bs.position.y) } })),
          ue_groups: c.ue_groups.map(u => ({ ...u, spawn_position: scale(u.spawn_position.x, u.spawn_position.y) })),
        });
      });
    });

    // Move handle (top-centre) — relocate the whole scenario by re-anchoring.
    const topC = metersToLatLon((area.x_min + area.x_max) / 2, area.y_max, anchor);
    const mover = L.marker(topC, { icon: boxMoveIcon(), draggable: true, zIndexOffset: 1000 }).addTo(layer);
    mover.on('drag', () => {
      const now = mover.getLatLng();
      const dLat = now.lat - topC[0], dLng = now.lng - topC[1];
      boxRect.setBounds(L.latLngBounds([areaSW[0] + dLat, areaSW[1] + dLng], [areaNE[0] + dLat, areaNE[1] + dLng]));
    });
    mover.on('dragend', () => {
      const now = mover.getLatLng();
      const a = anchorRef.current;
      setAnchor({ name: 'Custom · placed', lat: a.lat + (now.lat - topC[0]), lon: a.lon + (now.lng - topC[1]) });
    });
    L.marker(metersToLatLon(area.x_min, area.y_max, anchor), { icon: simAreaLabelIcon(), interactive: false }).addTo(layer);

    // Buildings (metre bounding boxes) — under the cells/UEs.
    config.buildings.forEach(b => {
      const c1 = metersToLatLon(b.bounds.x_min, b.bounds.y_min, anchor);
      const c2 = metersToLatLon(b.bounds.x_max, b.bounds.y_max, anchor);
      L.rectangle([c1, c2], { color: '#f59e0b', weight: 1, opacity: 0.5, fillColor: '#f59e0b', fillOpacity: 0.18, interactive: false }).addTo(layer);
    });

    // Group co-located sectors (same mast) into one marker so they don't stack/scatter.
    const groups = new Map<string, BaseStation[]>();
    for (const bs of config.base_stations) {
      const k = `${Math.round(bs.position.x / 3)},${Math.round(bs.position.y / 3)}`;
      const g = groups.get(k);
      if (g) g.push(bs); else groups.set(k, [bs]);
    }
    for (const grp of groups.values()) {
      const lead = grp[0];
      const ll = metersToLatLon(lead.position.x, lead.position.y, anchor);
      pts.push(ll);
      const rings: L.Circle[] = [];
      grp.forEach(bs => {
        const r = Math.max(80, 120 + (bs.radio.tx_power_dbm - 40) * 25);
        rings.push(L.circle(ll, { radius: r, color: '#14b8a6', weight: 1, opacity: 0.4, fillColor: '#14b8a6', fillOpacity: 0.08 }).addTo(layer));
      });
      const label = grp.length > 1 ? `${siteName(grp)} · ×${grp.length}` : lead.name;
      const ids = grp.map(b => b.id);
      const m = L.marker(ll, { icon: cellIcon(label), draggable: true }).addTo(layer);
      m.on('click', () => setSelected({ kind: 'cell', ids, label }));
      m.on('dragstart', () => { setSelected({ kind: 'cell', ids, label }); setDragging(true); });
      m.on('drag', () => { const p = m.getLatLng(); rings.forEach(r => r.setLatLng(p)); updateReadout(p); });
      m.on('dragend', () => { setDragging(false); commitDrag(ids, 'cell', m); });
    }

    config.ue_groups.forEach(ue => {
      const ll = metersToLatLon(ue.spawn_position.x, ue.spawn_position.y, anchor);
      pts.push(ll);
      const m = L.marker(ll, { icon: ueIcon(`${ue.name} · ${ue.count}`), draggable: true }).addTo(layer);
      m.on('click', () => setSelected({ kind: 'ue', ids: [ue.id], label: ue.name }));
      m.on('dragstart', () => { setSelected({ kind: 'ue', ids: [ue.id], label: ue.name }); setDragging(true); });
      m.on('drag', () => updateReadout(m.getLatLng()));
      m.on('dragend', () => { setDragging(false); commitDrag([ue.id], 'ue', m); });
    });

    const shouldFit = fitRef.current;
    fitRef.current = false;
    if (pts.length && shouldFit) map.fitBounds(L.latLngBounds(pts), { padding: [60, 60], maxZoom: 16 });
  }, [config, anchor, setConfig]);

  // REAL UEs from a running ns-O-RAN live session: positions stream in config
  // coordinates, so they anchor with the same transform as the design markers.
  // Own layer + colour = serving cell, so handovers show as colour flips while
  // the dots walk the map with the sim's actual random-walk motion.
  useEffect(() => {
    const ll = liveLayerRef.current;
    if (!ll) return;
    ll.clearLayers();
    const palette = ['#14b8a6', '#38bdf8', '#a78bfa', '#fbbf24', '#f43f5e', '#34d399', '#f97316'];
    for (const u of liveKpm?.ues ?? []) {
      if (u.x == null || u.y == null) continue;
      const color = palette[(u.cell ?? 0) % palette.length];
      L.circleMarker(metersToLatLon(u.x, u.y, anchor), {
        radius: 5, color: '#09090b', weight: 1.5, fillColor: color, fillOpacity: 1,
        interactive: true,
      }).bindTooltip(
        `UE ${u.imsi}${u.slice ? ` · ${u.slice}` : ''} — cell ${u.cell ?? '?'}` +
        `${u.thp_kbps != null ? ` · ${(u.thp_kbps / 1000).toFixed(1)} Mbps` : ''}`,
        { direction: 'top' },
      ).addTo(ll);
    }
  }, [liveKpm, anchor]);

  // Selection highlight ring — its own layer, so selecting doesn't redraw the whole map.
  useEffect(() => {
    const sl = selectionLayerRef.current;
    if (!sl) return;
    sl.clearLayers();
    if (!selected) return;
    let p: { x: number; y: number } | undefined;
    if (selected.kind === 'cell') p = config.base_stations.find(b => b.id === selected.ids[0])?.position;
    else p = config.ue_groups.find(u => u.id === selected.ids[0])?.spawn_position;
    if (!p) return;
    L.circleMarker(metersToLatLon(p.x, p.y, anchor), { radius: 20, color: '#fbbf24', weight: 2.5, fill: false, dashArray: '4,4', interactive: false }).addTo(sl);
  }, [selected, config, anchor]);

  // Pull real building footprints from OpenStreetMap for the current view → config.buildings.
  // Everything here reports WHICH of the five outcomes happened — bad bbox, server refused,
  // server answered with prose, nothing mapped here, everything filtered out — because the
  // remedies differ and the old single "pan/zoom and retry" was advice that cannot work:
  // the query is bound to config.area, so panning the map changes nothing about it.
  const importBuildings = async () => {
    const map = mapRef.current;
    const c = configRef.current;
    if (!map || !c || importing) return;
    setImporting(true);
    setImportMsg(null);
    try {
      // Query ONLY within the sim-area box (config.area), not the whole visible map,
      // so imported buildings belong to the scenario area instead of everything in view.
      const a = c.area;
      const bb = simAreaBbox(a, anchorRef.current);
      if (!bb) { setImportMsg('Sim area is not a valid lat/lon box here — move it away from the pole'); return; }
      const q = `[out:json][timeout:25];(way["building"](${bb.south},${bb.west},${bb.north},${bb.east}););out geom 400;`;

      let json: { elements?: OsmWay[]; remark?: string } | null = null;
      let lastErr = '';
      for (const url of OVERPASS_ENDPOINTS) {
        try {
          const res = await fetch(url, { method: 'POST', body: 'data=' + encodeURIComponent(q) });
          if (!res.ok) { lastErr = `OSM server busy (HTTP ${res.status})`; continue; }
          // Overpass answers an overload or a query error with 200 and a text/HTML body,
          // so a parse failure here is the server talking, not a broken response.
          const text = await res.text();
          try { json = JSON.parse(text); } catch { lastErr = 'OSM server returned an error page'; continue; }
          break;
        } catch { lastErr = 'Cannot reach the OSM server — check the connection'; }
      }
      if (!json) { setImportMsg(`${lastErr || 'Import failed'} — try again in a minute`); return; }
      if (json.remark) { setImportMsg(`OSM: ${json.remark.slice(0, 90)}`); return; }

      const { buildings: all, tooSmall, tooBig } = osmElementsToBuildings((json.elements ?? []) as OsmWay[], anchorRef.current);
      // Keep only buildings whose centre falls inside the sim area (drop edge-straddlers).
      const imported = all.filter(b => {
        const cx = (b.bounds.x_min + b.bounds.x_max) / 2, cy = (b.bounds.y_min + b.bounds.y_max) / 2;
        return cx >= a.x_min && cx <= a.x_max && cy >= a.y_min && cy <= a.y_max;
      });
      // Keep manually/AI-added buildings (you may be planning new construction); replace
      // only the previously OSM-imported ones (id "osm-…").
      const kept = c.buildings.filter(b => !b.id.startsWith('osm-'));
      setConfig({ ...c, buildings: [...kept, ...imported] });
      if (imported.length) { setImportMsg(`${imported.length} imported (+${kept.length} kept)`); return; }
      const dropped = tooSmall + tooBig;
      setImportMsg(
        dropped ? `${dropped} footprint(s) found but filtered out (${tooSmall} under 4 m, ${tooBig} over 400 m) — this area is too sparse for OSM buildings`
                : `Nothing is mapped in OSM here — sparse area, so place buildings by hand or run without them (${kept.length} kept)`);
    } catch (e) {
      setImportMsg(`Import failed: ${e instanceof Error ? e.message : 'unknown error'}`);
    } finally {
      setImporting(false);
    }
  };

  const toggleMeasure = () => {
    const next = !measuring;
    setMeasuring(next);
    measuringRef.current = next;
    measurePtsRef.current = [];
    setMeasureDist(null);
    measureLayerRef.current?.clearLayers();
    const el = mapRef.current?.getContainer();
    if (el) el.style.cursor = next ? 'crosshair' : '';
  };

  const useMapCentre = () => {
    const map = mapRef.current;
    if (!map) return;
    const c = map.getCenter();
    setAnchorAndFit({ name: 'Custom · map centre', lat: c.lat, lon: c.lng }, false);
  };

  const doSearch = async () => {
    const q = query.trim();
    if (!q || searching) return;
    setSearching(true);
    try {
      const res = await fetch(`https://nominatim.openstreetmap.org/search?format=json&limit=5&q=${encodeURIComponent(q)}`);
      const json = (await res.json()) as { display_name: string; lat: string; lon: string }[];
      setResults(json.map(r => ({ name: r.display_name, lat: parseFloat(r.lat), lon: parseFloat(r.lon) })));
    } catch {
      setResults([]);
    } finally {
      setSearching(false);
    }
  };

  const pickResult = (r: { name: string; lat: number; lon: number }) => {
    mapRef.current?.setView([r.lat, r.lon], 16);
    setAnchorAndFit({ name: r.name.split(',')[0], lat: r.lat, lon: r.lon }, false);
    setSearchOpen(false);
    setResults([]);
    setQuery('');
  };

  // Selected object's live position for the fine-placement panel.
  const selCell = selected?.kind === 'cell' ? config.base_stations.find(b => b.id === selected.ids[0]) : null;
  const selUe = selected?.kind === 'ue' ? config.ue_groups.find(u => u.id === selected.ids[0]) : null;
  const selPos = selCell ? selCell.position : selUe ? selUe.spawn_position : null;
  const selLL = selPos ? metersToLatLon(selPos.x, selPos.y, anchor) : null;
  const selNearB = selCell ? nearestBuilding(selCell.position.x, selCell.position.y, config.buildings) : null;
  const nudgeBtn = 'px-1.5 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 text-[10px] font-bold text-zinc-300 transition-colors';

  return (
    <div className="relative w-full h-full">
      <div ref={containerRef} className="w-full h-full z-0" />

      {/* Live drag readout (metres + nearest building) */}
      <div
        ref={readoutRef}
        className={`absolute bottom-12 left-3 z-[500] bg-sky-500/15 border border-sky-500/50 rounded-md px-2 py-1 text-[10px] font-mono text-sky-200 backdrop-blur pointer-events-none transition-opacity ${dragging ? 'opacity-100' : 'opacity-0'}`}
      />

      {/* Fine-placement panel for the selected cell / UE */}
      {selected && selPos && selLL && (
        <div className="absolute bottom-16 right-3 z-[500] w-56 bg-zinc-900/95 border border-zinc-700 rounded-lg p-2.5 backdrop-blur">
          <div className="flex items-center justify-between mb-2 gap-2">
            <span className="text-[11px] font-bold text-amber-300 truncate">{selected.label}</span>
            <button onClick={() => setSelected(null)} title="Deselect" className="text-zinc-500 hover:text-zinc-200 shrink-0"><X className="w-3.5 h-3.5" /></button>
          </div>
          {(['x', 'y'] as const).map(axis => (
            <div key={axis} className="flex items-center gap-1 mb-1.5">
              <span className="w-3 text-[10px] font-bold text-zinc-500 uppercase">{axis}</span>
              <button onClick={() => nudge(axis, -10)} className={nudgeBtn}>−10</button>
              <button onClick={() => nudge(axis, -1)} className={nudgeBtn}>−1</button>
              <span className="flex-1 text-center text-[11px] font-mono text-zinc-100">{selPos[axis]} m</span>
              <button onClick={() => nudge(axis, 1)} className={nudgeBtn}>+1</button>
              <button onClick={() => nudge(axis, 10)} className={nudgeBtn}>+10</button>
            </div>
          ))}
          <div className="text-[9.5px] text-zinc-500 font-mono mt-1">{selLL[0].toFixed(5)}, {selLL[1].toFixed(5)}</div>
          {selCell && config.buildings.length > 0 && (
            <button
              onClick={snapToNearestBuilding}
              className="mt-2 w-full flex items-center justify-center gap-1.5 bg-amber-500/15 border border-amber-500/40 text-amber-300 rounded-md px-2 py-1 text-[10px] font-bold hover:bg-amber-500/25 transition-colors"
            >
              <Building2 className="w-3 h-3" /> Snap onto nearest building{selNearB ? ` (${Math.round(selNearB.d)} m)` : ''}
            </button>
          )}
        </div>
      )}

      {/* Top-left: buildings */}
      <div className="absolute top-3 left-3 z-[500] flex flex-col items-start gap-1.5">
        <button
          onClick={importBuildings}
          disabled={importing || !config}
          title="Fetch real OpenStreetMap building footprints within the sim-area box"
          className="flex items-center gap-1.5 bg-zinc-900/90 border border-zinc-700 hover:border-amber-500/60 rounded-lg px-2.5 py-1.5 backdrop-blur text-[11px] font-medium text-zinc-200 disabled:opacity-50 transition-colors"
        >
          {importing ? <Loader2 className="w-3.5 h-3.5 text-amber-400 animate-spin" /> : <Building2 className="w-3.5 h-3.5 text-amber-400" />}
          {importing ? 'Fetching OSM…' : 'Import buildings'}
        </button>
        {(importMsg || config.buildings.length > 0) && (
          <span className="max-w-[280px] bg-zinc-900/85 border border-zinc-700 rounded-md px-2 py-0.5 text-[10px] leading-snug text-zinc-400 backdrop-blur">
            {importMsg ?? `${config.buildings.length} buildings in scenario`}
          </span>
        )}
      </div>

      {/* Top-right: location (presets + centre + search) */}
      <div className="absolute top-3 right-3 z-[500] flex flex-col items-end gap-1.5">
        <div className="flex items-center gap-1.5 bg-zinc-900/90 border border-zinc-700 rounded-lg px-2 py-1.5 backdrop-blur">
          <MapPin className="w-3.5 h-3.5 text-teal-400 shrink-0" />
          <select
            value={ANCHORS.findIndex(a => a.name === anchor.name)}
            onChange={e => { const i = Number(e.target.value); if (i >= 0) setAnchorAndFit(ANCHORS[i], true); }}
            className="bg-transparent text-[11px] font-medium text-zinc-200 outline-none cursor-pointer max-w-[130px]"
          >
            {ANCHORS.findIndex(a => a.name === anchor.name) < 0 && (
              <option value={-1} className="bg-zinc-900 text-zinc-200">{anchor.name}</option>
            )}
            {ANCHORS.map((a, i) => <option key={i} value={i} className="bg-zinc-900 text-zinc-200">{a.name}</option>)}
          </select>
          <button onClick={useMapCentre} title="Anchor the scenario to the current map centre" className="p-0.5 text-zinc-400 hover:text-teal-400 transition-colors">
            <Crosshair className="w-3.5 h-3.5" />
          </button>
          <button onClick={() => setSearchOpen(v => !v)} title="Search a place" className={`p-0.5 transition-colors ${searchOpen ? 'text-teal-400' : 'text-zinc-400 hover:text-teal-400'}`}>
            <Search className="w-3.5 h-3.5" />
          </button>
        </div>
        {searchOpen && (
          <div className="w-64 bg-zinc-900/95 border border-zinc-700 rounded-lg p-1.5 backdrop-blur">
            <div className="flex items-center gap-1">
              <input
                autoFocus
                value={query}
                onChange={e => setQuery(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') doSearch(); if (e.key === 'Escape') setSearchOpen(false); }}
                placeholder="Search city, street, place…"
                className="min-w-0 flex-1 bg-zinc-950 border border-zinc-700 rounded-md px-2 py-1 text-[11px] text-zinc-200 outline-none focus:border-teal-500/60"
              />
              <button onClick={doSearch} disabled={searching} className="p-1 rounded-md bg-teal-600/80 hover:bg-teal-500 text-white disabled:opacity-50">
                {searching ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Search className="w-3.5 h-3.5" />}
              </button>
            </div>
            {results.length > 0 && (
              <div className="mt-1 max-h-44 overflow-y-auto flex flex-col">
                {results.map((r, i) => (
                  <button key={i} onClick={() => pickResult(r)} className="text-left text-[10.5px] text-zinc-300 hover:bg-zinc-800 rounded px-1.5 py-1 leading-tight">
                    {r.name}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Bottom-left: measure + hint */}
      <div className="absolute bottom-3 left-3 z-[500] flex items-center gap-1.5">
        <button
          onClick={toggleMeasure}
          title="Measure the distance between two points"
          className={`flex items-center gap-1.5 border rounded-md px-2 py-1 backdrop-blur text-[10px] font-bold transition-colors ${measuring ? 'bg-sky-500/20 border-sky-500/60 text-sky-300' : 'bg-zinc-900/85 border-zinc-700 text-zinc-300 hover:text-sky-300 hover:border-sky-500/50'}`}
        >
          {measuring ? <X className="w-3 h-3" /> : <Ruler className="w-3 h-3" />}
          {measuring ? 'Measuring' : 'Measure'}
        </button>
        <span className="flex items-center gap-1.5 bg-zinc-900/85 border border-zinc-700 rounded-md px-2 py-1 backdrop-blur text-[10px] text-zinc-400">
          {measuring
            ? (measureDist != null ? <span className="text-sky-300 font-bold">{fmtDist(measureDist)}</span> : 'Click two points on the map')
            : <><Move className="w-3 h-3 text-teal-400 shrink-0" /> Drag a cell or UE to reposition</>}
        </span>
      </div>
    </div>
  );
}
