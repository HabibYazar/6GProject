"""A pool of engine instances, because throughput comes from more engines.

One run uses exactly one core: ns-3 is a discrete-event simulator with a single
ordered event queue, so it processes one event at a time no matter how many
cores it is offered (measured: 102% CPU with Cpus_allowed_list 0-7 and no
container, cgroup or affinity limit). Making one run faster is therefore capped
— but a campaign is many INDEPENDENT runs, and those scale almost linearly.

Measured 2026-09-02 on this path (the :8091 bridge, which is what a campaign
actually drives), one engine instance per run:

    parallel   wall/run   runs/min   slowdown
       1         10.2 s     5.88      1.00x
       6         10.6 s    33.96      1.04x

Each run gets its OWN instance rather than sharing one. That is not a detail:
`run_scenario.sh` writes fifteen fixed /tmp paths (UE positions, faults, antenna
files, logs), so two runs inside one engine would read each other's inputs —
silent cross-contamination that looks exactly like the run-to-run variance we
already have, and would be very hard to notice.

Locally an instance is a container with its own port; under Kubernetes it is a
pod. The pool does not care which, so the campaign code is the same in both.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

logger = logging.getLogger(__name__)

# Sessions get slower the longer they follow one another on the SAME engine:
# measured over forty consecutive runs the median wall time went 23 s -> 29 s and
# the worst inter-indication gap 5.5 s -> 8.2 s, recovering on its own when left
# alone. So the cooldown is a property of an instance, not of the campaign: with
# a pool, the next run goes to a rested engine instead of waiting.
COOLDOWN_S = float(os.environ.get("NSG_ENGINE_COOLDOWN_S", "20"))


def _configured() -> list[str]:
    """Instance addresses, most specific source first.

    NSG_ENGINE_POOL is a comma-separated list of host:port for the LIVE bridge
    of each instance ("127.0.0.1:8091,127.0.0.1:8201"). With nothing set the pool
    holds the single configured engine, so behaviour is unchanged until an
    operator opts in.
    """
    raw = os.environ.get("NSG_ENGINE_POOL", "").strip()
    if raw:
        return [a.strip() for a in raw.split(",") if a.strip()]
    return [os.environ.get("NSG_NSORAN_LIVE", "127.0.0.1:8091")]


class EnginePool:
    def __init__(self, addrs: list[str] | None = None, cooldown_s: float | None = None):
        self._addrs = list(addrs or _configured())
        self._cooldown = COOLDOWN_S if cooldown_s is None else cooldown_s
        self._free: asyncio.Queue[str] = asyncio.Queue()
        for a in self._addrs:
            self._free.put_nowait(a)
        # When each instance last finished a run, so a rested one is preferred
        # and a hot one is given its pause before being handed out again.
        self._last_used: dict[str, float] = {a: 0.0 for a in self._addrs}
        # Addresses already reported as unreachable, so a dead instance produces
        # one warning rather than one per acquisition.
        self._warned: set[str] = set()

    @property
    def size(self) -> int:
        return len(self._addrs)

    @property
    def addresses(self) -> list[str]:
        return list(self._addrs)

    @staticmethod
    async def _alive(addr: str, timeout: float = 3.0) -> bool:
        """Is this instance reachable AND is its handler responsive?

        This used to be a bare TCP connect-and-close, which was wrong in both
        directions. It under-tested: the bridge's accept loop can be perfectly
        healthy while every session it starts dies instantly, and a connect
        cannot tell those apart. And it was not read-only — the bridge starts a
        session for whoever connects, so each probe spawned ns-3, the RIC and the
        xApp, took the engine lock, and had them killed again the moment the
        first KPM line failed to send. Measured 2026-09-09: three probes, three
        "session ended" pairs in the engine log. Since `health()` runs on every
        UI poll and `acquire()` before every campaign run, the health check was
        itself churning the engine it was meant to be checking.

        So the probe now ASKS, using the handshake the bridge learned in 1.6: a
        `nsg_probe` line, answered with `probe_ok` and no session. Getting that
        answer back proves the handler is running, not just the socket.
        """
        return (await EnginePool._probe(addr, timeout)) != "down"

    @staticmethod
    async def _probe(addr: str, timeout: float = 3.0) -> str:
        """Returns "up", "busy" or "down".

        The bridge serves ONE connection at a time — its accept loop calls
        handle() synchronously — so while a run is in progress a second
        connection sits in the backlog unanswered. A probe that only asked "did
        the socket open" could not tell that from a crash, and the UI said
        "0 of 1 engines reachable" through every normal campaign, which reads as
        broken when it means busy. The two ARE distinguishable: a dead engine
        refuses the connection outright; a busy one accepts it and never gets
        round to replying.
        """
        host, _, port = addr.partition(":")
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, int(port or "8091")), timeout=timeout)
            writer.write(b'{"nsg_probe":true}\n')
            await writer.drain()
            # The bridge greets with live_status before it reads, so the answer
            # is the SECOND line; read a couple and look for the acknowledgement.
            deadline = asyncio.get_running_loop().time() + timeout
            for _ in range(4):
                left = deadline - asyncio.get_running_loop().time()
                if left <= 0:
                    break
                line = await asyncio.wait_for(reader.readline(), timeout=left)
                if not line:
                    break
                if b"probe_ok" in line:
                    return "up"
            # Connected, but nobody answered — the handler is occupied.
            return "busy"
        except ConnectionRefusedError:
            return "down"
        except OSError:
            return "down"
        except Exception:  # noqa: BLE001
            return "busy"
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass

    @contextlib.asynccontextmanager
    async def acquire(self):
        """Hand out one LIVE instance, waiting out its cooldown before yielding.

        The address is checked before it is handed over. Without that, an
        instance listed in NSG_ENGINE_POOL that is not actually running gets
        dealt out in rotation and every run that draws it dies with a connection
        error — measured: half a campaign's runs failed that way, and the UI
        showed a dash for every metric, which reads as "the engine measured
        nothing" rather than "this engine was not there".

        A dead instance is skipped and put back, so a container that comes up
        later rejoins on its own. If NONE answer the caller gets an error that
        names them, rather than a connection refused from somewhere deeper.
        """
        tried: list[str] = []
        addr = None
        for _ in range(self.size):
            candidate = await self._free.get()
            if await self._alive(candidate):
                addr = candidate
                break
            tried.append(candidate)
            self._free.put_nowait(candidate)   # it may come back later
            if candidate not in self._warned:
                logger.warning("engine %s is in the pool but not answering — "
                               "skipping it", candidate)
                self._warned.add(candidate)
            await asyncio.sleep(0.1)           # do not spin over a dead pool
        if addr is None:
            raise RuntimeError(
                "no engine instance in the pool is reachable: "
                + ", ".join(sorted(set(tried)) or self._addrs)
                + " — check the containers, or NSG_ENGINE_POOL")
        self._warned.discard(addr)
        try:
            idle = time.monotonic() - self._last_used[addr]
            if self._last_used[addr] and idle < self._cooldown:
                wait = self._cooldown - idle
                logger.info("engine %s resting %.0fs before its next run", addr, wait)
                await asyncio.sleep(wait)
            yield addr
        finally:
            self._last_used[addr] = time.monotonic()
            self._free.put_nowait(addr)


    async def health(self) -> list[dict]:
        """Which instances actually answer, for the UI to show BEFORE a run.

        A campaign on a pool of two where only one is alive takes twice as long
        and runs sequentially. That is correct behaviour — a dead instance is
        skipped rather than handed out — but it was only ever written to the log,
        so from the outside a serialised campaign looked like two runs that
        "started together" and finished minutes apart, with no explanation.
        """
        out = []
        for a in self._addrs:
            # Probe once per instance: the probe opens a real connection, and
            # doing it twice per address to fill two fields would double that.
            state = await self._probe(a, timeout=2.0)
            out.append({"address": a, "state": state, "alive": state != "down"})
        return out

_POOL: EnginePool | None = None


def pool() -> EnginePool:
    global _POOL
    if _POOL is None:
        _POOL = EnginePool()
        logger.info("engine pool: %d instance(s) %s", _POOL.size, _POOL.addresses)
    return _POOL


def reset_pool() -> None:
    """Drop the cached pool so a changed NSG_ENGINE_POOL takes effect (tests)."""
    global _POOL
    _POOL = None
