/**
 * NSG AI Platform — React Flow ↔ NSGSimulationConfig Mapper
 *
 * Two public functions:
 *   configToFlowNodes(config)   →  React Flow nodes for the canvas
 *   canvasNodeToConfigPatch(…)  →  partial config update when the user drags a node
 *
 * Coordinate system:
 *   Simulation : x = East (m), y = North (m) — y increases upward
 *   React Flow : x = right (px), y = down (px) — y increases downward
 *   Therefore  : canvas_x =  sim_x * SCALE + ORIGIN_PX.x
 *                canvas_y = -sim_y * SCALE + ORIGIN_PX.y   ← note the flip
 */

import type { Node } from 'reactflow';
import type {
  NSGSimulationConfig,
  BaseStation,
  UEGroup,
  Building,
  WallMaterial,
} from '@/types/nsg';

// ── Coordinate constants ──────────────────────────────────────────────────────

/**
 * Scale factor: simulation meters → React Flow canvas pixels.
 * 1.0 means 1 m = 1 px at 100% zoom.
 * React Flow handles large canvases natively via pan/zoom; no pre-scaling needed.
 * The inverse is applied on drag-back via canvasToSim.
 */
const SCALE = 1.0;

/**
 * Canvas pixel position of simulation (0, 0).
 * Placed near the centre of a 2000×2000 px viewport.
 */
const ORIGIN_PX = { x: 1000, y: 1000 } as const;

/**
 * Rendered box of each node type, in px, as its component actually draws it:
 * CellNode is `w-10 h-10` (40x40) and UENode is a fixed 160x120. React Flow
 * anchors a node by its TOP-LEFT corner (nodeOrigin defaults to [0,0], and this
 * version has no per-node `origin`), so a node placed straight at simToCanvas()
 * is drawn half its own size down and to the right of the position it claims.
 *
 * That is not a cosmetic wobble: the two types are different sizes, so a UE
 * group and a cell at the SAME simulation point render 60 m apart horizontally
 * and 40 m apart vertically. Anyone placing a UE relative to a cell by eye — the
 * whole point of the draw section — is off by that much before the engine sees
 * it. So cells and UE groups are shifted to centre them on their position here,
 * and the shift is undone on drag-back.
 *
 * Buildings are deliberately NOT in this list: a building's node position is a
 * CORNER (x_min, y_max), not a centre, and buildingToNode sizes the box to the
 * real footprint — so top-left anchoring is already correct for them. That is
 * also why the global `nodeOrigin={[0.5,0.5]}` prop is the wrong fix: it would
 * centre buildings too and move every one of them by half its own footprint.
 */
const CELL_NODE_PX = { w: 40, h: 40 } as const;
const UE_NODE_PX   = { w: 160, h: 120 } as const;

/** Top-left corner to give React Flow so the box CENTRES on a sim point. */
function centredOn(x: number, y: number, box: { w: number; h: number }) {
  const c = simToCanvas(x, y);
  return { x: c.x - box.w / 2, y: c.y - box.h / 2 };
}

/** Inverse of `centredOn`: a dragged top-left corner back to a sim point. */
function simFromCentred(px: number, py: number, box: { w: number; h: number }) {
  return canvasToSim(px + box.w / 2, py + box.h / 2);
}

/**
 * The box a node type renders at — for canvas code that has to reason about a
 * node's CENTRE rather than the corner React Flow anchors it by.
 *
 * Buildings (and the live-UE dots) report a zero box on purpose: their anchor
 * already IS the point they mean — a building's corner, the dot's centre — so
 * they need no correction.
 */
export function nodeBoxPx(type?: string): { w: number; h: number } {
  if (type === 'cellNode' || type === 'tower') return CELL_NODE_PX;
  if (type === 'ueNode' || type === 'user') return UE_NODE_PX;
  return { w: 0, h: 0 };
}

/** Where a node actually LOOKS like it is: its centre in canvas px. */
export function nodeCentrePx(
  node: { type?: string; position: { x: number; y: number } },
): { x: number; y: number } {
  const b = nodeBoxPx(node.type);
  return { x: node.position.x + b.w / 2, y: node.position.y + b.h / 2 };
}

/**
 * The simulation point a node of this type, anchored at (px, py), actually sits
 * on. Anything outside this module that reads a raw React Flow node position
 * must go through here rather than calling canvasToSim directly — a cell node's
 * anchor is 20 px up-left of the point it represents, and code that used the
 * anchor as the point fed that straight into a DELTA, so the error accumulated
 * 20 m on every drag instead of merely being wrong once.
 */
export function simFromNodeAnchor(
  type: string | undefined, px: number, py: number,
): { x: number; y: number } {
  return simFromCentred(px, py, nodeBoxPx(type));
}

/** Inverse: the anchor that puts a node of this type's centre on a canvas point. */
export function nodeAnchorPx(
  type: string | undefined, cx: number, cy: number,
): { x: number; y: number } {
  const b = nodeBoxPx(type);
  return { x: cx - b.w / 2, y: cy - b.h / 2 };
}

export function simToCanvas(x: number, y: number) {
  return {
    x: ORIGIN_PX.x + x * SCALE,
    y: ORIGIN_PX.y - y * SCALE,   // flip Y
  };
}

export function canvasToSim(px: number, py: number) {
  return {
    x:  (px - ORIGIN_PX.x) / SCALE,
    y: -(py - ORIGIN_PX.y) / SCALE, // flip Y back
  };
}

// ── Node factories ────────────────────────────────────────────────────────────

/**
 * Map one BaseStation → one React Flow cellNode.
 *
 * The `data` object is passed directly to CellNode.jsx.
 * Add or remove fields here to match whatever CellNode.jsx reads from `data`.
 *
 * Why one node per sector (not per site)?
 *   The schema uses one BaseStation per sector. Two co-located sectors simply
 *   share the same position but have different azimuths. React Flow renders
 *   them as two overlapping nodes — the CellNode can draw both cones.
 *   Alternatively, group by position and pass sectors as an array in data.
 */
function baseStationToNode(bs: BaseStation, antennaElement: string): Node {
  const { frequency_mhz } = bs.radio.band;
  let band = 'NR-3600';
  if (frequency_mhz < 1000)       band = 'NR-700';
  else if (frequency_mhz < 2500)  band = 'NR-2100';
  else if (frequency_mhz < 3300)  band = 'LTE-1800';

  return {
    id:       bs.id,
    type:     'cellNode',
    position: centredOn(bs.position.x, bs.position.y, CELL_NODE_PX),
    data: {
      // Identity
      label:     bs.name,
      stationId: bs.id,

      // Spatial
      height: bs.position.z,

      // Radio
      txPower:   bs.radio.tx_power_dbm,
      frequency: bs.radio.band.frequency_mhz,
      bandwidth: bs.radio.band.bandwidth_mhz,

      // Antenna
      antennaType:         bs.radio.antenna.type,
      // SCENARIO-level (settings.antenna_element), carried onto every cell so
      // CellNode can decide whether an azimuth is worth drawing. With the
      // isotropic element the array's rotation cancels out exactly, so a beam
      // cone on the canvas is a picture of something the engine does not do.
      antennaElement,
      azimuth:             bs.radio.antenna.azimuth_deg,
      tilt:                bs.radio.antenna.tilt_deg,
      gain:                bs.radio.antenna.gain_dbi,
      horizontalBeamwidth: bs.radio.antenna.horizontal_beamwidth_deg,
      verticalBeamwidth:   bs.radio.antenna.vertical_beamwidth_deg,
      mimo: bs.radio.antenna.massive_mimo ?? null,

      // Backward-compatible fields read by NetworkCanvas properties panel
      band,
      sectors: [{
        power:   bs.radio.tx_power_dbm,
        azimuth: bs.radio.antenna.azimuth_deg,
        tilt:    bs.radio.antenna.tilt_deg,
        height:  bs.position.z,
      }],
    },
  };
}

/**
 * Map one UEGroup → one React Flow ueNode.
 */
function ueGroupToNode(ue: UEGroup): Node {
  return {
    id:       ue.id,
    type:     'ueNode',
    position: centredOn(ue.spawn_position.x, ue.spawn_position.y, UE_NODE_PX),
    data: {
      label:    ue.name,
      groupId:  ue.id,
      count:    ue.count,
      pattern:  ue.mobility.pattern,
      speed:    ue.mobility.speed_mps,
      services: ue.services,
      // Backward-compatible fields read by NetworkCanvas properties panel
      ues:          ue.count,
      mobilityType: ue.mobility.pattern,
      description:  ue.name,
    },
  };
}

/**
 * Map one Building → one React Flow obstacleNode.
 *
 * Buildings have a finite width/height in the simulation coordinate system.
 * React Flow represents them as nodes with `style.width` / `style.height` in px.
 * The node's `position` is the top-left corner of the building in canvas pixels.
 *
 * Note the extra Y-flip:
 *   In simulation coords, y_min is the SOUTH edge (lower Y value = lower on screen).
 *   After the Y-flip, y_min becomes the NORTH edge — i.e. the top on canvas.
 *   So we place the node at canvas(x_min, y_max) to get the correct top-left.
 */
const WALL_MATERIAL_LABELS: Record<WallMaterial, string> = {
  wood:                  'Wood',
  concrete:              'Concrete',
  concrete_with_windows: 'Concrete',
};

function buildingToNode(b: Building): Node {
  const widthM  = b.bounds.x_max - b.bounds.x_min;
  const heightM = b.bounds.y_max - b.bounds.y_min;

  // Top-left in canvas coords = (x_min, y_max) in simulation coords (Y is flipped)
  const position = simToCanvas(b.bounds.x_min, b.bounds.y_max);

  return {
    id:       b.id,
    type:     'obstacleNode',
    position,
    zIndex:   -1,   // always render behind cell sites and UE groups
    data: {
      label:        b.id,
      buildingId:   b.id,
      buildingType: b.building_type,
      wallMaterial: b.wall_material,
      floors:       b.floors,
      widthM,
      heightM,
      // Backward-compatible fields read by NetworkCanvas properties panel
      material:     WALL_MATERIAL_LABELS[b.wall_material] ?? 'Concrete',
      height:       b.bounds.z_max,
      width:        widthM,
      depth:        heightM,
      loss:         b.wall_material === 'wood' ? 5 : 20,
      angle:        0,
    },
    style: {
      width:  widthM  * SCALE,
      height: heightM * SCALE,
    },
  };
}

// ── Public API ────────────────────────────────────────────────────────────────

/**
 * Convert a full NSGSimulationConfig into React Flow nodes.
 * Call this whenever `useNSGStore(s => s.config)` changes.
 *
 * Usage in NetworkCanvas.jsx:
 *   const config = useNSGStore(s => s.config);
 *   const { nodes } = useMemo(
 *     () => config ? configToFlowNodes(config) : { nodes: [] },
 *     [config]
 *   );
 */
export function configToFlowNodes(config: NSGSimulationConfig): {
  nodes: Node[];
} {
  const nodes: Node[] = [
    ...config.base_stations.map(bs =>
      baseStationToNode(bs, config.settings?.antenna_element ?? 'isotropic')),
    ...config.ue_groups.map(ueGroupToNode),
    ...config.buildings.map(buildingToNode),
  ];

  return { nodes };
}

/**
 * Convert a React Flow node drag event back into a partial config patch.
 * Apply the returned patch with `useNSGStore(s => s.setConfig)`.
 *
 * Usage in NetworkCanvas.jsx onNodeDragStop handler:
 *   const setConfig = useNSGStore(s => s.setConfig);
 *   const config    = useNSGStore(s => s.config);
 *
 *   function onNodeDragStop(_event, node) {
 *     if (!config) return;
 *     const patch = canvasNodeToConfigPatch(node.id, node.position, config);
 *     setConfig({ ...config, ...patch });
 *   }
 */
export function canvasNodeToConfigPatch(
  nodeId: string,
  canvasPosition: { x: number; y: number },
  config: NSGSimulationConfig,
): Partial<NSGSimulationConfig> {
  // The inverse has to match the anchor each node type was PLACED with, or a
  // node walks by half its own box on every drag: cells and UE groups are
  // centred (see centredOn), buildings keep the raw top-left corner.

  // Check base_stations
  const bsIndex = config.base_stations.findIndex(bs => bs.id === nodeId);
  if (bsIndex !== -1) {
    const { x, y } = simFromCentred(canvasPosition.x, canvasPosition.y, CELL_NODE_PX);
    const updated = [...config.base_stations];
    updated[bsIndex] = {
      ...updated[bsIndex],
      position: { ...updated[bsIndex].position, x, y },
    };
    return { base_stations: updated };
  }

  // Check ue_groups
  const ueIndex = config.ue_groups.findIndex(ue => ue.id === nodeId);
  if (ueIndex !== -1) {
    const { x, y } = simFromCentred(canvasPosition.x, canvasPosition.y, UE_NODE_PX);
    const updated = [...config.ue_groups];
    updated[ueIndex] = {
      ...updated[ueIndex],
      spawn_position: { x, y },
    };
    return { ue_groups: updated };
  }

  const { x: simX, y: simY } = canvasToSim(canvasPosition.x, canvasPosition.y);

  // Check buildings — drag moves the whole bounding box
  const bldIndex = config.buildings.findIndex(b => b.id === nodeId);
  if (bldIndex !== -1) {
    const bld    = config.buildings[bldIndex];
    const widthM = bld.bounds.x_max - bld.bounds.x_min;
    const heightM = bld.bounds.y_max - bld.bounds.y_min;
    // Node position is the top-left corner → corresponds to (x_min, y_max) in sim
    const updated = [...config.buildings];
    updated[bldIndex] = {
      ...bld,
      bounds: {
        ...bld.bounds,
        x_min: simX,
        x_max: simX + widthM,
        y_min: simY - heightM,  // y_max is the top (larger Y in sim = North = top of canvas)
        y_max: simY,
      },
    };
    return { buildings: updated };
  }

  return {};
}
