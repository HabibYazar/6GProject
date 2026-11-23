"""
The two files the write-up needs, and nothing else.

summary.csv    one row per arm x seed, across all three matrices.
oscillation.csv  a per-tick commanded-power trace for four S1 arms at one seed,
                 enough to draw the oscillation figure.

Both are derived, not raw: the full per-run JSON is 11 MB of tick-level cell and
UE rows, and none of that belongs in a paper repository.

One column beyond the requested schema: `energy_valid`. The S2 matrices ran
before the cell_off defect was found — a cell commanded off reports 0 W while it
keeps carrying 21-73 Mbps, so their power columns measure accounting dropping
out, not power being saved. Marking each row is safer than a note in a README
that a future reader may not have next to the file.
"""
from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path

from xapps import direction_flips, BASE_DBM

HERE = Path(__file__).parent
OUT = HERE / "out" / "paper"

# scenario label -> (directory, is the energy column trustworthy?)
MATRICES = {
    "S1_spread1.0": ("matrix", True),
    "S2_spread1.0": ("matrix_s2", False),
    "S2_spread1.8": ("matrix_s2_spread18", False),
}

FIELDS = [
    "scenario", "arm", "seed", "energy_valid",
    "n_control_intervals", "n_contended_intervals",
    "esave_requests", "maxtp_requests", "commands_issued",
    "suppressed", "reverted", "synthesised",
    "direction_changes_contended_cell", "direction_changes_other_cells",
    # Settling: the control intervals split in three, counting commands in the
    # first and last third. Reported as a pair on purpose — a low last-third
    # count means nothing without the first, since an arm that was never active
    # would look identical to one that converged. Across arms the first third is
    # near-constant (5-6 moves for every two-xApp arm), so the pair isolates the
    # trajectory rather than the overall activity level.
    "moves_first_third_contended", "moves_last_third_contended",
    "moves_first_third_total", "moves_last_third_total",
    "mean_network_power_w", "contended_cell_power_w",
    "dl_throughput_mbps", "p5_ue_throughput_mbps", "rail_hits",
]


def contended_cell(ticks: list[dict]) -> str | None:
    """Defined by contention, never by the outcome metrics.

    A cell qualifies when more than one xApp requested it in the same control
    interval. Choosing instead the cell with the largest power variance would be
    selecting on the very quantity being reported.
    """
    counts: dict[str, int] = {}
    for t in ticks:
        for o in (t.get("note", {}).get("outcomes") or []):
            if o.get("n_requests", 0) > 1:
                counts[str(o["cell"])] = counts.get(str(o["cell"]), 0) + 1
    return max(counts, key=counts.get) if counts else None


def settled(ticks: list[dict]) -> list[dict]:
    return [t for t in ticks if t.get("note", {}).get("phase") in ("measure", "stale")]


def row(scen: str, valid: bool, j: dict) -> dict:
    m, ticks = j["metrics"], j["ticks"]
    cell = contended_cell(ticks)
    flips = m.get("power_flips_by_cell") or {}

    cw = ""
    if cell:
        vals = [t["per_cell_w"][cell] for t in settled(ticks) if cell in t["per_cell_w"]]
        if vals:
            cw = round(statistics.mean(vals), 2)

    # Only "measure" ticks are control intervals — a stale repeat gave the
    # controller nothing new to act on and must not dilute the thirds.
    iv = [t for t in ticks if t.get("note", {}).get("phase") == "measure"]
    n = len(iv)
    lo, hi = n // 3, 2 * n // 3

    def moves(a: int, b: int, only: str | None) -> int:
        return sum(1 for t in iv[a:b] for c in t["applied"]
                   if only is None or str(c["cell"]) == only)

    return {
        "scenario": scen, "arm": j["arm"], "seed": j["seed"],
        "energy_valid": "true" if valid else "false",
        "n_control_intervals": m["control_intervals"],
        "n_contended_intervals": m["contested_intervals"],
        "esave_requests": m["requests_esave"],
        "maxtp_requests": m["requests_maxtp"],
        "commands_issued": m["writes_total"],
        "suppressed": m["suppressed"],
        "reverted": m["rolled_back"],
        "synthesised": m["synthesised"],
        "direction_changes_contended_cell": flips.get(cell, "") if cell else "",
        "direction_changes_other_cells": sum(v for k, v in flips.items() if k != cell),
        "moves_first_third_contended": moves(0, lo, cell) if cell else "",
        "moves_last_third_contended": moves(hi, n, cell) if cell else "",
        "moves_first_third_total": moves(0, lo, None),
        "moves_last_third_total": moves(hi, n, None),
        "mean_network_power_w": m["mean_network_w"],
        "contended_cell_power_w": cw,
        "dl_throughput_mbps": m["dl_mbps_mean"],
        "p5_ue_throughput_mbps": m["ue_thp_p5_mbps"],
        "rail_hits": m["rail_hits"],
    }


def write_summary() -> int:
    rows = []
    for scen, (d, valid) in MATRICES.items():
        for p in sorted((HERE / "out" / d).glob("*-s*.json")):
            if p.name.endswith(".FAILED.json"):
                continue
            rows.append(row(scen, valid, json.loads(p.read_text())))
    rows.sort(key=lambda r: (r["scenario"], r["arm"], r["seed"]))
    with (OUT / "summary.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


# One seed, four S1 arms: the two that bracket the effect (no conflict, and
# capacity-priority which oscillates), plus the two that do not (energy-priority
# and synthesis). Same seed throughout so the traces are comparable.
TRACE_ARMS = ["B_esave", "D_both_P0", "E_both_P1", "G_both_P3"]
TRACE_SEED = 1


def write_oscillation() -> int:
    out = []
    for arm in TRACE_ARMS:
        p = HERE / "out" / "matrix" / f"{arm}-s{TRACE_SEED}.json"
        if not p.exists():
            continue
        j = json.loads(p.read_text())
        # Every arm traces the SAME three cells, including ones it never wrote.
        # Emitting only the cells an arm happened to command would drop cell 4
        # from the single-xApp trace and make the panels non-comparable — a
        # cell held flat at 36 dBm is exactly the contrast the figure needs.
        cells = sorted({int(c["cell"]) for t in j["ticks"] for c in t["cells"]
                        if c.get("cell") is not None} or {2, 3, 4})
        # Commanded power is a step function: it holds until the next write, so
        # every settled tick carries the value in force at that moment. Plotting
        # only the write instants would hide how long each level was held.
        cur = {c: BASE_DBM for c in cells}
        for t in j["ticks"]:
            for c in t["applied"]:
                if c.get("dbm") is not None:
                    cur[int(c["cell"])] = int(c["dbm"])
            if t.get("note", {}).get("phase") not in ("measure", "stale"):
                continue
            for c in cells:
                out.append({"arm": arm, "tick": t["tick"], "cell_id": c,
                            "tx_power_dbm": cur[c]})
    with (OUT / "oscillation.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["arm", "tick", "cell_id", "tx_power_dbm"])
        w.writeheader()
        w.writerows(out)
    return len(out)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    n1, n2 = write_summary(), write_oscillation()
    print(f"summary.csv       {n1} rows  ({OUT / 'summary.csv'})")
    print(f"oscillation.csv   {n2} rows  ({OUT / 'oscillation.csv'})")
