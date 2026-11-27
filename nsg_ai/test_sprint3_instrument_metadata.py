
import unittest

import main
from campaign import (
    campaign_history,
    diff_campaigns,
    known_partition_groups_of,
)


def record(identifier, **changes):
    result = {
        "id": identifier,
        "name": "Experiment suite",
        "created_at": "2026-11-27T12:00:00+00:00",
        "status": "done",
        "verdict": "no_thresholds",
        "base_seed": 42,
        "thresholds": [],
        "runs": [],
    }
    result.update(changes)
    return result


class InstrumentMetadataTests(unittest.TestCase):

    def test_legacy_unknown_not_misreported_as_whole(self):
        legacy = record("legacy")
        self.assertIsNone(known_partition_groups_of(legacy))
        self.assertIsNone(
            main._campaign_brief(legacy)["partition_groups"]
        )
        column = campaign_history([legacy])["campaigns"][0]
        self.assertIsNone(column["tier"])
        self.assertIsNone(column["partition_groups"])
        self.assertIsNone(column["wall_cap_s"])

    def test_modern_history_has_execution_identity(self):
        modern = record(
            "modern",
            tier="slow",
            partition_groups=3,
            wall_cap_s=180.0,
        )
        column = campaign_history([modern])["campaigns"][0]
        self.assertEqual(column["tier"], "slow")
        self.assertEqual(column["partition_groups"], 3)
        self.assertEqual(column["wall_cap_s"], 180.0)

    def test_compare_preserves_both_instruments(self):
        baseline = record(
            "baseline",
            tier="fast",
            partition_groups=1,
            wall_cap_s=60.0,
        )
        current = record(
            "current",
            tier="slow",
            partition_groups=2,
            wall_cap_s=300.0,
        )
        result = diff_campaigns(baseline, current)
        self.assertEqual(result["baseline"]["tier"], "fast")
        self.assertEqual(result["current"]["tier"], "slow")
        self.assertEqual(
            result["current"]["partition_groups"], 2
        )
        self.assertEqual(
            result["current"]["wall_cap_s"], 300.0
        )

    def test_partition_shape_can_be_derived_from_results(self):
        historical = record(
            "measured",
            runs=[{
                "label": "test",
                "verdict": "pass",
                "summary": {
                    "partition_shape": {"groups": 3}
                },
            }],
        )
        self.assertEqual(known_partition_groups_of(historical), 3)
        self.assertEqual(
            main._campaign_brief(historical)["partition_groups"], 3
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
