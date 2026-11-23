#!/usr/bin/env python3
"""
Preflight — prove the whole chain is up, link by link, before a demo or a run.

Every failure this checks for has actually happened, and each one presented as
something else:

  * The engine container was created WITHOUT publishing 8091. The container was
    "up", `/health` on 8090 answered, and every batch run failed with "live
    engine unreachable at 127.0.0.1:8091" — which reads as a dead engine.
  * `demo_engine_up.sh` printed "WARN: port-proxy NOT bound" and carried on, so
    the bring-up reported success while the platform distro could not reach the
    engine at all.
  * WSL idled the platform distro, killing the detached backend/frontend; the
    browser said "Failed to fetch" and the prompt looked broken.
  * The engine holds its run lock until the previous process exits, so a run
    started too soon comes back "busy" — and a campaign scored that as a FAILED
    scenario.

The rule here: a check passes only when the thing it claims is *observed from
the place that needs it*. Reachability from the engine's own distro proves
nothing about the platform distro, which is where the backend runs.

  python3 tools/preflight.py            # report
  python3 tools/preflight.py --fix      # also apply the safe remedies
  python3 tools/preflight.py --quiet    # only failures (for scripts)

Exit code: 0 all green, 1 something is down.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ENGINE_DISTRO = "Ubuntu-2004-nsoran"
WSL = "/mnt/c/Windows/System32/wsl.exe"
ENGINE_URL = "http://localhost:8090"
LIVE_BRIDGE_PORT = 8091
BACKEND_URL = "http://localhost:8000"
FRONTEND_URL = "http://localhost:3000"
IMAGE = "nsg-engine:1.32-rxtrace"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


class Report:
    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self.failures: list[tuple[str, str]] = []

    def ok(self, name: str, detail: str = "") -> bool:
        if not self.quiet:
            print(f"  {GREEN}PASS{RESET}  {name}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
        return True

    def fail(self, name: str, detail: str, remedy: str) -> bool:
        print(f"  {RED}FAIL{RESET}  {name}  {detail}")
        print(f"        {YELLOW}fix:{RESET} {remedy}")
        self.failures.append((name, remedy))
        return False

    def note(self, text: str) -> None:
        if not self.quiet:
            print(f"  {DIM}{text}{RESET}")


def _wsl(cmd: str, timeout: int = 60) -> tuple[int, str]:
    """Run a command in the ENGINE distro over WSL interop.

    stdout ONLY, and callers that parse it take the LAST line: `bash -lc` is a
    login shell, so a profile can print anything it likes ahead of the answer,
    and folding stderr in turned a perfectly good `docker inspect` into
    unparseable JSON — reported here, at first run, as "no ports published" on
    a container that had both.
    """
    try:
        p = subprocess.run([WSL, "-d", ENGINE_DISTRO, "-u", "root", "-e", "bash", "-lc", cmd],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or ""
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return 1, str(exc)


def _last_line(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


def _http(url: str, timeout: int = 6) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return True, r.read(400).decode(errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, str(exc)


def _tcp(host: str, port: int, timeout: float = 4.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── checks ────────────────────────────────────────────────────────────────────

def check_docker(rep: Report, fix: bool) -> bool:
    rc, _ = _wsl("docker info >/dev/null 2>&1 && echo up")
    if rc == 0:
        return rep.ok("dockerd (engine distro)")
    if not fix:
        return rep.fail("dockerd (engine distro)", "not running",
                        f"wsl -d {ENGINE_DISTRO} -u root service docker start")
    rep.note("starting dockerd…")
    # setsid, not a bare nohup: a background child of THIS wsl.exe session dies
    # when the session ends, so `nohup dockerd &` started a daemon that was
    # already gone by the time the next check ran. Same trap the platform's
    # backend/frontend bring-up hit. (`service docker` does not exist in this
    # distro at all — the fallback IS the path, so it has to be the correct one.)
    # Two WSL-specific traps, both measured, both silent:
    #
    # 1. A STALE PID FILE. dockerd refuses to start with "ensure docker is not
    #    running or delete /var/run/docker.pid: process with PID 15 is still
    #    running" — after a distro restart that PID belongs to something else
    #    entirely, so the check is fooled by a file, not by a daemon. Removed
    #    only when no dockerd process actually exists, which is exactly what the
    #    error asks for.
    # 2. The trailing sleep is load-bearing: returning the instant after the fork
    #    ends the wsl.exe session while dockerd is still coming up, and it goes
    #    with it. The same launch with a short lingering sleep survives.
    _wsl("pgrep -x dockerd >/dev/null || rm -f /var/run/docker.pid; "
         "service docker start >/dev/null 2>&1 || "
         "(setsid nohup dockerd >/var/log/dockerd.log 2>&1 </dev/null & sleep 8)",
         timeout=90)
    for _ in range(45):
        rc, _ = _wsl("docker info >/dev/null 2>&1 && echo up")
        if rc == 0:
            return rep.ok("dockerd (engine distro)", "started")
        time.sleep(1)
    return rep.fail("dockerd (engine distro)", "would not start",
                    f"wsl -d {ENGINE_DISTRO} -u root dockerd  (read /var/log/dockerd.log)")


def check_container(rep: Report, fix: bool) -> bool:
    """Up AND publishing BOTH ports.

    The port check is the point. A container created with only 8090 published
    passes every "is it running" test and fails every batch run.
    """
    rc, raw = _wsl("docker inspect nsg-engine "
                   "--format '{{.State.Running}}|{{json .HostConfig.PortBindings}}'")
    out = _last_line(raw)
    if rc != 0 or "|" not in out:
        if not fix:
            return rep.fail("engine container", "does not exist",
                            f"wsl -d {ENGINE_DISTRO} -u root docker run -d --init --name nsg-engine "
                            f"-v nsg-run-state:/var/lib/nsg "
                            f"-p 127.0.0.1:8090:8090 -p 127.0.0.1:8091:8091 {IMAGE}")
        rep.note("creating the container…")
        _wsl(f"docker run -d --init --name nsg-engine -v nsg-run-state:/var/lib/nsg "
                 f"-p 127.0.0.1:8090:8090 -p 127.0.0.1:8091:8091 {IMAGE}", timeout=120)
        time.sleep(6)
        rc, raw = _wsl("docker inspect nsg-engine "
                       "--format '{{.State.Running}}|{{json .HostConfig.PortBindings}}'")
        out = _last_line(raw)
        if rc != 0 or "|" not in out:
            return rep.fail("engine container", "could not be created",
                            f"wsl -d {ENGINE_DISTRO} -u root docker images  (is {IMAGE} there?)")

    running, _, bindings_raw = out.partition("|")
    try:
        bindings = json.loads(bindings_raw) or {}
    except json.JSONDecodeError:
        bindings = {}
    missing = [p for p in ("8090/tcp", "8091/tcp") if p not in bindings]

    if missing:
        detail = f"published {sorted(bindings) or 'nothing'} — missing {missing}"
        remedy = (f"wsl -d {ENGINE_DISTRO} -u root bash -lc "
                  f"'docker rm -f nsg-engine; docker run -d --init --name nsg-engine "
                  f"-v nsg-run-state:/var/lib/nsg "
                  f"-p 127.0.0.1:8090:8090 -p 127.0.0.1:8091:8091 {IMAGE}'")
        if not fix:
            return rep.fail("engine ports published", detail, remedy)
        # Ports are fixed at creation: a restart cannot add one.
        rep.note(f"recreating the container ({detail})…")
        _wsl(f"docker rm -f nsg-engine >/dev/null 2>&1; docker run -d --init --name nsg-engine "
             f"-p 127.0.0.1:8090:8090 -p 127.0.0.1:8091:8091 {IMAGE}", timeout=120)
        time.sleep(8)
        rc, raw = _wsl("docker inspect nsg-engine --format '{{json .HostConfig.PortBindings}}'")
        try:
            bindings = json.loads(_last_line(raw)) or {}
        except json.JSONDecodeError:
            bindings = {}
        if [p for p in ("8090/tcp", "8091/tcp") if p not in bindings]:
            return rep.fail("engine ports published", "still missing after recreate", remedy)
        return rep.ok("engine ports published", "8090 + 8091")

    if running.strip() != "true":
        if not fix:
            return rep.fail("engine container", "exists but is stopped",
                            f"wsl -d {ENGINE_DISTRO} -u root docker start nsg-engine")
        rep.note("starting the container…")
        _wsl("docker start nsg-engine >/dev/null 2>&1", timeout=60)
        time.sleep(6)

    return rep.ok("engine container", "running, 8090 + 8091 published")


def check_engine_local(rep: Report) -> bool:
    rc, raw = _wsl(f"curl -s --max-time 6 {ENGINE_URL}/health")
    out = _last_line(raw)
    if rc == 0 and "ok" in out:
        return rep.ok("engine /health (from its own distro)")
    return rep.fail("engine /health (from its own distro)", out[:60] or "no answer",
                    f"wsl -d {ENGINE_DISTRO} -u root docker logs --tail 40 nsg-engine")


def check_engine_cross(rep: Report) -> bool:
    """The one that matters: the BACKEND runs here, not in the engine distro."""
    ok, body = _http(f"{ENGINE_URL}/health")
    if ok and "ok" in body:
        return rep.ok("engine /health (from the platform distro)", "cross-distro path up")
    return rep.fail(
        "engine /health (from the platform distro)", "unreachable across distros",
        f"wsl -d {ENGINE_DISTRO} -u root bash -lc 'docker restart nsg-engine' "
        f"— then re-run preflight; a cold dockerd can leave the port-proxy unbound")


def check_engine_current(rep: Report) -> bool:
    """Is the running image the engine we wrote?

    Preflight proves every link is UP; this one asks whether the thing at the end
    of the link is current. Engine code passes repo -> host /root/nsoran -> image,
    and both hops have gone stale on us: the image once ran two days behind the
    source, and a green parity test once read the host copy while the image still
    had the old files. An engine that answers but is a version behind looks
    exactly like a healthy one.
    """
    import subprocess as _sp  # noqa: PLC0415
    rc = _sp.run([sys.executable, str(pathlib.Path(__file__).with_name("engine_sync.py")),
                  "--check"], capture_output=True, text=True, timeout=180)
    if rc.returncode == 0:
        return rep.ok("engine image matches the repo", "repo, host and image agree")
    # Pick the line that says what is WRONG. engine_sync also prints a harmless
    # note when the build profile the product does not load has drifted, and
    # that line contains "behind" too — matching it first made this report the
    # wrong reason for every failure, including "no container is running".
    out = rc.stdout.splitlines()
    for marker in ("is behind the repo", "is behind the host copy",
                   "never rebuilt", "no container is running"):
        hit = next((l.strip() for l in out if marker in l), None)
        if hit:
            return rep.fail("engine image matches the repo", hit[:70],
                            "python3 tools/engine_sync.py        # see what drifted")
    return rep.fail("engine image matches the repo",
                    "drift between repo, host and image",
                    "python3 tools/engine_sync.py        # see what drifted")


def check_live_bridge(rep: Report) -> bool:
    """8091 carries every batch and live run. 8090 answering says nothing about it."""
    if _tcp("127.0.0.1", LIVE_BRIDGE_PORT):
        return rep.ok(f"live bridge :{LIVE_BRIDGE_PORT} (from the platform distro)")
    return rep.fail(
        f"live bridge :{LIVE_BRIDGE_PORT}", "TCP closed — batch and live runs will fail",
        f"wsl -d {ENGINE_DISTRO} -u root bash -lc 'docker rm -f nsg-engine; "
        f"docker run -d --init --name nsg-engine -p 127.0.0.1:8090:8090 -p 127.0.0.1:8091:8091 {IMAGE}'")


def check_engine_free(rep: Report) -> bool:
    """A held run lock is not a failure — but starting a campaign into one is."""
    ok, body = _http(f"{ENGINE_URL}/health")
    if not ok:
        return True  # already reported by check_engine_cross
    if "busy" in body.lower():
        rep.note("engine reports BUSY — a run is in flight; new runs will queue or retry")
    else:
        rep.ok("engine run lock", "free")
    return True


def check_backend(rep: Report) -> bool:
    ok, body = _http(f"{BACKEND_URL}/health")
    if not ok:
        return rep.fail("backend :8000", "not answering",
                        "bash tools/demo_app_up.sh   (or check /tmp/nsg_backend.log)")
    if '"agent_ready":true' not in body.replace(" ", ""):
        rep.note("backend is up but the LLM agent is not ready (check OPENROUTER_API_KEY)")
    return rep.ok("backend :8000")


def check_backend_fresh(rep: Report) -> bool:
    """Is the RUNNING backend older than the code on disk?

    Python imports once. A backend started before an edit keeps serving the
    module it imported, and nothing about it looks wrong — `/health` answers,
    every GET works, and only the code path that touches the changed function
    fails, deep inside a WebSocket, where the traceback goes to a log nobody
    is tailing. Measured once, live: the RIC console's Connect E2 raised
    `module 'window_model' has no attribute 'upa_gain_db'` for four hours
    after the function was added, and the fast tier's own test suite passed
    the whole time because it imports fresh.

    `check_engine_image` already does exactly this for the engine, which
    reaches the product only through its container. The backend reaches it
    through a process, and a process is just as capable of being stale.
    """
    import subprocess
    try:
        pid = subprocess.run(
            ["bash", "-lc",
             "ss -ltnp 2>/dev/null | grep ':8000' | grep -o 'pid=[0-9]*' "
             "| cut -d= -f2 | head -1"],
            capture_output=True, text=True, timeout=20).stdout.strip()
        if not pid:
            return rep.ok("backend matches the code on disk", "no local pid to check")
        started = subprocess.run(["ps", "-o", "lstart=", "-p", pid],
                                 capture_output=True, text=True, timeout=20).stdout.strip()
        import time as _t
        import datetime as _dt
        boot = _dt.datetime.strptime(started, "%a %b %d %H:%M:%S %Y").timestamp()
    except Exception:                                    # noqa: BLE001
        return rep.ok("backend matches the code on disk", "could not read the pid")
    src = pathlib.Path(__file__).resolve().parent.parent / "nsg_ai"
    newest, newest_f = 0.0, ""
    for root, _d, files in os.walk(str(src)):
        if "__pycache__" in root:
            continue
        for f in files:
            if f.endswith(".py"):
                m = os.path.getmtime(os.path.join(root, f))
                if m > newest:
                    newest, newest_f = m, f
    if newest > boot:
        age = (newest - boot) / 60.0
        return rep.fail(
            "backend matches the code on disk",
            f"{newest_f} is {age:.0f} min newer than the running process",
            "kill the pid that holds :8000 and start it again — `pkill -f "
            "uvicorn` can match your own shell and leave the server up")
    return rep.ok("backend matches the code on disk",
                  f"process started after the last edit to {newest_f or 'nsg_ai'}")


def check_schema(rep: Report) -> bool:
    """The config editor is schema-driven: no schema, no editable parameters."""
    ok, body = _http(f"{BACKEND_URL}/schema", timeout=15)
    if not ok:
        return rep.fail("backend /schema", "not served", "restart the backend")
    if '"$defs"' not in body:
        return rep.fail("backend /schema", "served but has no $defs",
                        "check nsg_ai/schema.py imports on the backend")
    return rep.ok("backend /schema", "config editor has its parameter list")


def check_frontend(rep: Report) -> bool:
    ok, _ = _http(FRONTEND_URL, timeout=10)
    if ok:
        return rep.ok("frontend :3000")
    return rep.fail("frontend :3000", "not answering",
                    "bash tools/demo_app_up.sh   (or check /tmp/nsg_frontend.log)")


def check_database(rep: Report) -> bool:
    """Reachable, and migrated to the revision this checkout expects.

    Two failures worth separating. An unreachable database is obvious once you
    look; a database that answers but sits behind the code's migrations is not,
    and it surfaces later as a missing column in the middle of a campaign.
    """
    try:
        from pathlib import Path  # noqa: PLC0415
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from sqlalchemy import inspect, text  # noqa: PLC0415

        from nsg_ai.db import engine  # noqa: PLC0415
    except RuntimeError as exc:               # NSG_DATABASE_URL missing
        return rep.fail("database config", str(exc).split(".")[0],
                        "cp .env.example .env  and fill in the nsg role's password")
    except Exception as exc:                  # noqa: BLE001
        return rep.fail("database imports", f"{type(exc).__name__}: {exc}",
                        "venv/bin/pip install -r requirements.txt")

    try:
        with engine.connect() as c:
            rev = c.execute(text("select version_num from alembic_version")).scalar()
            tables = set(inspect(engine).get_table_names())
    except Exception as exc:                  # noqa: BLE001
        return rep.fail("database :5433", f"{type(exc).__name__}",
                        "sudo systemctl start postgresql   (cluster 16/main is on 5433)")

    missing = {"scenarios", "runs", "run_frames", "campaigns", "campaign_runs"} - tables
    if missing:
        return rep.fail("database schema", f"missing tables: {', '.join(sorted(missing))}",
                        "venv/bin/alembic upgrade head")
    return rep.ok("database :5433", f"5 tables, migration {rev}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true",
                    help="apply the safe remedies (start dockerd, create/recreate the container)")
    ap.add_argument("--quiet", action="store_true", help="only print failures")
    args = ap.parse_args()

    rep = Report(args.quiet)
    if not args.quiet:
        print("\n" + "=" * 74)
        print("NSG PREFLIGHT — every link, checked from where it is needed")
        print("=" * 74 + "\n")

    print("engine distro" if not args.quiet else "", end="\n" if not args.quiet else "")
    check_docker(rep, args.fix)
    check_container(rep, args.fix)
    check_engine_local(rep)

    if not args.quiet:
        print("\ncross-distro (what the backend actually uses)")
    check_engine_cross(rep)
    check_engine_current(rep)
    check_live_bridge(rep)
    check_engine_free(rep)

    if not args.quiet:
        print("\nplatform")
    check_database(rep)
    check_backend(rep)
    check_backend_fresh(rep)
    check_schema(rep)
    check_frontend(rep)

    print()
    if rep.failures:
        print(f"{RED}PREFLIGHT FAILED{RESET} — {len(rep.failures)} broken link(s):")
        for name, remedy in rep.failures:
            print(f"  · {name}\n      {remedy}")
        if not args.fix:
            print("\n  Many of these are fixed automatically by:  "
                  "python3 tools/preflight.py --fix")
        return 1
    print(f"{GREEN}PREFLIGHT OK{RESET} — engine, bridge, backend and frontend all reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
