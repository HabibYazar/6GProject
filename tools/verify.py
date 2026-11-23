#!/usr/bin/env python3
"""One command that proves the platform still works — and says what it did not check.

Everything here already existed as a separate tool, run from memory. That is how
`tools/test_geometry.py` sat broken from 2026-07-15 to 2026-09-15 without anyone
noticing: nothing ran it. A day that produces fourteen commits, three migrations
and eight engine images is protected by whatever the person happened to remember,
which is not protection.

Tiered because the checks have very different costs, and a suite nobody runs
because it takes twenty minutes protects nothing:

    fast    seconds      no engine, no network — unit tests, contracts, types
    engine  a minute     needs the Ubuntu-2004-nsoran distro up
    slow    minutes      real runs against the engine

    python3 tools/verify.py                 # fast + engine
    python3 tools/verify.py --fast          # just the cheap tier
    python3 tools/verify.py --all           # everything, including slow

WHAT IT REPORTS IS THE POINT. A check that could not run is SKIPPED with the
reason, never silently dropped and never counted as a pass — "12 passed" when
four of them never executed is the failure mode this exists to remove.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_BIN = os.path.join(ROOT, "venv", "bin", "python")
GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


class Check:
    def __init__(self, tier: str, name: str, cmd: list[str], why: str,
                 cwd: str | None = None, env: dict | None = None,
                 timeout: int = 900):
        self.tier, self.name, self.cmd, self.why = tier, name, cmd, why
        self.cwd, self.env, self.timeout = cwd or ROOT, env, timeout


def checks() -> list[Check]:
    env_pp = {"PYTHONPATH": f"{ROOT}:{os.path.join(ROOT, 'nsg_ai')}"}
    return [
        Check("fast", "unit + contract tests",
              [PY_BIN, "-m", "pytest", "nsg_ai/", "-q"], env=env_pp,
              why="the prompt contract and campaign scoring, and the LLM tests "
                  "skip themselves unless NSG_LLM_TESTS=1"),
        Check("fast", "geometry guards",
              [PY_BIN, "tools/test_geometry.py"],
              why="a UE on a base station returns zero DU metrics SILENTLY, which "
                  "is why this is a test and not a comment"),
        Check("fast", "no inert schema fields",
              [PY_BIN, "tools/param_audit.py", "--cached", "--check"],
              why="a field the config translator never reads is the bug class "
                  "this repo went to 0 for; --cached so it needs no engine"),
        Check("fast", "frontend types",
              ["node_modules/.bin/tsc", "--noEmit", "-p", "tsconfig.json"],
              cwd=os.path.join(ROOT, "ui"),
              why="the UI reads engine fields by name; a renamed field is a "
                  "compile error here and a blank panel in a demo"),
        Check("fast", "partition adviser matches its measurements",
              [PY_BIN, "tools/test_partition_advice.py"],
              why="it turns measured shape tables into advice; a model that "
                  "drifts from its own runs gives a number with the authority "
                  "of a measurement and none of the truth"),
        Check("fast", "partition pool lease",
              [PY_BIN, "tools/test_partition_pool.py"],
              why="the lifecycle a run route needs: the core cap must REFUSE "
                  "(past it UEs stop being served while the run still says ok), "
                  "and a refused lease must not strand the lock"),
        Check("fast", "partition split + merge",
              [PY_BIN, "tools/test_partition_run.py"],
              why="a bad split hands a partition another one's per-UE strings "
                  "and a bad merge averages means over different populations; "
                  "both produce plausible numbers instead of an error"),
        Check("fast", "partition LIVE merge",
              [PY_BIN, "tools/test_partition_live.py"],
              why="merging N live streams fails SILENTLY: every partition numbers "
                  "its cells from the same base and hands its first UE IMSI 1, so "
                  "a raw merge collapses the network and stacks UEs; and control "
                  "has to reach the one process holding the named cell"),
        Check("fast", "E2 feed contract",
              [PY_BIN, "tools/test_e2_feed.py"],
              why="the seam between Tier B and a real E2AP agent is a flat "
                  "file agreed in Python and parsed in C, and a format agreed "
                  "in two languages drifts in one of them; this builds the C "
                  "reader and runs it against a feed Python wrote"),
        Check("fast", "engine telemetry guards",
              [PY_BIN, "tools/test_kpm_health.py"],
              why="the slow tier's physics is consistent but five of its own "
                  "counters are not, and three feed headline numbers: a queue "
                  "that underflows to 4.29e9, a PRB figure that under-reports "
                  "the airtime it measured, and a 'throughput' that is bits "
                  "over a latency. All contained on the way in, and a "
                  "container that silently stops containing looks identical"),
        Check("fast", "fast tier",
              [PY_BIN, "tools/test_fast_tier.py"],
              why="the RIC console's RAN computes its window through the "
                  "engine's own AMC tables now, and every way that swap breaks "
                  "still renders: a key the console reads disappears, a counter "
                  "stops scaling with the report window, or the array gain "
                  "lands on the interferers too and cancels itself out"),
        Check("fast", "metrics have readable names",
              [PY_BIN, "tools/test_metric_names.py"],
              why="the trend chart renders whatever the backend publishes; a "
                  "metric added without a label silently reappears as a raw "
                  "field path, which says neither what it is nor its unit"),
        Check("fast", "map building import",
              ["node", "tools/test_map_import.mjs"],
              why="sparse-area imports used to fail with one message and advice "
                  "that could not work; the fetch itself needs a browser"),
        Check("engine", "every link, end to end",
              [PY_BIN, "tools/preflight.py"],
              why="both engine ports, the cross-distro path, the database "
                  "migration, the backend and the frontend"),
        Check("engine", "every engine patch is in the tree",
              [PY_BIN, "tools/patch_audit.py", "--check"],
              why="engines/nsoran/*.patch is 16 real diffs and 23 prose NOTES; "
                  "grepping a note for its own text reports shipped features as "
                  "missing, and one probe per diff reported three present ones "
                  "as missing because later patches edited the probed lines"),
        Check("engine", "engine image matches the repo",
              [PY_BIN, "tools/engine_sync.py"],
              why="engine code lives in three places and reaches the product ONLY "
                  "through the image; drift is silent"),
        Check("slow", "the engine still tells the truth",
              [PY_BIN, "tools/engine_acceptance.py"], timeout=1800,
              why="compares the engine's own roster against the platform view "
                  "plus physical invariants"),
    ]


def run(c: Check) -> tuple[str, str, float]:
    env = dict(os.environ)
    if c.env:
        env.update(c.env)
    # A check is skipped only when its command genuinely is not there. It may be a
    # repo-relative script, an absolute path, or a bare name on PATH (`node`) — the
    # last one used to be reported as "not present" while sitting in /usr/bin.
    if not (os.path.exists(os.path.join(c.cwd, c.cmd[0]))
            or c.cmd[0].startswith("/")
            or shutil.which(c.cmd[0], path=env.get("PATH"))):
        return "SKIP", f"{c.cmd[0]} not present", 0.0
    t0 = time.time()
    try:
        p = subprocess.run(c.cmd, cwd=c.cwd, env=env, capture_output=True,
                           text=True, timeout=c.timeout)
    except subprocess.TimeoutExpired:
        return "FAIL", f"timed out after {c.timeout}s", time.time() - t0
    except FileNotFoundError as exc:
        return "SKIP", str(exc)[:80], time.time() - t0
    out = (p.stdout + p.stderr).strip().splitlines()
    tail = out[-1] if out else ""
    return ("PASS" if p.returncode == 0 else "FAIL"), tail[:100], time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fast", action="store_true", help="cheap tier only")
    ap.add_argument("--all", action="store_true", help="include the slow tier")
    args = ap.parse_args()
    tiers = {"fast"} if args.fast else {"fast", "engine"}
    if args.all:
        tiers |= {"engine", "slow"}

    selected = [c for c in checks() if c.tier in tiers]
    skipped_tiers = sorted({c.tier for c in checks()} - tiers)

    print("=" * 74)
    print("NSG VERIFY — " + ", ".join(sorted(tiers)) + " tier(s)")
    print("=" * 74)
    results = []
    for c in selected:
        print(f"\n  {c.name} {DIM}[{c.tier}]{RESET}")
        print(f"    {DIM}{c.why}{RESET}")
        status, detail, secs = run(c)
        colour = {"PASS": GREEN, "FAIL": RED, "SKIP": YELLOW}[status]
        print(f"    {colour}{status}{RESET}  {DIM}{secs:.1f}s  {detail}{RESET}")
        results.append((c, status, detail))

    failed = [(c, d) for c, s, d in results if s == "FAIL"]
    skipped = [(c, d) for c, s, d in results if s == "SKIP"]

    print("\n" + "=" * 74)
    passed = sum(1 for _, s, _ in results if s == "PASS")
    print(f"{passed} passed, {len(failed)} failed, {len(skipped)} skipped")

    # WHAT WAS NOT CHECKED, always. A summary that counts only what ran reads as
    # coverage it does not have.
    if skipped:
        print("\nnot checked (skipped):")
        for c, d in skipped:
            print(f"  · {c.name} — {d}")
    if skipped_tiers:
        print(f"\nnot checked (tier not selected): {', '.join(skipped_tiers)}")
        if "slow" in skipped_tiers:
            print("  · engine acceptance — run with --all")
    print("\nnever checked by this command at all:")
    print("  · the AI generator against a live model (NSG_LLM_TESTS=1)")
    print("  · a long-horizon run surviving hours — no soak test exists yet")
    print("  · anything in the browser beyond types compiling")

    if failed:
        print(f"\n{RED}VERIFY FAILED{RESET}")
        for c, d in failed:
            print(f"  · {c.name}: {d}")
        return 1
    print(f"\n{GREEN}VERIFY OK{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
