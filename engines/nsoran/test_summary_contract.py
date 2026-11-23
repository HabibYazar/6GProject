#!/usr/bin/env python3
"""
The run-summary CONTRACT: the shape three layers silently depend on.

Two contract breaks got through in one afternoon, and both presented as a
legitimate test result rather than as a bug:

  * The E2SM-KPM measurement names are themselves DOTTED (`RRU.PrbUsedDl`,
    `CARR.PDSCHMCSDist.Bin1`). A threshold path split naively on "." walked into
    keys that do not exist, so every campaign criterion on a real measurement
    scored as "never measured" — which the campaign correctly calls a FAIL.
  * `energy_final` is an OBJECT (`{total_w, per_cell}`), not a number. The UI
    offered `energy_final` as a metric preset, so an energy threshold could
    never pass.

Neither was a wrong number. Both were a wrong ASSUMPTION about the shape, held
in a different file from the one that produced it. This test pins the shape, and
— the part that matters — checks the UI's own metric list against it by READING
the UI source, so the two cannot drift apart again.

  python3 test_summary_contract.py            # against the golden sample
  python3 test_summary_contract.py --engine   # + a fresh run, shape-diffed
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path("/home/chancelor/nsg_ai_platform")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "nsg_ai"))
sys.path.insert(0, str(ROOT / "engines" / "nsoran"))

from campaign import resolve_metric                      # noqa: E402
from nsg_test_util import Checker                        # noqa: E402

GOLDEN = ROOT / "engines" / "nsoran" / "fixtures" / "run_summary.golden.json"
UI_CONSOLE = ROOT / "ui" / "components" / "CampaignConsole.tsx"

check = Checker()


def ui_metric_presets() -> list[str]:
    """The metric paths the UI offers, read from the UI source itself.

    Deliberately parsed rather than copied: a copy is exactly how the list and
    the engine drifted in the first place.
    """
    src = UI_CONSOLE.read_text()
    block = re.search(r"const METRIC_PRESETS[^=]*=\s*\[(.*?)\n\]", src, re.S)
    if not block:
        return []
    return re.findall(r"path:\s*'([^']+)'", block.group(1))


def shape_of(node, prefix="", out=None) -> dict:
    """A structural fingerprint: path -> type name, leaves only."""
    out = {} if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            shape_of(v, f"{prefix}{k}." if not prefix else f"{prefix}{k}.", out)
    else:
        out[prefix.rstrip(".")] = type(node).__name__
    return out


def check_contract(summary: dict, label: str) -> None:
    print(f"\n=== {label} ===")

    for key, kind in (("ticks", int), ("dl_mbps_mean", (int, float)),
                      ("dl_mbps_max", (int, float)), ("ue_count_mean", (int, float)),
                      ("measurements", dict), ("slices", dict)):
        check(isinstance(summary.get(key), kind),
              f"{key} is {getattr(kind, '__name__', kind)}: {type(summary.get(key)).__name__}")

    # The two that broke. Pinned as SHAPE, not as value.
    energy = summary.get("energy_final")
    check(isinstance(energy, dict),
          f"energy_final is an object, not a number ({type(energy).__name__}) — "
          f"a threshold on `energy_final` alone can never resolve")
    if isinstance(energy, dict):
        check(isinstance(energy.get("total_w"), (int, float)),
              "energy_final.total_w is the number a threshold can use")
        check(isinstance(energy.get("per_cell"), dict),
              "energy_final.per_cell is keyed by cell")

    dotted = [n for n in summary.get("measurements", {}) if "." in n]
    check(len(dotted) > 0,
          f"measurement NAMES contain dots ({len(dotted)} of "
          f"{len(summary.get('measurements', {}))}, e.g. {dotted[:2]}) — any path "
          f"resolver that splits on '.' must handle this")

    for name, stats in list(summary.get("measurements", {}).items())[:3]:
        check(isinstance(stats, dict) and "mean" in stats,
              f"measurement '{name}' carries a mean")

    # A dotted name must survive a round trip through the resolver campaigns use.
    if dotted:
        probe = f"measurements.{dotted[0]}.mean"
        check(resolve_metric(summary, probe) is not None,
              f"a dotted measurement resolves end to end: {probe}")

    check(resolve_metric(summary, "energy_final") is None,
          "resolving an object leaf returns None rather than a bogus number")
    check(resolve_metric(summary, "energy_final.total_w") is not None,
          "energy_final.total_w resolves")


def carried_traffic(summary: dict) -> bool:
    """Did this run actually do anything?

    A run that carried no traffic reports only the handful of measurements that
    exist without it. Checking metric coverage against such a run asserts
    something the run never had the chance to produce — the same mistake as
    asserting a handover effect on static UEs.
    """
    return float(summary.get("dl_mbps_mean") or 0.0) > 0.0


def check_ui_presets(summary: dict) -> None:
    print("\n=== UI metric presets vs a real summary ===")
    presets = ui_metric_presets()
    check(len(presets) >= 5, f"read {len(presets)} presets from CampaignConsole.tsx")
    if not carried_traffic(summary):
        print(f"  SKIP  run carried no traffic (dl_mbps_mean="
              f"{summary.get('dl_mbps_mean')}) — metric coverage is not "
              f"judgeable against it")
        return
    for path in presets:
        value = resolve_metric(summary, path)
        check(value is not None,
              f"preset resolves against a real run: {path} = {value}")


def main() -> int:
    if not GOLDEN.exists():
        print(f"no golden sample at {GOLDEN}")
        return 1
    golden = json.loads(GOLDEN.read_text())
    check_contract(golden, "golden sample")
    check_ui_presets(golden)

    if "--engine" in sys.argv:
        from nsg_ai.main import nsoran_params                  # noqa: PLC0415
        from nsg_ai.schema import NSGSimulationConfig          # noqa: PLC0415
        from nsoran_live import summarise_batch_frames         # noqa: PLC0415
        sys.path.insert(0, str(ROOT / "tools"))
        from fidelity_report import baseline_config            # noqa: PLC0415
        import asyncio                                          # noqa: PLC0415
        from nsoran_live import run_batch_simulation           # noqa: PLC0415

        cfg = NSGSimulationConfig(**baseline_config())
        # Long enough to carry traffic. The golden sample is 20 ticks; a
        # short run reports a stub of the measurement set and every coverage
        # check against it is meaningless.
        res = asyncio.run(run_batch_simulation(cfg, nsoran_params(cfg), frames_target=30))
        fresh = summarise_batch_frames(res["frames"])
        check_contract(fresh, "fresh engine run")
        check_ui_presets(fresh)

        print("\n=== shape drift ===")
        if not carried_traffic(fresh):
            print("  SKIP  run carried no traffic — a stub summary is not drift")
        else:
            gone = sorted(set(shape_of(golden)) - set(shape_of(fresh)))
            added = sorted(set(shape_of(fresh)) - set(shape_of(golden)))
            # Keys that legitimately come and go with the traffic mix: per-UE
            # neighbour lists, per-cell energy, and the MCS/SINR histogram bins,
            # which only appear for the modulations a run actually used. A key
            # outside those families vanishing is a contract change.
            volatile = ("measurements.L3neighSINRListOf", "energy_final.per_cell",
                        "measurements.CARR.PDSCHMCSDist.", "measurements.L1M.RS-SINR.",
                        "measurements.TB.")
            structural = [k for k in gone if not k.startswith(volatile)]
            check(not structural,
                  f"no structural key vanished since the golden sample: {structural[:5]}")
            if added:
                print(f"  note: {len(added)} new key(s), e.g. {added[:3]}")
    else:
        print("\n(skipping the fresh engine run; pass --engine to include it)")

    return check.report()


if __name__ == "__main__":
    raise SystemExit(main())
