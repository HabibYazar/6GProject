#!/usr/bin/env python3
"""The partition adviser has to agree with the runs it was fitted to.

A model that drifts from its own measurements is worse than no model: it gives a
number with the authority of a measurement and none of the truth. This checks it
against the two shape tables on disk, and checks the rules that are NOT fitted —
the core cap and the handover verdict — which are the ones that stop someone
picking a shape that silently deletes their scenario.

    python3 tools/test_partition_advice.py
"""
import sys

sys.path.insert(0, "/home/chancelor/nsg_ai_platform")
sys.path.insert(0, "/home/chancelor/nsg_ai_platform/nsg_ai")
from partition_advice import (advise, partition_options, recommend, EXPONENT,
                              interference_kept, fidelity_verdict,
                              removed_interference_dbm)

fails = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'  ok  ' if ok else 'FAIL  '}{name}{'   ' + detail if detail else ''}")
    if not ok:
        fails.append(name)


# ── the fit, against the run it came from ────────────────────────────────────
# 12 cells / 72 UEs / 20 m/s, speed = slowest group's rate relative to 1 group.
MEASURED = {1: 1.0, 2: 2.2, 3: 4.4, 4: 6.7, 6: 11.0, 12: 26.2}
print("== speed law vs the measured shape table ==")
opts = {o["groups"]: o for o in partition_options(12, 72, cores=12)}
worst = 0.0
for g, meas in MEASURED.items():
    pred = opts[g]["speed_x"]
    err = abs(pred - meas) / meas
    worst = max(worst, err)
    check(f"{g:>2} groups: predicted {pred:>5.1f}x vs measured {meas:>5.1f}x", err <= 0.20,
          f"{err*100:.0f}% off")
check("no point is more than 20% off", worst <= 0.20, f"worst {worst*100:.0f}%")

# ── the handover verdict, which is a measurement not a fit ───────────────────
print("\n== handover verdict by cells per group ==")
# Measured at 12 cells: 31/32/31 handovers at 6/4/3 cells per group (intact),
# 21 at 2 (reduced), and at 1 the only ones left are anchor transitions.
for per, want in ((6, "intact"), (4, "intact"), (3, "intact"), (2, "reduced"), (1, "none")):
    g = 12 // per
    check(f"{per} cell(s) per group -> {want}", opts[g]["handover"] == want, opts[g]["handover"])

# ── the rules that protect the user ──────────────────────────────────────────
print("\n== the rules ==")
r12 = recommend(partition_options(12, 72, cores=12), cores=12)
check("12 cells on 12 cores recommends 4 (most groups keeping handover)", r12 == 4, f"got {r12}")

r2 = recommend(partition_options(12, 72, cores=2), cores=2)
check("2 cores caps the recommendation at 2", r2 == 2, f"got {r2}")

r1 = recommend(partition_options(12, 72, cores=1), cores=1)
check("1 core recommends no partitioning", r1 == 1, f"got {r1}")

over = [o for o in partition_options(12, 72, cores=4) if o["over_cores"]]
check("shapes beyond the core count are listed, not hidden", {o["groups"] for o in over} == {6, 12},
      str(sorted(o["groups"] for o in over)))

never = [o for o in partition_options(12, 72, cores=12) if o["recommended"] and o["handover"] != "intact"]
check("a shape that breaks handover is never recommended", not never)

# ── shapes have to be real divisors ──────────────────────────────────────────
print("\n== shapes ==")
seven = [o["groups"] for o in partition_options(7, 21, cores=12)]
check("7 cells offers only 1 and 7 (a cell cannot be split)", seven == [1, 7], str(seven))
one = partition_options(1, 6, cores=12)
check("1 cell offers only the unpartitioned shape", [o["groups"] for o in one] == [1])

# ── there is no "static scenario" exemption, and there must not be ───────────
# It was built and removed. A zero speed cannot be expressed (the platform
# replaces it with 1.4 m/s), and slow is not safe: the end-to-end run produced
# 7 handovers whole and 34 partitioned on about six metres of travel. A config
# cannot tell you whether your run will hand over, so the adviser must not claim
# it can.
print("\n== no mobility exemption ==")
check("the adviser takes no mobility argument",
      "mobile" not in advise(3, 12, cores=6))
check("3 cells that can only split 3 ways is still refused the split",
      recommend(partition_options(3, 12, cores=6), 6) == 1)
check("why is stated in the caveats",
      any("1.4" in c or "six metres" in c for c in advise(3, 12, cores=6)["caveats"]))

# ── the advice carries its own health warnings ───────────────────────────────
print("\n== the advice says what it does not know ==")
a = advise(12, 72, cores=12)
check("speeds are declared estimates", any("ESTIMATE" in c.upper() for c in a["caveats"]))
check("the served-UE trap is stated", any("served" in c.lower() for c in a["caveats"]))
check("the anchor-handover trap is stated", any("anchor" in c.lower() for c in a["caveats"]))
check("the model reports its own fit error", a["model"]["fit_error_pct"] > 0)
check("the exponent is the measured one", abs(EXPONENT - 1.37) < 1e-9)

# ── the split is real, not UEs / groups ──────────────────────────────────────
# This is the defect that made a six-cell live session abort: the panel promised
# 50 UEs per partition, the run gave one partition 75, and 37.5 UEs per cell is
# past the density with a known unfixed abort. An average never runs anywhere.
print("\n== the UE split is measured, not divided ==")
SPLITS = {1: [150], 2: [75, 75], 3: [75, 25, 50], 6: [75, 0, 0, 25, 25, 25]}
opts = {o["groups"]: o for o in partition_options(6, 150, cores=6, splits=SPLITS)}
check("the UE column is the BUSIEST partition, not the mean",
      opts[3]["ues_per_group"] == 75, f"got {opts[3]['ues_per_group']} (the mean is 50)")
check("the whole split is reported", opts[3]["ues_split"] == [75, 25, 50])
check("an uneven split is called uneven", opts[3]["balanced"] is False)
check("an even one is not", opts[2]["balanced"] is True)
check("per-cell density is the busiest partition's",
      opts[3]["ues_per_cell_max"] == 37.5, str(opts[3]["ues_per_cell_max"]))
check("a density with a known abort is flagged", opts[3]["over_density"] is True)
check("the UNPARTITIONED row is judged by the same rule — this scenario is "
      "already dense at 25 UEs/cell, and hiding that would blame the split",
      opts[1]["over_density"] is True, f"{opts[1]['ues_per_cell_max']} UEs/cell whole")
check("a genuinely safe scenario is not flagged",
      partition_options(6, 60, cores=6,
                        splits={1: [60], 2: [30, 30]})[0]["over_density"] is False)
check("and the split's OWN contribution is separated out",
      opts[3]["density_vs_whole"] == 1.5, str(opts[3]["density_vs_whole"]))
check("running whole concentrates nothing", opts[1]["density_vs_whole"] == 1.0)
check("empty partitions are counted", opts[6]["empty_groups"] == 2,
      str(opts[6]["empty_groups"]))
check("a shape that would abort is never RECOMMENDED",
      not opts[3]["recommended"] and not opts[6]["recommended"])
check("the recommendation falls back to one that survives",
      recommend(list(opts.values()), cores=6) in (1, 2),
      str(recommend(list(opts.values()), cores=6)))
check("without splits it still answers (the old estimate, clearly the fallback)",
      partition_options(6, 150, cores=6)[0]["ues_split"] == [])
check("the caveat names the trap",
      any("75 / 25 / 50" in c for c in advise(6, 150, 6, SPLITS)["caveats"]))

# Speed follows the BUSIEST partition, not the group count. Six groups whose
# busiest still holds 75 of 150 UEs cannot be 11.6x, whatever the group count.
check("speed is capped by the busiest partition, not the group count",
      opts[6]["speed_x"] == opts[3]["speed_x"] == opts[2]["speed_x"] == 2.6,
      f"2:{opts[2]['speed_x']} 3:{opts[3]['speed_x']} 6:{opts[6]['speed_x']}")
check("and it says which form it used", opts[6]["speed_from"] == "measured split")
# The law must be UNCHANGED on the evenly-split runs it was fitted to, or this
# is a new model rather than a faithful rewriting of the old one.
EVEN = {g: [72 // g] * g for g in (1, 2, 3, 4, 6, 12)}
even = {o["groups"]: o["speed_x"] for o in partition_options(12, 72, cores=12, splits=EVEN)}
plain = {o["groups"]: o["speed_x"] for o in partition_options(12, 72, cores=12)}
check("an even split reproduces the old groups ** exponent exactly",
      even == plain, f"{even} vs {plain}")
check("with no split information it falls back and says so",
      partition_options(12, 72, cores=12)[1]["speed_from"] == "even-split estimate")

print("\n== is splitting VALID for this scenario, not just fast ==")
# The question the whole feature turns on. A partition does not approximate the
# network: every cell outside a group leaves that group's world entirely. What
# that costs is geometry — how much of each UE's interference those cells were
# contributing — and it is computable before anything runs.
def scn(gnb, ue):
    return {"gnb_positions": ";".join(f"{x},{y},25" for x, y in gnb),
            "ue_positions": ";".join(f"{x},{y},1.5" for x, y in ue)}

DENSE = scn([(0, 0), (400, 0), (800, 0), (0, 400), (400, 400), (800, 400)],
            [(x * 133, y * 133) for x in range(6) for y in range(6)])
SITES = scn([(0, 0), (300, 0), (8000, 0), (8300, 0), (16000, 0), (16300, 0)],
            [(b + dx, dy) for b in (0, 8000, 16000) for dx in (0, 150, 300)
             for dy in (-100, 100)])
CLUSTERS = scn([(0, 0), (400, 0), (200, 350), (3000, 0), (3400, 0), (3200, 350)],
               [(b + dx, dy) for b in (0, 3000) for dx in (0, 200, 400)
                for dy in (0, 175, 350)])

d3 = interference_kept(DENSE, 3)
check("cutting a dense cluster three ways removes most interference",
      d3 is not None and d3 < 0.40, f"kept {d3:.0%}" if d3 else "None")
s3 = interference_kept(SITES, 3)
check("cutting three separate sites along their separation keeps ALL of it",
      s3 is not None and s3 > 0.95, f"kept {s3:.0%}" if s3 else "None")
c2 = interference_kept(CLUSTERS, 2)
check("and two clusters split in two keep all of it",
      c2 is not None and c2 > 0.95, f"kept {c2:.0%}" if c2 else "None")
c3 = interference_kept(CLUSTERS, 3)
check("while the WRONG cut on the same scenario does not",
      c3 is not None and c3 < 0.50, f"kept {c3:.0%}" if c3 else "None")
check("one group keeps everything by definition", interference_kept(DENSE, 1) == 1.0)

print("\n== the ratio does not hang on the path-loss exponent ==")
lo, hi = interference_kept(SITES, 3, exponent=2.5), interference_kept(SITES, 3, exponent=4.5)
check("a separated cut scores high across 2.5-4.5", min(lo, hi) > 0.95, f"{lo:.0%}..{hi:.0%}")
lo, hi = interference_kept(DENSE, 3, exponent=2.5), interference_kept(DENSE, 3, exponent=4.5)
check("a dense cut scores low across 2.5-4.5", max(lo, hi) < 0.50, f"{lo:.0%}..{hi:.0%}")

print("\n== the verdict reaches the choice ==")
check("kept", fidelity_verdict(0.97) == "kept")
check("approximate", fidelity_verdict(0.6) == "approximate")
check("a different network", fidelity_verdict(0.26) == "different network")
check("unknown without geometry", fidelity_verdict(None) == "unknown")

opts = {o["groups"]: o for o in partition_options(6, 36, cores=6, params=DENSE)}
check("every option carries the number", all(o["interference_kept_pct"] is not None
                                             for o in opts.values()))
check("a cut that changes the network is NEVER recommended",
      not opts[3]["recommended"] and not opts[6]["recommended"],
      str({g: (o["fidelity"], o["recommended"]) for g, o in opts.items()}))
sopts = {o["groups"]: o for o in partition_options(6, 36, cores=6, params=SITES)}
check("but the natural cut on a separated network IS",
      sopts[3]["recommended"], str({g: (o["fidelity"], o["recommended"])
                                    for g, o in sopts.items()}))
check("the caveat says splitting is not an approximation",
      any("NOT AN APPROXIMATION" in c for c in advise(6, 36, 6, params=DENSE)["caveats"]))

print("\n== how much power the cut removes, as a SHAPE not a level ==")
# The ratio is robust; the absolute is not, and the code says so. What this has
# to get right is the ORDERING — more groups removes more, a separated cut
# removes almost nothing that matters — because a measured offset sets the level.
d2 = removed_interference_dbm(DENSE, 2)
d3 = removed_interference_dbm(DENSE, 3)
d6 = removed_interference_dbm(DENSE, 6)
check("splitting a cluster further removes MORE power",
      d2 < d3 < d6, f"{d2} / {d3} / {d6} dBm")
check("one group removes nothing, so there is no answer",
      removed_interference_dbm(DENSE, 1) is None)
check("a shape that does not divide the cells has no answer",
      removed_interference_dbm(DENSE, 4) is None)
check("a separated cut removes far less than a dense one at the same shape",
      removed_interference_dbm(SITES, 3) < removed_interference_dbm(DENSE, 3) - 10,
      f"sites {removed_interference_dbm(SITES, 3)} vs dense {removed_interference_dbm(DENSE, 3)} dBm")
check("raising every cell's TX raises the removed power with it",
      removed_interference_dbm(dict(DENSE, gnb_powers=";".join(["40"] * 6)), 3)
      - removed_interference_dbm(dict(DENSE, gnb_powers=";".join(["30"] * 6)), 3) == 10.0)
check("no geometry, no answer", removed_interference_dbm({}, 3) is None)

print("\n== when should the bursty term be on? ==")
dense = {o["groups"]: o for o in partition_options(6, 36, cores=6, params=DENSE)}
sites = {o["groups"]: o for o in partition_options(6, 36, cores=6, params=SITES)}
check("running whole: the question does not arise",
      dense[1]["burst"] == "not applicable")
check("a cut that removed interference: yes, for ERROR behaviour",
      dense[3]["burst"] == "for error behaviour", dense[3]["burst"])
check("a natural cut removed nothing, so adding some would invent it",
      sites[3]["burst"] == "no", sites[3]["burst"])
check("the caveat says what it does NOT restore",
      any("changes throughput, PRB use and" in c
          for c in advise(6, 36, 6, params=DENSE)["caveats"]))
check("and that its level is one calibration point",
      any("ONE scenario and should be re-fitted" in c
          for c in advise(6, 36, 6, params=DENSE)["caveats"]))

print("\n== the law knows it is currently optimistic ==")
m = advise(6, 36, 6, params=DENSE)
check("the model says which engine it was fitted on",
      "1.25" in m["model"]["measured_on"])
check("and that the shipped engine moved it",
      "2.96x" in m["model"]["measured_on"] and "1.54x" in m["model"]["measured_on"])
check("a reader choosing a shape is told before they read a speed",
      any("OPTIMISTIC" in c for c in m["caveats"]))

print("\n" + (f"{len(fails)} FAILED: {', '.join(fails)}" if fails else "all passed"))
raise SystemExit(1 if fails else 0)
