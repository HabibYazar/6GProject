#!/usr/bin/env python3
"""The PARTITION pool lease (partition_pool, not engine_pool), checked without docker.

The lifecycle is the part that lets a run route accept partitioning at all, and
it has two ways to fail quietly: sizing past the physical cores (which produces
a fast run that has silently stopped serving UEs) and leaving instances standing
after a run. Both are checked here by substituting the subprocess call, so this
runs anywhere — the real bring-up is exercised end to end by a partitioned run.

    python3 tools/test_partition_pool.py
"""
import subprocess
import sys
import threading

sys.path.insert(0, "/home/chancelor/nsg_ai_platform")
sys.path.insert(0, "/home/chancelor/nsg_ai_platform/nsg_ai")
import partition_pool

fails = []
calls: list[list[str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'  ok  ' if ok else 'FAIL  '}{name}{'   ' + detail if detail else ''}")
    if not ok:
        fails.append(name)


class _Result:
    returncode = 0
    stdout = "ok"
    stderr = ""


def _fake_run(cmd, **kw):
    calls.append(list(cmd))
    return _Result()


partition_pool.subprocess.run = _fake_run          # type: ignore[assignment]
partition_pool.physical_cores = lambda: 6          # type: ignore[assignment]

print("== ports ==")
check("instance 0 is the existing engine on 8090", partition_pool.ports_for(1) == [8090],
      str(partition_pool.ports_for(1)))
check("extras are 8101, 8102, …", partition_pool.ports_for(4) == [8090, 8101, 8102, 8103],
      str(partition_pool.ports_for(4)))
check("ports are unique", len(set(partition_pool.ports_for(6))) == 6)

print("\n== the core cap refuses, it does not warn ==")
try:
    partition_pool.ensure(7)
    check("7 partitions on 6 cores is refused", False, "no error raised")
except ValueError as e:
    check("7 partitions on 6 cores is refused", True)
    check("and the refusal says why", "served" in str(e).lower(), str(e)[:70])
check("6 on 6 is allowed", partition_pool.ensure(6) == partition_pool.ports_for(6))
try:
    partition_pool.ensure(0)
    check("0 instances is refused", False, "no error raised")
except ValueError:
    check("0 instances is refused", True)

print("\n== a single-engine run does not touch the pool ==")
calls.clear()
check("ensure(1) returns the plain engine", partition_pool.ensure(1) == [8090])
check("and starts nothing", calls == [], str(calls))

print("\n== the lease always releases ==")
calls.clear()
with partition_pool.leased(4) as ports:
    check("the lease hands over 4 ports", ports == [8090, 8101, 8102, 8103], str(ports))
    check("the pool was sized up", any(c[-1] == "4" for c in calls), str(calls))
check("and torn down after", any(c[-1] == "0" for c in calls), str(calls))

calls.clear()
try:
    with partition_pool.leased(2):
        raise RuntimeError("the run blew up")
except RuntimeError:
    pass
check("a failed run still releases the pool", any(c[-1] == "0" for c in calls), str(calls))

print("\n== the lock is not left held ==")
# A lease that raises while SIZING must not strand the lock, or every later
# partitioned run blocks forever on a pool it never got.
try:
    with partition_pool.leased(99):
        pass
except ValueError:
    pass
got = partition_pool._LOCK.acquire(timeout=2)
check("a refused lease releases the lock", got)
if got:
    partition_pool._LOCK.release()

done = threading.Event()
def _second():
    with partition_pool.leased(2):
        pass
    done.set()
with partition_pool.leased(2):
    t = threading.Thread(target=_second, daemon=True); t.start()
    check("a second run waits for the first", not done.wait(1.5))
t.join(timeout=10)
check("and proceeds once the first releases", done.is_set())

print("\n" + (f"{len(fails)} FAILED: {', '.join(fails)}" if fails else "all passed"))
raise SystemExit(1 if fails else 0)
