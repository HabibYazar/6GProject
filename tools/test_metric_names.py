#!/usr/bin/env python3
"""Every metric the backend publishes has a name a person can read.

The campaign trend chart used to render raw field paths — `measurements.
RRU.PrbUsedDl.mean` — which say neither what the number is nor what it is in.
Names now live in one dictionary (`ui/lib/metrics.ts`), shared by the trend
chart and the threshold picker.

The failure this guards is the boring one: the backend gains a metric, nobody
adds a label, and it silently appears in the dropdown as a field path again.

    python3 tools/test_metric_names.py
"""
import re
import sys

sys.path.insert(0, "/home/chancelor/nsg_ai_platform")
sys.path.insert(0, "/home/chancelor/nsg_ai_platform/nsg_ai")
from campaign import HEADLINE_METRICS

TS = "/home/chancelor/nsg_ai_platform/ui/lib/metrics.ts"

fails = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'  ok  ' if ok else 'FAIL  '}{name}{'   ' + detail if detail else ''}")
    if not ok:
        fails.append(name)


src = open(TS).read()
paths = set(re.findall(r"path:\s*'([^']+)'", src))
labels = re.findall(r"label:\s*'([^']+)'", src)
groups = set(re.findall(r"group:\s*'([^']+)'", src))

print("== the dictionary covers what the backend publishes ==")
missing = [m for m in HEADLINE_METRICS if m not in paths]
check("every headline metric has a label", not missing, str(missing))

print("\n== the names are names, not field paths ==")
check("no label is a dotted field path",
      not [l for l in labels if "." in l and "_" in l],
      str([l for l in labels if "." in l and "_" in l]))
check("every entry is grouped", len(groups) >= 4, f"{len(groups)} groups")

print("\n== the traps are stated where the metric is chosen ==")
check("PDCP delay is marked unsafe to judge on",
      any("NOISY" in l for l in labels))
check("and its own group says so", any("Not safe" in g for g in groups), str(sorted(groups)))
check("the tick count warns that a partitioned run reports fewer",
      "duplicates at one instant collapse" in src)
check("the handover metric warns that splitting deletes some",
      "DELETES" in src)
check("the UE count explains why a split run can serve MORE",
      "more UEs finish attaching" in src)

print("\n== enough of them to answer a question ==")
# Nine metrics, five of which described the run rather than the network, was
# the complaint that started this. The point is not the count but the coverage:
# a throughput change has to be attributable to something.
check("at least 18 metrics", len(paths) >= 18, f"{len(paths)}")
for want, why in (("TB.ErrTotalNbrDl", "block errors — WHY throughput moved"),
                  ("64Qam", "modulation reached — the other half of that answer"),
                  ("BufferSize", "queue depth — whether the cell kept up"),
                  ("PdcpSduBitRateUl", "uplink, not just downlink")):
    check(f"covers {why}", any(want in p for p in paths))

print("\n" + (f"{len(fails)} FAILED: {', '.join(fails)}" if fails else "all passed"))
raise SystemExit(1 if fails else 0)
