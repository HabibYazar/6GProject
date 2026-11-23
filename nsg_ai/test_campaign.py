"""Campaign verdict logic — the part that must be right without an engine.

Run: cd nsg_ai && ../venv/bin/python test_campaign.py
"""
from campaign import (CampaignItem, CampaignSpec, Threshold, aggregate_item,
                      campaign_history, campaign_verdict, diff_campaigns,
                      evaluate, expand, flatten_numeric, horizon_violations,
                      resolve_metric)

FAILS: list[str] = []


# ── horizon: did the run reach the faults it staged ───────────────────────────
class _Anom:
    """Stand-in for schema.Anomaly — horizon_violations only reads two fields."""
    def __init__(self, start_s, duration_s=0.0):
        self.start_s = start_s
        self.duration_s = duration_s


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILS.append(name)


CFG = {
    "name": "campaign-probe",
    "base_stations": [{
        "id": "c0", "name": "C0", "position": {"x": 0, "y": 0, "z": 25},
        "radio": {"band": {"label": "N77", "frequency_mhz": 3600,
                           "bandwidth_mhz": 50, "numerology": 2}},
    }],
    "ue_groups": [{
        "id": "u0", "name": "U0", "count": 6, "spawn_position": {"x": 100, "y": 100},
        "services": [{"slice_type": "eMBB", "qos_id": 9,
                      "target_throughput_gbps": 0.01}],
    }],
}

# Shaped exactly like a real ns-O-RAN run summary (2026-07-29, campaign
# 96c2fe4503e8) — dotted measurement names and an object-valued energy_final.
REAL_SHAPED = {
    "ticks": 20,
    "dl_mbps_mean": 56.62,
    "energy_final": {"total_w": 5707.9,
                     "per_cell": {"2": {"j": 743.3, "w": 665.7},
                                  "3": {"j": 1225.2, "w": 1356.6}}},
    "measurements": {
        "L3servingSINR": {"mean": 8.1},
        "RRU.PrbUsedDl": {"mean": 41.2},
        "DRB.UEThpDl": {"mean": 12580.0},
        "CARR.PDSCHMCSDist.Bin1": {"mean": 3.0},
    },
}

SUMMARY = {
    "ticks": 40,
    "dl_mbps_mean": 25.5,
    "energy_final": 3800.0,
    "measurements": {"L3servingSINR": {"mean": 7.2, "min": 1.1, "max": 14.0}},
    "slices": {"eMBB": {"mean_mbps": 22.0}},
}


def main() -> None:
    print("\n[1] metric resolution")
    check("run-level path", resolve_metric(SUMMARY, "dl_mbps_mean") == 25.5)
    check("nested measurement",
          resolve_metric(SUMMARY, "measurements.L3servingSINR.mean") == 7.2)
    check("nested slice", resolve_metric(SUMMARY, "slices.eMBB.mean_mbps") == 22.0)
    check("missing path -> None", resolve_metric(SUMMARY, "nope.nope") is None)
    check("non-numeric leaf -> None",
          resolve_metric({"tick_range": [1, 2]}, "tick_range") is None)
    # Regression: the real engine's measurement names contain dots, and
    # energy_final is an object. Both made every threshold on them "missing".
    check("dotted measurement name resolves",
          resolve_metric(REAL_SHAPED, "measurements.RRU.PrbUsedDl.mean") == 41.2)
    check("three-part dotted name resolves",
          resolve_metric(REAL_SHAPED, "measurements.CARR.PDSCHMCSDist.Bin1.mean") == 3.0)
    check("object leaf -> None (energy_final is a dict)",
          resolve_metric(REAL_SHAPED, "energy_final") is None)
    check("energy total reachable one level in",
          resolve_metric(REAL_SHAPED, "energy_final.total_w") == 5707.9)
    check("per-cell energy reachable",
          resolve_metric(REAL_SHAPED, "energy_final.per_cell.3.w") == 1356.6)

    print("\n[2] evaluation")
    v, viol = evaluate(SUMMARY, [Threshold(metric="dl_mbps_mean", op=">=", value=20)])
    check("satisfied lower bound passes", v == "pass" and not viol)

    v, viol = evaluate(SUMMARY, [Threshold(metric="energy_final", op="<=", value=3000)])
    check("violated upper bound fails", v == "fail" and len(viol) == 1)
    check("violation carries the measured value", viol[0].measured == 3800.0)
    check("violation reason is above_bound", viol[0].reason == "above_bound")

    v, viol = evaluate(SUMMARY, [Threshold(metric="measurements.BLER.mean",
                                           op="<=", value=10)])
    check("threshold on a metric the run never produced FAILS",
          v == "fail" and viol[0].reason == "missing")

    v, viol = evaluate(SUMMARY, [])
    check("no thresholds -> no_thresholds", v == "no_thresholds" and not viol)

    v, viol = evaluate(SUMMARY, [
        Threshold(metric="dl_mbps_mean", op=">=", value=20),
        Threshold(metric="energy_final", op="<=", value=3000),
        Threshold(metric="measurements.L3servingSINR.mean", op=">=", value=5),
    ])
    check("mixed set fails once, reporting only the broken one",
          v == "fail" and len(viol) == 1 and viol[0].metric == "energy_final")

    print("\n[3] campaign roll-up")
    check("all pass -> pass", campaign_verdict(["pass", "pass"]) == "pass")
    check("any fail -> fail", campaign_verdict(["pass", "fail"]) == "fail")
    check("error outranks fail", campaign_verdict(["fail", "error"]) == "error")
    check("empty -> empty", campaign_verdict([]) == "empty")
    check("all unthresholded -> no_thresholds",
          campaign_verdict(["no_thresholds", "no_thresholds"]) == "no_thresholds")

    print("\n[4] expansion + seeds")
    spec = CampaignSpec(
        name="suite",
        base_seed=100,
        items=[
            CampaignItem(label="baseline", config=CFG, frames=20),
            CampaignItem(label="stress", config=CFG, frames=20, repeats=3),
        ],
    )
    planned = expand(spec)
    check("one run per repeat", len(planned) == 4)
    check("single-repeat item keeps its plain label", planned[0].label == "baseline")
    check("repeats are numbered", planned[1].label == "stress #1")
    check("seeds step from base_seed",
          [p.seed for p in planned] == [100, 100, 101, 102])
    check("seed is written INTO the config",
          all(p.config.settings.random_seed == p.seed for p in planned))
    check("configs are independent copies",
          planned[1].config is not planned[2].config)

    print("\n[5] spec validation")
    try:
        CampaignSpec(name="dup", items=[CampaignItem(label="a", config=CFG),
                                        CampaignItem(label="a", config=CFG)])
        check("duplicate item labels rejected", False)
    except Exception as exc:  # noqa: BLE001
        check("duplicate item labels rejected", "duplicate item labels" in str(exc))

    print("\n[6] flatten + aggregate")
    flat = flatten_numeric(SUMMARY)
    check("nested paths are flattened",
          flat["measurements.L3servingSINR.mean"] == 7.2)
    check("non-numeric leaves dropped", "tick_range" not in flat)

    agg = aggregate_item([
        {"summary": {"dl_mbps_mean": 20.0}, "verdict": "pass"},
        {"summary": {"dl_mbps_mean": 24.0}, "verdict": "pass"},
        {"summary": None, "verdict": "error"},   # errored run contributes nothing
    ])
    check("repeats averaged", agg["dl_mbps_mean"]["mean"] == 22.0)
    check("spread kept", agg["dl_mbps_mean"]["spread"] == 4.0)
    check("errored run excluded from n", agg["dl_mbps_mean"]["n"] == 2)

    print("\n[7] campaign diff")
    THR = [{"metric": "dl_mbps_mean", "op": ">=", "value": 10}]

    def rec(rid, runs, thresholds=THR, seed=42):
        return {"id": rid, "name": rid, "created_at": "t", "verdict": None,
                "base_seed": seed, "thresholds": thresholds, "runs": runs}

    def run(item, verdict, dl, sinr=7.0):
        return {"item_label": item, "label": item, "verdict": verdict,
                "summary": {"dl_mbps_mean": dl,
                            "measurements": {"L3servingSINR": {"mean": sinr}}}}

    base = rec("b", [run("urban", "pass", 20.0), run("urban", "pass", 22.0)])
    # SINR moves too, but nothing bounds it — the diff must stay silent on
    # whether that move is good news.
    cur  = rec("c", [run("urban", "fail", 8.0, sinr=6.0),
                     run("urban", "fail", 8.4, sinr=6.2)])
    d = diff_campaigns(base, cur)
    item = d["items"][0]
    check("matched item is compared", item["status"] == "matched")
    check("pass -> fail is a regression", item["change"] == "regressed")
    check("campaign-level overall regressed", d["overall"] == "regressed")
    m = next(x for x in item["metrics"] if x["metric"] == "dl_mbps_mean")
    check("delta is current - baseline", m["delta"] == -12.8)
    check("percent computed", m["pct"] == -60.95)
    check("threshold metric marked", m["is_threshold"] is True)
    check("'>=' threshold makes a drop 'worse'", m["direction"] == "worse")
    check("drop exceeds baseline spread -> real", m["within_noise"] is False)

    sinr = next(x for x in item["metrics"] if x["metric"].endswith("L3servingSINR.mean"))
    check("unbounded metric gets no direction", sinr["direction"] is None)

    noise = diff_campaigns(
        rec("b", [run("urban", "pass", 20.0), run("urban", "pass", 24.0)]),
        rec("c", [run("urban", "pass", 22.0), run("urban", "pass", 23.0)]),
    )
    nm = next(x for x in noise["items"][0]["metrics"] if x["metric"] == "dl_mbps_mean")
    check("move inside the baseline spread is flagged as noise",
          nm["within_noise"] is True)
    check("two repeats make the noise question answerable",
          nm["noise_known"] is True)

    # Measured on the real engine: same seed, same 20-tick horizon, throughput
    # still moved +3.5%. A single-repeat baseline has spread 0, so it must NOT
    # claim the move is significant.
    single = diff_campaigns(rec("b", [run("urban", "pass", 56.62)]),
                            rec("c", [run("urban", "pass", 58.63)]))
    sm = next(x for x in single["items"][0]["metrics"] if x["metric"] == "dl_mbps_mean")
    check("single-sample baseline cannot judge noise", sm["noise_known"] is False)
    check("single-sample baseline does not claim 'within noise'",
          sm["within_noise"] is False)
    check("no verdict change -> unchanged", noise["overall"] == "unchanged")

    fixed = diff_campaigns(rec("b", [run("urban", "fail", 5.0)]),
                           rec("c", [run("urban", "pass", 30.0)]))
    check("fail -> pass reports fixed", fixed["overall"] == "fixed")

    added = diff_campaigns(rec("b", [run("urban", "pass", 20.0)]),
                           rec("c", [run("urban", "pass", 20.0),
                                     run("stadium", "pass", 15.0)]))
    labels = {i["label"]: i["status"] for i in added["items"]}
    check("new scenario marked added", labels["stadium"] == "added")
    check("added scenario is not a regression", added["overall"] != "regressed")

    removed = diff_campaigns(rec("b", [run("urban", "pass", 20.0),
                                       run("stadium", "pass", 15.0)]),
                             rec("c", [run("urban", "pass", 20.0)]))
    check("dropped scenario marked removed",
          {i["label"]: i["status"] for i in removed["items"]}["stadium"] == "removed")

    mismatched = diff_campaigns(rec("b", [run("urban", "pass", 20.0)], seed=1),
                                rec("c", [run("urban", "pass", 20.0)], seed=9))
    check("differing base seeds are surfaced", mismatched["seed_match"] is False)
    changed_thr = diff_campaigns(
        rec("b", [run("urban", "pass", 20.0)], thresholds=THR),
        rec("c", [run("urban", "pass", 20.0)],
            thresholds=[{"metric": "dl_mbps_mean", "op": ">=", "value": 50}]),
    )
    check("changed thresholds are surfaced",
          changed_thr["threshold_match"] is False)

    print("\n[8] suite history")
    h = campaign_history([
        {**rec("h1", [run("urban", "pass", 20.0), run("urban", "pass", 24.0)]),
         "created_at": "2026-07-01T01:00:00+00:00"},
        {**rec("h2", [run("urban", "fail", 9.0), run("urban", "fail", 9.4),
                      run("stadium", "pass", 30.0)]),
         "created_at": "2026-07-08T01:00:00+00:00"},
    ])
    check("campaigns ordered oldest-first",
          [c["id"] for c in h["campaigns"]] == ["h1", "h2"])
    check("scenario added later still gets a row",
          set(h["scenarios"]) == {"urban", "stadium"})
    first_urban = h["campaigns"][0]["items"][0]
    check("history carries the item verdict", first_urban["verdict"] == "pass")
    check("history averages the repeats", first_urban["metrics"]["dl_mbps_mean"] == 22.0)
    check("history carries the spread", first_urban["spread"]["dl_mbps_mean"] == 4.0)
    check("two repeats -> noise answerable", first_urban["noise_known"] is True)
    single_item = next(i for i in h["campaigns"][1]["items"] if i["label"] == "stadium")
    check("single-repeat item marked unanswerable", single_item["noise_known"] is False)
    check("threshold metric is included in history",
          "dl_mbps_mean" in first_urban["metrics"])

    trimmed = campaign_history(
        [{**rec(f"c{i}", [run("urban", "pass", 20.0)]),
          "created_at": f"2026-07-{i + 1:02d}T01:00:00+00:00"} for i in range(9)],
        limit=3,
    )
    check("limit keeps the NEWEST campaigns",
          [c["id"] for c in trimmed["campaigns"]] == ["c6", "c7", "c8"])

    print("\nhorizon_violations")
    # A batch run covers a fraction of a simulated second, so a fault at 5 s never
    # fires. Before this check, only "NOTHING fired" was reported, so a scenario with
    # one early and one late fault passed in silence.
    _short = {"energy": {"span_s": 0.3}}
    check("fault beyond the horizon is reported",
          len(horizon_violations([_Anom(5.0)], _short)) == 1)
    check("fault inside the horizon is not",
          horizon_violations([_Anom(0.05)], _short) == [])
    check("partial coverage is caught (early fires, late does not)",
          len(horizon_violations([_Anom(0.05), _Anom(5.0)], _short)) == 1)
    check("duration counts toward the horizon",
          len(horizon_violations([_Anom(0.2, duration_s=1.0)], _short)) == 1)
    check("no anomalies means nothing to report",
          horizon_violations([], _short) == [] and horizon_violations(None, _short) == [])
    check("missing span is reported rather than assumed fine",
          horizon_violations([_Anom(5.0)], {})[0].reason == "missing")
    check("the message names both the requirement and what was covered",
          "0.3" in horizon_violations([_Anom(5.0)], _short)[0].expected)

    print("\n" + ("ALL CAMPAIGN CHECKS PASSED" if not FAILS
                  else f"{len(FAILS)} CHECK(S) FAILED: {FAILS}"))
    raise SystemExit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
