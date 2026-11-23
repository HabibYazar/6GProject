#!/usr/bin/env python3
"""Does engine_acceptance.py actually DETECT anything?

The suite passes all ten checks against the live engine, but a check that can
never fail passes too, and it looks exactly the same in the output. A vacuous
invariant — one computed over an empty list, or comparing a value to itself —
is the most likely defect in a suite like this, and the least visible.

So: feed each check data that violates it and require it to fail. This tests the
DETECTOR, not the engine. It needs no container, no live run, and no rebuild, so
it can run on every change; proving the suite against a real reverted engine bug
is the separate, slower exercise.

Run: python3 tools/test_acceptance_detects.py
"""
from __future__ import annotations

import copy
import io
import contextlib
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import engine_acceptance as ea  # noqa: E402

# Every invariant case below is about tick data; the negative-counter check reads
# the engine's log, so it is neutralised by default and exercised explicitly.
ea.negative_counters = lambda: []

RESULTS: list[tuple[str, bool, str]] = []


def _tick(i: int, *, cells, ues, energy_w, per_cell_w, sim_t, kpm_fresh=True):
    return SimpleNamespace(tick=i, cells=cells, ues=ues, energy_w=energy_w,
                           per_cell_w=per_cell_w, sim_t=sim_t, kpm_fresh=kpm_fresh)


def clean_ticks(n: int = 8) -> list:
    """A run with nothing wrong with it: every check must pass on this."""
    out = []
    for i in range(n):
        cells = [{"cell": 2, "ues": 3, "thp_kbps": 900 + 10 * i, "power_w": 20.0, "prb": 40},
                 {"cell": 3, "ues": 2, "thp_kbps": 600 + 10 * i, "power_w": 18.0, "prb": 30}]
        # Each UE carries its SERVING cell: cells 2 and 3 hold 3 and 2 UEs, which
        # is what the cells list above reports.
        ues = [{"imsi": u, "cell": 2 if u <= 3 else 3, "thp_kbps": 100 + u + i}
               for u in range(1, 6)]
        out.append(_tick(i, cells=cells, ues=ues, energy_w=38.0,
                         per_cell_w={2: 20.0, 3: 18.0}, sim_t=round(0.1 * i, 2)))
    return out


def run_checks(ticks: list, fn) -> list[str]:
    """Run one check group over ticks, returning the names that FAILED."""
    ea.FAILS = []
    with contextlib.redirect_stdout(io.StringIO()):
        fn(ticks)
    return list(ea.FAILS)


def expect(label: str, ticks: list, fn, must_fail_substr: str | None) -> None:
    failed = run_checks(ticks, fn)
    if must_fail_substr is None:
        ok, detail = not failed, f"unexpected failures: {failed}" if failed else "clean"
    else:
        hit = [f for f in failed if must_fail_substr in f]
        ok = bool(hit)
        detail = f"fired: {hit[0]}" if hit else f"DID NOT FIRE (failures seen: {failed})"
    RESULTS.append((label, ok, detail))


def main() -> int:
    print("=" * 74)
    print("ACCEPTANCE SELF-TEST — can each check actually fail?")
    print("=" * 74)

    # Baseline: a clean run must not trip anything. If this fails, every
    # mutation below proves nothing, because the check was already firing.
    expect("clean run trips no invariant", clean_ticks(), ea.invariants, None)
    expect("clean run trips no liveness check", clean_ticks(), ea.responds_to_config, None)

    # A cell serving nobody, carrying nothing, awake, and still billing watts.
    # This is the shape of the TX-power bug's mirror image.
    t = clean_ticks()
    t[4].cells.append({"cell": 4, "ues": 0, "thp_kbps": 0, "power_w": 17.0,
                       "prb": 0, "sleep_state": "awake"})
    expect("ghost power draw is detected", t, ea.invariants, "draws power")

    # A cell transmitting for free: real traffic, real UEs, ~0 W reported.
    # No check caught this class until 2026-09-02, and it is precisely the
    # TX-power PSD bug that made cell-off and RU sleep look free for a session.
    t = clean_ticks()
    t[5].cells[0]["power_w"] = 0.0
    expect("traffic at zero reported power is detected", t, ea.invariants,
           "carries traffic while reporting no power")

    # A counter reported negative. The check reads the engine's log rather than
    # tick data, so it is stubbed here the way engine_roster is.
    orig_neg = ea.negative_counters
    try:
        ea.negative_counters = lambda: []
        expect("clean counters trip nothing", clean_ticks(), ea.invariants, None)
        ea.negative_counters = lambda: [("DRB.EstabSucc.5QI.UEID", -4.0)]
        expect("a negative counter is detected", clean_ticks(), ea.invariants,
               "count metric is reported negative")
    finally:
        ea.negative_counters = orig_neg

    # Per-cell power that does not add up to the reported network total.
    t = clean_ticks()
    t[2].energy_w = 95.0
    expect("power that does not sum is detected", t, ea.invariants, "sums to the network total")

    # PRB outside 0-100: a decode error, not a busy cell.
    t = clean_ticks()
    t[3].cells[0]["prb"] = 145
    expect("out-of-range PRB is detected", t, ea.invariants, "PRB stays within")
    t = clean_ticks()
    t[3].cells[0]["prb"] = -4
    expect("negative PRB is detected", t, ea.invariants, "PRB stays within")

    # UEs vanishing after the roster settled (not the attach ramp).
    t = clean_ticks()
    for k in range(5, 8):
        t[k].ues = t[k].ues[:1]
    expect("UEs vanishing after attach is detected", t, ea.invariants, "stable once attached")

    # Frames that do not say whether they carried a fresh KPM report: without
    # this every experiment over-counts n by ~10x.
    t = clean_ticks()
    for x in t:
        x.kpm_fresh = None
    expect("unlabelled frames are detected", t, ea.invariants, "sample identity")

    # A frozen or replayed run: identical throughput in every frame.
    t = clean_ticks()
    for x in t:
        x.ues = [{"imsi": u, "thp_kbps": 100} for u in range(1, 6)]
    expect("frozen throughput is detected", t, ea.responds_to_config, "throughput varies")

    # Simulated time standing still.
    t = clean_ticks()
    for x in t:
        x.sim_t = 1.0
    expect("stalled sim_t is detected", t, ea.responds_to_config, "simulated time advances")

    # Every UE parked on one cell — the shape of a broken attach.
    t = clean_ticks()
    for x in t:
        x.cells = [dict(x.cells[0]), {**x.cells[1], "ues": 0}]
    expect("single-cell traffic is detected", t, ea.responds_to_config, "more than one cell")

    # Roster agreement: the engine reports a cell the platform never shows.
    orig = ea.engine_roster
    try:
        # The engine reports on MORE UEs than a cell serves (neighbour
        # measurements), so a correct roster is a superset, never an equal count.
        ea.engine_roster = lambda: {2: {1, 2, 3, 7}, 3: {4, 5, 1}}
        expect("agreement passes when the roster covers the platform's view",
               clean_ticks(), ea.agreement, None)

        ea.engine_roster = lambda: {2: {1, 2, 3}, 3: {4, 5}, 9: {8}}
        expect("a cell missing from the platform view is detected", clean_ticks(),
               ea.agreement, "appears in the platform view")

        # The platform claims UE 5 is served by cell 3, but cell 3's own report
        # never mentions it — an attachment the engine did not observe.
        ea.engine_roster = lambda: {2: {1, 2, 3}, 3: {4}}
        expect("an attachment the engine never reported is detected",
               clean_ticks(), ea.agreement, "in that cell's own report")

        ea.engine_roster = lambda: {}
        expect("an unreadable roster is detected", clean_ticks(),
               ea.agreement, "roster is readable")
    finally:
        ea.engine_roster = orig

    for label, ok, detail in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}  ({detail})")

    bad = [l for l, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 74)
    if bad:
        print(f"{len(bad)} CHECK(S) CANNOT DETECT THEIR OWN DEFECT: {'; '.join(bad)}")
        print("Such a check passes against the engine no matter what it reports.")
        return 1
    print(f"ALL {len(RESULTS)} MUTATIONS CAUGHT — every acceptance check can fail.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
