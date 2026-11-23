"""Bring engine instances up and down for a partitioned run.

NOT `engine_pool`, which is a different job with a similar name. That one hands
out instances that ALREADY EXIST to campaign runs on the live bridge (:8091),
with cooldown and health probing, and deliberately leaves their lifecycle
outside itself — locally they are containers, under Kubernetes they are pods.
This one owns the LOCAL lifecycle of the instances a partitioned run needs: it
creates them and removes them again. Both engine ports come with the instance,
so the same lease serves a batch partition (`ports_for`, :8090) and a live
streaming one (`live_addrs_for`, :8091) — a campaign or a RIC real-engine
session takes the live addresses.

A partitioned run needs one engine instance per partition — never two runs on
one instance, because `run_scenario.sh` writes fifteen fixed /tmp paths and two
runs sharing an engine would read each other's inputs.

Until now that was `tools/engine_pool_up.sh`, run by hand. A run route cannot
ask the user to do that first, so the lifecycle moves here. This SHELLS OUT to
the same script rather than reimplementing it: the script already handles the
things that took measurements to find — recreating an instance created from a
stale image, giving every instance its own state volume so they do not overwrite
each other's snapshot, and waiting until each one actually answers.

The cap is physical cores and it is enforced, not advised. Past it the wall
clock keeps improving while the served-UE count collapses and every run still
reports `ok`, so it is the kind of limit that has to refuse rather than warn.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import threading

from partition_advice import physical_cores

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools", "engine_pool_up.sh")

# One pool serves the whole backend, so two partitioned runs must not resize it
# underneath each other. Held for the duration of a run, not just the resize.
_LOCK = threading.Lock()


def ports_for(n: int) -> list[int]:
    """BATCH ports. Instance 0 is the existing engine on 8090; extras are 8101…"""
    return [8090] + [8100 + i for i in range(1, n)]


def live_addrs_for(n: int) -> list[str]:
    """LIVE bridge addresses, the other port every instance exposes.

    The batch path posts to :8090 and gets one answer; the live path holds a
    socket on :8091 and streams frames. A partitioned LIVE run needs these, and
    handing it the batch ports instead fails as a connection that opens and
    never speaks — which is indistinguishable from a busy engine.
    """
    return ["127.0.0.1:8091"] + [f"127.0.0.1:{8200 + i}" for i in range(1, n)]


def ensure(n: int, timeout: int = 600) -> list[int]:
    """Make sure `n` instances are up and return their ports.

    Idempotent: the script clears extras first, so asking for the size you want
    is always correct regardless of what was running before.
    """
    cap = physical_cores()
    if n < 1:
        raise ValueError("a run needs at least one engine instance")
    if n > cap:
        raise ValueError(
            f"{n} partitions need {n} engine instances, and this machine has {cap} "
            f"physical cores. Past the physical count the wall clock keeps improving "
            f"while UEs stop being served and the run still reports ok, so this is "
            f"refused rather than allowed with a warning.")
    if n == 1:
        return ports_for(1)

    proc = subprocess.run(["bash", SCRIPT, str(n)], cwd=ROOT,
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"could not bring up {n} engine instances: "
            f"{(proc.stderr or proc.stdout or '').strip()[:300]}")
    return ports_for(n)


def release(timeout: int = 300) -> None:
    """Drop the extra instances, leaving the single engine on 8090.

    Best effort on purpose: a pool left standing costs idle containers, while a
    raised exception here would mask whatever the run itself returned.
    """
    try:
        subprocess.run(["bash", SCRIPT, "0"], cwd=ROOT,
                       capture_output=True, text=True, timeout=timeout)
    except Exception:
        pass


class leased:
    """`with leased(n) as ports:` — one resize at a time, always released.

    The lock is held for the whole run rather than just the resize: two
    partitioned runs sharing a pool would each be sized for themselves and then
    find instances missing when the other one finished.
    """

    def __init__(self, n: int):
        self.n = n
        self.ports: list[int] = []

    def __enter__(self) -> list[int]:
        _LOCK.acquire()
        try:
            self.ports = ensure(self.n)
            return self.ports
        except Exception:
            _LOCK.release()
            raise

    def __exit__(self, *exc) -> None:
        try:
            if self.n > 1:
                release()
        finally:
            _LOCK.release()


# The async half. `_sim_job` and the campaign are coroutines on the event loop,
# and `ensure` shells out to docker for the better part of ten seconds — doing
# that inline would freeze every other request, including the poll the UI uses
# to show that the run started. The lock is the SAME one the sync path takes, so
# a batch partition and a campaign cannot size the pool against each other.
@contextlib.asynccontextmanager
async def aleased(n: int):
    """`async with aleased(n) as live_addrs:` — live addresses, always released."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _LOCK.acquire)
    try:
        await loop.run_in_executor(None, ensure, n)
        yield live_addrs_for(n)
    finally:
        try:
            if n > 1:
                await loop.run_in_executor(None, release)
        finally:
            _LOCK.release()
