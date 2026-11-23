#!/usr/bin/env python3
"""Is the engine the product runs the engine we wrote?

Engine code passes through THREE copies, and every hop has silently gone stale:

    repo engines/nsoran/  ->  host /root/nsoran/  ->  the running container
       (what we edit)         (what build-slim         (what actually
                               packages)                 answers :8090/:8091)

Both gaps have cost us a session. The image once drifted two days behind the
source, so engine fixes simply were not in the product. And on 2026-09-02 the
path-parity test read the HOST copy, went green on a freshly patched file, and
would have certified a fix while the running image still had the old code.

There is no automated sync, because the two live in different WSL distros. This
is that sync, and it verifies by CONTENT: `cp` reporting success says nothing
about whether the bytes arrived, which is a mistake this project has made four
times in other forms.

    python3 tools/engine_sync.py            # report drift across all three
    python3 tools/engine_sync.py --push     # repo -> host, verified by md5
    python3 tools/engine_sync.py --check    # exit 1 on any drift (CI/preflight)
"""
from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

WSL = "/mnt/c/Windows/System32/wsl.exe"
ENGINE_DISTRO = "Ubuntu-2004-nsoran"
REPO = pathlib.Path(__file__).resolve().parents[1] / "engines" / "nsoran"
HOST = "/root/nsoran"

# What build-slim.sh copies into the image: these exist in all three places.
SHIPPED = ["run_scenario.sh", "engine_service.py",
           "run_live_session.sh", "live_bridge_server.py"]
# Build inputs: they shape the image but never live inside it.
BUILD_ONLY = ["build-slim.sh", "Dockerfile", "Dockerfile.base"]

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def wsl(cmd: str, timeout: int = 120) -> str:
    return subprocess.run([WSL, "-d", ENGINE_DISTRO, "-u", "root", "-e", "bash", "-lc", cmd],
                          capture_output=True, text=True, timeout=timeout).stdout


def container() -> str:
    return wsl("docker ps --format '{{.ID}} {{.Image}}' 2>/dev/null "
               "| grep nsg-engine | head -1 | cut -d' ' -f1").strip()


def repo_md5(name: str) -> str | None:
    f = REPO / name
    return hashlib.md5(f.read_bytes()).hexdigest() if f.is_file() else None


def host_md5(names: list[str]) -> dict[str, str]:
    out = wsl(f"cd {HOST} && md5sum " + " ".join(names) + " 2>/dev/null")
    return {p[1]: p[0] for p in (l.split() for l in out.splitlines()) if len(p) == 2}


def container_md5(cid: str, names: list[str]) -> dict[str, str]:
    out = wsl(f"docker exec {cid} sh -c 'cd {HOST} && md5sum " + " ".join(names) + " 2>/dev/null'")
    return {p[1].lstrip("./"): p[0] for p in (l.split() for l in out.splitlines()) if len(p) == 2}


def push() -> list[str]:
    """Copy repo -> host, then VERIFY. Returns the names that failed to land."""
    names = SHIPPED + BUILD_ONLY
    for name in names:
        f = REPO / name
        if not f.is_file():
            continue
        subprocess.run([WSL, "-d", ENGINE_DISTRO, "-u", "root", "-e", "bash", "-lc",
                        f"cat > {HOST}/{name}"], input=f.read_bytes(), timeout=120)
    after = host_md5(names)
    return [n for n in names if repo_md5(n) and after.get(n) != repo_md5(n)]


# The ns-3 tree the image is built from. Scripts are one kind of engine work;
# C++ is the other, and it reaches the product through compiled libraries that
# this tool was blind to.
NS3 = "/home/builder/ns-O-RAN-flexric/mmwave-LENA-oran"


def cxx_staleness() -> tuple[list[str], list[str]]:
    """Modified C++ sources newer than the library they belong to, and the
    state of the profile the product does NOT load.

    Two failures this catches, both silent and both already seen here:

      · a source is edited and the library is never rebuilt, so the patch
        exists in the tree and not in the product
      · the OTHER build profile drifts. The product loads `-default`; the
        `-optimized` libraries sat five days behind with no bursty
        interference patch and no beamforming cache in them. Nothing breaks
        until someone flips the profile, and then the engine quietly becomes
        the one from last week.
    """
    script = (
        f'cd {NS3} 2>/dev/null || exit 0; '
        'git status --porcelain -- src/ scratch/ 2>/dev/null | awk \'$1=="M"{print $2}\' | '
        'while read -r f; do '
        '  case "$f" in src/*) m=$(echo "$f"|cut -d/ -f2); lib=build/lib/libns3.42-$m-default.so;; '
        '    scratch/*) lib=build/scratch/ns3.42-scenario-zero-with_parallel_loging-default;; '
        '    *) continue;; esac; '
        '  [ -f "$lib" ] || continue; '
        '  [ "$(stat -c %Y "$f")" -gt "$(stat -c %Y "$lib")" ] && echo "STALE $f"; '
        'done; '
        'for m in mmwave spectrum; do '
        '  d=build/lib/libns3.42-$m-default.so; o=build/lib/libns3.42-$m-optimized.so; '
        '  [ -f "$d" ] && [ -f "$o" ] && '
        '    [ "$(stat -c %Y "$d")" -gt "$(( $(stat -c %Y "$o") + 86400 ))" ] && '
        '    echo "PROFILE $m optimized is $(( ($(stat -c %Y "$d") - $(stat -c %Y "$o")) / 86400 )) day(s) behind default"; '
        'done'
    )
    out = wsl(script, timeout=180)
    stale = [l.split(" ", 1)[1] for l in out.splitlines() if l.startswith("STALE ")]
    prof = [l.split(" ", 1)[1] for l in out.splitlines() if l.startswith("PROFILE ")]
    return stale, prof


def main() -> int:
    do_push = "--push" in sys.argv
    do_check = "--check" in sys.argv

    if do_push:
        print(f"pushing {len(SHIPPED) + len(BUILD_ONLY)} file(s) to {ENGINE_DISTRO}:{HOST} …")
        failed = push()
        if failed:
            print(f"{RED}FAILED to land: {', '.join(failed)}{RESET}")
            print("The copy command returned success; the content did not change.")
            return 2
        print(f"{GREEN}all files landed and verified by md5{RESET}\n")

    cid = container()
    names = SHIPPED + BUILD_ONLY
    r = {n: repo_md5(n) for n in names}
    h = host_md5(names)
    c = container_md5(cid, SHIPPED) if cid else {}

    print(f"container: {cid[:12] if cid else RED + 'NONE RUNNING' + RESET}")
    print(f"\n  {'file':<24} {'repo→host':<12} {'host→image':<12}")
    print("  " + "-" * 50)

    stale_host, stale_image = [], []
    for n in names:
        if r[n] is None:
            continue
        a = "same" if h.get(n) == r[n] else "DRIFT"
        if a == "DRIFT":
            stale_host.append(n)
        if n in SHIPPED and cid:
            b = "same" if c.get(n) == h.get(n) else "DRIFT"
            if b == "DRIFT":
                stale_image.append(n)
        else:
            b = f"{DIM}build input{RESET}" if n in BUILD_ONLY else "?"
        col = RED if "DRIFT" in (a, b) else GREEN
        print(f"  {n:<24} {col}{a:<12}{RESET} {b}")

    print()
    if stale_host:
        print(f"{YELLOW}the host copy is behind the repo:{RESET} {', '.join(stale_host)}")
        print("  fix: python3 tools/engine_sync.py --push")
    if stale_image:
        print(f"{YELLOW}the running image is behind the host copy:{RESET} {', '.join(stale_image)}")
        print("  fix: build and run a new tag, e.g.")
        print(f"    wsl -d {ENGINE_DISTRO} -u root -e bash -lc "
              "'bash /root/nsoran/build-slim.sh nsg-engine:<next>'")
        print("  Engine work reaches the product ONLY through the image.")
    if not cid:
        print(f"{YELLOW}no container is running, so the image cannot be compared.{RESET}")

    # The C++ half. Scripts can be in sync while the engine's own code is not.
    stale_cxx, profiles = cxx_staleness()
    if stale_cxx:
        print(f"\n{RED}C++ sources edited but never rebuilt:{RESET}")
        for f in stale_cxx:
            print(f"  · {f}")
        print(f"  fix: wsl -d {ENGINE_DISTRO} -u root -e bash -lc "
              f"'runuser -u builder -- bash -lc \"cd {NS3} && ./ns3 build\"'")
    else:
        print(f"\n{GREEN}every modified C++ source is built into its library.{RESET}")
    for line in profiles:
        print(f"{YELLOW}build profile:{RESET} {line}")
        print("  Harmless while the product loads -default, and a trap the day "
              "it does not: the other profile is a different engine.")

    if stale_cxx:
        return 1 if do_check else 0
    if not stale_host and not stale_image and cid:
        print(f"{GREEN}IN SYNC{RESET} — repo, host and the running image carry the same engine.")
        return 0
    return 1 if do_check else 0


if __name__ == "__main__":
    raise SystemExit(main())
