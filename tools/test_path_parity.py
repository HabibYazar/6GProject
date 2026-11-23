#!/usr/bin/env python3
"""
BATCH vs LIVE path parity — one payload, two consumers.

The platform builds ONE engine payload (`nsoran_params`) and two different
programs consume it: `engine_service.py` + `run_scenario.sh` for batch validation,
and `live_bridge_server.py` + `run_live_session.sh` for live sessions AND every
`/sim/run` — which is every campaign run.

A key mapped by one and not the other is a feature that silently does nothing on
that path. Measured 2026-07-30: **14 keys** reached batch and were dropped by live,
including `gnb_faults` (so no campaign run could stage an anomaly at all), the two
handover-robustness timers added the day before, and the entire RRC
measurement/timer block. Every one of them passed `param_audit`, because that
audit measures the PAYLOAD — not which consumer honours it. This test closes that
blind spot.

Run: python3 tools/test_path_parity.py
"""
from __future__ import annotations

import re
import subprocess
import sys

ENGINE_DISTRO = "Ubuntu-2004-nsoran"
WSL = "/mnt/c/Windows/System32/wsl.exe"

# Differences that are intentional, with the reason. Anything NOT here is a gap.
EXPECTED_ONLY_BATCH = {
    "extra_args": "an escape hatch for ad-hoc ns-3 flags on batch runs only",
}
EXPECTED_ONLY_LIVE = {
    "lock_wait_s": "the caller's patience for the engine lock; the batch script "
                   "takes it from its own default",
}
# Env vars the batch SCRIPT consumes that the live one legitimately does not.
EXPECTED_SCRIPT_ONLY_BATCH = {
    "EXTRA_ARGS": "see extra_args",
    "NS3_SIM": "batch runs a bounded simTime; a live session runs simTime=100000 "
               "and is stopped by the operator",
    "SIMTIME": "same — the live session's horizon is the bridge window, not a "
               "scenario argument",
}

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"\n        {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def _wsl(cmd: str, timeout: int = 60) -> str:
    return subprocess.run([WSL, "-d", ENGINE_DISTRO, "-u", "root", "-e", "bash", "-lc", cmd],
                          capture_output=True, text=True, timeout=timeout).stdout


def _running_container() -> str:
    """The container actually serving :8090/:8091, if one is up."""
    # Match on the image NAME, not `--filter ancestor=`, which wants the exact
    # tag and silently matches nothing across a version bump.
    return _wsl("docker ps --format '{{.ID}} {{.Image}}' 2>/dev/null "
                "| grep nsg-engine | head -1 | cut -d' ' -f1").strip()


# WHERE we read matters. /root/nsoran on the host is the BUILD INPUT; the
# container built from it is what answers requests. They drift: on 2026-09-02
# this test went green against a freshly-patched host copy while the running
# image still had the old files, i.e. it would have certified a fix that had
# not shipped. Read the container when one is up, and say which was read.
SOURCE = _running_container()
ORIGIN = f"running container {SOURCE[:12]}" if SOURCE else "host /root/nsoran (NO CONTAINER UP)"


def read(path: str) -> str:
    cmd = f"docker exec {SOURCE} cat {path}" if SOURCE else f"cat {path}"
    out = _wsl(cmd)
    if not out:
        sys.exit(f"could not read {path} from {ORIGIN} — is the distro up?")
    return out


def mapped_keys(src: str) -> set[str]:
    """Payload keys a consumer maps to an NSG_* env var."""
    return set(re.findall(r'"([a-z0-9_]+)":\s*"NSG_[A-Z0-9_]+"', src))


def env_used(src: str) -> set[str]:
    return set(re.findall(r"NSG_([A-Z0-9_]+)", src))


def main() -> int:
    batch_api = read("/root/nsoran/engine_service.py")
    bridge = read("/root/nsoran/live_bridge_server.py")
    batch_sh = read("/root/nsoran/run_scenario.sh")
    live_sh = read("/root/nsoran/run_live_session.sh")

    b, l = mapped_keys(batch_api), mapped_keys(bridge)
    print(f"\nread from: {ORIGIN}")
    print(f"payload keys mapped: batch {len(b)} · live {len(l)}\n")

    only_batch = sorted(b - l - set(EXPECTED_ONLY_BATCH))
    check("no payload key is mapped by the batch API and dropped by the live bridge",
          not only_batch,
          f"dropped on the live path: {only_batch}" if only_batch else "")

    only_live = sorted(l - b - set(EXPECTED_ONLY_LIVE))
    check("no payload key is mapped by the live bridge and dropped by the batch API",
          not only_live,
          f"dropped on the batch path: {only_live}" if only_live else "")

    b_env, l_env = env_used(batch_sh), env_used(live_sh)
    print(f"\nenv vars consumed: run_scenario.sh {len(b_env)} · "
          f"run_live_session.sh {len(l_env)}\n")

    script_gap = sorted(b_env - l_env - set(EXPECTED_SCRIPT_ONLY_BATCH))
    check("no env var is consumed by the batch script and ignored by the live one",
          not script_gap,
          f"ignored by run_live_session.sh: {['NSG_' + k for k in script_gap]}"
          if script_gap else "")

    # A mapped key whose env var no script reads is dead in BOTH paths.
    dead = sorted({re.search(r'"(NSG_[A-Z0-9_]+)"', m).group(1)[4:]
                   for m in re.findall(r'"[a-z0-9_]+":\s*"NSG_[A-Z0-9_]+"', batch_api)}
                  - b_env - l_env)
    check("every mapped env var is read by at least one run script",
          not dead, f"mapped but never read: {['NSG_' + k for k in dead]}" if dead else "")

    print("\n" + ("PATH PARITY OK" if not FAILS
                  else f"{len(FAILS)} PARITY GAP(S): {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
