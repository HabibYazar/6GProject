#!/usr/bin/env python3
"""
Docker security and stability verification for the NSG AI platform.

Run: python tools/verify_docker_security.py
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys

INTERNAL_ONLY_PORTS = {"8090", "8091"}
LOCALHOST_ONLY_PREFIXES = ("nsg-backend", "nsg-frontend", "nsg-db")
MUST_BE_HEALTHY_PREFIXES = ("nsg-backend", "nsg-db")

def sh(*args: str) -> str:
    out = subprocess.run(["docker", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed:\n{out.stderr.strip()}")
    return out.stdout

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict-root", action="store_true", help="Fail when image runs as root.")
    args = ap.parse_args()

    try:
        names = [n for n in sh("ps", "--format", "{{.Names}}").splitlines() if n.strip()]
    except Exception as e:
        print(f"Error: {e}")
        return 1

    if not names:
        print("No running containers.")
        return 1

    failures = []

    for name in names:
        print(f"\n=== {name} ===")
        spec = json.loads(sh("inspect", name))[0]
        state = spec["State"]
        host_cfg = spec["HostConfig"]
        img_user = (spec["Config"].get("User") or "").strip()

        if not state.get("Running"):
            failures.append(f"{name}: not running")
            print(f"  FAIL   not running")
            continue

        # 1. Restart Policy
        policy = host_cfg.get("RestartPolicy", {}).get("Name", "no") or "no"
        if policy == "no":
            failures.append(f"{name}: restart policy is 'no'")
            print("  FAIL   restart policy: no")
        else:
            print(f"  ok     restart policy: {policy}")

        # 2. Port Bindings
        bindings = host_cfg.get("PortBindings") or {}
        is_localhost_required = any(name.startswith(p) for p in LOCALHOST_ONLY_PREFIXES)
        for cport, binds in bindings.items():
            cport_num = cport.split("/")[0]
            if cport_num in INTERNAL_ONLY_PORTS:
                for b in binds or []:
                    ip = (b.get("HostIp") or "").strip() or "0.0.0.0"
                    port = b.get("HostPort") or "?"
                    failures.append(f"{name}: {cport_num} published to {ip}:{port}, must be expose-only")
                    print(f"  FAIL   {cport_num} published ({ip}:{port})")
                continue
            for b in binds or []:
                ip = (b.get("HostIp") or "").strip()
                port = b.get("HostPort") or "?"
                shown_ip = ip or "0.0.0.0"
                if is_localhost_required and ip in ("127.0.0.1", "::1"):
                    print(f"  ok     {cport_num} -> {ip}:{port}")
                elif is_localhost_required and ip in ("", "0.0.0.0", "::"):
                    failures.append(f"{name}: {cport_num} bound to {shown_ip}, must be 127.0.0.1")
                    print(f"  FAIL   {cport_num} -> {shown_ip}:{port} (world-exposed)")
                else:
                    print(f"  --     {cport_num} -> {shown_ip}:{port}")

        # 3. Healthcheck
        health = (state.get("Health") or {}).get("Status")
        if any(name.startswith(p) for p in MUST_BE_HEALTHY_PREFIXES):
            if health == "healthy":
                print("  ok     health: healthy")
            else:
                failures.append(f"{name}: health status {health!r}")
                print(f"  FAIL   health: {health!r}")

        # 4. Image User (Root check)
        if img_user:
            print(f"  --     image USER: {img_user}")
        else:
            print("  --     image USER: (root by default)")
            if args.strict_root:
                failures.append(f"{name}: runs as root")
                print("  FAIL   runs as root (strict-root)")

    print()
    if failures:
        print(f"FAIL - {len(failures)} issue(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - Docker security and stability checks passed.")
    return 0

if __name__ == "__main__":
    sys.exit(main())