/**
 * RIC-test scenario presets — curated, purpose-built challenges so a user can
 * test an xApp without hand-building a scenario. Each preset is engineered so
 * that "do nothing" is measurably worse than a known right answer, and every
 * scenario is deterministic (shadowing/fast-fading off, fixed seed) so two AIs
 * compared on it face identical physics.
 *
 * The baseline/solved QoS figures in each `hint` are verified against the
 * analytical core (ric_sim) — see the challenge each one poses:
 *   congestion → load balancing (handover),  energy → cell on/off,
 *   coverage   → power control.
 */
import type { NSGSimulationConfig, BaseStation, UEGroup, NetworkSlice } from '@/types/nsg';

function bs(id: string, name: string, x: number, y: number, powerDbm = 46): BaseStation {
  return {
    id, name,
    position: { x, y, z: 30 },
    radio: {
      band: { label: 'N77', frequency_mhz: 3600, bandwidth_mhz: 100, numerology: 1 },
      tx_power_dbm: powerDbm,
      antenna: {
        type: 'isotropic', azimuth_deg: 0, tilt_deg: 0, gain_dbi: 15,
        horizontal_beamwidth_deg: 360, vertical_beamwidth_deg: 90,
      },
    },
  };
}

function ue(
  id: string, name: string, count: number, x: number, y: number,
  tgtGbps = 0.01, slice: NetworkSlice = 'eMBB', qosId = 8,
): UEGroup {
  return {
    id, name, count,
    spawn_position: { x, y },
    mobility: { pattern: 'pedestrian', speed_mps: 1.4 },
    services: [{ slice_type: slice, qos_id: qosId, target_throughput_gbps: tgtGbps }],
  };
}

function scenario(name: string, description: string, stations: BaseStation[], groups: UEGroup[]): NSGSimulationConfig {
  return {
    name, description,
    base_stations: stations,
    ue_groups: groups,
    buildings: [],
    area: { x_min: -250, x_max: 700, y_min: -250, y_max: 550, resolution_m: 40 },
    settings: {
      duration_s: 60, report_interval_ms: 1000, scenario: 'UMa',
      shadowing_enabled: false, random_seed: 42,
    },
  };
}

export interface RicPreset {
  id: string;
  label: string;
  /** Short control domain this preset exercises. */
  tag: string;
  /** What's wrong in the scenario (the incident the AI must notice). */
  challenge: string;
  /** The known right answer + the verified QoS swing, so users can judge their AI. */
  hint: string;
  config: NSGSimulationConfig;
}

export const RIC_PRESETS: RicPreset[] = [
  {
    id: 'congestion-hotspot',
    label: 'Congestion Hotspot',
    tag: 'Load balancing · handover',
    challenge: 'All four UE groups camp on one downtown cell (100% PRB) while its two neighbours sit idle — QoS collapses to ~25.',
    hint: 'Hand a couple of groups over to the idle neighbour cells to spread the load. Verified: baseline QoS ~25 → well-balanced ~65.',
    config: scenario(
      'Congestion Hotspot', '3 co-channel cells; load piled on one — steer traffic to the idle two.',
      [bs('cell-A', 'Downtown / A', 0, 0), bs('cell-B', 'Downtown / B', 300, 0), bs('cell-C', 'Downtown / C', 150, 260)],
      [ue('g1', 'eMBB core', 45, 20, 20, 0.005), ue('g2', 'eMBB north', 45, 110, 15, 0.005),
       ue('g3', 'eMBB south', 45, 70, 130, 0.005), ue('g4', 'eMBB dense', 45, 25, 40, 0.005)],
    ),
  },
  {
    id: 'energy-saving-night',
    label: 'Energy Saving — Night',
    tag: 'Energy saving · cell on/off',
    challenge: 'Five cells all powered on for a light night-time load; two of them (D, E) carry zero UEs — pure wasted energy.',
    hint: 'Switch off the idle cells to cut energy with no QoS cost. Verified: QoS stays 100 while energy drops 100% → 60%. Trap: switching off a loaded cell drops QoS.',
    config: scenario(
      'Energy Saving — Night', 'Light load; two redundant cells — switch them off without causing outages.',
      [bs('cell-A', 'Grid / A', 0, 0), bs('cell-B', 'Grid / B', 200, 0), bs('cell-C', 'Grid / C', 400, 0),
       bs('cell-D', 'Grid / D', 100, 180), bs('cell-E', 'Grid / E', 300, 180)],
      [ue('g1', 'eMBB A', 15, 10, 10, 0.008), ue('g2', 'eMBB B', 15, 200, 10, 0.008), ue('g3', 'eMBB C', 15, 400, 10, 0.008)],
    ),
  },
  {
    id: 'coverage-hole',
    label: 'Coverage Hole',
    tag: 'Power control',
    challenge: 'A stranded edge group is weakly served by a distant cell (~33 QoS); a nearby cell runs at low power.',
    hint: 'Boost the weak edge cell’s tx power (or steer the group onto it) to restore coverage. Verified: baseline QoS ~33 → ~75 once the edge cell is turned up.',
    config: scenario(
      'Coverage Hole', 'Edge users starved by a distant cell; a nearby low-power cell can be turned up.',
      [bs('cell-A', 'Core / A', 0, 0), bs('cell-edge', 'Edge (low power)', 400, 0, 30)],
      [ue('g1', 'eMBB core', 40, 20, 20, 0.01), ue('g2', 'stranded villagers', 40, 400, 160, 0.02)],
    ),
  },
];
