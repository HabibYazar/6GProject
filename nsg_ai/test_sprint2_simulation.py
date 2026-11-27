import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
import tier_b
from campaign import CampaignItem, CampaignSpec, expand
from nsoran_live import summarise_batch_frames
from schema import NSGSimulationConfig


def example_config(seed=None):
    return NSGSimulationConfig.model_validate({
        "name": "Sprint 2 regression",
        "base_stations": [{
            "id": "gnb-1",
            "name": "Test gNB",
            "position": {"x": 0, "y": 0, "z": 30},
            "radio": {
                "band": {
                    "label": "n77",
                    "frequency_mhz": 3600,
                    "bandwidth_mhz": 100
                }
            }
        }],
        "ue_groups": [{
            "id": "ue-sprint2",
            "name": "Sprint 2 test UEs",
            "count": 2,
            "spawn_position": {"x": 150, "y": 100},
            "mobility": {"pattern": "pedestrian", "speed_mps": 1.0},
            "services": [
                {"slice_type": "eMBB", "qos_id": 9}
            ]
        }],
        "settings": {
            "random_seed": seed,
            "report_interval_ms": 2000,
            "shadowing_enabled": False
        }
    })


def sample_frames():
    return [
        {
            "tick": 0, "sim_t": 0.5,
            "ues": [{"thp_kbps": 1000}],
            "ue_ids": [1],
            "measurements": {
                "DRB.UEThpDl": {"mean": 1000},
                "RRU.PrbUsedDl": {"mean": 20},
                "L3servingSINR": {"mean": 8}
            },
            "slices": {"eMBB": 1000}
        },
        {
            "tick": 1, "sim_t": 1.0,
            "ues": [{"thp_kbps": 3000}],
            "ue_ids": [1],
            "measurements": {
                "DRB.UEThpDl": {"mean": 3000},
                "RRU.PrbUsedDl": {"mean": 40},
                "L3servingSINR": {"mean": 12}
            },
            "slices": {"eMBB": 3000}
        }
    ]


class Sprint2SimulationTests(unittest.TestCase):

    def test_seed_zero_is_preserved(self):
        self.assertEqual(
            tier_b.seed_of(
                {"rng_run": 0, "random_seed": 99}, None
            ), 0
        )

    def test_seed_fallback(self):
        self.assertEqual(
            tier_b.seed_of({"random_seed": 42}, None), 42
        )

    def test_explicit_seed_override(self):
        self.assertEqual(
            tier_b.seed_of({"rng_run": 42}, 17), 17
        )

    def simulated_walk(self, seed, explicit=None):
        params = {
            "rng_run": seed,
            "ue_positions": "200,200,1.5",
            "ue_speeds": "120",
            "ind_period_s": 1.0,
        }

        def fake_frame(*args, **kwargs):
            return {
                "seed": kwargs["seed"],
                "positions": [
                    tuple(p) for p in kwargs["positions"]
                ]
            }

        with patch.object(tier_b, "link_state", return_value={}):
            with patch.object(
                tier_b, "frame", side_effect=fake_frame
            ):
                return tier_b.frames(params, 3, seed=explicit)

    def test_same_seed_same_movement(self):
        a = self.simulated_walk(42)
        b = self.simulated_walk(42)
        self.assertEqual(a, b)
        self.assertEqual(a[0]["seed"], 42)

    def test_different_seed_changes_movement(self):
        a = self.simulated_walk(42)
        b = self.simulated_walk(43)
        self.assertNotEqual(a[-1]["positions"], b[-1]["positions"])

    def test_explicit_seed_reaches_stream(self):
        frames = self.simulated_walk(42, explicit=17)
        self.assertTrue(all(f["seed"] == 17 for f in frames))

    def test_backend_passes_seed_to_engine(self):
        cfg = example_config(42)
        params = main.nsoran_params(cfg)
        self.assertEqual(params["rng_run"], 42)

    def test_backend_preserves_zero_seed(self):
        cfg = example_config(0)
        params = main.nsoran_params(cfg)
        self.assertEqual(params["rng_run"], 0)

    def test_backend_parameters(self):
        params = main.nsoran_params(example_config(10))
        self.assertEqual(params["ind_period_s"], 2.0)
        self.assertEqual(params["shadowing"], "0")
        self.assertEqual(params["rng_run"], 10)

    def test_campaign_seed_sequence(self):
        cfg = example_config(7)
        spec = CampaignSpec(
            name="Seed regression",
            base_seed=41,
            items=[CampaignItem(
                label="test",
                config=cfg,
                frames=4,
                repeats=3
            )],
        )
        planned = expand(spec)
        self.assertEqual([x.seed for x in planned], [41, 42, 43])
        self.assertEqual(
            [main.nsoran_params(x.config)["rng_run"]
             for x in planned],
            [41, 42, 43]
        )
        self.assertEqual(cfg.settings.random_seed, 7)

    def test_kpm_aggregation(self):
        summary = summarise_batch_frames(sample_frames())
        self.assertEqual(summary["ticks"], 2)
        self.assertEqual(summary["dl_mbps_mean"], 2.0)
        self.assertEqual(
            summary["measurements"]["DRB.UEThpDl"]["mean"],
            2000.0
        )
        self.assertEqual(
            summary["measurements"]["RRU.PrbUsedDl"]["mean"],
            30.0
        )
        self.assertEqual(
            summary["measurements"]["L3servingSINR"]["mean"],
            10.0
        )
        self.assertEqual(
            summary["slices"]["eMBB"]["mean_mbps"],
            2.0
        )

    def test_empty_kpm_is_not_successful_measurement(self):
        summary = summarise_batch_frames([])
        self.assertEqual(summary["ticks"], 0)

    def test_run_api_returns_kpm_summary(self):
        rid = "sprint2-contract"
        summary = summarise_batch_frames(sample_frames())
        dataset = {
            "id": rid,
            "owner_id": None,
            "status": "done",
            "summary": summary,
            "frames": sample_frames()
        }

        with patch.dict(os.environ, {"NSG_AUTH_REQUIRED": "0"}):
            with patch.object(main, "_SIM_RUNS", {rid: dataset}):
                with patch.object(
                    main.store, "load_run", return_value=dataset
                ):
                    client = TestClient(main.app)
                    response = client.get(f"/sim/run/{rid}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["summary"]["dl_mbps_mean"], 2.0
        )
        self.assertEqual(len(response.json()["frames"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
