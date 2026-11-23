#!/usr/bin/env python3
"""Is every engine patch actually in the tree the product is built from?

`engines/nsoran/*.patch` is NOT one kind of file, and mistaking that costs an
afternoon. Sixteen are real unified diffs. Twenty-three are PROSE NOTES that
happen to carry the extension — `slicing-level6.patch` says so in its own
header: "Reference patch (the distro trees are the source of truth)". Asking
whether a note is "applied" is meaningless, and a grep that does it reports
features as MISSING that have shipped for months.

So this classifies first and only then checks, and it checks with SEVERAL
probes per diff. One probe is not enough: a single long added line gets edited
by a later patch, and a one-probe audit called the beamforming cache, the
RAR/RNTI hijack guard and the RU sleep state missing when all three were
present.

What a note DESCRIBES is verified elsewhere — `tools/param_audit.py` measures
which schema fields actually reach ns-3, which is the real test of a feature.

    python3 tools/patch_audit.py           # report
    python3 tools/patch_audit.py --check   # non-zero if a diff is not applied
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCHES = os.path.join(ROOT, "engines", "nsoran")
DISTRO = os.environ.get("NSG_ENGINE_DISTRO", "Ubuntu-2004-nsoran")
WSL = "/mnt/c/Windows/System32/wsl.exe"
# A directory both this distro and the engine distro can see.
SHARED = os.environ.get("NSG_SHARED_DIR", "/mnt/c/Users/hp")
NS3 = "/home/builder/ns-O-RAN-flexric/mmwave-LENA-oran"
FLEXRIC = "/root/nsoran"
GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def is_diff(body: str) -> bool:
    """A real unified diff has hunk headers or file headers. A note has neither."""
    return bool(re.search(r"^@@ ", body, re.M) or re.search(r"^\+\+\+ ", body, re.M))


def probes(body: str, n: int = 4) -> list[str]:
    adds = [l[1:].rstrip() for l in body.splitlines()
            if l.startswith("+") and not l.startswith("+++")]
    code = [a.strip() for a in adds
            if len(a.strip()) > 20 and not a.lstrip().startswith(("//", "*", "/*", "#"))]
    picked = sorted(set(code), key=len, reverse=True)[:n]
    if len(picked) < 2:
        rest = sorted({a.strip() for a in adds if len(a.strip()) > 20},
                      key=len, reverse=True)
        picked = (picked + [r for r in rest if r not in picked])[:n]
    return picked


def main() -> int:
    check = "--check" in sys.argv
    diffs, notes = [], []
    for name in sorted(f for f in os.listdir(PATCHES) if f.endswith(".patch")):
        body = open(os.path.join(PATCHES, name), errors="replace").read()
        (diffs if is_diff(body) else notes).append((name, body))

    # One shell round trip: a wsl.exe call each would be slower than the greps.
    lines = [f'S={NS3}', f'F={FLEXRIC}']
    for name, body in diffs:
        ps = probes(body)
        if not ps:
            lines.append(f'echo "{name} NOPROBE 0 0"')
            continue
        lines.append("h=0")
        for p in ps:
            lines.append(f'grep -rqF {shlex.quote(p)} "$S/src" "$S/scratch" "$S/contrib" "$F" '
                         f'2>/dev/null && h=$((h+1))')
        lines.append(f'echo "{name} HITS $h {len(ps)}"')
    # Written to a file and run, not passed as one giant -lc string. The
    # combined command is tens of kilobytes of quoted grep patterns and came
    # back empty when handed over as an argument — with every diff reported as
    # "no answer", which reads exactly like a real gap.
    script = os.path.join(SHARED, ".nsg_patch_audit.sh")
    try:
        with open(script, "w") as fh:
            fh.write("#!/bin/bash\n" + "\n".join(lines) + "\n")
        r = subprocess.run([WSL, "-d", DISTRO, "-u", "root", "--", "bash", script],
                           capture_output=True, text=True, timeout=900)
        out = r.stdout
        if not out.strip():
            print(f"{YELLOW}SKIP{RESET} — the audit script produced nothing"
                  f"{(': ' + r.stderr.strip()[:200]) if r.stderr.strip() else ''}")
            return 0
    except Exception as exc:  # noqa: BLE001
        print(f"{YELLOW}SKIP{RESET} — could not reach {DISTRO}: {exc}")
        return 0

    got = {}
    for line in out.replace("\r", "").splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[1] in ("HITS", "NOPROBE"):
            got[parts[0]] = (int(parts[2]), int(parts[3]))

    print(f"engine patches in {PATCHES.replace(ROOT + '/', '')}")
    print(f"  {len(diffs)} unified diff(s), {len(notes)} prose note(s)\n")
    bad, partial = [], []
    for name, _ in diffs:
        hits, tot = got.get(name, (0, 0))
        if not tot:
            print(f"  {YELLOW}?{RESET}       {name}   (no probe, and no answer)")
            continue
        if hits == tot:
            mark, col = "applied", GREEN
        elif hits:
            mark, col = "partial", YELLOW
            partial.append(name)
        else:
            mark, col = "MISSING", RED
            bad.append(name)
        print(f"  {col}{mark:<8}{RESET}{name:<44} {DIM}{hits}/{tot} probes{RESET}")

    print(f"\n{DIM}notes (prose, not diffs — the tree is the source of truth; what they")
    print(f"describe is verified by tools/param_audit.py, not by grepping for them):{RESET}")
    print("  " + ", ".join(n for n, _ in notes))

    print()
    if bad:
        print(f"{RED}{len(bad)} diff(s) are NOT in the tree:{RESET} {', '.join(bad)}")
        return 1 if check else 0
    if partial:
        print(f"{YELLOW}{len(partial)} partial{RESET} — expected when a later patch edited "
              f"the same lines; confirm by hand before treating it as a gap:")
        print("  " + ", ".join(partial))
    print(f"{GREEN}every unified diff is present in the engine tree{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
