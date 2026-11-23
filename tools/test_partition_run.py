#!/usr/bin/env python3
"""The partition splitter and merger, checked without touching an engine.

Both halves have a failure mode that produces plausible numbers instead of an
error, which is why they are tested rather than eyeballed:

  * the SPLIT can hand a four-UE partition a twelve-slot speed string, so it
    silently runs with another partition's speeds
  * the MERGE can average means over populations of different sizes, which is
    the mistake that made a throughput gap look like physics three times in one
    day

    python3 tools/test_partition_run.py
"""
import sys

sys.path.insert(0, "/home/chancelor/nsg_ai_platform")
sys.path.insert(0, "/home/chancelor/nsg_ai_platform/nsg_ai")
from partition_run import (assign_groups, merge, slice_faults,
                           _UE_SLOTTED, _CELL_SLOTTED, _NOT_SLOTTED,
                           _HANDLED_ELSEWHERE)

fails = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'  ok  ' if ok else 'FAIL  '}{name}{'   ' + detail if detail else ''}")
    if not ok:
        fails.append(name)


# Four cells on a line, eight UEs sitting two per cell. Nearest-cell assignment
# is then unambiguous and the expected split is arithmetic, not a judgement.
PARAMS = {
    "n_gnb": 4, "n_ue": 8,
    "gnb_positions": "0,0,25;100,0,25;200,0,25;300,0,25",
    "ue_positions": ";".join(f"{x},10,1.5" for x in (5, 10, 105, 110, 205, 210, 305, 310)),
    "ue_speeds": ";".join(str(i) for i in range(8)),          # per-UE, 8 slots
    # A waypoint path per UE. ns-3 places a UE AT ITS FIRST WAYPOINT, so this
    # one silently overrides ue_positions — which is how a partition given its
    # own positions ended up simulating another partition's UEs.
    "ue_waypoints": ";".join(f"{i},{i},1.5,0" for i in range(8)),
    "gnb_powers": "30;31;32;33",                               # per-cell, 4 slots
    "gnb_antenna": "0,0,4,4;90,0,4,4;180,0,4,4;270,0,4,4",     # per-cell, 4 slots
    "buildings": "a;b;c;d",            # NOT per-cell, even at four entries
    "gnb_faults": "0,1.0,2.0,0,5;2,1.0,2.0,0,5;3,1.0,2.0,0,5;0,4.0,1.0,3,0",
    "kpm_collect_s": 60, "rng_run": 1,
}

print("== split ==")
g2 = assign_groups(PARAMS, 2)
check("2 groups of 2 cells", [p["n_gnb"] for p in g2] == [2, 2], str([p["n_gnb"] for p in g2]))
check("UEs follow their nearest cell", [p["n_ue"] for p in g2] == [4, 4], str([p["n_ue"] for p in g2]))
check("per-UE speeds are cut to the partition", [p["ue_speeds"] for p in g2] == ["0;1;2;3", "4;5;6;7"],
      str([p["ue_speeds"] for p in g2]))
check("per-cell powers are cut to the partition", [p["gnb_powers"] for p in g2] == ["30;31", "32;33"],
      str([p["gnb_powers"] for p in g2]))
check("positions are SLICED from the original, not recomputed",
      g2[1]["gnb_positions"] == "200,0,25;300,0,25", g2[1]["gnb_positions"])
check("every UE lands in exactly one partition",
      sum(p["n_ue"] for p in g2) == 8)

g4 = assign_groups(PARAMS, 4)
check("4 groups of 1 cell", [p["n_gnb"] for p in g4] == [1] * 4)
check("slot strings stay aligned at 1 cell per group",
      all(len(p["ue_speeds"].split(";")) == p["n_ue"] for p in g4))

for bad in (0, 3, 5):
    try:
        assign_groups(PARAMS, bad)
        check(f"{bad} groups is refused", False, "no error raised")
    except ValueError:
        check(f"{bad} groups is refused", True)

# ── merge ────────────────────────────────────────────────────────────────────
print("\n== merge ==")
# Two partitions, deliberately unequal: 2 UEs at 100 and 8 UEs at 200. The plain
# average is 150; the correct, count-weighted answer is 180.
A = {"status": "ok", "ue_count": 2, "handovers": 3, "_cells": 2,
     "measurements": {"DRB.UEThpDl": {"count": 2, "min": 90, "max": 110, "mean": 100}},
     "energy": {"span_s": 1.0, "total_w": 100, "per_cell": {"2": {"w": 50}}},
     "derived": {"bler_pct": 10.0}}
B = {"status": "ok", "ue_count": 8, "handovers": 5, "_cells": 2,
     "measurements": {"DRB.UEThpDl": {"count": 8, "min": 150, "max": 260, "mean": 200}},
     "energy": {"span_s": 3.0, "total_w": 300, "per_cell": {"3": {"w": 150}}},
     "derived": {"bler_pct": 5.0}}
m = merge([A, B])
thp = m["measurements"]["DRB.UEThpDl"]
check("mean is COUNT-weighted, not averaged", thp["mean"] == 180.0,
      f"got {thp['mean']} (a plain average would be 150)")
check("counts add up", thp["count"] == 10)
check("min/max span both partitions", (thp["min"], thp["max"]) == (90, 260))
check("UEs add up", m["ue_count"] == 10)
check("handovers add up", m["handovers"] == 8)
check("per-cell energy is unioned", set(m["energy"]["per_cell"]) == {"2", "3"})
check("BLER is UE-weighted", m["derived"]["bler_pct"] == 6.0,
      f"got {m['derived']['bler_pct']} (a plain average would be 7.5)")
check("BLER is flagged as an estimate", m["derived"]["bler_pct_is_estimate"] is True)

print("\n== the horizon is not one number ==")
check("span_s reports the SLOWEST partition", m["span_s"] == 1.0, str(m.get("span_s")))
check("both ends are kept", (m["span_s_min"], m["span_s_max"]) == (1.0, 3.0))
check("the spread is called out", any("simulated seconds" in n for n in m["merge_notes"]))

print("\n== the result carries its own verdict ==")
# The picker warns at the moment of choosing. A saved, exported or compared run
# is read long after that, so the numbers have to carry the trade themselves.
for per, want in ((6, "intact"), (3, "intact"), (2, "reduced"), (1, "none")):
    m2 = merge([A, B], cells_per_group=per)
    check(f"{per} cell(s) per group -> handover {want}",
          m2["partition_shape"]["handover"] == want, m2["partition_shape"]["handover"])
    check(f"  and {per} is recorded on the result",
          m2["partition_shape"]["cells_per_group"] == per)
one_cell = merge([A, B], cells_per_group=1)
check("a one-cell run says control tests did not happen",
      any("did not happen" in n for n in one_cell["merge_notes"]))
check("and says what it IS still good for",
      any("capacity study" in n for n in one_cell["merge_notes"]))
two_cell = merge([A, B], cells_per_group=2)
check("a two-cell run warns too — it used to say nothing",
      any("crossing a group boundary" in n for n in two_cell["merge_notes"]))
safe = merge([A, B], cells_per_group=6)
check("a handover-safe shape raises no handover warning",
      not any("did not happen" in n or "crossing a group" in n for n in safe["merge_notes"]))
check("groups is always recorded", safe["partition_shape"]["groups"] == 2)

print("\n== the merge says what it could not do ==")
check("the BLER approximation is stated", any("bler" in n.lower() for n in m["merge_notes"]))
single = merge([dict(A, _cells=1), dict(B, _cells=1)])
check("one-cell partitions get the anchor-handover warning",
      any("anchor" in n for n in single["merge_notes"]))
check("multi-cell partitions do NOT get it", not any("anchor" in n for n in m["merge_notes"]))

bad = merge([A, dict(B, status="truncated")])
check("a failed partition makes the whole result non-ok", bad["status"] == "partial", bad["status"])
check("and it is named", any("did not return ok" in n for n in bad["merge_notes"]))

print("\n== every slot-aligned field is cut, and the list is not guessed ==")
# THE DEFECT: the key list named four fields that do not exist in the payload
# ("ue_paths", "ue_services", "gnb_antennas", "ue_heights") and missed two that
# do. `ue_waypoints` went whole to every partition, and because ns-3 places a UE
# at its first waypoint, each partition simulated the FIRST four UEs of the run
# no matter which four it had been given. Measured: shifting a partition's
# ue_positions by 400 m changed its reported positions by nothing.
check("per-UE waypoints are cut to the partition",
      [p["ue_waypoints"] for p in g2] == ["0,0,1.5,0;1,1,1.5,0;2,2,1.5,0;3,3,1.5,0",
                                          "4,4,1.5,0;5,5,1.5,0;6,6,1.5,0;7,7,1.5,0"],
      str([p["ue_waypoints"] for p in g2]))
check("per-cell antenna is cut to the partition",
      [p["gnb_antenna"] for p in g2] == ["0,0,4,4;90,0,4,4", "180,0,4,4;270,0,4,4"],
      str([p["gnb_antenna"] for p in g2]))
check("buildings are NOT cut — every partition simulates the same scene, and "
      "their count matching the cell count is a coincidence",
      all(p["buildings"] == "a;b;c;d" for p in g2))

print("\n== faults name a cell, so they are remapped and filtered ==")
f2 = [p["gnb_faults"] for p in g2]
check("a fault on a cell this partition holds is renumbered to it",
      f2[0].startswith("0,1.0,2.0,0,5"), f2[0])
check("a fault on cell 2 lands in the SECOND partition as its cell 0",
      "0,1.0,2.0,0,5" in f2[1] and "1,1.0,2.0,0,5" in f2[1], f2[1])
check("a fault on a cell in another partition is dropped, not remapped to 0",
      f2[0].count("1.0,2.0,0,5") == 1, f2[0])
check("a handover block belongs to the anchor, so every partition keeps it",
      all("4.0,1.0,3,0" in v for v in f2), str(f2))
check("an empty schedule stays empty", slice_faults("", [0, 1]) == "")

print("\n== a new slot-aligned field cannot be missed ==")
# The guard that would have caught the original defect: anything in a real
# payload that is a ';'-list the length of the UE or cell count must be
# classified. Silence here means the next field added to nsoran_params gets
# handed to every partition whole.
try:
    sys.path.insert(0, "/home/chancelor/nsg_ai_platform/tools")
    from dotenv import load_dotenv
    load_dotenv("/home/chancelor/nsg_ai_platform/.env")
    from nsg_ai import store
    from nsg_ai.schema import NSGSimulationConfig
    import main as _M
    known = set(_UE_SLOTTED) | set(_CELL_SLOTTED) | set(_NOT_SLOTTED) | set(_HANDLED_ELSEWHERE)
    unclassified = {}
    for scn in store.load_scenarios():
        pay = _M.nsoran_params(NSGSimulationConfig(**scn["config"]))
        ng, nu = pay["n_gnb"], pay["n_ue"]
        for k, v in pay.items():
            if not isinstance(v, str) or ";" not in v or k in known:
                continue
            n = len(v.split(";"))
            if n in (ng, nu):
                unclassified[k] = f"{n} entries ({ng} cells / {nu} UEs)"
    check("no payload field of slot length is unclassified", not unclassified,
          str(unclassified))
except Exception as exc:  # noqa: BLE001
    check("payload audit ran", False, f"could not build payloads: {exc}")

print("\n" + (f"{len(fails)} FAILED: {', '.join(fails)}" if fails else "all passed"))
raise SystemExit(1 if fails else 0)
