import unittest
from contextlib import contextmanager
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import store
from models import Base, Campaign


class CampaignPersistenceTests(unittest.TestCase):

    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)

        @contextmanager
        def isolated_session():
            with Session(self.engine) as session:
                try:
                    yield session
                    session.commit()
                except Exception:
                    session.rollback()
                    raise

        patcher = patch.object(
            store, "session_scope", isolated_session
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)

    def record(self, identifier, **extras):
        return {
            "id": identifier,
            "name": "Sprint 3 experiment",
            "description": "",
            "status": "done",
            "verdict": "pass",
            "base_seed": 42,
            "thresholds": [],
            "planned": [],
            "runs": [],
            **extras
        }

    def test_settings_survive_database_roundtrip(self):
        store.save_campaign(self.record(
            "new-campaign",
            tier="fast",
            partition_groups=1,
            wall_cap_s=120.5
        ))

        record = store.load_campaigns()["new-campaign"]

        self.assertEqual(record["tier"], "fast")
        self.assertEqual(record["partition_groups"], 1)
        self.assertEqual(record["wall_cap_s"], 120.5)

    def test_legacy_settings_remain_unknown(self):
        store.save_campaign(self.record("legacy-campaign"))

        record = store.load_campaigns()["legacy-campaign"]

        self.assertIsNone(record["tier"])
        self.assertIsNone(record["partition_groups"])
        self.assertIsNone(record["wall_cap_s"])

    def test_updating_campaign_preserves_settings(self):
        record = self.record(
            "updated-campaign",
            tier="slow",
            partition_groups=3,
            wall_cap_s=900.0
        )

        store.save_campaign(record)

        record["status"] = "done"
        record["verdict"] = "pass"
        store.save_campaign(record)

        saved = store.load_campaigns()["updated-campaign"]

        self.assertEqual(saved["tier"], "slow")
        self.assertEqual(saved["partition_groups"], 3)
        self.assertEqual(saved["wall_cap_s"], 900.0)

    def test_columns_are_nullable(self):
        for name in ("tier", "partition_groups", "wall_cap_s"):
            self.assertTrue(Campaign.__table__.c[name].nullable)


if __name__ == "__main__":
    unittest.main(verbosity=2)
