import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import auth
import main


class UserIsolationTests(unittest.TestCase):

    def setUp(self):
        def start(patcher):
            mock = patcher.start()
            self.addCleanup(patcher.stop)
            return mock

        start(patch.dict(os.environ, {
            "NSG_AUTH_REQUIRED": "1",
            "NSG_AUTH_SECRET": "isolated-test-secret-not-for-production",
        }))

        self.users = {
            "a": {"id": "a", "username": "user_a",
                  "role": "user", "disabled": False},
            "b": {"id": "b", "username": "user_b",
                  "role": "user", "disabled": False},
            "admin": {"id": "admin", "username": "admin",
                      "role": "admin", "disabled": False},
        }

        start(patch.object(
            main.store, "get_user",
            side_effect=lambda uid: self.users.get(uid)
        ))
        start(patch.object(
            main.store, "usernames_by_id",
            return_value={k: v["username"] for k, v in self.users.items()}
        ))

        scenarios = [
            {"id": "sa", "name": "Scenario A", "owner_id": "a"},
            {"id": "sb", "name": "Scenario B", "owner_id": "b"},
            {"id": "sl", "name": "Legacy", "owner_id": None},
        ]
        start(patch.object(
            main.store, "load_scenarios",
            side_effect=lambda: [dict(x) for x in scenarios]
        ))
        self.delete = start(patch.object(
            main.store, "delete_scenario", return_value=True
        ))

        runs = {
            "ra": {"id": "ra", "owner_id": "a", "status": "done"},
            "rb": {"id": "rb", "owner_id": "b", "status": "done"},
            "rl": {"id": "rl", "owner_id": None, "status": "done"},
        }
        start(patch.object(main, "_SIM_RUNS", dict(runs)))
        start(patch.object(
            main.store, "load_run",
            side_effect=lambda rid: dict(runs[rid]) if rid in runs else None
        ))

        campaigns = {
            key: {
                "id": key, "name": key, "owner_id": owner,
                "status": "done", "runs": [], "created_at": "2026-11-27"
            }
            for key, owner in [("ca", "a"), ("cb", "b"), ("cl", None)]
        }
        start(patch.object(main, "_CAMPAIGNS", campaigns))
        self.datasets = start(patch.object(
            main.store, "load_campaign_datasets", return_value={}
        ))

        start(patch.object(main.store, "list_users", return_value=[]))

        self.headers = {
            k: {"Authorization": "Bearer " + auth.create_token(
                k, u["username"], u["role"]
            )}
            for k, u in self.users.items()
        }
        self.client = TestClient(main.app)

    def get(self, path, user):
        return self.client.get(path, headers=self.headers[user])

    def test_anonymous_access_rejected(self):
        for path in ("/scenarios", "/campaigns", "/sim/runs", "/ai/status"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_invalid_token_rejected(self):
        r = self.client.get(
            "/scenarios",
            headers={"Authorization": "Bearer invalid.jwt.token"}
        )
        self.assertEqual(r.status_code, 401)

    def test_scenario_visibility(self):
        for user, expected in [
            ("a", {"sa"}), ("b", {"sb"}), ("admin", {"sl"})
        ]:
            r = self.get("/scenarios", user)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(
                {x["id"] for x in r.json()["scenarios"]}, expected
            )

    def test_admin_all_scenarios(self):
        r = self.get("/scenarios?all=true", "admin")
        self.assertEqual(
            {x["id"] for x in r.json()["scenarios"]},
            {"sa", "sb", "sl"}
        )

    def test_regular_user_cannot_escalate(self):
        r = self.get("/scenarios?all=true", "b")
        self.assertEqual(
            {x["id"] for x in r.json()["scenarios"]}, {"sb"}
        )

    def test_foreign_scenario_cannot_be_deleted(self):
        r = self.client.delete("/scenarios/sa", headers=self.headers["b"])
        self.assertEqual(r.status_code, 404)
        self.delete.assert_not_called()

        r = self.client.delete("/scenarios/sa", headers=self.headers["a"])
        self.assertEqual(r.status_code, 200)
        self.delete.assert_called_once_with("sa")

    def test_run_visibility(self):
        for user, expected in [
            ("a", {"ra"}), ("b", {"rb"}), ("admin", {"rl"})
        ]:
            r = self.get("/sim/runs", user)
            self.assertEqual(
                {x["id"] for x in r.json()["runs"]}, expected
            )

        r = self.get("/sim/runs?all=true", "admin")
        self.assertEqual(
            {x["id"] for x in r.json()["runs"]},
            {"ra", "rb", "rl"}
        )

    def test_foreign_run_returns_404(self):
        self.assertEqual(self.get("/sim/run/ra", "b").status_code, 404)
        self.assertEqual(self.get("/sim/run/ra", "a").status_code, 200)
        self.assertEqual(self.get("/sim/run/rl", "a").status_code, 404)

    def test_campaign_visibility(self):
        for user, expected in [
            ("a", {"ca"}), ("b", {"cb"}), ("admin", {"cl"})
        ]:
            r = self.get("/campaigns", user)
            self.assertEqual(
                {x["id"] for x in r.json()["campaigns"]}, expected
            )

        r = self.get("/campaigns?all=true", "admin")
        self.assertEqual(
            {x["id"] for x in r.json()["campaigns"]},
            {"ca", "cb", "cl"}
        )
        self.assertEqual(
            {x["id"] for x in self.get(
                "/campaigns?all=true", "b"
            ).json()["campaigns"]},
            {"cb"}
        )

    def test_foreign_campaign_endpoints(self):
        self.assertEqual(self.get("/campaigns/ca", "b").status_code, 404)
        self.assertEqual(
            self.get("/campaigns/ca/datasets", "b").status_code, 404
        )
        self.datasets.assert_not_called()

        self.assertEqual(
            self.client.post(
                "/campaigns/ca/stop", headers=self.headers["b"]
            ).status_code, 404
        )
        self.assertEqual(
            self.client.delete(
                "/campaigns/ca", headers=self.headers["b"]
            ).status_code, 404
        )
        self.assertEqual(
            self.get("/campaigns/ca/compare?to=cb", "a").status_code,
            404
        )
        self.assertEqual(self.get("/campaigns/ca", "a").status_code, 200)

    def test_admin_only_endpoint(self):
        self.assertEqual(
            self.get("/auth/users", "a").status_code, 403
        )
        self.assertEqual(
            self.get("/auth/users", "admin").status_code, 200
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
