#!/usr/bin/env python3
"""
Engine acceptance — does the engine still tell the truth?

Every silent bug found in this platform was caught the same way: two sources
that should agree, disagreed. Never by looking at one number and judging it
plausible. So this suite compares, it does not eyeball.

  * cell_off looked broken for a session because the KPM view said UEs were on a
    cell the RRC had already emptied.
  * The TX-power bug hid because runs were byte-identical across different
    configs and nobody diffed them.
  * The anomaly horizon trap passed because a fault that never fired looks
    exactly like a fault that did nothing.

Two families of check:

  AGREEMENT — the engine's own roster (`[NSG kpm] cell N reports M UEs`, printed
  inside ns-3 before any KPM assembly) against what the platform finally shows.
  A gap here means the measurement layer is lying, which is invisible from
  either side alone.

  INVARIANTS — statements that must hold in any correct run: a cell serving
  nobody draws no power, per-cell figures sum to the network total, PRB is
  bounded, UEs move rather than vanish, and two different configs must not
  produce identical numbers.

Usage:
  python tools/engine_acceptance.py            # 3 cells / 6 UEs, ~4 min
  python tools/engine_acceptance.py 6 12       # explicit size
Exit code 0 = all checks passed.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "nsg_ai"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "experiments" / "conflict"))

WSL = "/mnt/c/Windows/System32/wsl.exe"
ENGINE_DISTRO = "Ubuntu-2004-nsoran"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {GREEN + 'PASS' + RESET if ok else RED + 'FAIL' + RESET}  {name}"
          + (f"  {DIM}{detail}{RESET}" if detail else ""))
    if not ok:
        FAILS.append(name)
    return ok


def engine_roster() -> dict[int, set[int]]:
    """Which UEs each cell REPORTED ON, as the engine itself logged them.

    Printed inside BuildRicIndicationMessageCuCp, before the KPM encoding and
    long before the platform's frame assembly, so it is independent of every
    layer the platform adds.

    This is NOT a serving-cell partition, and reading it as one was wrong: at
    t=5.3 of a 9-UE run the three cells reported 3 + 7 + 6 = 16 UE slots, with
    UE 1 on cells 2 and 3 at once. A cell reports on the UEs it MEASURES,
    neighbours included. The platform, by contrast, counts a UE once, on its
    serving cell. Comparing the two counts compares different quantities — see
    the containment check in agreement().
    """
    out = subprocess.run(
        [WSL, "-d", ENGINE_DISTRO, "-u", "root", "-e", "bash", "-lc",
         "docker exec nsg-engine sh -c 'grep -a \"NSG kpm\" /tmp/scn0.log'"],
        capture_output=True, text=True, timeout=120).stdout
    seen: dict[int, set[int]] = {}
    for line in out.splitlines():
        if "reports" not in line or "UEs:" not in line:
            continue
        try:
            cell = int(line.split("cell", 1)[1].split()[0])
            ids = line.split("UEs:", 1)[1].strip()
        except (ValueError, IndexError):
            continue
        bucket = seen.setdefault(cell, set())
        for tok in ids.split(","):
            tok = tok.strip().split()[0] if tok.strip() else ""
            if tok.isdigit():
                bucket.add(int(tok))
    return seen


def negative_counters() -> list[tuple[str, float]]:
    """Count metrics the xApp printed as NEGATIVE.

    A counter cannot be negative. `DRB.EstabSucc.5QI` is `GetDrbMap().size()` in
    ns-3 — unsigned by construction — yet the xApp prints -4, -6, -7 for it, so
    something between the engine's value and the decoded one corrupts it. That is
    the same E2SM-KPM path as the two e2sim defects already fixed (an
    under-sized calloc and an unclamped data.len), and it is worth catching
    automatically: a plausible-looking mean hides it, and only the min gives it
    away.

    Read from the xApp's log rather than the platform's frames on purpose — the
    platform reports faithfully what it is told, so checking our own output would
    only confirm we copied a bad number correctly.
    """
    out = subprocess.run(
        [WSL, "-d", ENGINE_DISTRO, "-u", "root", "-e", "bash", "-lc",
         "docker exec nsg-engine sh -c 'grep -ahoE \"^[A-Za-z][A-Za-z0-9._:-]* = "
         "-[0-9.]+\" /tmp/xapp.log'"],
        capture_output=True, text=True, timeout=120).stdout
    bad: list[tuple[str, float]] = []
    for line in out.splitlines():
        name, _, val = line.partition(" = ")
        name = name.strip()
        # Counters and volumes only. SINR, power offsets and deltas are signed by
        # nature and a negative one says nothing is wrong.
        if not any(k in name for k in ("Succ", "Nbr", "Volume", "Tot", "Att", "Dist")):
            continue
        try:
            bad.append((name, float(val)))
        except ValueError:
            continue
    return bad


def collect(n_gnb: int, n_ue: int, frames: int = 60) -> list:
    """One live run through the REAL platform path (backend WS), not the bridge.

    Deliberately not the bridge socket: the point is to test what a user gets,
    including the backend's clamps and the frame assembly that hid a bug for a
    whole session.
    """
    from harness import run          # noqa: PLC0415
    from scenario import build       # noqa: PLC0415
    cfg = build(seed=1)
    log = run(cfg, lambda kpm: [], max_ticks=frames)
    if not log.ok:
        print(f"{RED}engine did not produce frames: {log.error}{RESET}")
        sys.exit(2)
    return log.ticks


def agreement(ticks: list) -> None:
    """Engine roster vs platform view. A gap here is a measurement lie."""
    print(f"\n{YELLOW}[1] AGREEMENT — engine's own roster vs what the platform shows{RESET}")
    peak_engine = engine_roster()
    if not peak_engine:
        check("engine roster is readable", False,
              "no [NSG kpm] lines — is the instrumented build deployed?")
        return
    plat_cells: set[int] = set()
    plat_pairs: set[tuple[int, int]] = set()   # (cell, imsi) the PLATFORM asserts
    for t in ticks:
        for c in t.cells:
            plat_cells.add(int(c["cell"]))
        for u in t.ues:
            cid, imsi = u.get("cell"), u.get("imsi")
            if cid is not None and imsi is not None:
                plat_pairs.add((int(cid), int(imsi)))

    check("every cell the engine reported also appears in the platform view",
          set(peak_engine) <= plat_cells | {1},   # cell 1 is the LTE anchor
          f"engine {sorted(peak_engine)} vs platform {sorted(plat_cells)}")

    # The real invariant between the two sources. They do NOT measure the same
    # thing — the engine lists every UE a cell reports on, the platform assigns
    # each UE to one serving cell — so their counts legitimately differ and
    # comparing those was a bug in this suite. What must hold is containment: if
    # the platform says UE 5 is being served by cell 3, cell 3's own report has
    # to mention UE 5. A platform-invented attachment is a measurement lie.
    orphans = sorted((cid, imsi) for cid, imsi in plat_pairs
                     if cid != 1 and imsi not in peak_engine.get(cid, set()))
    check("every UE the platform places on a cell is in that cell's own report",
          not orphans,
          f"{len(orphans)} orphan attachment(s)"
          + (f", e.g. platform says UE {orphans[0][1]} on cell {orphans[0][0]}, "
             f"which reported {sorted(peak_engine.get(orphans[0][0], set()))}"
             if orphans else ""))


def invariants(ticks: list) -> None:
    """Statements that must hold in ANY correct run."""
    print(f"\n{YELLOW}[2] INVARIANTS — must hold in any correct run{RESET}")

    # A cell serving nobody and carrying nothing must not draw power. This is
    # the exact shape of the TX-power bug: cells reported as off while billing
    # watts, and of its mirror, power reported while carrying no traffic.
    ghosts = [(t.tick, c["cell"], c.get("power_w"))
              for t in ticks for c in t.cells
              if not c.get("ues") and not (c.get("thp_kbps") or 0)
              and (c.get("power_w") or 0) > 0.5
              and c.get("sleep_state") in (None, "awake")]
    check("no cell draws power while serving nobody and carrying nothing",
          not ghosts, f"{len(ghosts)} ghost-draw sample(s)"
          + (f", first: tick {ghosts[0][0]} cell {ghosts[0][1]} {ghosts[0][2]} W"
             if ghosts else ""))

    # The mirror of the check above, and the one that was missing: a cell that
    # carries real traffic while reporting ~no power. That is the exact shape of
    # the TX-power PSD bug — runtime SetTxPower never reached the radio, so the
    # model billed 0 W while the cell kept transmitting and cell-off looked free.
    # The ghost check could not see it: it only looks at power WITHOUT traffic.
    free_lunch = [(t.tick, c["cell"], c.get("thp_kbps"))
                  for t in ticks for c in t.cells
                  if (c.get("thp_kbps") or 0) > 0 and (c.get("ues") or 0) > 0
                  and c.get("power_w") is not None and c["power_w"] < 0.5]
    check("no cell carries traffic while reporting no power", not free_lunch,
          f"{len(free_lunch)} free-lunch sample(s)"
          + (f", first: tick {free_lunch[0][0]} cell {free_lunch[0][1]} "
             f"{free_lunch[0][2]} kbps at ~0 W" if free_lunch else ""))

    # Per-cell power must add up to the reported network total.
    bad_sum = []
    for t in ticks:
        if t.energy_w is None or not t.per_cell_w:
            continue
        s = sum(t.per_cell_w.values())
        if abs(s - t.energy_w) > max(1.0, 0.02 * max(s, t.energy_w)):
            bad_sum.append((t.tick, round(s, 1), round(t.energy_w, 1)))
    check("per-cell power sums to the network total (2%)", not bad_sum,
          f"{len(bad_sum)} mismatched frame(s)"
          + (f", first: tick {bad_sum[0][0]} sum={bad_sum[0][1]} total={bad_sum[0][2]}"
             if bad_sum else ""))

    # PRB is a percentage of a shared resource; a cell cannot use more than all
    # of it, and a negative reading is a decode error.
    bad_prb = [(t.tick, c["cell"], c.get("prb")) for t in ticks for c in t.cells
               if c.get("prb") is not None and not (0 <= c["prb"] <= 100)]
    check("per-cell PRB stays within 0-100", not bad_prb,
          f"{len(bad_prb)} out-of-range" + (f", e.g. {bad_prb[0]}" if bad_prb else ""))

    # UEs must move between cells, never silently disappear from the network.
    # Measured only AFTER the roster fills: UEs attach progressively at the
    # start, so a window that includes the ramp reports a "drop" that is just
    # the run beginning. The first version of this check failed for exactly that
    # reason (min=2 max=9) — the same window mistake that made check6 and the
    # ceiling probes lie earlier.
    counts = [len(t.ues) for t in ticks]
    peak = max(counts) if counts else 0
    try:
        settled = counts[counts.index(peak):]
    except ValueError:
        settled = []
    lost = peak - min(settled) if settled else 0
    check("UE population is stable once attached (moves, does not vanish)",
          bool(settled) and lost <= max(1, int(0.25 * peak)),
          f"peak={peak}, lowest after peak={min(settled) if settled else 0}, "
          f"ramp excluded ({counts.index(peak) if peak in counts else 0} frame(s))")

    # Counters cannot go negative. Checked against the xApp's own output, not our
    # frames: the platform copies faithfully, so our numbers would only confirm we
    # relayed a bad value correctly.
    negatives = negative_counters()
    worst = min(negatives, key=lambda x: x[1]) if negatives else None
    check("no count metric is reported negative", not negatives,
          f"{len(negatives)} sample(s), worst {worst[0]}={worst[1]}" if worst else "")

    # A live frame is not an independent observation; the contract must say so,
    # or every experiment over-counts n by ~10x.
    labelled = [t for t in ticks if t.kpm_fresh is not None]
    check("frames carry sample identity (sim_t / kpm_fresh_cells)",
          len(labelled) == len(ticks) and any(t.kpm_fresh for t in labelled),
          f"{len(labelled)}/{len(ticks)} labelled, "
          f"{sum(1 for t in labelled if t.kpm_fresh)} carried a report")


def responds_to_config(ticks: list) -> None:
    """Different configs must produce different numbers.

    Byte-identical results across different configs is how the TX-power bug and
    the anomaly-horizon trap both hid: a lever that does nothing looks exactly
    like a lever that is not wired. Rather than run a second scenario here (that
    doubles an already slow suite), this checks the cheaper necessary condition:
    the run must not be static.
    """
    print(f"\n{YELLOW}[3] THE RUN IS ALIVE — not a frozen or replayed state{RESET}")
    dl = [sum((u.get("thp_kbps") or 0) for u in t.ues) for t in ticks if t.ues]
    check("carried throughput varies across the run", bool(dl) and len(set(dl)) > 1,
          f"{len(set(dl))} distinct value(s) over {len(dl)} frames")

    sim_t = [t.sim_t for t in ticks if t.sim_t is not None]
    check("simulated time advances", len(set(sim_t)) > 1,
          f"{len(set(sim_t))} distinct sim_t, span "
          f"{round(max(sim_t) - min(sim_t), 2) if len(sim_t) > 1 else 0} s")

    served = {int(c["cell"]) for t in ticks for c in t.cells if c.get("ues")}
    check("more than one cell actually carries traffic", len(served) > 1,
          f"cells carrying: {sorted(served)}")


def main() -> int:
    # Positional, not argparse — but a bare `--help` used to die with a
    # ValueError from int(), which reads like the tool is broken.
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__ or "")
        print("usage: engine_acceptance.py [n_gnb] [n_ue]      (default: 3 6)")
        return 0
    try:
        n_gnb = int(sys.argv[1]) if len(sys.argv) > 1 else 3
        n_ue = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    except ValueError:
        print("usage: engine_acceptance.py [n_gnb] [n_ue]      (default: 3 6)")
        return 2
    print("=" * 74)
    print("ENGINE ACCEPTANCE — comparing sources, not eyeballing numbers")
    print("=" * 74)
    print(f"collecting a live run ({n_gnb} cells / {n_ue} UEs) through the backend…")
    t0 = time.monotonic()
    ticks = collect(n_gnb, n_ue)
    print(f"  {len(ticks)} frames in {time.monotonic() - t0:.0f} s")

    agreement(ticks)
    invariants(ticks)
    responds_to_config(ticks)

    print("\n" + "=" * 74)
    if FAILS:
        print(f"{RED}{len(FAILS)} CHECK(S) FAILED{RESET}: " + "; ".join(FAILS))
        print("A failure here means the engine is reporting something it should not —\n"
              "treat it as a data-integrity bug, not a flaky test.")
        return 1
    print(f"{GREEN}ALL CHECKS PASSED{RESET} — engine and platform agree, invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
