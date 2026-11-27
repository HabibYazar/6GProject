
import unittest
from unittest.mock import AsyncMock, patch

import main
from campaign import CampaignSpec, CampaignItem, expand
from test_sprint2_simulation import example_config


class OfflinePool:
    size = 1

    def __init__(self):
        self.acquired = False

    def acquire(self):
        self.acquired = True
        raise AssertionError("Fast tier attempted to lease engine")


class CampaignRoutingTests(unittest.IsolatedAsyncioTestCase):

    def spec(self):
        return CampaignSpec(
            name="Sprint 3 routing regression",
            tier="fast",
            wall_cap_s=60,
            items=[
                CampaignItem(
                    label="baseline",
                    config=example_config(42),
                    frames=4,
                    repeats=1
                )
            ]
        )

    async def test_fast_campaign_skips_engine_pool(self):
        spec = self.spec()
        offline = OfflinePool()

        record = {
            "id": "fast-routing",
            "owner_id": "user-1",
            "stop_requested": False,
            "runs": []
        }

        with (
            patch.object(main, "_CAMPAIGNS", {record["id"]: record}),
            patch.object(main, "pool", return_value=offline),
            patch.object(main, "_campaign_save"),
            patch.object(
                main, "_campaign_run_one",
                new_callable=AsyncMock
            ) as runner
        ):
            await main._campaign_job(record["id"], spec)

            self.assertFalse(offline.acquired)
            runner.assert_awaited_once()
            self.assertIsNone(runner.await_args.args[-1])

    async def test_fast_error_never_falls_back_to_slow(self):
        spec = self.spec()

        record = {
            "id": "fast-error",
            "owner_id": "user-1",
            "runs": [{}]
        }

        async def fail_sim(run_id, *args, **kwargs):
            main._SIM_RUNS[run_id].update(
                status="error",
                error="simulated fast tier failure",
                n_frames=0
            )

        with (
            patch.object(main, "_SIM_RUNS", {}),
            patch.object(main.store, "save_run"),
            patch.object(main, "_campaign_save"),
            patch.object(
                main, "_sim_job",
                new_callable=AsyncMock,
                side_effect=fail_sim
            ) as runner
        ):
            await main._campaign_run_one(
                record["id"], record, spec,
                expand(spec)[0], 0, None
            )

            self.assertEqual(runner.await_count, 1)
            self.assertEqual(
                record["runs"][0]["verdict"], "error"
            )
            self.assertNotIn(
                "bench_retry", record["runs"][0]
            )

    async def test_campaign_run_inherits_owner(self):
        spec = self.spec()

        record = {
            "id": "owned-fast",
            "owner_id": "user-123",
            "runs": [{}]
        }

        async def finish_sim(run_id, *args, **kwargs):
            main._SIM_RUNS[run_id].update(
                status="done",
                n_frames=4,
                error=None,
                summary={
                    "ticks": 4,
                    "ue_count_mean": 2.0,
                    "dl_mbps_mean": 5.0
                }
            )

        with (
            patch.object(main, "_SIM_RUNS", {}),
            patch.object(main.store, "save_run") as save,
            patch.object(main, "_campaign_save"),
            patch.object(
                main, "_sim_job",
                new_callable=AsyncMock,
                side_effect=finish_sim
            )
        ):
            await main._campaign_run_one(
                record["id"], record, spec,
                expand(spec)[0], 0, None
            )

            run_id = record["runs"][0]["run_id"]

            self.assertEqual(
                main._SIM_RUNS[run_id]["owner_id"],
                "user-123"
            )

            self.assertEqual(
                save.call_args.args[0]["owner_id"],
                "user-123"
            )

            self.assertEqual(
                record["runs"][0]["verdict"],
                "no_thresholds"
            )


    async def test_fast_tier_completion_status(self):
        from test_sprint2_simulation import sample_frames

        config = example_config(42)
        params = main.nsoran_params(config)

        with patch(
            "tier_b.frames",
            return_value=sample_frames()
        ):
            result = await main._run_fast_tier(config, params, 2)

        self.assertEqual(result["status"], "done")
        self.assertEqual(result["n_frames"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
