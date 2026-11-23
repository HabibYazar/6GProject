#!/usr/bin/env python3
"""Security probe — attack the product the way a user can reach it.

Deliberately through the BACKEND (:8000), not the engine directly. The engine
API trusts its caller by design; the question that matters is whether anything a
user can send through the product survives validation and reaches somewhere it
should not.

The specific risk this codebase has, by construction: config values become
environment variables (`env[dst] = str(params[src])`, no escaping) and
run_scenario.sh interpolates many of them UNQUOTED into SCN_ARGS, which is then
executed as `runuser -u builder -- bash -lc "... $SCN_ARGS"`. A value carrying
`;` or `$(...)` in an unquoted position is executed. Values written to files with
`printf '%s' "$VAR" > f` are safe — quoted, and they become content, not code.

So this does not argue from the source. It sends the payloads and then looks in
the engine container for the marker file they would create.

    python3 tools/security_probe.py
"""
from __future__ import annotations

import json
import subprocess
import sys

import pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
from _backend_auth import auth_headers  # noqa: E402
import time
import urllib.error
import urllib.request

API = "http://localhost:8000"
WSL = "/mnt/c/Windows/System32/wsl.exe"
DISTRO = "Ubuntu-2004-nsoran"
CONTAINER = "nsg-engine"
MARKER = "/tmp/nsg_probe_pwned"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
FINDINGS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {GREEN + 'PASS' + RESET if ok else RED + 'FAIL' + RESET}  {name}"
          + (f"  {DIM}{detail}{RESET}" if detail else ""))
    if not ok:
        FINDINGS.append(name)


def wsl(cmd: str, timeout: int = 120) -> str:
    return subprocess.run([WSL, "-d", DISTRO, "-u", "root", "-e", "bash", "-lc", "cd /; " + cmd],
                          capture_output=True, text=True, timeout=timeout).stdout


def post(path: str, body: dict, timeout: int = 120):
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **auth_headers(API)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)[:80]}


def markers() -> list[str]:
    out = wsl(f"docker exec {CONTAINER} sh -c 'ls -1 {MARKER}* 2>/dev/null'")
    return [l.strip() for l in out.splitlines() if l.strip()]


def base_config() -> dict:
    sys.path.insert(0, "/home/chancelor/nsg_ai_platform/experiments/conflict")
    from scenario import build as build_scenario  # noqa: PLC0415
    return build_scenario(seed=1)


# ─────────────────────────────────────────────────────────────────────────────
def injection(cfg: dict) -> None:
    """Can a config value execute a command in the engine container?"""
    print(f"\n{YELLOW}[1] COMMAND INJECTION — config value -> env -> shell{RESET}")
    wsl(f"docker exec {CONTAINER} sh -c 'rm -f {MARKER}*'")
    before = markers()

    payloads = [
        f"; touch {MARKER}_semicolon ;",
        f"$(touch {MARKER}_subshell)",
        f"`touch {MARKER}_backtick`",
        f"| touch {MARKER}_pipe",
        f"\n touch {MARKER}_newline \n",
        f"&& touch {MARKER}_and",
    ]

    # Every free-text field the schema exposes, carrying every payload.
    poisoned = json.loads(json.dumps(cfg))
    for i, p in enumerate(payloads):
        poisoned["name"] = f"probe{i}{p}"
        poisoned["description"] = p
        status, body = post("/config/normalize", poisoned)
        if status not in (200, 422):
            check(f"normalize survives payload {i}", False, f"status {status}")

    # Then actually RUN one, because normalize never reaches the engine.
    poisoned = json.loads(json.dumps(cfg))
    poisoned["name"] = f"probe-run{payloads[0]}"
    poisoned["description"] = payloads[1]
    status, body = post("/sim/run", {"config": poisoned, "frames": 6}, timeout=200)
    run_id = (body or {}).get("run_id") or (body or {}).get("id")
    if run_id:
        for _ in range(40):
            time.sleep(6)
            try:
                with urllib.request.urlopen(f"{API}/sim/run/{run_id}", timeout=30) as r:
                    st = json.loads(r.read().decode()).get("status")
            except Exception:
                break
            if st not in ("running", "queued", None):
                break
    new = [m for m in markers() if m not in before]
    check("no config value executed a command in the engine", not new,
          f"created: {new}" if new else "no marker files")


def limits(cfg: dict) -> None:
    """Does the product bound what a caller can ask it to do?"""
    print(f"\n{YELLOW}[2] RESOURCE LIMITS — what a caller can demand{RESET}")

    huge = json.loads(json.dumps(cfg))
    huge["name"] = "x" * 200_000
    status, _ = post("/config/normalize", huge)
    check("a 200 KB name is rejected or handled", status in (200, 413, 422),
          f"status {status}")

    deep: dict = {"a": {}}
    node = deep["a"]
    for _ in range(400):
        node["a"] = {}
        node = node["a"]
    status, _ = post("/config/normalize", deep)
    check("a 400-deep nested object does not crash the API", status in (400, 422, 500),
          f"status {status}")

    over = json.loads(json.dumps(cfg))
    status, body = post("/campaigns", {"name": "probe", "items": [
        {"label": "x", "config": over, "frames": 10, "repeats": 999}], "thresholds": []})
    check("a campaign cannot request unbounded repeats", status == 422, f"status {status}")


def exposure() -> None:
    """What is reachable, and by whom?"""
    print(f"\n{YELLOW}[3] EXPOSURE — what answers without credentials{RESET}")

    # Report what the deployment actually does rather than a fixed sentence: this
    # said "requires no auth" long after enforcement was switched on.
    st, me = post("/auth/me", {})
    if st != 200:
        with urllib.request.urlopen(API + "/auth/me", timeout=15) as r:
            me = json.loads(r.read() or b"{}")
    enforced = bool(me.get("enforced"))
    check("authentication state is deliberate, not accidental", True,
          "ENFORCED — guarded routes need a token" if enforced else
          "NOT enforced: fine for a single-tenant dev box, a finding the moment "
          "the platform is shared")
    if not enforced:
        unguarded, _ = post("/scenarios", {})
        check("with auth off, data routes are open by design", True,
              f"/scenarios answers {unguarded} anonymously")

    # The LOCAL address is ss's 4th column; the 5th is the peer, which is always
    # `0.0.0.0:*` ("accept from anywhere") even for a loopback-bound socket. An
    # earlier version matched 0.0.0.0 anywhere in the line and flagged every
    # loopback listener as public — the bind is safe, the check was wrong.
    listen = wsl("ss -lntH 2>/dev/null | grep -E ':(8090|8091)'")
    public = []
    for line in listen.splitlines():
        cols = line.split()
        if len(cols) >= 4:
            local = cols[3]
            if local.startswith("0.0.0.0:") or local.startswith("*:") or local.startswith("[::]:"):
                public.append(line)
    check("engine ports are not bound to all interfaces",
          not public,
          f"{len(public)} listener(s) on 0.0.0.0 — the engine API trusts its caller "
          f"and accepts extra_args, an explicit shell pass-through" if public else "")

    status, body = post("/sim/run", {"config": {"name": "x"}, "frames": 4})
    check("a malformed run request is refused, not crashed", status in (400, 422),
          f"status {status}")


def main() -> int:
    print("=" * 74)
    print("SECURITY PROBE — through the product, not around it")
    print("=" * 74)
    cfg = base_config()
    injection(cfg)
    limits(cfg)
    exposure()
    print("\n" + "=" * 74)
    if FINDINGS:
        print(f"{RED}{len(FINDINGS)} FINDING(S){RESET}: " + "; ".join(FINDINGS))
        return 1
    print(f"{GREEN}NO FINDINGS{RESET} — nothing a user can send got out of its lane.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
