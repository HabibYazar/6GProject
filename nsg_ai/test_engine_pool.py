#!/usr/bin/env python3
"""The pool's job is to bound concurrency and rest each instance. Prove both.

A pool that hands the same instance to two runs at once would put two sessions
in one engine, where fifteen fixed /tmp paths would let them read each other's
inputs — silent cross-contamination, not a crash. That is the failure this
guards against, so "never exceeds its size" is the check that matters.

Run: python3 nsg_ai/test_engine_pool.py
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import engine_pool as ep  # noqa: E402


def _all_alive(pool):
    """Stub the reachability probe for the checks that are not about it."""
    async def _yes(addr, timeout=3.0):  # noqa: ARG001
        return True
    pool._alive = _yes
    return pool

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILS.append(name)


async def concurrency_never_exceeds_size() -> None:
    pool = _all_alive(ep.EnginePool(["a:1", "b:2"], cooldown_s=0))
    live = 0
    peak = 0
    seen: set[str] = set()

    async def worker():
        nonlocal live, peak
        async with pool.acquire() as addr:
            seen.add(addr)
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.05)
            live -= 1

    await asyncio.gather(*(worker() for _ in range(8)))
    check("concurrency never exceeds pool size", peak <= 2, f"peak {peak} of 2")
    check("every instance gets used", seen == {"a:1", "b:2"}, f"used {sorted(seen)}")


async def a_dead_instance_is_skipped() -> None:
    """An address in NSG_ENGINE_POOL whose container is not running must not be
    dealt out: every run that drew it died with a connection error, and the UI
    showed a dash for every metric — which reads as "the engine measured
    nothing" rather than "this engine was not there"."""
    pool = ep.EnginePool(["dead:1", "live:2"], cooldown_s=0)

    async def _only_live(addr, timeout=3.0):  # noqa: ARG001
        return addr == "live:2"
    pool._alive = _only_live

    seen = []
    for _ in range(4):
        async with pool.acquire() as addr:
            seen.append(addr)
    check("a dead instance is never handed out", set(seen) == {"live:2"},
          f"got {sorted(set(seen))}")


async def an_all_dead_pool_says_so() -> None:
    """With nothing reachable the caller gets an error naming the addresses,
    not a connection refused from somewhere deeper in the stack."""
    pool = ep.EnginePool(["a:1", "b:2"], cooldown_s=0)

    async def _none(addr, timeout=3.0):  # noqa: ARG001
        return False
    pool._alive = _none

    try:
        async with pool.acquire():
            check("an all-dead pool raises", False, "it handed one out")
    except RuntimeError as e:
        check("an all-dead pool raises and names the instances",
              "a:1" in str(e) and "b:2" in str(e), str(e)[:60])


async def instance_rests_between_runs() -> None:
    pool = _all_alive(ep.EnginePool(["only:1"], cooldown_s=0.3))
    t = asyncio.get_event_loop().time
    async with pool.acquire():
        pass
    t0 = t()
    async with pool.acquire():
        waited = t() - t0
    check("a reused instance waits out its cooldown", waited >= 0.25,
          f"waited {waited:.2f}s of 0.3")


async def a_rested_instance_is_not_delayed() -> None:
    # The cooldown is per instance, so a second engine must NOT inherit the
    # first one's pause — that was the whole point of moving it off the campaign.
    pool = _all_alive(ep.EnginePool(["a:1", "b:2"], cooldown_s=0.4))
    t = asyncio.get_event_loop().time
    async with pool.acquire():
        pass
    t0 = t()
    async with pool.acquire():
        waited = t() - t0
    check("an unused instance is handed over immediately", waited < 0.2,
          f"waited {waited:.2f}s")


def configuration() -> None:
    os.environ.pop("NSG_ENGINE_POOL", None)
    os.environ["NSG_NSORAN_LIVE"] = "10.0.0.1:9999"
    ep.reset_pool()
    check("with no pool configured it holds the single engine",
          ep.pool().addresses == ["10.0.0.1:9999"], str(ep.pool().addresses))

    os.environ["NSG_ENGINE_POOL"] = " x:1 , y:2 ,, z:3 "
    ep.reset_pool()
    check("NSG_ENGINE_POOL is parsed and trimmed",
          ep.pool().addresses == ["x:1", "y:2", "z:3"], str(ep.pool().addresses))
    os.environ.pop("NSG_ENGINE_POOL", None)
    os.environ.pop("NSG_NSORAN_LIVE", None)
    ep.reset_pool()


async def main() -> int:
    print("=" * 62)
    print("ENGINE POOL")
    print("=" * 62)
    await concurrency_never_exceeds_size()
    await a_dead_instance_is_skipped()
    await an_all_dead_pool_says_so()
    await instance_rests_between_runs()
    await a_rested_instance_is_not_delayed()
    configuration()
    print()
    if FAILS:
        print(f"{len(FAILS)} CHECK(S) FAILED: {'; '.join(FAILS)}")
        return 1
    print("ALL ENGINE POOL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
