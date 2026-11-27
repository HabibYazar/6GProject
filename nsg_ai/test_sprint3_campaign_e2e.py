
import os
import time
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import main
import store
from models import Base
from test_sprint2_simulation import example_config


class CampaignEndToEndTests(unittest.TestCase):

    def test_fast_campaign_seed_history_and_compare(self):
        engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool
        )
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)

        @contextmanager
        def isolated_session():
            with Session(engine) as session:
                try:
                    yield session
                    session.commit()
                except Exception:
                    session.rollback()
                    raise

        # Three distinct base stations and six UEs.
        baseline = example_config(42)
        baseline.name = "Sprint 3B baseline"

        for index, x in enumerate((350, 700), start=2):
            station = baseline.base_stations[0].model_copy(deep=True)
            station.id = f"gnb-{index}"
            station.name = f"Test gNB {index}"
            station.position.x = x
            station.position.y = 0
            baseline.base_stations.append(station)

        baseline.ue_groups[0].count = 6

        current = baseline.model_copy(deep=True)
        current.name = "Sprint 3B variant"
        current.ue_groups[0].count = 8

        def payload(config):
            return {
                "name": "Sprint3B-E2E-suite",
                "description": "Isolated Fast-tier integration test",
                "tier": "fast",
                "base_seed": 42,
                "wall_cap_s": 60,
                "partition_groups": 1,
                "thresholds": [
                    {
                        "metric": "ticks",
                        "op": ">=",
                        "value": 1
                    }
                ],
                "items": [
                    {
                        "label": "coverage",
                        "config": config.model_dump(mode="json"),
                        "frames": 4,
                        "repeats": 2
                    }
                ]
            }

        def await_campaign(client, campaign_id):
            for _ in range(400):
                response = client.get(
                    f"/campaigns/{campaign_id}"
                )
                self.assertEqual(
                    response.status_code, 200, response.text
                )

                record = response.json()

                if record["status"] != "running":
                    return record

                time.sleep(0.05)

            self.fail(f"Campaign did not complete: {campaign_id}")

        # A real slow-tier connection is forbidden in this test.
        with (
            patch.object(store, "session_scope", isolated_session),
            patch.object(main, "_SIM_RUNS", {}),
            patch.object(main, "_CAMPAIGNS", {}),
            patch.dict(os.environ, {"NSG_AUTH_REQUIRED": "0"}),
            patch.object(
                main,
                "run_batch_simulation",
                new_callable=AsyncMock,
                side_effect=AssertionError(
                    "Slow-tier engine must not be called"
                )
            ),
            patch.object(
                main.partition_live,
                "run_partitioned_live",
                new_callable=AsyncMock,
                side_effect=AssertionError(
                    "Partition engine must not be called"
                )
            )
        ):
            with TestClient(main.app) as client:

                campaign_ids = []

                for config in (baseline, current):
                    response = client.post(
                        "/campaigns", json=payload(config)
                    )

                    self.assertEqual(
                        response.status_code, 200, response.text
                    )

                    created = response.json()
                    self.assertEqual(created["planned_runs"], 2)

                    campaign_id = created["campaign_id"]
                    campaign_ids.append(campaign_id)

                    record = await_campaign(client, campaign_id)

                    self.assertEqual(
                        record["status"], "done", str(record)
                    )
                    self.assertEqual(
                        record["verdict"], "pass", str(record)
                    )

                    self.assertEqual(record["tier"], "fast")
                    self.assertEqual(record["partition_groups"], 1)
                    self.assertEqual(record["wall_cap_s"], 60.0)

                    self.assertEqual(
                        [run["seed"] for run in record["runs"]],
                        [42, 43]
                    )

                    for run in record["runs"]:
                        self.assertEqual(
                            run["status"], "done", str(run)
                        )
                        self.assertEqual(
                            run["verdict"], "pass",
                            str(run.get("error"))
                        )

                        run_response = client.get(
                            f"/sim/run/{run['run_id']}"
                        )

                        self.assertEqual(
                            run_response.status_code,
                            200,
                            run_response.text
                        )

                        dataset = run_response.json()

                        self.assertEqual(dataset["status"], "done")
                        self.assertEqual(len(dataset["frames"]), 4)

                        self.assertGreater(
                            dataset["summary"]["ue_count_mean"], 0
                        )

                    print(
                        "PASS | Campaign:",
                        campaign_id,
                        "| seeds: 42,43",
                        "| runs: 2"
                    )

                # Read saved campaigns through the persistence layer,
                # not just through the in-memory API records.
                stored = store.load_campaigns()
                self.assertEqual(len(stored), 2)

                for campaign_id in campaign_ids:
                    self.assertEqual(stored[campaign_id]["tier"], "fast")
                    self.assertEqual(
                        stored[campaign_id]["partition_groups"], 1
                    )
                    self.assertEqual(
                        stored[campaign_id]["wall_cap_s"], 60.0
                    )

                print("PASS | Campaigns persisted and read back")

                base_id, current_id = campaign_ids

                compare = client.get(
                    f"/campaigns/{current_id}/compare",
                    params={"to": base_id}
                )

                self.assertEqual(
                    compare.status_code, 200, compare.text
                )

                diff = compare.json()

                self.assertTrue(diff["seed_match"])
                self.assertTrue(diff["threshold_match"])
                self.assertEqual(len(diff["items"]), 1)
                self.assertEqual(
                    diff["items"][0]["status"], "matched"
                )
                self.assertGreater(
                    len(diff["items"][0]["metrics"]), 0
                )
                self.assertGreater(
                    diff["items"][0]["config_delta"]["total"], 0
                )

                print("PASS | Baseline comparison and config delta")

                history = client.get(
                    "/campaigns/history",
                    params={"name": "Sprint3B-E2E-suite"}
                )

                self.assertEqual(
                    history.status_code, 200, history.text
                )

                self.assertEqual(
                    len(history.json()["campaigns"]), 2
                )

                print("PASS | Experiment history: 2 campaigns")

                datasets = client.get(
                    f"/campaigns/{current_id}/datasets"
                )

                self.assertEqual(
                    datasets.status_code, 200, datasets.text
                )

                self.assertEqual(
                    len(datasets.json()["datasets"]), 2
                )

                print("PASS | Campaign KPM datasets exported")
                print("SPRINT 3B FAST-TIER E2E VERIFIED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
