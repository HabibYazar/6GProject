#!/usr/bin/env python3
"""What actually limits the engine's uplink? Sweep one lever, keep a control.

WHERE THIS STARTED. Tier B's uplink delivered 3.9x what the engine does at a
matched window, and nothing in the counters said whose fault that was. Two
facts had to be established first and both came out of data already on disk:

  * THE UNIT. `QosFlow.PdcpPduVolumeDL` over `TB.TotNbrDl.1` is 520-1283
    BYTES per transport block across six stored cases — inside the engine's
    own MCS table, where kbit would put it at 65-160 kB against a
    largest-possible 5,097 bytes at 50 MHz. `DRB.PdcpSduVolumeUl` is kbit.
  * THE WINDOW. The gap first read 18x because it compared a 30-frame model
    against a 40-frame engine run, on a counter that is extensive.

WHAT THIS TOOL IS FOR. Each sweep moves ONE engine lever and asks whether the
delivered uplink follows it. The verdict is only readable because of the
CONTROL: a lever that moves nothing at all cannot tell a genuinely flat
uplink from a parameter that never reached the radio, and this repo has
shipped that failure — a runtime SetTxPower that never arrived, where
byte-identical runs were the only tell. So every sweep prints what the
DOWNLINK did too, and refuses a verdict when neither moved.

WHAT THE SWEEPS SETTLED, 2026-09-21. Five levers, each with the downlink
beside it, and the answer is that the 5x is TWO effects of about equal size
and NEITHER of them is the link budget:

  load      20x the offered uplink (2,500 -> 50,000 kbit/UE) moves the
            delivered uplink 1.07x and not monotonically, while the
            downlink moves 4.50x. So it is a CEILING, not a proportional
            loss, and no symbol share alone produces a fixed rate under a
            rising offer.
  ues       12/36/72 UEs on the same six cells: per-UE uplink 0.24x against
            0.33x predicted by a shared CELL CAPACITY and 1.00x by a per-UE
            GRANT RATE. It is AIRTIME being divided, so a symbol share IS
            the right shape — the model's is merely too large.
  ue_tx     20 dB of UE transmit power moves the uplink 0.88x and NOT
            monotonically; the downlink moves 1.85x monotonically, through
            the UE-to-UE interference every node on one SpectrumChannel
            contributes. Not the link budget. (Raising every UE together
            raises uplink signal and uplink interference equally.)
  srs       80 vs 320 ms: 0.99x. Not the sounding rate. The parameter
            demonstrably arrives — at 20 ms the run dies with the engine's
            own "too many UEs (21) for current SRS periodicity 20".
  mcs_ul    PINNING the uplink MCS moves it 7.15x and monotonically:
            720 / 994 / 5,148 kbit/UE at MCS 4 / 12 / 24. THIS lever is a
            limit.
            CORRECTION, from reading the scheduler: the free-AMC run at
            1,944 CANNOT be interpolated between those arms to read off an
            effective MCS, and an earlier note here did exactly that and
            got "about 1.8x". `mmwave-flex-tti-mac-scheduler.cc` has
                if (cqi == 0 && !m_fixedMcsUl) continue;
            so pinning the MCS also stops the scheduler SKIPPING UEs whose
            uplink CQI is out of range. The pinned arms therefore serve a
            different population from the free one, and the comparison
            measures two things at once. The 7.15x across the pinned arms
            stands, because those three share the population.
  symperslot  MEASURED NOTHING. `settings.sym_per_slot` reaches ns-3 as
            MmWaveFlexTtiMacScheduler::SymPerSlot (it is in the launched
            command line), yet arms at 2 and 6 are byte-identical on every
            counter. The engine is deterministic at a fixed seed, so those
            two runs were the same configuration whatever the payload said.
            `phy-mac.patch` records the mechanism for a sibling field: the
            scenario Config::SetDefault's it AFTER cmd.Parse. The probe
            detects this itself now.

WHAT THE SOURCE ADDS, read 2026-09-21 in the engine distro at
/home/builder/ns-O-RAN-flexric/mmwave-LENA-oran/src/mmwave/model. The uplink
is a different code path from the downlink in five ways, and this model has
none of them:

  * the AMC decides for a 12-symbol block ALWAYS (`m_numSymForCqi = 12`,
    a compile-time constant citing TS 38.214 5.2.2.5) while a UE here holds
    about 0.4 uplink symbols — an MCS chosen for a block thirty times the
    one sent;
  * the downlink round-trips MCS -> CQI -> MCS through sixteen levels and
    the uplink does not;
  * a UE with no uplink CQI gets MCS 0, and the uplink CQI exists only after
    a real PUSCH transmission and expires after `CqiTimerThreshold` passes;
  * the uplink needs a BSR, quantised through `BsrId2BufferSize` and
    decremented by each grant;
  * an out-of-range uplink CQI skips the UE unless the MCS is pinned.

THE DECOMPOSITION, with the caveat above applied. This model delivers 9,711
kbit/UE where the engine's free AMC gives 1,944 — 5.0x. Against the engine
PINNED at MCS 24, a higher index than its AMC chooses AND a run that serves
more UEs, the model is still 1.89x high. So part of the 5.0 is airtime and
part is a conservative uplink AMC; the split between them is NOT cleanly
measured, and the earlier "1.8x times 1.9x" here claimed more precision than
the data carries.

NOTHING IS FITTED FROM THIS. Either half could be closed with one constant —
a UL MCS offset, or a smaller UL_SYMBOLS_PER_SLOT — and both would be
absorbing a mechanism rather than modelling it, which is what
`window_model.BEAMFORMING_LOSS_DB` stands as the warning for.

    python3 tools/uplink_probe.py --sweep mcs_ul --frames 30
    python3 tools/uplink_probe.py --sweep load
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "nsg_ai"))
sys.path.insert(0, HERE)

import tier_criterion as tc

GREEN, YELLOW, RED, DIM, RESET = (
    "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m")
OUT = os.path.join(HERE, "uplink_probe.json")

# Each sweep: (values, how to apply it, what the payload should then read).
# The payload assertion is not ceremony — `nsoran_params` clamps, and a lever
# that the clamp flattens produces a perfect null for the wrong reason.
SWEEPS = {
    "ue_tx": {
        # The schema's default is 30 and its ceiling 40. Twenty dB is far
        # outside the engine's own run-to-run noise, so a null is a real null.
        "values": (20.0, 30.0, 40.0),
        "unit": "dBm",
        "apply": lambda cfg, v: setattr(cfg.settings, "ue_tx_power_dbm", v),
        "payload": lambda p: float(p["ue_tx_dbm"]),
    },
    "srs": {
        # Only the TS 36.213 table values exist; ns-3 looks the number up and
        # nothing else is defined. 320 is the default, 20 is 16x more often.
        "values": (20, 80, 320),
        "unit": "ms",
        "apply": lambda cfg, v: setattr(cfg.settings, "srs_periodicity", v),
        "payload": lambda p: float(p["srs_periodicity"]),
    },
    "mcs_ul": {
        # THE LAST CANDIDATE STANDING. Per uplink SYMBOL the model carries 5x
        # what the engine does, which is a spectral-efficiency ratio and so
        # an MCS ratio — and `ue_tx` proved it is not SINR. Pinning the
        # uplink MCS takes the engine's AMC out of the loop entirely
        # (MmWaveFlexTtiMacScheduler::FixedMcsUl + McsDefaultUl).
        #
        # If pinning it high multiplies the delivered uplink, the engine's UL
        # AMC is choosing a low index for a reason that is not the link
        # budget, and the ratio between the arms MEASURES the index it was
        # choosing. If pinning changes nothing, the ceiling is not the MCS
        # either and the airtime itself is smaller than the symbol arithmetic
        # says.
        "values": (4, 12, 24),
        "unit": "MCS",
        "apply": lambda cfg, v: setattr(cfg.settings, "fixed_mcs_ul", v),
        "payload": lambda p: float(p["fixed_mcs_ul"]),
    },
    "symperslot": {
        # `settings.sym_per_slot` -> MmWaveFlexTtiMacScheduler::SymPerSlot,
        # the scheduler's TTI GRANULARITY, default 6. `window_model` hardcodes
        # 14 for the whole symbol arithmetic and has never read this field —
        # it was found by reading the arguments an actual run was launched
        # with, which is the only place the shipped value appears.
        #
        # radio-2c.patch is explicit that this is NOT the TDD split: the
        # scheduler "derives the DL and UL symbol counts from demand every
        # slot (m_maxDlSymbols / m_maxUlSymbols with a deficit loop)". But a
        # granularity of 6 in a slot with 12 data symbols is two allocations
        # a slot, where the model assumes twelve one-symbol ones, and that is
        # exactly the kind of factor the uplink is missing.
        "values": (2, 6, 14),
        "unit": "sym",
        "apply": lambda cfg, v: setattr(cfg.settings, "sym_per_slot", v),
        "payload": lambda p: float(p["sym_per_slot"]),
    },
    "ues": {
        # THE ONE THAT SEPARATES A GRANT RATE FROM A CAPACITY. Both saturate
        # against offered load, so the `load` sweep cannot tell them apart —
        # but they scale oppositely in the number of UEs sharing a cell. A
        # per-UE grant ceiling gives every UE the same rate however many
        # there are; a per-CELL capacity divides. 6 cells, so these are 2, 6
        # and 12 UEs per cell.
        "values": (12, 36, 72),
        "unit": "UEs",
        "apply": lambda cfg, v: _set_ue_total(cfg, v),
        "payload": lambda p: float(p["n_ue"]),
    },
    "load": {
        # Gbps per UE. The reference offers 0.05; the uplink OnOff is built
        # at a quarter of it, so the offered uplink is 12.5 Mbps there.
        "values": (0.01, 0.05, 0.2),
        "unit": "Gbps/UE",
        "apply": lambda cfg, v: [setattr(s, "target_throughput_gbps", v)
                                 for g in cfg.ue_groups for s in g.services],
        "payload": lambda p: None,
    },
}


def _set_ue_total(cfg, total: int) -> None:
    """Spread `total` UEs over the existing groups, keeping their positions.

    Scaling the counts rather than adding groups keeps the GEOMETRY fixed —
    same spawn points, same spread — so the only thing that changes is how
    many UEs share each cell, which is the whole question.
    """
    groups = cfg.ue_groups
    base = total // len(groups)
    for i, g in enumerate(groups):
        g.count = base + (1 if i < total - base * len(groups) else 0)


def harvest_of(frames: list) -> dict:
    """The uplink figures, and the downlink ones that are the control."""
    settled = tc.settled(frames)
    out = {}
    for k in ("DRB.PdcpSduVolumeUl", "DRB.PdcpSduBitRateUl",
              "QosFlow.PdcpPduVolumeDL", "TB.TotNbrDl.1", "RRU.PrbUsedDl"):
        v = tc.wmean(settled, k)
        if v is not None:
            out[k] = v
    # Bytes per DOWNLINK block: the check that the run is the run we think it
    # is, because it has to stay inside the MCS table.
    if out.get("QosFlow.PdcpPduVolumeDL") and out.get("TB.TotNbrDl.1"):
        out["_dl_bytes_per_block"] = (out["QosFlow.PdcpPduVolumeDL"]
                                      / out["TB.TotNbrDl.1"])
    return out


def offered_ul_kbit(cfg) -> float:
    """What the scenario's uplink OnOff is built to offer, per UE per second.

    `DataRate((uint64_t)(svc.rate * 1e6 * 0.25))` — a quarter of the service
    rate, which is the only place the 0.25 in `window_model.UL_DEMAND_RATIO`
    comes from.
    """
    rates = [sum(s.target_throughput_gbps for s in g.services)
             for g in cfg.ue_groups]
    return statistics.fmean(rates) * 1e9 * 0.25 / 1e3 if rates else float("nan")


async def main_async(sweep: str, frames: int, wall_cap: float) -> int:
    from main import nsoran_params
    spec = SWEEPS[sweep]
    crit = json.load(open(os.path.join(HERE, "tier_criterion.json")))["scenario"]
    name, base, _ = tc.load_scenario(crit[:20])
    print(f"scenario: {name}\nsweep:    {sweep} over "
          f"{', '.join(str(v) for v in spec['values'])} {spec['unit']}\n")

    all_saved = json.load(open(OUT)) if os.path.exists(OUT) else {}
    # The older file was a flat {value: row} for the ue_tx sweep only.
    if all_saved and not isinstance(next(iter(all_saved.values())), dict):
        all_saved = {}
    if all_saved and "status" in next(iter(all_saved.values())):
        all_saved = {"ue_tx": all_saved}
    saved = all_saved.setdefault(sweep, {})

    for v in spec["values"]:
        key = f"{v:g}"
        if key in saved:
            print(f"  {sweep} {key:>6} {spec['unit']}  {DIM}already saved{RESET}")
            continue
        cfg = copy.deepcopy(base)
        spec["apply"](cfg, v)
        params = nsoran_params(cfg)
        want = spec["payload"](params)
        if want is not None and abs(want - float(v)) > 1e-6:
            print(f"  {RED}{sweep}={v} reaches the payload as {want} — the "
                  f"envelope clamped it, so this arm would measure the "
                  f"clamp{RESET}")
            continue
        t0 = time.time()
        # ONE ENGINE, ONE RUN AT A TIME. The first attempt fired three arms
        # back to back, the second and third hit the run lock and came back
        # `busy` with no frames — and the probe SAVED them, so a resume would
        # have skipped them forever and the sweep would have reported one arm
        # as though it were three.
        r = {}
        for attempt in range(8):
            r, _ = await tc.run_tier_a(cfg, params, frames, wall_cap)
            if r.get("status") != "busy":
                break
            print(f"    engine busy, waiting 60 s "
                  f"(attempt {attempt + 1}/8)", flush=True)
            await asyncio.sleep(60)
        got = harvest_of(r.get("frames") or [])
        if r.get("status") == "busy" or not got.get("DRB.PdcpSduVolumeUl"):
            print(f"  {sweep} {key:>6}  {RED}{r.get('status')} — not saved"
                  f"{RESET}", flush=True)
            continue
        saved[key] = {"status": r.get("status"), "wall": round(time.time() - t0, 1),
                      "frames": frames, "offered_ul_kbit": offered_ul_kbit(cfg),
                      **got}
        json.dump(all_saved, open(OUT, "w"), indent=1)
        print(f"  {sweep} {key:>6} {spec['unit']}  {r.get('status')}  "
              f"UL {got.get('DRB.PdcpSduVolumeUl', float('nan')):>9,.0f} kbit/UE"
              f"   {time.time() - t0:.0f} s", flush=True)

    report(sweep, spec, saved)
    return 0


def report(sweep: str, spec: dict, saved: dict) -> None:
    if not saved:
        print(f"\n  {RED}no arms with data{RESET}")
        return
    keys = sorted(saved, key=float)
    print(f"\n{sweep + ' ' + spec['unit']:>16}{'UL kbit/UE':>13}{'of offered':>12}"
          f"{'DL bytes/UE':>14}{'DL B/blk':>10}")
    for k in keys:
        s = saved[k]
        off = s.get("offered_ul_kbit") or float("nan")
        ul = s.get("DRB.PdcpSduVolumeUl", float("nan"))
        print(f"{k:>16}{ul:>13,.0f}{ul / off:>12.1%}"
              f"{s.get('QosFlow.PdcpPduVolumeDL', float('nan')):>14,.0f}"
              f"{s.get('_dl_bytes_per_block', float('nan')):>10,.0f}")

    if len(keys) < len(spec["values"]):
        print(f"\n  {YELLOW}{len(keys)} of {len(spec['values'])} arms have "
              f"data — the verdict is a comparison BETWEEN arms, and one arm "
              f"against a stored number from a different run length is not "
              f"a measurement of anything.{RESET}")
    if len(keys) < 2:
        return

    def series(metric):
        return [(float(k), saved[k][metric]) for k in keys if saved[k].get(metric)]

    def shape(pts):
        if len(pts) < 2:
            return None, False
        r = pts[-1][1] / pts[0][1] if pts[0][1] else float("inf")
        mono = (all(b[1] >= a[1] for a, b in zip(pts, pts[1:]))
                or all(b[1] <= a[1] for a, b in zip(pts, pts[1:])))
        return r, mono

    ul_r, ul_mono = shape(series("DRB.PdcpSduVolumeUl"))
    dl_r, dl_mono = shape(series("QosFlow.PdcpPduVolumeDL"))
    print(f"\n  end to end")
    print(f"    uplink   {ul_r:.2f}x  {'monotonic' if ul_mono else 'NOT monotonic'}")
    if dl_r:
        print(f"    downlink {dl_r:.2f}x  {'monotonic' if dl_mono else 'NOT monotonic'}")

    if sweep == "ues":
        per = [(float(k), saved[k]["DRB.PdcpSduVolumeUl"]) for k in keys]
        n0, n1 = per[0][0], per[-1][0]
        # Shared capacity predicts per-UE rate falling as n0/n1; a per-UE
        # grant rate predicts no change at all. Which one the data is nearer
        # to, in log space, is the answer.
        shared = n0 / n1
        d_shared = abs(math.log(ul_r / shared)) if ul_r > 0 else float("inf")
        d_flat = abs(math.log(ul_r)) if ul_r > 0 else float("inf")
        print(f"\n  {n1 / n0:.0f}x the UEs on the same {len(keys) and ''}cells")
        print(f"    a shared CELL CAPACITY predicts per-UE {shared:.2f}x")
        print(f"    a per-UE GRANT RATE predicts           1.00x")
        print(f"    measured                               {ul_r:.2f}x")
        if d_shared < d_flat:
            print(f"\n  {GREEN}a shared CELL CAPACITY{RESET}: the per-UE "
                  f"uplink divides as the UEs multiply, so the ceiling is "
                  f"AIRTIME and a symbol share is the right shape for it — "
                  f"the model's share is simply too generous.")
        else:
            print(f"\n  {YELLOW}a per-UE GRANT RATE{RESET}: the per-UE "
                  f"uplink barely moves when the cell holds "
                  f"{n1 / n0:.0f}x as many, so it is not airtime being "
                  f"divided. No symbol share reproduces that; it needs a "
                  f"rate limit per UE.")
        return

    if sweep == "load":
        # A different question, so a different verdict. The other sweeps ask
        # whether a lever moves the uplink; this one asks what SHAPE the
        # uplink's limit has, and the answer is in the "of offered" column.
        fr = [saved[k]["DRB.PdcpSduVolumeUl"] / saved[k]["offered_ul_kbit"]
              for k in keys if saved[k].get("offered_ul_kbit")]
        spread = max(fr) / min(fr) if fr and min(fr) else float("inf")
        if spread < 1.5:
            print(f"\n  {GREEN}a PROPORTIONAL loss{RESET}: the delivered "
                  f"uplink holds at {statistics.fmean(fr):.0%} of offered "
                  f"across a {series('offered_ul_kbit')[-1][1] / series('offered_ul_kbit')[0][1]:.0f}x "
                  f"range of offered load ({min(fr):.0%}-{max(fr):.0%}). A "
                  f"fixed fraction is what a SYMBOL SHARE produces, so the "
                  f"model's share is simply too generous and the shape is "
                  f"expressible. A grant-rate ceiling would saturate instead.")
        else:
            print(f"\n  {YELLOW}a CEILING, not a fraction{RESET}: the "
                  f"delivered share runs {min(fr):.0%}-{max(fr):.0%} across "
                  f"the sweep, falling as the offer rises. The uplink hits "
                  f"something absolute — a grant rate — and no symbol share "
                  f"reproduces that.")
        return

    # THE DOWNLINK IS THE CONTROL, and it is the half that makes a null mean
    # anything. These levers are not supposed to touch the downlink at all
    # — except that every node shares one SpectrumChannel — so if neither
    # column moves, the sweep measured nothing.
    # BYTE-IDENTICAL ARMS ARE THE TELL, and this repo has the scar: a runtime
    # SetTxPower that never reached the radio was found exactly this way.
    # The engine is deterministic at a fixed seed, so two arms agreeing to
    # every digit did not run two configurations — whatever the payload said,
    # the value was overwritten or ignored downstream. `phy-mac.patch`
    # records the mechanism for a sibling field: the scenario
    # Config::SetDefault's it AFTER cmd.Parse.
    ident = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = saved[keys[i]], saved[keys[j]]
            same = [m for m in ("DRB.PdcpSduVolumeUl", "QosFlow.PdcpPduVolumeDL",
                                "TB.TotNbrDl.1")
                    if a.get(m) is not None and a.get(m) == b.get(m)]
            if len(same) >= 2:
                ident.append((keys[i], keys[j], len(same)))
    if ident:
        pairs = ", ".join(f"{a} and {b}" for a, b, _ in ident)
        print(f"\n  {RED}IDENTICAL ARMS{RESET}: {pairs} agree on every "
              f"counter to the digit. The engine is deterministic at a fixed "
              f"seed, so those arms did not run two configurations — the "
              f"value reached the payload and was overwritten or ignored "
              f"below it. This sweep measured nothing about {sweep}.")
        return

    moved = dl_r is not None and dl_mono and (dl_r > 1.3 or dl_r < 0.77)
    if not moved:
        print(f"\n  {RED}NO VERDICT{RESET}: the downlink did not move either, "
              f"so this cannot tell a flat uplink from a parameter that never "
              f"reached the radio. Check the payload and the image first.")
    elif ul_mono and (ul_r > 1.4 or ul_r < 0.7):
        print(f"\n  {GREEN}this lever IS the limit{RESET}: the uplink follows "
              f"it {ul_r:.2f}x and monotonically, while the downlink control "
              f"confirms the parameter reached the radio.")
    else:
        print(f"\n  {GREEN}the parameter reaches the radio{RESET}: the "
              f"downlink moves {dl_r:.2f}x and monotonically.")
        print(f"  {YELLOW}this lever is NOT the limit{RESET} — the uplink "
              f"spread is {ul_r:.2f}x and "
              f"{'monotonic' if ul_mono else 'not monotonic'}, inside or "
              f"barely outside the engine's own run-to-run noise.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", choices=sorted(SWEEPS), default="ue_tx")
    ap.add_argument("--frames", type=int, default=30)
    ap.add_argument("--wall-cap", type=float, default=600.0)
    a = ap.parse_args()
    return asyncio.run(main_async(a.sweep, a.frames, a.wall_cap))


if __name__ == "__main__":
    raise SystemExit(main())
