#!/usr/bin/env python3
"""The engine's telemetry defects, caught where the product reads them.

The slow tier runs and its physics is consistent — everything the fast tier
derived from its source reproduced its behaviour. What it REPORTS about
itself has five known defects, three of which feed headline numbers, and
none of them needs an engine rebuild to contain. These checks keep the
containment in place.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "nsg_ai"))

import kpm_health

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(name)


print("wrapped counters")
check("the measured underflow is caught",
      kpm_health.is_wrapped(4294966948),
      "2^32 - 348, reported for a queue whose ceiling is 10 MB")
check("a full buffer is not",
      not kpm_health.is_wrapped(10485760) and not kpm_health.is_wrapped(2 ** 30),
      "a gigabyte is implausible for this queue but it is still a number")
check("junk does not crash the guard",
      not kpm_health.is_wrapped(None) and not kpm_health.is_wrapped("x"))

kept, lost = kpm_health.clean_samples(
    "DRB.BufferSize.Qos", [1.0e6, 4294966948.0, 2.0e6, 4294967000.0])
check("wrapped samples are removed and counted",
      kept == [1.0e6, 2.0e6] and lost == 2,
      "a value silently removed misleads as much as a wrong one kept, so the "
      "count goes into the summary")

print("\ncaveats reach the run that carries them")
c = kpm_health.caveats_for(["DRB.UEThpDl", "RRU.PrbUsedDl", "TB.TotNbrDl.1"])
check("a counter with a known defect carries its caveat",
      "DRB.UEThpDl" in c and "RRU.PrbUsedDl" in c)
check("a clean counter carries none",
      "TB.TotNbrDl.1" not in c)
check("the throughput caveat names the actual problem",
      "delay" in c["DRB.UEThpDl"].lower(),
      "it is bits over a latency, and a reader who does not know that will "
      "compare it with a rate")

print("\nthe summary path uses the guard")
import nsoran_live
src = open(nsoran_live.__file__).read()
check("nsoran_live cleans samples before averaging",
      "kpm_health.clean_samples" in src)
check("and publishes what it dropped",
      "telemetry_dropped" in src and "telemetry_caveats" in src)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    raise SystemExit(1)
print("all passed")
