#!/usr/bin/env python3
"""The fast tier: does it still answer, and does it still answer consistently?

`ric_sim` is the RIC Test console's RAN, and it now computes its window through
`window_model` — the engine's own AMC tables, transport-block size and
scheduler split — instead of a Shannon capacity. That swap can fail in ways no
error surfaces: a key the console reads quietly disappears, a counter stops
scaling with the report window, or the array gain lands on the interferers as
well as the signal and cancels itself out. Every one of those produces a report
that renders.

These are contract checks, not accuracy checks. How close the tier is to the
engine is `tools/tier_agreement.py`, which needs the database.
"""
from __future__ import annotations

import math
import statistics
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "nsg_ai"))

import window_model as wm
from mcs import mcs_from_se, se_from_sinr
from ric_sim import RicSimulation
from schema import (AntennaConfig, BaseStation, FrequencyBand,
                    MassiveMIMOConfig, NetworkSlice, NSGSimulationConfig,
                    Position2D, Position3D, RadioConfig, ServiceRequirement,
                    UEGroup)

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(name)


def scenario(rows: int = 8, cols: int = 8) -> NSGSimulationConfig:
    """Three cells in a line, two UE groups, enough load to fill a slot."""
    bss = [
        BaseStation(
            id=f"c{i}", name=f"cell {i}",
            position=Position3D(x=x, y=0.0, z=25.0),
            radio=RadioConfig(
                band=FrequencyBand(label="NR-N77", frequency_mhz=3600.0,
                                   bandwidth_mhz=50.0, numerology=1),
                tx_power_dbm=43.0,
                antenna=AntennaConfig(
                    massive_mimo=MassiveMIMOConfig(rows=rows, cols=cols)),
            ),
        )
        for i, x in enumerate((0.0, 400.0, 800.0))
    ]
    ues = [
        UEGroup(id=f"g{i}", name=f"group {i}", count=4,
                spawn_position=Position2D(x=x, y=60.0),
                services=[ServiceRequirement(slice_type=NetworkSlice.EMBB,
                                             qos_id=9,
                                             target_throughput_gbps=0.05)])
        for i, x in enumerate((50.0, 450.0))
    ]
    return NSGSimulationConfig(name="fast tier contract",
                               base_stations=bss, ue_groups=ues)


# ── the counter chain, on its own ────────────────────────────────────────────
print("transport-block chain")
check("MCS quantises DOWN, never up",
      all(wm.SE_FOR_MCS[m] <= se_from_sinr(s) + 1e-9
          for s in range(0, 40) if (m := mcs_from_se(se_from_sinr(s))) >= 0),
      "a tabled efficiency above the link's own would invent throughput")
check("a link too weak for MCS 0 is outage, not MCS 0",
      mcs_from_se(se_from_sinr(-15.0)) < 0)
check("the block error rate is the chance of falling under the threshold",
      abs(wm.bler_for(wm.shannon_threshold_db(20), 20) - 0.5) < 1e-6,
      "at the threshold itself the channel is below it half the time")
check("it falls as the margin grows and rises as the channel moves",
      wm.bler_for(wm.shannon_threshold_db(20) + 12, 20)
      < wm.bler_for(wm.shannon_threshold_db(20) + 4, 20)
      and wm.bler_for(wm.shannon_threshold_db(20) + 8, 20, sigma_db=12.0)
      > wm.bler_for(wm.shannon_threshold_db(20) + 8, 20, sigma_db=3.0))
check("the shipped gap and Rayleigh fading produce the operating point",
      0.03 < wm.bler_for(wm.required_sinr_db(20), 20) < 0.15,
      f"{wm.bler_for(wm.required_sinr_db(20), 20) * 100:.1f}% at the selection "
      f"point, against the 6.7-10% the engine reports — not assumed, derived "
      f"from an 8.2 dB gap and a {wm.FADING_SIGMA_DB:.2f} dB Rayleigh spread")
check("numerology 1 in a config runs as 2",
      wm.engine_numerology(1) == 2 and wm.engine_numerology(0) == 2,
      "MmWavePhyMacCommon supports 2 and 3 only, and the platform clamps")
check("a 1 s window is 4000 slots",
      round(1.0 / wm.slot_period_s(2)) == 4000)
check("the array gain is the panel's, with nothing fitted on top",
      abs(wm.array_gain_db(8, 8)
          - (10 * math.log10(64) - wm.BEAMFORMING_LOSS_DB)) < 1e-9
      and wm.BEAMFORMING_LOSS_DB == 0.0,
      "it was 6 dB once, fitted against an estimator that was wrong; if it "
      "is ever non-zero again the fit must be re-run against current frames")
check("the beamforming loss is an OFFSET, not a scale",
      abs((wm.array_gain_db(8, 8) - wm.array_gain_db(4, 4))
          - 10 * math.log10(4)) < 1e-9,
      "quadrupling the elements must still buy 6.02 dB — a loss that scaled "
      "with the panel would quietly change what a bigger array is worth")

# ── the report the console reads ─────────────────────────────────────────────
print("\nKPM report contract")
sim = RicSimulation(scenario(), window_s=1.0)
r = sim.get_kpm_report()

check("every top-level key the console reads is present",
      {"tick", "cells", "ue_groups", "slices", "control_state",
       "anomalies", "measurements"} <= set(r))
check("cell rows keep their fields",
      all({"id", "name", "active", "faulted", "tx_power_dbm", "power_w",
           "prb_used_pct", "throughput_mbps", "connected_ues"} <= set(c)
          for c in r["cells"]))
check("UE rows keep their fields",
      all({"id", "name", "serving_cell", "rsrp_dbm", "sinr_db",
           "throughput_mbps", "count"} <= set(u) for u in r["ue_groups"]))
check("occupancy is a percentage, not a demand ratio",
      all(0.0 <= c["prb_used_pct"] <= 100.0 for c in r["cells"]),
      "the old demand/supply figure could read past 100")

m = {k: v["mean"] for k, v in r["measurements"].items()}
check("the counters the criterion binds are all reported",
      {"RRU.PrbUsedDl", "TB.TotNbrDl.1", "TB.ErrTotalNbrDl.1",
       "TB.TotNbrDlInitial.64Qam", "QosFlow.PdcpPduVolumeDL"} <= set(m))
check("initial transmissions = sent minus errored",
      abs(m["TB.TotNbrDlInitial"] - (m["TB.TotNbrDl.1"] - m["TB.ErrTotalNbrDl.1"])) < 1.0)
check("the modulation split accounts for every initial block",
      abs(sum(m[f"TB.TotNbrDlInitial.{q}"] for q in ("Qpsk", "16Qam", "64Qam"))
          - m["TB.TotNbrDlInitial"]) < 1.0)
check("no UE is scheduled more than once a slot",
      m["TB.TotNbrDl.1"] <= round(1.0 / wm.slot_period_s(2)) + 1)

# ── the two ways it can look right and be wrong ──────────────────────────────
print("\nbehaviour")
half = RicSimulation(scenario(), window_s=0.5).get_kpm_report()
hm = {k: v["mean"] for k, v in half["measurements"].items()}
check("blocks scale with the report window",
      abs(hm["TB.TotNbrDl.1"] * 2 - m["TB.TotNbrDl.1"]) <= 2,
      "these counters are EXTENSIVE; a fixed window silently misreports them")
check("bytes scale with the report window",
      abs(hm["QosFlow.PdcpPduVolumeDL"] * 2 - m["QosFlow.PdcpPduVolumeDL"])
      < 0.02 * m["QosFlow.PdcpPduVolumeDL"])

small = RicSimulation(scenario(rows=2, cols=2), window_s=1.0).get_kpm_report()
big_sinr = max(u["sinr_db"] for u in r["ue_groups"] if u["sinr_db"] is not None)
small_sinr = max(u["sinr_db"] for u in small["ue_groups"] if u["sinr_db"] is not None)
check("a bigger panel raises SINR",
      big_sinr > small_sinr + 5.0,
      f"8x8 {big_sinr:.1f} dB vs 2x2 {small_sinr:.1f} dB — if the gain reached "
      f"the interferers too it would cancel and these would match")

off = RicSimulation(scenario(), window_s=1.0)
before = sum(c["throughput_mbps"] for c in off.get_kpm_report()["cells"])
off.apply_control({"action": "cell_power", "cell": "c0", "state": "off"})
rep = off.get_kpm_report()
after = sum(c["throughput_mbps"] for c in rep["cells"])
check("switching a cell off reaches the numbers",
      next(c for c in rep["cells"] if c["id"] == "c0")["throughput_mbps"] == 0.0
      and not next(c for c in rep["cells"] if c["id"] == "c0")["active"]
      and after != before,
      f"network {before:.0f} -> {after:.0f} Mbps")

print("\none draw is a sample, not an answer")
import tier_b as _tb2, tier_criterion as _tc2, statistics as _st2
_p = {"gnb_positions": "0,0,25;500,0,25;250,433,25",
      "ue_positions": ";".join(f"{80*i%600},{60*i%400},1.5" for i in range(1, 13)),
      "gnb_powers": "40;40;40", "bw_hz": 50e6, "freq_hz": 3.6e9,
      "ind_period_s": 1.0}
_runs = [_tc2.harvest(_tb2.frames(_p, 10, seed=s)) for s in range(6)]
_prb = [r["RRU.PrbUsedDl"] for r in _runs if r.get("RRU.PrbUsedDl")]
check("a different realisation gives a different answer",
      len(set(_prb)) > 1,
      f"spectrum over six draws: {min(_prb):.1f} to {max(_prb):.1f} — the "
      f"shadowing sigma is 4 to 7.8 dB, so a single draw is a sample")
check("the same realisation gives the same answer",
      _tc2.harvest(_tb2.frames(_p, 10, seed=3))["RRU.PrbUsedDl"]
      == _tc2.harvest(_tb2.frames(_p, 10, seed=3))["RRU.PrbUsedDl"],
      "reproducibility is what a criterion built on repeats depends on")
check("the comparison tools average over realisations",
      _tc2.TIER_B_REALISATIONS >= 4,
      f"{_tc2.TIER_B_REALISATIONS} draws — Tier A is compared as a mean over "
      f"seeds and Tier B was compared as one draw, which is a different question")

print("\nthe spread is an input, and inputs have to arrive")
_lo = wm.cell_window(1, {i: 18.0 for i in range(3)}, {i: 80e6 for i in range(3)},
                     50.0, 1, decay_db=3.0)
_hi = wm.cell_window(1, {i: 18.0 for i in range(3)}, {i: 80e6 for i in range(3)},
                     50.0, 1, decay_db=12.0)
check("a wider spread produces more block errors",
      _hi.ues[0].bler > _lo.ues[0].bler * 1.5,
      f"{_lo.ues[0].bler*100:.1f}% at 3 dB against {_hi.ues[0].bler*100:.1f}% at 12 dB "
      f"— this argument was silently dead once, overridden by the model's own "
      f"computed value, and a sweep over it produced six identical rows")

print("\nthe engine's counters, guarded")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
import tier_criterion as _tc
check("a wrapped uint32 is not a measurement",
      _tc.looks_underflowed(4294966948) and not _tc.looks_underflowed(10485760),
      "2^32 - 348 for a 10 MB buffer: the engine computed minus 348 bytes at "
      "low load and it wrapped")
check("the guard does not eat a full buffer",
      not _tc.looks_underflowed(10485760) and not _tc.looks_underflowed(2 ** 30),
      "a gigabyte is implausible but not a wrap; only 2^31 and above is")

print("\nbuildings decide line of sight, they do not bias a coin")
import tier_b as _tb
wall = _tb._parse_buildings("250,350,-100,100,0,30,0,Office,1,1,1")
check("a wall across the path blocks it",
      not _tb.line_of_sight((400.0, 0.0, 1.5), (0.0, 0.0, 25.0), wall))
check("a wall beside the path does not",
      _tb.line_of_sight((400.0, 300.0, 1.5), (0.0, 300.0, 25.0), wall))
# Compare the BRANCHES, not two draws. At 400 m the stochastic LOS
# probability is already 0.045, so an unwalled link is almost always NLOS too
# and comparing "wall" against "no wall" compares NLOS against NLOS — which
# is how the first version of this check failed while the code was right.
_los = _tb.umi_path_loss_db(400.0, 401.0, 3.6, 25.0, ("k", 1), los=True)
_nlos = _tb.umi_path_loss_db(400.0, 401.0, 3.6, 25.0, ("k", 1), los=False)
check("the NLOS branch costs real power, it does not relabel the link",
      _nlos > _los + 10.0, f"{_los:.1f} dB LOS against {_nlos:.1f} dB NLOS")

# With buildings the engine swaps in BuildingsChannelConditionModel, which has
# no probability in it. Tier B must stop drawing too, or a UE behind a wall is
# in line of sight whenever the coin says so.
blocked_pairs = sum(
    1 for y in range(-90, 91, 10)
    if not _tb.line_of_sight((400.0, float(y), 1.5), (0.0, float(y), 25.0), wall))
check("every link behind the wall is blocked, not most of them",
      blocked_pairs == len(range(-90, 91, 10)),
      f"{blocked_pairs}/{len(range(-90, 91, 10))} — a stochastic LOS would "
      f"let some through")

print("\nstate that carries between windows")
sim2 = RicSimulation(scenario(), window_s=1.0)
q = []
for _ in range(4):
    r2 = sim2.get_kpm_report()
    q.append(r2["measurements"]["DRB.BufferSize.Qos"]["mean"])
    sim2.tick()
# THE CLAIM IS PERSISTENCE, NOT GROWTH, and this used to assert growth.
# It passed while every UE in a group sat on one point and saw one link, so
# the cell was uniformly short and the backlog only ever climbed. With the
# geometry per UE the group spreads, the better-placed members drain, and a
# cell at this load holds a steady queue instead of a rising one — which is
# what a cell at this load should do. Growth under a load that OVERRUNS the
# cell is the next check's job, at 500 Mbps a UE.
check("the queue carries between windows instead of resetting",
      all(v > 0 for v in q[:3]) and max(q[:3]) / min(q[:3]) < 2.0,
      f"{q[0]/1e3:.0f} -> {q[1]/1e3:.0f} -> {q[2]/1e3:.0f} kB — non-zero and "
      f"steady; a queue rebuilt from scratch each window would read the same "
      f"arrivals every time and a reset one would read zero")

# Offer far more than the cell can carry, so the queue REACHES the ceiling.
# Checking only that it stays below one would pass with no ceiling at all.
flood = scenario()
for g in flood.ue_groups:
    for svc in g.services:
        svc.target_throughput_gbps = 0.5
deep = RicSimulation(flood, window_s=1.0)
for _ in range(60):
    deep.get_kpm_report()
    deep.tick()
peak = deep.get_kpm_report()["measurements"]["DRB.BufferSize.Qos"]["max"]
check("the queue pins AT the RLC buffer instead of growing forever",
      0.99 * wm.RLC_BUFFER_BYTES <= peak <= wm.RLC_BUFFER_BYTES + 1,
      f"{peak/1e6:.2f} MB against a {wm.RLC_BUFFER_BYTES/1e6:.0f} MB buffer, "
      f"after 60 windows of 500 Mbps per UE")

fast = scenario()
for g in fast.ue_groups:
    g.mobility.speed_mps = 30.0
mov = RicSimulation(fast, window_s=1.0)
for _ in range(30):
    mov.get_kpm_report()
    mov.tick()
_fast_ho = mov.get_kpm_report()["handovers"]
check("UEs that move hand over", _fast_ho > 0,
      f"{_fast_ho} over 30 windows at 30 m/s")

still = RicSimulation(scenario(), window_s=1.0)
for _ in range(30):
    still.get_kpm_report()
    still.tick()
_slow_ho = still.get_kpm_report()["handovers"]
# THIS USED TO ASSERT ZERO, and zero was a property of the old geometry
# rather than of the scenario. The console resolved ONE position and ONE
# serving cell per GROUP, with no shadowing, so a group either crossed a
# boundary or it did not. It now resolves every UE separately through
# `tier_b`, with the 3GPP channel's shadowing on each link, so a group
# straddling a boundary genuinely hands some of its UEs over while the
# rest stay — which is what a real group of six people standing near a
# cell edge does. Asserting zero would be asserting the old approximation.
# What must still hold is the RATE.
check("UEs that barely move hand over far less often",
      _slow_ho * 3 < _fast_ho,
      f"{_slow_ho} at 3 m/s against {_fast_ho} at 30 m/s over the same 30 "
      f"windows — 90 m of travel against 900, between cells 400 m apart")

print("\nthe seed has to survive a process boundary")
import subprocess
_draws = [subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, %r); import tier_b; print(tier_b._draw('los', 7, 0, 1))"
     % os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "nsg_ai")],
    capture_output=True, text=True).stdout.strip() for _ in range(3)]
check("the same seed draws the same channel in a fresh interpreter",
      len(set(_draws)) == 1,
      "this failed for the whole of Phase 2: `hash()` of a str is randomised "
      "per process, so every tool drew its own shadowing — " + ", ".join(
          d[:8] for d in _draws))

print("\na second carrier is a second network, not a louder one")
import tier_b
from main import nsoran_params


def two_site(freqs, azimuths=(0.0,), element="isotropic"):
    """One cell per (site, frequency, azimuth) — VIAVI's own layout shape."""
    from schema import SimulationArea, SimulationSettings
    bss, k = [], 0
    for x, y in ((-400.0, 200.0), (200.0, -250.0), (550.0, 150.0)):
        for f, bw, mu in freqs:
            for az in azimuths:
                bss.append(BaseStation(
                    id=f"c{k}", name=f"c{k}",
                    position=Position3D(x=x, y=y, z=25.0),
                    radio=RadioConfig(
                        band=FrequencyBand(label="NR", frequency_mhz=f,
                                           bandwidth_mhz=bw, numerology=mu),
                        tx_power_dbm=43.0,
                        antenna=AntennaConfig(
                            azimuth_deg=az, tilt_deg=6.0,
                            massive_mimo=MassiveMIMOConfig(rows=8, cols=8)))))
                k += 1
    ues = [UEGroup(id=f"g{i}", name=f"g{i}", count=8,
                   spawn_position=Position2D(x=x, y=y),
                   services=[ServiceRequirement(slice_type=NetworkSlice.EMBB,
                                                qos_id=9,
                                                target_throughput_gbps=0.05)])
           for i, (x, y) in enumerate([(0.0, 0.0), (-300.0, 0.0), (300.0, 100.0),
                                       (0.0, 300.0), (100.0, -300.0)])]
    return NSGSimulationConfig(
        name="bands", base_stations=bss, ue_groups=ues,
        settings=SimulationSettings(antenna_element=element),
        area=SimulationArea(x_min=-1500.0, x_max=1500.0,
                            y_min=-1500.0, y_max=1500.0))


N77, N28 = (3600.0, 40.0, 1), (728.0, 10.0, 0)
_one = nsoran_params(two_site([N77, N77]))
_two = nsoran_params(two_site([N77, N28]))
check("a single-band scene carries no band spec at all",
      _one["gnb_bands"] == "" and _two["gnb_bands"] != "",
      "empty means every tool downstream behaves exactly as it did")


def _sinr(params):
    fr = tier_b.frames(params, 3,
                       demand_bps={i: 50e6 for i in range(1, params["n_ue"] + 1)})
    return sum(u["sinr"] for u in fr[-1]["ues"]) / max(1, len(fr[-1]["ues"]))


_s1, _s2 = _sinr(_one), _sinr(_two)
check("splitting the cells across two carriers raises SINR",
      _s2 > _s1 + 3.0,
      f"{_s1:.1f} dB co-channel against {_s2:.1f} dB on two bands — half the "
      f"cells stop being interferers, which is what the second band is for")

check("a carrier that does not overlap does not interfere",
      tier_b.spectral_overlap(N77[:2], N28[:2]) == 0.0
      and tier_b.spectral_overlap(N77[:2], N77[:2]) == 1.0
      and abs(tier_b.spectral_overlap((3600.0, 40.0), (3620.0, 40.0)) - 0.5) < 1e-9,
      "co-channel 1.0, disjoint 0.0, half-overlapped 0.5")

_fr = tier_b.frames(_two, 2, demand_bps={i: 50e6 for i in range(1, _two["n_ue"] + 1)})
_blocks = max(u["tb"] for u in _fr[-1]["ues"])
check("the per-cell numerology decides the block ceiling",
      _blocks <= 2000,
      f"{_blocks} blocks at 30 kHz, where the engine's compiled-in 60 kHz "
      f"would allow 4000 — the clamp that makes Tier B reproduce ns-3 is off "
      f"when the carrier is one ns-3 cannot express")

print("\nthree cells on one mast are only told apart by the pattern")
_iso = nsoran_params(two_site([N77], (0.0, 120.0, 240.0), "isotropic"))
_dir = nsoran_params(two_site([N77], (0.0, 120.0, 240.0), "3gpp"))
check("the element pattern moves which sector serves a UE",
      [u["cell"] for u in tier_b.frames(_iso, 1)[0]["ues"]]
      != [u["cell"] for u in tier_b.frames(_dir, 1)[0]["ues"]],
      "isotropic elements rotate to nothing, which is why azimuth looked like "
      "a dead parameter in the engine for months")
check("the pattern's front-to-back is the model's, not the config's",
      abs(wm.element_gain_db(0.0, 90.0) - 8.0) < 1e-9
      and abs(wm.element_gain_db(180.0, 90.0) + 22.0) < 1e-9,
      "8 dBi on boresight, 30 dB down behind it — TR 38.901 Table 7.3-1, and "
      "the schema's own beamwidth fields are analytical-tier only")

print("\nthe uplink exists, and says so when it does not")
_ul = tier_b.frames(nsoran_params(two_site([N77])), 3,
                    demand_bps={i: 10e6 for i in range(1, 200)})[-1]
_rows = _ul["ues"]
check("every UE reports both directions",
      all("thp_ul_kbps" in u for u in _rows) and _ul["measurements"].get("TB.TotNbrUl"),
      "DRB.PdcpSduVolumeUl / DRB.PdcpSduBitRateUl are what Tier A reports; "
      "RRU.PrbUsedUl / TB.TotNbrUl / TB.ErrTotalNbrUl are what VIAVI does")
_dl_sum = sum(u["thp_kbps"] for u in _rows)
_ul_sum = sum(u["thp_ul_kbps"] for u in _rows)
check("the uplink is a quarter of the downlink, not half and not all",
      0.15 <= _ul_sum / max(1e-9, _dl_sum) <= 0.45,
      f"{_ul_sum / _dl_sum:.2f} — the scenario's uplink OnOff is built at "
      f"DataRate(rate * 0.25), and VIAVI's own captures give 0.16-0.34 once "
      f"their per-beam duplicate rows are collapsed and the MIMO rank is "
      f"divided out")
_cw = wm.cell_window(0, {1: 20.0}, {1: 50e6}, 50.0, 2, window_s=1.0)
check("a window with no uplink SINR reports NO uplink counter",
      not [k for k in _cw.ues[0].measurements() if "Ul" in k],
      "absent, not zero — a zero is a measurement and would be a false one")
_d = [u["sinr_ul"] - u["sinr"] for u in _rows]
_med = sorted(_d)[len(_d) // 2]
check("with nothing interfering, the uplink gap IS the power gap",
      -16.0 < _med < -8.0 and max(_d) < 2.0,
      f"median {_med:+.1f} dB on a two-site scene — the UE transmits at "
      f"ue_tx_power_dbm (30) against the cell's 43 and there is almost "
      f"nothing else on the air, so the gap is the transmit powers and "
      f"nobody is better off on the uplink. This used to assert that some "
      f"UE WAS, on a scene with no engine behind it, which was asserting a "
      f"downlink drowning in interference the engine does not have")

# ...and on the scene the engine actually measured, against its own trace.
import tier_criterion as _tc2
_n2, _cfg2, _ = _tc2.load_scenario("Partition test")
_l2 = tier_b.link_state(nsoran_params(_cfg2, engine_clamped=False))
_d2 = sorted(v["sinr_ul_db"] - v["sinr_db"] for v in _l2.values())
_better = sum(1 for v in _d2 if v > 0)
check("some UEs ARE better on the uplink, as the engine measures",
      0.15 < _better / len(_d2) < 0.45 and -12.0 < _d2[len(_d2) // 2] < -1.0,
      f"{_better} of {len(_d2)} better on the uplink, median "
      f"{_d2[len(_d2) // 2]:+.1f} dB — the engine's own PHY trace gives 10 "
      f"of 36 and -7.5 dB on this scenario. A UE swamped on the downlink "
      f"can be quiet on the uplink because its interferers are other UEs "
      f"rather than masts, and the model only reproduces that once the "
      f"uplink re-draws its LOS/NLOS condition the way the downlink does")

print("\nthe channel the engine is RUNNING, not the one it defaults to")
_p = nsoran_params(two_site([N77]))
check("the reference family reaches the model",
      _p.get("prop_scenario") in ("uma", "umi", "rma"),
      f"prop_scenario={_p.get('prop_scenario')!r} — the launcher passes this "
      f"as --propScenario and the criterion's own scenario is UMA_BUILDINGS, "
      f"so the model computed UMi for a UMa run until it was read off an "
      f"actual command line")
_umi = dict(_p, prop_scenario="umi")
_uma = dict(_p, prop_scenario="uma")
check("switching the family changes the channel",
      [round(u["sinr"], 1) for u in tier_b.frames(_umi, 1)[0]["ues"]]
      != [round(u["sinr"], 1) for u in tier_b.frames(_uma, 1)[0]["ues"]],
      "UMa NLOS is 5-9 dB lossier than UMi's and its LOS is two-slope; if "
      "these matched, the parameter would not be wired")
check("UMa keeps the UE at its own height",
      abs(tier_b.uma_path_loss_db(300.0, 301.0, 3.6, 25.0, 1.5, (0, 0, 0),
                                  los=False)
          - tier_b.uma_path_loss_db(300.0, 301.0, 3.6, 25.0, 25.0, (0, 0, 0),
                                    los=False)) > 10.0,
      "the -0.6*(hUt - 1.5) term is real here. UMi's helper identifies a base "
      "station by being exactly 10 m tall and swaps our 25 m masts with the "
      "UEs; UMa's reference height IS 25 m, so it does not")

print("\nthe engine's own AMC and error model, ported and switchable")
import mi_error_model as _mi
_nrb = wm.rb_count(50.0, 2)
check("the tables came out of the engine at the right sizes",
      len(_mi.MI_map_qpsk) == 797 and len(_mi.MI_map_16qam) == 994
      and len(_mi.MI_map_64qam) == 752 and len(_mi.bEcrTable) == 9
      and len(_mi.bEcrTable[0]) == 38,
      "797/994/752 MI points and a 9x38 BLER curve table, extracted by "
      "tools/extract_mi_tables.py — 5,800 numbers nobody copied by eye")
_sel = [_mi.select_mcs(s, lambda k: wm.tb_bytes(k, _mi.NUM_SYM_FOR_CQI, _nrb))
        for s in (0, 10, 20)]
_gap = [mcs_from_se(se_from_sinr(s)) for s in (0, 10, 20)]
check("the engine's AMC is more aggressive than the Shannon gap",
      all(a > b for a, b in zip(_sel, _gap)),
      f"{_sel} against {_gap} at 0/10/20 dB — five to six MCS steps, which "
      f"is why the gap rule under-reports block errors")
check("it is OFF by default, and the measurement says why",
      wm.USE_ENGINE_AMC is False,
      "on the reference it moves the block error rate 3.11 -> 13.32 against "
      "10.11, but across six breadth cases the mean |ratio-1| goes 0.452 -> "
      "0.923: it swaps a flat 3% for a flat 13% where the engine ranges "
      "4.4-28.5%")
_before = wm.USE_ENGINE_AMC
try:
    wm.USE_ENGINE_AMC = True
    wm._MCS_CACHE.clear()
    _on = wm.cell_window(0, {1: 20.0}, {1: 50e6}, 50.0, 2, window_s=1.0).ues[0]
    wm.USE_ENGINE_AMC = False
    wm._MCS_CACHE.clear()
    _off = wm.cell_window(0, {1: 20.0}, {1: 50e6}, 50.0, 2, window_s=1.0).ues[0]
finally:
    wm.USE_ENGINE_AMC = _before
    wm._MCS_CACHE.clear()
check("both paths run and disagree",
      _on.mcs != _off.mcs and _on.bler != _off.bler,
      f"MCS {_off.mcs} -> {_on.mcs}, BLER {_off.bler:.3f} -> {_on.bler:.3f} "
      f"at 20 dB — a switch nothing exercises is a switch that has rotted")

print("\nthe config editor's badges are built, not asserted")
from schema import NSGSimulationConfig as _Cfg
_ui = _Cfg.ui_json_schema()
_reads, _tier = [], []
def _walk(n):
    if isinstance(n, dict):
        if "x_reads" in n: _reads.append(tuple(n["x_reads"]))
        if "x_tier" in n: _tier.append(n["x_tier"])
        for v in n.values(): _walk(v)
    elif isinstance(n, list):
        for v in n: _walk(v)
_walk(_ui)
check("ui_json_schema still builds",
      isinstance(_ui, dict) and "$defs" in _ui,
      "it calls `_resolve_path`, which a block rewrite DELETED for two "
      "commits — `ui_json_schema()` raised NameError the whole time and "
      "nothing here noticed, because every test used model_json_schema")
check("every field carries a measured consumer set",
      len(_reads) > 150,
      f"{len(_reads)} fields stamped with x_reads from "
      f"tools/param_consumers.json — the editor says who reads a field "
      f"instead of the user finding out from a run")
check("the tier badges are stamped",
      len(_tier) > 10 and set(_tier) <= {"fast", "slow", "coverage", "label",
                                         "partial"},
      f"{len(_tier)} fields: {sorted(set(_tier))} — 'analytical' is gone "
      f"with the tier it named")

print("\nthe levers the config offers and this tier has to honour")
import copy as _copy
from schema import Anomaly as _An
import tier_criterion as _tc
_n, _cfg, _ = _tc.load_scenario("Partition test")


def _hand(mut, mobile=True, n=20):
    c = _copy.deepcopy(_cfg)
    if mobile:
        for g in c.ue_groups:
            g.mobility.speed_mps = 20.0
    mut(c)
    return tier_b.frames(nsoran_params(c, engine_clamped=False), n)


_loose = _hand(lambda c: setattr(c.settings, "ho_a3_entry", False))[-1]
_tight = _hand(lambda c: setattr(c.settings, "ho_sinr_margin_db", 12.0))[-1]
check("the A3 offset holds UEs on their serving cell",
      _tight["handovers"] < _loose["handovers"],
      f"{_loose['handovers']} handovers with A3 off, {_tight['handovers']} at "
      f"a 12 dB offset — the tier took the strongest cell every window, so "
      f"every threshold an xApp tunes was inert and a boundary UE ping-ponged "
      f"on the shadowing draw")

_a2 = [_hand(lambda c, a=a: setattr(c.settings, "ho_a2_threshold_db", a))[-1]
       ["handovers"] for a in (25.0, 5.0, -5.0)]
check("A2 is monotone, including at 0 dB",
      _a2[0] >= _a2[1] >= _a2[2],
      f"{_a2} for A2 = 25/5/-5 dB. `getattr(...) or -1000.0` folded the "
      f"legitimate value 0.0 dB into the DISABLED sentinel, so the strictest "
      f"setting in the field became the loosest")

_pw = [c["power_w"] for c in _hand(lambda c: None, mobile=False)[-1]["cells"]]
check("every cell reports an RU draw",
      len(_pw) == 6 and all(400.0 < w < 3800.0 for w in _pw),
      f"{[round(w) for w in _pw]} W — the engine charges "
      f"`GetStateA(state) * 5 V`, so a cell sits between 431 W idle and "
      f"3.7 kW transmitting. Tier B had no energy model at all and the "
      f"console reported RADIATED power, 10 W at 40 dBm, beside a slow tier "
      f"reporting kilowatts for the same scenario")

_cid = _cfg.base_stations[1].id
_out = _hand(lambda c: setattr(c, "anomalies", [
    _An(cell_id=_cid, type="cell_outage", start_s=4.0, duration_s=3.0)]),
    mobile=False, n=8)
_during = sum(c["power_w"] for c in _out[4]["cells"])
_after = sum(c["power_w"] for c in _out[7]["cells"])
check("a scheduled outage fires, and stops",
      _during < _after
      and _out[4]["cells"][1]["power_w"] == 0.0
      and _out[7]["cells"][1]["power_w"] > 0.0,
      f"failed cell 0 W at t=5 and {_out[7]['cells'][1]['power_w']:.0f} W at "
      f"t=8, network {_during:.0f} -> {_after:.0f} W. It does NOT return to "
      f"the 17870 W it started at, and should not: the cell comes back up "
      f"EMPTY because the A3 offset keeps its UEs on the cells they fled to. "
      f"Asserting the network recovers fully was asserting a tier with no "
      f"handover hysteresis")

_blk = _hand(lambda c: setattr(c, "anomalies", [
    _An(cell_id=_cid, type="handover_block", start_s=3.0, duration_s=5.0)]),
    n=8)
_free = _hand(lambda c: None, n=8)
check("a handover block freezes handovers without dropping UEs",
      _blk[6]["handovers"] < _free[6]["handovers"]
      and len(_blk[6]["ues"]) == len(_free[6]["ues"]),
      f"{_blk[6]['handovers']} vs {_free[6]['handovers']} by t=7, same "
      f"{len(_blk[6]['ues'])} UEs — a blocked handover is a UE stuck on a "
      f"cell it is walking away from, not a UE that disappears")

def _sleeping(mode):
    # THROUGH THE CONFIG, so the depth encoding is exercised end to end.
    # Handing `frames` a "7,200,20.0" string tested the ns-3 enum, which is
    # not what `_sleep_params` puts on the wire — the assertion passed while
    # no real scenario could reach the branch.
    c = _copy.deepcopy(_cfg)
    for b in c.base_stations:
        b.radio.sleep.mode = mode
    return tier_b.frames(nsoran_params(c, engine_clamped=False), 3,
                         demand_bps={i: 0.0 for i in range(1, 40)})[-1]


_empty, _busy = _sleeping("deep"), _sleeping("none")
check("deep sleep costs less than idle on an unloaded cell",
      (sum(c["power_w"] for c in _empty["cells"])
       < sum(c["power_w"] for c in _busy["cells"]) * 0.95),
      f"{sum(c['power_w'] for c in _empty['cells']):.0f} W asleep vs "
      f"{sum(c['power_w'] for c in _busy['cells']):.0f} W awake — the idle "
      f"timer is why this only pays on a cell serving nobody")

_sub = _hand(lambda c: setattr(c, "anomalies", [
    _An(cell_id=_cid, type="cell_outage", start_s=0.1, duration_s=0.2)]),
    mobile=False, n=3)
check("a fault SHORTER than a window still fires",
      _sub[0]["cells"][1]["power_w"] == 0.0,
      f"{_sub[0]['cells'][1]['power_w']} W in the window covering 0.1-0.3 s. "
      f"The schedule test asked whether the fault straddled the INSTANT the "
      f"window closes, so every fault shorter than a window vanished — "
      f"including the whole of the slow tier's own fixture set, whose "
      f"anomalies last 0.2 s against a 1 s window here")

import asyncio as _aio
from main import _run_fast_tier as _rft
from nsoran_live import summarise_batch_frames as _sbf
_c2 = _copy.deepcopy(_cfg)
_c2.anomalies = [_An(cell_id=_cid, type="cell_outage", start_s=3.0, duration_s=3.0),
                 _An(cell_id=_cfg.base_stations[2].id, type="noise_rise",
                     start_s=99.0, duration_s=2.0, noise_rise_db=20.0)]
_res = _aio.run(_rft(_c2, nsoran_params(_c2, engine_clamped=False), 8))
_sum = _sbf(_res["frames"], _res.get("fault_events"), len(_c2.anomalies))
check("the horizon audit works on this tier too",
      _sum.get("anomalies_configured") == 2 and _sum.get("anomalies_fired") == 1,
      f"{_sum.get('anomalies_fired')} of {_sum.get('anomalies_configured')} "
      f"fired — the one at start_s=99 is past an 8-window horizon and has "
      f"to be caught. `fault_events` was hardcoded None here, so a fast-tier "
      f"run with any anomaly reported that NONE of them happened")

from schema import SlicePolicy as _SP


def _sliced(policy):
    c = _copy.deepcopy(_cfg)
    # ENOUGH UEs THAT THE CELL IS ACTUALLY CONTENDED. A slice policy only
    # binds when its members want more airtime than they are allowed, and
    # each UE is now capped at the share of the window its serving link is
    # reachable in — about half, at this geometry — so six UEs a cell fit
    # with room to spare and no policy has anything to cap. The engine's
    # own reference behaves the same way: three or four UEs a cell get a
    # block every slot and the rest get a tenth of that. Contention needs
    # more UEs, not more demand.
    for _g in c.ue_groups:
        _g.count = int(_g.count) * 4
    # Two classes, because a policy everyone is inside partitions nothing.
    half = max(1, len(c.ue_groups) // 2)
    for g in c.ue_groups[:half]:
        for svc in g.services:
            svc.qos_id = 1
    for g in c.ue_groups[half:]:
        for svc in g.services:
            svc.qos_id = 9
    c.slices = policy
    p = nsoran_params(c, engine_clamped=False)
    tags = tier_b.slice_policy(p)[0]
    f = tier_b.frames(p, 3, demand_bps={i: 60e6 for i in range(1, 60)})[-1]
    a = sum(u["thp_kbps"] for u in f["ues"] if tags.get(u["imsi"]) == 1)
    b = sum(u["thp_kbps"] for u in f["ues"] if tags.get(u["imsi"]) == 9)
    return a / 1e3, b / 1e3


_none = _sliced([])
_floor = _sliced([_SP(qos_id=1, max_pct=100.0, min_pct=60.0)])
_cap = _sliced([_SP(qos_id=1, max_pct=10.0, min_pct=0.0)])
_share_none = _none[0] / max(1e-9, sum(_none))
_share_floor = _floor[0] / max(1e-9, sum(_floor))
check("a floor trims the OTHER slices, it does not cap its owner",
      _share_floor > _share_none and _floor[1] < _none[1]
      and sum(_floor) > sum(_none) * 0.9
      and _share_floor > 0.60,
      f"a 60% floor takes the protected slice's share from "
      f"{_share_none * 100:.0f}% to {_share_floor * 100:.0f}% and the rest "
      f"from {_none[1]:.0f} to {_floor[1]:.0f} Mbps, with the total holding "
      f"at {sum(_floor):.0f} of {sum(_none):.0f}. Three things and the old "
      f"code failed all three: it set the protected budget EQUAL to the "
      f"floor, so a guarantee became a ceiling; it never trimmed anybody; "
      f"and where every slice had a floor the rest of the cell went to "
      f"nobody — two floors of 20% and 10% delivered 57 Mbps against an "
      f"unsliced 159. The slice does not necessarily gain THROUGHPUT, "
      f"because its own UEs are capped at the share of the window they are "
      f"reachable in; what it gains is share")
check("a ceiling still caps its owner",
      _cap[0] < _none[0] and _cap[1] > _none[1],
      f"capped {_none[0]:.0f} -> {_cap[0]:.0f} Mbps while the rest go "
      f"{_none[1]:.0f} -> {_cap[1]:.0f} — max_pct could not bind at ANY "
      f"value while the floor was already holding the budget below it")

print("\nthe spread is a MIXTURE, and it has a ground truth")
_pk = wm.upa_gain_db(8, 8, 0.5, 0.5, 20.0, 100.0, 20.0, 100.0)
check("steered at itself, the array factor IS the coherent peak",
      abs(_pk - wm.array_gain_db(8, 8)) < 1e-9,
      f"{_pk:.2f} dB — if the general form did not reduce to the serving "
      f"case, the two would be different models of one panel")
_sph = 0.0
for _n in range(4000):
    _ct = -1.0 + 2.0 * (_n + 0.5) / 4000
    _th = math.degrees(math.acos(_ct))
    _sph += 10 ** (wm.upa_gain_db(8, 8, 0.5, 0.5, (_n * 137.5) % 360 - 180,
                                  _th, 0.0, 90.0) / 10)
check("an array moves power, it does not make it",
      -3.0 < 10 * math.log10(_sph / 4000) < 1.0,
      f"{10 * math.log10(_sph / 4000):+.2f} dB averaged over the sphere — a "
      f"gain that integrated above 0 would be inventing energy, and the "
      f"whole reason an interferer's beam is NOT worth 18 dB at a bystander")
# The first null of an 8-element half-wavelength array is at
# asin(1/(N*d)) = 14.48 degrees, and that is where a neighbour's beam has
# to be to leave a bystander alone.
_null = wm.upa_gain_db(8, 8, 0.5, 0.5, 0.0, 90.0, 14.48, 90.0)
_lobe = wm.upa_gain_db(8, 8, 0.5, 0.5, 0.0, 90.0, 20.0, 90.0)
check("steered away, the gain collapses into nulls and sidelobes",
      _null < _pk - 40.0 and _lobe < _pk - 10.0 and _lobe > _null + 20.0,
      f"{_pk:.1f} dB on the beam, {_null:.1f} at the first null (14.5 deg) "
      f"and {_lobe:.1f} on the sidelobe past it — NOT a smooth roll-off. "
      f"That structure is the point: the engine's trace measures up to "
      f"50.6 dB between a neighbour serving one of its UEs and another, "
      f"which no average over directions can produce")

_lk = tier_b.link_state(nsoran_params(_cfg2, engine_clamped=False))
_sd = sorted(v.get("sinr_sd_db", 0.0) for v in _lk.values())
_mid = _sd[len(_sd) // 2]
check("the per-UE spread lands on the engine's own measurement",
      10.0 < _mid < 21.0,
      f"median {_mid:.2f} dB against the engine's PHY trace at 15.22 "
      f"(mean 12.68, range 0.07-26.49). Flat Rayleigh gives 5.57 dB for "
      f"every UE and five closed forms all scored worse than that, because "
      f"every one of them modelled a FADING sigma — the trace says "
      f"consecutive transmissions 0.25 ms apart differ by 0.020 dB while "
      f"the same UE swings tens of dB, which is the LOS/NLOS condition "
      f"re-drawing every 100 ms and the neighbour's beam moving")
check("and the level with it",
      38.0 < statistics.fmean(
          v["sinr_db"] for v in _lk.values()) < 50.0,
      f"{statistics.fmean(v['sinr_db'] for v in _lk.values()):.2f} dB mean "
      f"against the trace's 45.46. It read 28.25 before: the tier summed "
      f"interference from every cell in the scene where ns-3 delivers "
      f"nothing above MaxLossDb=110, and gave the UE no array although "
      f"MmWaveHelper builds it a 2x2")

import tier_breadth as _tbr
_bld = tier_b.link_state(nsoran_params(
    _tbr.CASES["buildings"](_copy.deepcopy(_cfg2)), engine_clamped=False))
_blv = statistics.fmean(v["sinr_db"] for v in _bld.values())
_bsd = statistics.fmean(v.get("sinr_sd_db", 0.0) for v in _bld.values())
check("with buildings the level is right and the spread is HALF",
      21.0 < _blv < 28.0 and 4.0 < _bsd < 9.0,
      f"level {_blv:.2f} against the engine's 24.50 and spread {_bsd:.2f} "
      f"against 11.68 — measured on its own PHY trace "
      f"(tools/rx_trace.py --run --case buildings). Buildings switch ns-3 to "
      f"BuildingsChannelConditionModel, which is GEOMETRIC, so the condition "
      f"never re-draws and two of the mixture's three terms are dead: the "
      f"beam carries it alone. Ruled out as the missing half — the cells are "
      f"100% slot-occupied so it is not duty, and a Rayleigh factor on the "
      f"interferers buys 1 dB. This bound is asserted so it cannot drift "
      f"quietly while it is open")

import tier_breadth as _tbr2
_pp = nsoran_params(_cfg2, engine_clamped=False)
check("the two tiers are compared over the same SIMULATED time",
      _tbr2._windows_for(_pp, 2.0) == 2 and _tbr2._windows_for(_pp, None) == 40,
      f"{_tbr2._windows_for(_pp, 2.0)} windows for a 2.0 s span at a "
      f"{tier_b.window_s(_pp):g} s window. Tier A's 40 frames ARE about 2 "
      f"simulated seconds and Tier B's 40 windows are 40, so anything that "
      f"accumulates was compared over 20x the time: the queue pinned at its "
      f"10 MB RLC buffer in every breadth case against the engine's "
      f"5.5-6.6 MB, which read as a systematic 1.7-1.9x model error. "
      f"Matched, 1.13-1.44")

_qc = _copy.deepcopy(_cfg2)
_qc.slices = [_SP(qos_id=1, max_pct=30.0, min_pct=0.0)]
for _g in _qc.ue_groups[:len(_qc.ue_groups) // 2]:
    for _s in _g.services:
        _s.qos_id = 1
_qf = tier_b.frames(nsoran_params(_qc, engine_clamped=False), 2,
                    demand_bps={i: 60e6 for i in range(1, 60)})[-1]
_qt = _qf["measurements"]["TB.TotNbrDl.1"]
check("a capped slice is filled greedily, so its blocks are few and big",
      _qt["mean"] < _qt["max"] * 0.75,
      f"blocks/UE min {_qt['min']:.0f} mean {_qt['mean']:.0f} max "
      f"{_qt['max']:.0f}. The engine hands a capped slice's ceiling out UE "
      f"BY UE from a rotating start and whoever it runs out on gets ZERO "
      f"that subframe, so the same airtime comes out as few big transport "
      f"blocks. Sharing it evenly instead put the slice case's mean at 2067 "
      f"blocks against the engine's 450; greedy puts it at 1000")

_av = tier_b.link_state(nsoran_params(_cfg2, engine_clamped=False))
_frac = sorted(v.get("served_frac", 1.0) for v in _av.values())
_lk2 = tier_b.frames(nsoran_params(_cfg2, engine_clamped=False), 2,
                     demand_bps={i: 50e6 for i in range(1, 60)})[-1]
_tbm = _lk2["measurements"]["TB.TotNbrDl.1"]
check("a UE is only schedulable while its serving link is in range",
      0.35 < statistics.fmean(_frac) < 0.75 and _tbm["min"] < _tbm["max"] * 0.3,
      f"availability {statistics.fmean(_frac):.2f} mean over 36 UEs, "
      f"{sum(1 for x in _frac if x > 0.999)} of them always reachable — so "
      f"about {sum(_frac):.0f} are schedulable at any instant, against the "
      f"engine's median of 19 UEs per frame. Its own serving link flips "
      f"LOS/NLOS every 100 ms and at this geometry the NLOS branch is often "
      f"past MaxLossDb, so blocks/UE run {_tbm['min']:.0f}/{_tbm['mean']:.0f}"
      f"/{_tbm['max']:.0f} against the engine's 0/1950/4005. Serving every "
      f"UE for the whole window put that minimum at 3000")

_pp2 = nsoran_params(_cfg2, engine_clamped=False)
_hk = {k: tier_b.frames({**_pp2, "rsrp_filter_coeff": k}, 4)[-1]["handovers"] / 4
       for k in (0, 4, 12)}
_hoff = tier_b.frames({**_pp2, "admit_handover": "0"}, 4)[-1]["handovers"]
check("a handover is the serving cell going OUT OF RANGE, filtered",
      10.0 < _hk[4] < 35.0 and _hk[0] > _hk[4] > _hk[12] and _hoff == 0,
      f"{_hk[4]:.1f} a second at the shipped rsrp_filter_coeff of 4, "
      f"{_hk[0]:.1f} unfiltered and {_hk[12]:.1f} at 12, and {_hoff} with "
      f"admission off. The engine gives 13 — over 0.9 s in one run and over "
      f"2.0 s in another, so its own spread on this metric is about 2x. "
      f"Counting every change in the strongest-cell RANKING instead gave "
      f"1743 over twenty windows; the event is narrower, and the L3 filter "
      f"is what keeps a single 100 ms dropout from being a handover. At a "
      f"matched horizon a deterministic walk crosses no boundary, so "
      f"without this the tier reported ZERO")

_two = _copy.deepcopy(_cfg2)
for _i, _b in enumerate(_two.base_stations):
    if _i == len(_two.base_stations) - 1:
        _b.radio.band.frequency_mhz = 700.0
        _b.radio.band.bandwidth_mhz = 20.0
_2p = nsoran_params(_two, engine_clamped=False)
_ls = _copy.copy(tier_b.link_state(_2p))
_fr = tier_b.frames(_2p, 1, demand_bps={i: 20e6 for i in range(1, 60)})[0]
_a = sorted((u["cell"] - tier_b.wm_cell_base()) for u in _fr["ues"])
_b2 = sorted(v["cell"] for v in _ls.values())
check("the serving cell a frame reports is the one link_state chose",
      _a == _b2,
      f"{len(set(_a))} cells in the frame against {len(set(_b2))} in the "
      f"links. The L3 filter used to re-pick the serving cell by a plain "
      f"argmax on the filtered level, which silently replaced "
      f"`_select_cell` — and reselection PRIORITY lives there. On a "
      f"two-band scene the argmax moves every UE to the low band because "
      f"700 MHz propagates better: 133 of 150 UEs landed on the N28 "
      f"coverage cell that the same run's live console showed empty. The "
      f"reference scenario is single-band, which is why nothing caught it")

_sl = _copy.deepcopy(_cfg2)
for _b in _sl.base_stations:
    _b.radio.sleep.mode = "deep"
    _b.radio.sleep.idle_timer_ms = 1000.0
_sf = tier_b.frames(nsoran_params(_sl, engine_clamped=False), 4,
                    demand_bps={i: 0.0 for i in range(1, 60)})
_first = max(c["power_w"] for c in _sf[0]["cells"])
_later = max(c["power_w"] for c in _sf[-1]["cells"])
check("an idle timer longer than one window can still expire",
      _later < _first * 0.85,
      f"{_first:.0f} W in the first window and {_later:.0f} W in the fourth, "
      f"with a 1000 ms idle timer on a 1 s window. The timer used to restart "
      f"every window, so a cell configured for deep sleep never slept at "
      f"all — the energy-saving scenario's own 'South Coverage — Deep Sleep' "
      f"cell sat at 684 W, the IDLE figure, for the whole run")

_mv = _copy.deepcopy(_cfg2)
for _g in _mv.ue_groups:
    _g.mobility.speed_mps = 14.0
_mp = nsoran_params(_mv, engine_clamped=False)
_p0 = tier_b._parse_points(_mp["ue_positions"])
_end = None
for _f in tier_b.stream(_mp, 600):
    _end = _f
_far = max(abs(v) for pt in tier_b._parse_points(_mp["ue_positions"]) for v in pt[:2])
_lo, _hi = tier_b.WALK_BOUNDS[0], tier_b.WALK_BOUNDS[1]
check("a UE walks like ns-3's: bounded, and diffusive",
      tier_b.WALK_LEG_M == 30.0 and _hi == 4000.0,
      f"box {_lo:.0f}..{_hi:.0f} m with a new direction every "
      f"{tier_b.WALK_LEG_M:.0f} m — `scenario-zero` gives every UE a "
      f"RandomWalk2dOutdoorMobilityModel with Bounds Rectangle(0, 4000, 0, "
      f"4000) and overrides neither Mode nor Distance, so ns-3 defaults to "
      f"turning every 30 m. This walked a FIXED bearing forever: over the "
      f"two simulated seconds a batch run used to reach that is invisible, "
      f"and at 600 windows the UEs spanned 16 km against a 1.4 km scene "
      f"with the median distance to the nearest cell going 62 m to 762. "
      f"Bounded and diffusive it is 136 m, and net displacement grows with "
      f"the square root of time")

_hoq = _copy.deepcopy(_cfg2)
_route = nsoran_params(_hoq, engine_clamped=False)
_route["ue_waypoints"] = ";".join(
    "1900.0,2000.0,1.5,0.000|2300.0,2000.0,1.5,20.000"
    for _ in _route["ue_positions"].split(";"))
_pp0 = tier_b._on_path(tier_b._waypoint_paths(_route)[0], 0.0)
_pp1 = tier_b._on_path(tier_b._waypoint_paths(_route)[0], 10.0)
_pp2 = tier_b._on_path(tier_b._waypoint_paths(_route)[0], 99.0)
check("a UE with a route follows it instead of wandering",
      abs(_pp0[0] - 1900) < 1 and abs(_pp1[0] - 2100) < 1
      and abs(_pp2[0] - 2300) < 1,
      f"x goes {_pp0[0]:.0f} -> {_pp1[0]:.0f} at half time -> {_pp2[0]:.0f} "
      f"and stays there. `ue_waypoints` was in the payload all along and "
      f"this tier read none of it, so the two handover demos — whose whole "
      f"point is a vehicle crossing a boundary on a defined route — walked "
      f"at random. The mode is already baked into the path by "
      f"`_ue_waypoint_specs`, so nothing here has to know about it")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    raise SystemExit(1)
print("all passed")
