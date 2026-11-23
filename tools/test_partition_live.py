#!/usr/bin/env python3
"""The LIVE partition merge, checked without an engine.

The batch merge combines N numbers; this combines N time-series, and its
failure modes all produce a plausible-looking network rather than an error:

  * every partition numbers its cells from the same base and hands its first UE
    IMSI 1, so merging raw COLLAPSES six cells into two and stacks partition 2's
    UE 1 on partition 1's — the merged run then reports a third of the network
  * partitions advance at different rates (measured 0.7 to 1.1 simulated
    seconds on one three-way run), so frame i of one is not frame i of another
  * a control command names a whole-run cell and has to reach the ONE process
    holding it, with the id translated back

    python3 tools/test_partition_live.py
"""
import sys

sys.path.insert(0, "/home/chancelor/nsg_ai_platform")
sys.path.insert(0, "/home/chancelor/nsg_ai_platform/nsg_ai")
from partition_live import (global_cell, local_cell, relabel_frame, submap,
                            union_frames, merge_live, check_cell_base, CELL_BASE,
                            Rolling)

fails = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'  ok  ' if ok else 'FAIL  '}{name}{'   ' + detail if detail else ''}")
    if not ok:
        fails.append(name)


def frame(sim_t, ues, cells, meas, slices=None, handovers=0):
    return {"sim_t": sim_t, "tick": 0, "ues": ues, "cells": cells,
            "measurements": meas, "slices": slices or {}, "handovers": handovers,
            "ue_ids": [u["imsi"] for u in ues]}


# A six-cell run split three ways. Every partition sees its own cells as 2 and 3.
P = [{"n_gnb": 2, "_cell_index": [0, 1], "_ue_index": [0, 1]},
     {"n_gnb": 2, "_cell_index": [2, 3], "_ue_index": [2]},
     {"n_gnb": 2, "_cell_index": [4, 5], "_ue_index": [3, 4]}]

print("== identities ==")
check("a cell keeps its id in the first partition", global_cell(2, [0, 1], 2) == 2)
check("and is shifted in the third", global_cell(2, [4, 5], 2) == 6,
      str(global_cell(2, [4, 5], 2)))
check("the mapping inverts for control", local_cell(7, [4, 5], 2) == 3,
      str(local_cell(7, [4, 5], 2)))
check("a cell in another partition is refused, not guessed",
      local_cell(2, [4, 5], 2) is None)
check("no base yet means no routing", local_cell(7, [4, 5], None) is None)

f = relabel_frame(frame(0.1, [{"imsi": 1, "cell": 2}, {"imsi": 2, "cell": 3}],
                        [{"cell": 2}, {"cell": 3}], {}), [4, 5], [3, 4], 2)
check("frame cells are rewritten", [c["cell"] for c in f["cells"]] == [6, 7],
      str([c["cell"] for c in f["cells"]]))
check("frame IMSIs are rewritten", [u["imsi"] for u in f["ues"]] == [4, 5],
      str([u["imsi"] for u in f["ues"]]))
check("a UE's serving cell is rewritten too",
      [u["cell"] for u in f["ues"]] == [6, 7])

print("\n== the slice map follows the UE, not the slot ==")
whole = {1: "eMBB", 2: "uRLLC", 3: "eMBB", 4: "mMTC", 5: "uRLLC"}
check("partition 3's UE 1 keeps UE 4's slice", submap(whole, [3, 4]) == {1: "mMTC", 2: "uRLLC"},
      str(submap(whole, [3, 4])))
check("a UE the map does not cover is left out", submap(whole, [9]) == {})

print("\n== one instant of the whole network ==")
a = frame(0.1, [{"imsi": 1, "cell": 2, "thp_kbps": 100.0}], [{"cell": 2, "ues": 1}],
          {"T": {"count": 1, "mean": 100.0, "min": 100.0, "max": 100.0}}, {"eMBB": 100.0})
b = frame(0.1, [{"imsi": 3, "cell": 6, "thp_kbps": 300.0}], [{"cell": 6, "ues": 1}],
          {"T": {"count": 3, "mean": 300.0, "min": 250.0, "max": 350.0}}, {"eMBB": 300.0}, 2)
u = union_frames([a, b], 7, sim_t=0.1)
check("cells are unioned", [c["cell"] for c in u["cells"]] == [2, 6])
check("the mean is COUNT-weighted", u["measurements"]["T"]["mean"] == 250.0,
      f"got {u['measurements']['T']['mean']} (a plain average would be 200)")
check("min/max span both", (u["measurements"]["T"]["min"], u["measurements"]["T"]["max"]) == (100.0, 350.0))
check("per-slice totals ADD", u["slices"] == {"eMBB": 400.0}, str(u["slices"]))
check("handovers add", u["handovers"] == 2)
check("the frame says it is partitioned", u["partitioned"] and u["partitions"] == 2)

print("\n== clocks diverge ==")
A = {"status": "done", "n_frames": 3, "status_lines": [], "fault_events": [], "frames": [
    frame(0.1, [{"imsi": 1, "cell": 2}], [{"cell": 2}], {}),
    frame(0.2, [{"imsi": 1, "cell": 2}], [{"cell": 2}], {}),
    frame(0.3, [{"imsi": 1, "cell": 2}], [{"cell": 2}], {})]}
B = {"status": "done", "n_frames": 1, "status_lines": [], "fault_events": [], "frames": [
    frame(0.2, [{"imsi": 1, "cell": 2}], [{"cell": 2}], {})]}
m = merge_live([A, B], [{"n_gnb": 2, "_cell_index": [0, 1], "_ue_index": [0]},
                        {"n_gnb": 2, "_cell_index": [2, 3], "_ue_index": [1]}])
check("the timeline is cut to the window BOTH covered", m["n_frames"] == 1,
      f"{m['n_frames']} frames (0.2 only; 0.1 is before B started, 0.3 after it stopped)")
check("and every merged frame holds both partitions",
      all(len(f["cells"]) == 2 for f in m["frames"]))

print("\n== the result carries the trade ==")
check("shape is recorded", m["partition_shape"]["cells_per_group"] == 2)
check("two cells per group warns", any("crossing a group boundary" in n
                                       for n in m["partition_notes"]))
one = merge_live([A, B], [{"n_gnb": 1, "_cell_index": [0], "_ue_index": [0]},
                          {"n_gnb": 1, "_cell_index": [1], "_ue_index": [1]}])
check("one cell per group says handover did not happen",
      any("did not happen" in n for n in one["partition_notes"]))
three = merge_live([A, B], [{"n_gnb": 3, "_cell_index": [0, 1, 2], "_ue_index": [0]},
                            {"n_gnb": 3, "_cell_index": [3, 4, 5], "_ue_index": [1]}])
check("three or more raises no handover warning",
      not any("did not happen" in n or "crossing a group" in n
              for n in three["partition_notes"]))

print("\n== a partition that failed is not hidden ==")
bad = merge_live([A, {"status": "timeout", "n_frames": 0, "frames": [],
                      "status_lines": [], "fault_events": [], "error": "quiet engine"}],
                 [{"n_gnb": 2, "_cell_index": [0, 1], "_ue_index": [0]},
                  {"n_gnb": 2, "_cell_index": [2, 3], "_ue_index": [1]}])
check("the worst status wins", bad["status"] == "timeout", bad["status"])
check("the error travels", bad["error"] == "quiet engine")
check("and the mismatch is named", any("ended differently" in n
                                       for n in bad["partition_notes"]))

print("\n== the first cell id is a constant, not something to learn ==")
# THE DEFECT THIS LOCKS OUT, seen live. The base used to be min() over the cells
# a frame reported. A cell only appears in a frame once it HAS a UE, so a first
# frame holding only the higher of the two taught the base as 3 — then every
# partition's cell 2 failed the offset check and stayed 2, three partitions'
# first cells stacked onto one id ("cell 2 · 12 UE" for cells that hold four),
# and a control command for cell 2 belonged to no process at all.
SPARSE = [
    {"status": "done", "n_frames": 1, "status_lines": [], "fault_events": [],
     # only the SECOND cell of the partition has a UE in this frame
     "frames": [frame(0.1, [{"imsi": 1, "cell": 3}], [{"cell": 3}], {})]}
    for _ in range(3)
]
PARTS = [{"n_gnb": 2, "_cell_index": [0, 1], "_ue_index": [0]},
         {"n_gnb": 2, "_cell_index": [2, 3], "_ue_index": [1]},
         {"n_gnb": 2, "_cell_index": [4, 5], "_ue_index": [2]}]
m = merge_live(SPARSE, PARTS)
got = [c["cell"] for c in m["frames"][0]["cells"]]
check("three partitions reporting only their second cell stay THREE cells",
      got == [3, 5, 7], f"got {got} (learning the base gave [3, 5, 7] only by luck, "
                        f"and [2, 2, 2] when the low cell was the one reporting)")
check("the low cell maps to the base itself",
      global_cell(2, [0, 1], CELL_BASE) == 2)
check("and the same local id in another partition does NOT collide",
      global_cell(2, [4, 5], CELL_BASE) == 6)
check("control for the low cell resolves before any frame has arrived",
      local_cell(2, [0, 1], CELL_BASE) == 2)
check("the assumption is checked against the data, not just asserted",
      check_cell_base(SPARSE) is None)
BELOW = [{"frames": [{"cells": [{"cell": 1}]}]}]
check("a cell below the base is reported as numbering drift",
      "numbering has changed" in (check_cell_base(BELOW) or ""))

print("\n== a frame is not a complete observation ==")
# THE DEFECT: the merge carried forward each partition's newest FRAME. Most
# frames hold only positions or energy — each gNB sends its CU-CP report at its
# own time — so whichever frame happened to be newest at a chosen instant
# decided which counters survived. Measured: a split run came back with 13
# measurement names against 88 for the same scenario run whole, and Compare
# showed blanks for throughput, PRB and every transport-block counter.
def meas(name, mean, count=1):
    return {name: {"count": count, "mean": mean, "min": mean, "max": mean}}

SPARSE = [[
    # t=0.1 carries the KPM report...
    {"sim_t": 0.1, "ues": [{"imsi": 1, "cell": 2}], "cells": [{"cell": 2}],
     "measurements": meas("DRB.UEThpDl", 900.0), "handovers": 0, "ue_ids": [1]},
    # ...and t=0.2 is a position-only frame, as most of them are
    {"sim_t": 0.2, "ues": [{"imsi": 1, "cell": 2}], "cells": [{"cell": 2}],
     "measurements": {}, "handovers": 0, "ue_ids": [1]},
], [
    {"sim_t": 0.1, "ues": [{"imsi": 1, "cell": 2}], "cells": [{"cell": 2}],
     "measurements": meas("DRB.UEThpDl", 100.0), "handovers": 0, "ue_ids": [1]},
    {"sim_t": 0.2, "ues": [{"imsi": 1, "cell": 2}], "cells": [{"cell": 2}],
     "measurements": {}, "handovers": 0, "ue_ids": [1]},
]]
P2 = [{"n_gnb": 3, "_cell_index": [0, 1, 2], "_ue_index": [0]},
      {"n_gnb": 3, "_cell_index": [3, 4, 5], "_ue_index": [1]}]
m = merge_live([{"status": "done", "n_frames": 2, "status_lines": [], "fault_events": [],
                 "frames": SPARSE[0]},
                {"status": "done", "n_frames": 2, "status_lines": [], "fault_events": [],
                 "frames": SPARSE[1]}], P2)
last = m["frames"][-1]
check("a counter survives the frames that do not carry it",
      "DRB.UEThpDl" in last["measurements"],
      f"names at the last instant: {sorted(last['measurements'])}")
check("and it is still the count-weighted value",
      last["measurements"]["DRB.UEThpDl"]["mean"] == 500.0,
      str(last["measurements"]["DRB.UEThpDl"]["mean"]))
check("every merged frame carries it, not just the one that reported",
      all("DRB.UEThpDl" in f["measurements"] for f in m["frames"]))

r = Rolling()
r.feed({"measurements": meas("A", 1.0), "ues": [{"imsi": 1}], "sim_t": 0.1})
r.feed({"measurements": {"A": {"count": 0, "mean": None}}, "sim_t": 0.2})
check("an empty reading does not erase a real one",
      r.frame()["measurements"]["A"]["mean"] == 1.0)
r.feed({"measurements": meas("A", 2.0), "sim_t": 0.3})
check("but a real one replaces it", r.frame()["measurements"]["A"]["mean"] == 2.0)
check("the rolling clock follows the newest frame", r.frame()["sim_t"] == 0.3)

print("\n" + (f"{len(fails)} FAILED: {', '.join(fails)}" if fails else "all passed"))
raise SystemExit(1 if fails else 0)
