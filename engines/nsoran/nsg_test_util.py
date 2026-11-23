#!/usr/bin/env python3
"""Shared helpers for the engine test suites.

This exists because of one specific bug. The engine serialises batch runs
behind a lock and answers `status: busy` when it cannot get it, and that lock
outlives the previous HTTP response by a moment — so back-to-back runs hit it. A
busy reply carries no measurements, which reads as zero throughput, and a test
comparing two runs then scores the collapse as "the lever works".

That cost a green suite that was entirely fictional. Every new engine test
should import `run` and `thp` from here rather than write its own, so the fix
stays fixed.
"""
import json
import time
import urllib.request

ENGINE = "http://localhost:8090/run"


class Checker:
    """Prints PASS/FAIL as it goes and remembers what failed."""

    def __init__(self) -> None:
        self.fails: list[str] = []

    def __call__(self, ok: bool, msg: str) -> bool:
        print(f"  {'PASS' if ok else 'FAIL'}  {msg}", flush=True)
        if not ok:
            self.fails.append(msg)
        return ok

    def guarded(self, name, fn) -> None:
        """A run that could not be made is a FAILED check, not a crashed
        suite — and never a silent zero."""
        try:
            fn()
        except Exception as exc:
            self(False, f"{name}: {exc}")

    def report(self) -> int:
        print(f"\n{'ALL PASS' if not self.fails else str(len(self.fails)) + ' FAILED'}")
        for f in self.fails:
            print("  -", f)
        return 1 if self.fails else 0


def run(params: dict, sim_time: int = 20) -> dict:
    """One engine run that is actually a run. Raises on anything else."""
    body = json.dumps({"sim_time": sim_time, **params}).encode()
    for _ in range(6):
        req = urllib.request.Request(ENGINE, body,
                                     {"Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=900).read())
        if d.get("status") == "ok":
            return d
        if d.get("status") != "busy":
            raise RuntimeError(f"engine returned {d.get('status')}: {d.get('error')}")
        time.sleep(20)
    raise RuntimeError("engine stayed busy across every retry")


def metric(d: dict, name: str, field: str = "mean") -> float:
    return float((d.get("measurements") or {}).get(name, {}).get(field, 0.0))


def thp(d: dict) -> float:
    """Total DL throughput across the reporting UEs, in kbps.

    The SUM, not the per-UE mean. Anything that redistributes capacity —
    a slice reservation, a failed cell — makes the mean over reporting UEs rise
    while the network carries less, because the starved UEs drop out of the
    average. Measured: holding half a cell out of the pool read as a 14%
    throughput INCREASE on the mean.
    """
    m = (d.get("measurements") or {}).get("DRB.UEThpDl", {})
    total = float(m.get("mean", 0.0)) * int(m.get("count", 0))
    if not total:
        raise RuntimeError("run reported no DL throughput; refusing to compare")
    return total
