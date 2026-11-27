"""
NSG AI — Test campaigns.

A single interactive run answers "what happened?". A campaign answers the
question a test bench exists for: **"is this still good enough?"** — a fixed set
of scenarios, executed with fixed seeds, scored against declared thresholds,
producing a verdict that can be compared to last week's.

The pieces:
  CampaignSpec   what to run and what "pass" means
  expand()       spec -> the concrete list of runs (repeats become seeds)
  evaluate()     one run's summary + thresholds -> PASS / FAIL + violations

Execution lives in main.py (it owns the batch-run job machinery); everything
here is pure and unit-testable without an engine, which is the point — the
verdict logic is the part that must not be wrong, and it is the part that would
otherwise only ever be exercised by a 10-minute engine run.
"""
from __future__ import annotations

import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from schema import NSGSimulationConfig

# Comparison operators a threshold may use. Deliberately no "!=" or "==" on
# floats: a measured KPI never lands exactly on a number, so an equality
# threshold would be a test that always fails.
Op = Literal[">=", "<=", ">", "<"]

_OPS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">":  lambda a, b: a > b,
    "<":  lambda a, b: a < b,
}


class Threshold(BaseModel):
    """One pass/fail criterion applied to a run's summary.

    `metric` is a dotted path INTO the summary that summarise_batch_frames
    produces, so anything the run measured can be asserted without this module
    knowing the metric catalogue:

        dl_mbps_mean                       run-level downlink
        energy_final                       final energy reading
        handovers_total                    mobility churn
        measurements.L3servingSINR.mean    any E2SM-KPM measurement
        slices.eMBB.mean_mbps              any slice
    """
    metric: str = Field(..., description="Dotted path into the run summary")
    op: Op = Field(..., description="Comparison the measured value must satisfy")
    value: float = Field(..., description="The bound being compared against")
    label: str = Field("", description="Human-readable name for reports")

    def describe(self) -> str:
        return f"{self.label or self.metric} {self.op} {self.value}"


class CampaignItem(BaseModel):
    """One scenario in the suite, run `repeats` times with successive seeds."""
    label: str = Field(..., description="Name for this scenario in the report")
    config: NSGSimulationConfig
    frames: int = Field(80, ge=4, le=600,
                        description="Captured KPM windows (ceiling, not a horizon)")
    repeats: int = Field(1, ge=1, le=10,
                         description="Runs of this scenario, one seed apart")


class CampaignSpec(BaseModel):
    """What to run and what counts as passing."""
    name: str
    description: str = ""
    items: list[CampaignItem] = Field(..., min_length=1)
    thresholds: list[Threshold] = Field(
        default_factory=list,
        description="Applied to EVERY run. Empty = the campaign only records "
                    "results and never fails, which is a valid baseline pass.",
    )
    base_seed: int = Field(
        42, description="Seed for the first repeat of each item; repeat i uses "
                        "base_seed + i, so a campaign is reproducible while its "
                        "repeats are not identical.",
    )
    wall_cap_s: float = Field(
        900.0, ge=30.0, le=172800.0,
        description="Wall-clock budget PER RUN. This is the real horizon "
                    "control on this path: ns-3 runs at a few percent of real "
                    "time, so a run almost always ends here rather than at its "
                    "frame target. At the 900 s default a six-cell scenario "
                    "reaches about 1.1 SIMULATED seconds no matter how many "
                    "frames are asked for — measured, 39 of 50 collected.",
    )
    partition_groups: int = Field(
        1, ge=1, le=16,
        description="Run each scenario as this many ns-3 processes of whole "
                    "cells. This does NOT make the suite finish sooner — a "
                    "campaign already runs its configs concurrently, and both "
                    "kinds of parallelism spend the same cores. What it buys is "
                    "a longer horizon PER RUN, which is what a campaign is short "
                    "of: at the automatic window a whole run covers well under a "
                    "simulated second. Shapes below three cells per group lose "
                    "handovers, so a campaign testing mobility or cell sleep "
                    "should stay at or above that.",
    )
    tier: Literal["fast", "slow"] = Field(
        "slow",
        description="Which tier of the engine runs this suite. 'slow' is "
                    "ns-3 / ns-O-RAN itself — the authority, and minutes per "
                    "run. 'fast' is the per-window model: the engine's own "
                    "AMC, transport-block sizes and 3GPP channel computed one "
                    "KPM window at a time instead of stepping ns-3 4000 times "
                    "for each, which turns a suite from an overnight job into "
                    "a coffee break. They are scored on the same axis because "
                    "they emit the same frames, and where they DISAGREE is "
                    "published — see tools/tier_agreement.py. Default stays "
                    "'slow' so an existing suite reruns as the suite it was.",
    )

    @model_validator(mode="after")
    def _fast_tier_does_not_partition(self) -> "CampaignSpec":
        """Partitioning buys ns-3 a longer horizon per process. The fast tier
        has no horizon problem, so asking for both is asking for something
        that cannot happen — and silently ignoring it would let a card
        promise a shape the run never had."""
        if self.tier == "fast" and self.partition_groups > 1:
            raise ValueError(
                "partition_groups is a slow-tier control: it splits a scenario "
                "across ns-3 processes to buy each one a longer horizon. The "
                "fast tier computes a whole window in one step and has no "
                "horizon to buy.")
        return self

    @model_validator(mode="after")
    def _reject_duplicate_labels(self) -> "CampaignSpec":
        labels = [i.label for i in self.items]
        dupes = {x for x in labels if labels.count(x) > 1}
        if dupes:
            # Results are keyed by label in every report and comparison; two
            # items sharing one turns "the VIP scenario regressed" into a
            # question nobody can answer.
            raise ValueError(f"duplicate item labels: {sorted(dupes)}")
        return self


class PlannedRun(BaseModel):
    """One concrete execution: an item at a specific seed."""
    label: str
    item_label: str
    repeat: int
    seed: int
    frames: int
    config: NSGSimulationConfig


def expand(spec: CampaignSpec) -> list[PlannedRun]:
    """Flatten the spec into the runs to execute, stamping each one's seed.

    The seed is written INTO the config rather than passed alongside it, so the
    stored dataset carries the seed that produced it — a run you cannot re-run
    identically is not evidence.
    """
    planned: list[PlannedRun] = []
    for item in spec.items:
        for r in range(item.repeats):
            cfg = item.config.model_copy(deep=True)
            cfg.settings.random_seed = spec.base_seed + r
            planned.append(PlannedRun(
                label=item.label if item.repeats == 1 else f"{item.label} #{r + 1}",
                item_label=item.label,
                repeat=r,
                seed=cfg.settings.random_seed,
                frames=item.frames,
                config=cfg,
            ))
    return planned


def resolve_metric(summary: dict, path: str) -> Optional[float]:
    """Follow a dotted path into a run summary. None when it is not there.

    The E2SM-KPM measurement names ARE dotted — `RRU.PrbUsedDl`, `DRB.UEThpDl`,
    `CARR.PDSCHMCSDist.Bin1` — so a naive split on "." walks into keys that do
    not exist and every threshold on a real measurement failed as "missing".
    Each level therefore takes the LONGEST key that matches, which resolves
    `measurements.RRU.PrbUsedDl.mean` against the literal key `RRU.PrbUsedDl`
    while leaving plain paths like `dl_mbps_mean` untouched.
    """
    parts = path.split(".")
    node: Any = summary
    i = 0
    while i < len(parts):
        if not isinstance(node, dict):
            return None
        for j in range(len(parts), i, -1):
            key = ".".join(parts[i:j])
            if key in node:
                node = node[key]
                i = j
                break
        else:
            return None
    if isinstance(node, bool) or not isinstance(node, (int, float)):
        return None
    return float(node)


class Violation(BaseModel):
    metric: str
    expected: str
    measured: Optional[float] = None
    reason: Literal["below_bound", "above_bound", "missing"] = "below_bound"


def evaluate(summary: dict, thresholds: list[Threshold]) -> tuple[str, list[Violation]]:
    """Score one run: 'pass', 'fail', or 'no_thresholds'.

    A metric the run never produced is a FAILURE, not a skip. A threshold on a
    KPI the engine did not report means the run did not demonstrate what the
    campaign asked it to demonstrate — silently passing that is how a test
    bench starts lying.
    """
    if not thresholds:
        return "no_thresholds", []

    violations: list[Violation] = []
    for t in thresholds:
        measured = resolve_metric(summary, t.metric)
        if measured is None:
            violations.append(Violation(metric=t.metric, expected=t.describe(),
                                        measured=None, reason="missing"))
            continue
        if not _OPS[t.op](measured, t.value):
            violations.append(Violation(
                metric=t.metric, expected=t.describe(), measured=measured,
                reason="below_bound" if t.op in (">=", ">") else "above_bound",
            ))
    return ("fail" if violations else "pass"), violations


def horizon_violations(anomalies, summary: dict) -> list[Violation]:
    """Did the run ever reach the events it was asked to test?

    Anomalies are written in whole simulated seconds, but a batch run covers a
    fraction of one: measured 0.3 s at 30 UEs, 3 gNBs. So a fault at start_s=5
    is never injected, nothing degrades, every threshold holds, and the run
    reports PASS for a test that did not happen.

    The engine already reports how far it got, as `energy.span_s`. Comparing the
    two is the whole check.

    Reported, not scored. main.py deliberately keeps "no fault fired" a warning
    rather than a failure ("the thresholds were honestly met"), and that call is
    not this function's to overturn -- but it only fires when NOTHING fired, so
    a run where an early fault fired and a later one never did stays silent.
    That gap is what this closes.
    """
    latest = 0.0
    for a in anomalies or []:
        start = float(getattr(a, "start_s", 0.0) or 0.0)
        dur = float(getattr(a, "duration_s", 0.0) or 0.0)
        latest = max(latest, start, start + dur)
    if latest <= 0.0:
        return []

    span = resolve_metric(summary, "energy.span_s")
    if span is None:
        return [Violation(
            metric="energy.span_s",
            expected=f"run must reach {latest:g} s to exercise its faults",
            measured=None, reason="missing")]
    if span < latest:
        # Say how to FIX it, not just that it happened. The collection window is
        # the horizon and the trade is linear (~0.10 simulated s per wall s on a
        # small topology, ~4x less on a rich config), so the number the scenario
        # needs is computable rather than a matter of trying again smaller.
        need = max(10, min(900, int(latest / 0.025) + 10))
        return [Violation(
            metric="energy.span_s",
            expected=(f">= {latest:g} s (last scheduled fault); run covered {span:g} s"
                      f" — set settings.kpm_collect_s to about {need} s to buy it"),
            measured=span, reason="below_bound")]
    return []


def campaign_verdict(run_verdicts: list[str]) -> str:
    """Roll per-run verdicts into the campaign's. Any failure fails the campaign;
    an errored run (engine down, crash) is not a pass — it is 'error', which is
    distinct from 'fail' because the cause is the bench, not the scenario."""
    if not run_verdicts:
        return "empty"
    if "error" in run_verdicts:
        return "error"
    if "fail" in run_verdicts:
        return "fail"
    if all(v == "no_thresholds" for v in run_verdicts):
        return "no_thresholds"
    return "pass"


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════════════════════
# REGRESSION DIFF — this campaign against a baseline
# A verdict says whether a run passed. A diff says whether it got WORSE, which is
# the question a test bench is actually kept around to answer.
# ═══════════════════════════════════════════════════════════════════════════════

def flatten_numeric(summary: dict, prefix: str = "") -> dict[str, float]:
    """Every numeric leaf of a run summary, as dotted paths.

    Same address space as Threshold.metric, so a threshold and a diff row always
    refer to the same number.
    """
    out: dict[str, float] = {}
    for key, value in (summary or {}).items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten_numeric(value, f"{path}."))
        elif isinstance(value, bool):
            continue
        elif isinstance(value, (int, float)):
            out[path] = float(value)
    return out


def aggregate_item(runs: list[dict]) -> dict[str, dict]:
    """Fold an item's repeats into one number per metric, keeping the spread.

    The spread is not decoration. Repeats of the same scenario at different seeds
    already differ by several percent, so a delta smaller than the baseline's own
    spread is noise — and a bench that reports noise as a regression gets muted
    within a week.
    """
    per_metric: dict[str, list[float]] = {}
    for run in runs:
        if run.get("summary") is None:
            continue
        for path, value in flatten_numeric(run["summary"]).items():
            per_metric.setdefault(path, []).append(value)
    return {
        path: {
            "mean": sum(vals) / len(vals),
            "min": min(vals),
            "max": max(vals),
            "spread": max(vals) - min(vals),
            "n": len(vals),
        }
        for path, vals in per_metric.items() if vals
    }


def _item_verdict(runs: list[dict]) -> str:
    return campaign_verdict([r.get("verdict") or "error" for r in runs])


def _direction(metric: str, delta: float, thresholds: list[dict]) -> Optional[str]:
    """'better' / 'worse' — but only where the campaign SAID which way is good.

    A threshold's operator carries that intent: `>=` means more is better, `<=`
    means less is. For a metric nobody set a bound on there is no answer — more
    handovers is not self-evidently good or bad — so this returns None rather
    than guessing, and the UI shows the delta without a colour.
    """
    if delta == 0:
        return "same"
    ops = {t.get("op") for t in thresholds if t.get("metric") == metric}
    if not ops:
        return None
    higher_is_better = bool(ops & {">=", ">"})
    lower_is_better = bool(ops & {"<=", "<"})
    if higher_is_better and lower_is_better:
        return None  # bounded on both sides — "better" is not a direction
    if higher_is_better:
        return "better" if delta > 0 else "worse"
    return "better" if delta < 0 else "worse"


# Metrics every campaign history carries, whether or not a threshold bounded
# them — the handful that describe what a run DID. The campaign's own threshold
# metrics are added on top, since those are the ones it was judged on.
HEADLINE_METRICS = (
    # What the network carried
    "dl_mbps_mean",
    "dl_mbps_max",
    "measurements.DRB.UEThpDl.mean",
    "measurements.DRB.PdcpSduBitRateDl.mean",
    "measurements.DRB.PdcpSduBitRateUl.mean",
    "measurements.DRB.PdcpSduVolumeDl.mean",
    # How good the radio was. The modulation split and the error count are the
    # two that say WHY throughput moved — without them a trend line drops and
    # there is nothing to attribute it to. The error count is only readable
    # against the total, so both are carried.
    "measurements.L3servingSINR.mean",
    "measurements.TB.TotNbrDlInitial.64Qam.mean",
    "measurements.TB.TotNbrDlInitial.Qpsk.mean",
    "measurements.TB.ErrTotalNbrDl.1.mean",
    "measurements.TB.TotNbrDl.1.mean",
    # How hard the cells were working
    "measurements.RRU.PrbUsedDl.mean",
    "measurements.DRB.BufferSize.Qos.mean",
    "ue_count_mean",
    "energy_final.total_w",
    # Mobility and control
    "handovers_total",
    "anomalies_fired",
    "anomalies_configured",
    # What the run actually covered. Ticks are KPM WINDOWS, ~12 to the simulated
    # second, so a tick count alone never answered "how much network time did
    # this cover". `sim_span_s` is the answer, measured rather than assumed, and
    # it sits beside the tick count so the two are read together.
    "ticks",
    "sim_span_s",
    # Real, but nothing may be judged on them: PDCP delay swings 14x across
    # repeats of the SAME config and seed. Carried because a gap that large is
    # worth SEEING; the UI labels them NOISY where they are chosen.
    "measurements.DRB.PdcpSduDelayDl.mean",
    "measurements.DRB.PdcpSduDelayUl.mean",
)


def partition_groups_of(record: dict) -> int:
    """How many processes each scenario in this campaign ACTUALLY ran as.

    Read from the runs' own results, not from the request. The spec field is
    what was asked for; `summary.partition_shape` is what the engine did, and
    they differ whenever a scenario's cell count did not divide — that one runs
    whole while the rest run split.

    Reading the results also survives a restart. The record carries the request
    in memory but the campaigns table has no column for it, so it came back as
    None after a reload — the History row showed the shape right after the run
    and had forgotten it by the next page load.
    """
    seen = {
        int((((r.get("summary") or {}).get("partition_shape") or {}).get("groups")) or 1)
        for r in (record.get("runs") or [])
        if (r.get("summary") or {}).get("partition_shape")
    }
    if seen:
        return max(seen)
    return int(record.get("partition_groups") or 1)


def known_partition_groups_of(record: dict) -> int | None:
    """Return actual/requested shape only when there is supporting evidence.

    NULL on old records is UNKNOWN, not proof that they ran whole.
    """
    if record.get("partition_groups") is not None:
        return partition_groups_of(record)
    if any(
        (r.get("summary") or {}).get("partition_shape")
        for r in record.get("runs") or []
    ):
        return partition_groups_of(record)
    return None


def campaign_history(records: list[dict], limit: int = 12) -> dict:
    """Fold a suite's campaigns into a scenario × campaign grid.

    A single campaign answers "did tonight pass?". This answers "which scenario
    has been sliding for three weeks?" — the question that needs the runs lined
    up in time rather than a pair diffed against each other.

    `records` must already be the campaigns of ONE suite: results from different
    suites share no scenarios and lining them up would invent a trend.
    """
    ordered = sorted(records, key=lambda r: r.get("created_at") or "")[-limit:]

    columns: list[dict] = []
    labels: list[str] = []
    for record in ordered:
        by_item: dict[str, list[dict]] = {}
        for run in record.get("runs", []):
            by_item.setdefault(run.get("item_label") or run.get("label", "?"), []).append(run)

        wanted = list(dict.fromkeys(
            [t["metric"] for t in (record.get("thresholds") or [])] + list(HEADLINE_METRICS)
        ))

        items = []
        for label, runs in by_item.items():
            if label not in labels:
                labels.append(label)
            agg = aggregate_item(runs)
            items.append({
                "label": label,
                "verdict": _item_verdict(runs),
                "repeats": len(runs),
                "metrics": {
                    m: round(agg[m]["mean"], 4) for m in wanted if m in agg
                },
                # Carried so the trend view can grey a point the same way the
                # diff does, instead of drawing a confident line through noise.
                "spread": {m: round(agg[m]["spread"], 4) for m in wanted if m in agg},
                "noise_known": all(agg[m]["n"] >= 2 for m in wanted if m in agg) and bool(agg),
            })

        columns.append({
            "id": record["id"],
            "created_at": record.get("created_at"),
            "verdict": record.get("verdict"),
            "status": record.get("status"),
            # WHICH INSTRUMENT PRODUCED THIS COLUMN. A suite run split across
            # processes and the same suite run whole are lined up here side by
            # side and scored on the same axes — and they are not the same
            # measurement: at one window budget the split run reaches several
            # times more simulated time, serves more UEs, and loses the
            # interference between groups. Without this the chart draws a trend
            # line through two different experiments.
            "tier": record.get("tier"),
            "partition_groups": known_partition_groups_of(record),
            "wall_cap_s": record.get("wall_cap_s"),
            "thresholds": record.get("thresholds") or [],
            "items": items,
        })

    return {"scenarios": labels, "campaigns": columns}



# ── WHICH PARAMETER CHANGED ──────────────────────────────────────────────────
# The metric diff answers "did it get better". It cannot answer "because of
# WHAT", which is the question anyone tuning a baseline is actually asking: they
# altered a field or two and want to know whether that is what moved the number.
# Every run already stores the exact config it executed, so the answer was one
# comparison away and nobody was making it.
#
# Deliberately leaf-level and dotted, not a pretty-printed object: "the antenna
# tilt went 3 -> 8" is the sentence someone needs, and an indented JSON blob with
# a changed line in it is not that sentence.

# Keys that differ between two configs for reasons nobody tuned. Comparing them
# buries the two fields that matter under forty that do not.
_DIFF_SKIP_LEAVES = {"id", "name", "description", "created_at", "updated_at"}


def _flatten_config(obj, prefix: str = "") -> dict:
    """Config to {dotted.path: leaf}. Lists are indexed, so a per-cell change
    reads as `base_stations[2].radio.tx_power_dbm` rather than "something in a
    list moved"."""
    out: dict = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _DIFF_SKIP_LEAVES and not isinstance(v, (dict, list)):
                continue
            out.update(_flatten_config(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_flatten_config(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def diff_configs(baseline_cfg: dict | None, current_cfg: dict | None,
                 limit: int = 40) -> dict:
    """What differs between two scenario configs, leaf by leaf.

    `truncated` is reported rather than silently cutting the list: "these are the
    changes" and "these are some of the changes" support very different
    conclusions about which one moved the metric.
    """
    if not baseline_cfg or not current_cfg:
        return {"changed": [], "available": False, "truncated": False, "total": 0}
    b, c = _flatten_config(baseline_cfg), _flatten_config(current_cfg)
    changed = []
    for path in sorted(set(b) | set(c)):
        bv, cv = b.get(path, "—"), c.get(path, "—")
        if bv != cv:
            changed.append({"path": path, "baseline": bv, "current": cv})
    return {"changed": changed[:limit], "available": True,
            "truncated": len(changed) > limit, "total": len(changed)}


def diff_campaigns(baseline: dict, current: dict) -> dict:
    """Compare two campaign records item by item.

    Matching is by item_label, not by run index: repeats and seeds may differ
    between the two, and "the VIP scenario" is the thing whose regression matters,
    not "the fourth run".
    """
    thresholds = current.get("thresholds") or []

    def by_item(record: dict) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for run in record.get("runs", []):
            grouped.setdefault(run.get("item_label") or run.get("label", "?"), []).append(run)
        return grouped

    base_items, cur_items = by_item(baseline), by_item(current)

    # RENAMING A SCENARIO SHOULD NOT DESTROY THE COMPARISON. Matching is by
    # label, which is right when a suite carries several scenarios — but the
    # commonest tuning loop is "copy the baseline, change two fields, give it a
    # new name", and that produced two `incomparable` rows and nothing else.
    # When neither side shares a label and each holds exactly ONE scenario,
    # there is only one thing the user can mean. Paired explicitly rather than
    # silently: the result says the labels differed and which two it used, so a
    # genuinely unrelated pair is visible as such rather than presented as a
    # like-for-like regression.
    renamed_pair: tuple[str, str] | None = None
    if not (set(base_items) & set(cur_items)) and len(base_items) == 1 == len(cur_items):
        b_label, c_label = next(iter(base_items)), next(iter(cur_items))
        renamed_pair = (b_label, c_label)
        base_items = {c_label: base_items[b_label]}

    items: list[dict] = []

    for label in sorted(set(base_items) | set(cur_items)):
        b_runs, c_runs = base_items.get(label), cur_items.get(label)

        if b_runs is None or c_runs is None:
            items.append({
                "label": label,
                "status": "added" if b_runs is None else "removed",
                "baseline_verdict": _item_verdict(b_runs) if b_runs else None,
                "current_verdict": _item_verdict(c_runs) if c_runs else None,
                "change": "incomparable",
                "metrics": [],
            })
            continue

        b_agg, c_agg = aggregate_item(b_runs), aggregate_item(c_runs)
        b_verdict, c_verdict = _item_verdict(b_runs), _item_verdict(c_runs)

        metrics = []
        for path in sorted(set(b_agg) & set(c_agg)):
            b, c = b_agg[path], c_agg[path]
            delta = c["mean"] - b["mean"]
            metrics.append({
                "metric": path,
                "baseline": round(b["mean"], 4),
                "current": round(c["mean"], 4),
                "delta": round(delta, 4),
                "pct": round(delta / b["mean"] * 100, 2) if b["mean"] else None,
                "baseline_spread": round(b["spread"], 4),
                "samples": [b["n"], c["n"]],
                # With one repeat the spread is 0 by construction, so EVERY
                # wiggle would read as a real change. Measured on this engine:
                # the same seed and the same 20-tick horizon still moved
                # throughput 56.62 -> 58.63 (+3.5%) between two identical runs,
                # while energy was bit-identical. So a single-sample baseline
                # cannot answer the noise question at all — say so instead of
                # answering it wrongly.
                "noise_known": b["n"] >= 2,
                "within_noise": b["n"] >= 2 and abs(delta) <= b["spread"],
                "is_threshold": any(t.get("metric") == path for t in thresholds),
                "direction": _direction(path, delta, thresholds),
            })

        # The config the two sides actually ran. Repeats of one item share a
        # config, so the first run of each is representative.
        config_delta = diff_configs((b_runs[0] or {}).get("config"),
                                    (c_runs[0] or {}).get("config"))

        if b_verdict == c_verdict:
            change = "unchanged"
        elif c_verdict == "fail" and b_verdict == "pass":
            change = "regressed"
        elif c_verdict == "pass" and b_verdict == "fail":
            change = "fixed"
        else:
            change = "changed"

        items.append({
            "label": label, "status": "matched",
            "baseline_verdict": b_verdict, "current_verdict": c_verdict,
            "change": change, "metrics": metrics,
            # What was DIFFERENT about the run, beside what came out of it.
            "config_delta": config_delta,
            # Set only when the two sides were paired despite different names,
            # so the reader knows the pairing was inferred rather than declared.
            "matched_by": "position" if renamed_pair else "label",
            "baseline_label": renamed_pair[0] if renamed_pair else label,
        })

    changes = {i["change"] for i in items}
    if "regressed" in changes:
        overall = "regressed"
    elif "fixed" in changes and changes <= {"fixed", "unchanged", "incomparable"}:
        overall = "fixed"
    elif changes <= {"unchanged"}:
        overall = "unchanged"
    else:
        overall = "changed"

    def head(record: dict) -> dict:
        return {
            k: record.get(k)
            for k in ("id", "name", "created_at", "verdict", "base_seed")
        } | {
            "tier": record.get("tier"),
            "partition_groups": known_partition_groups_of(record),
            "wall_cap_s": record.get("wall_cap_s"),
        }

    return {
        "baseline": head(baseline),
        "current": head(current),
        "overall": overall,
        "seed_match": baseline.get("base_seed") == current.get("base_seed"),
        "threshold_match": (baseline.get("thresholds") or []) == thresholds,
        "items": items,
    }
