import asyncio
import json
import logging
import os
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

import auth
import main

logging.disable(logging.CRITICAL)


class APIErrorTests(unittest.TestCase):

    def setUp(self):
        self.env = patch.dict(os.environ, {"NSG_AUTH_REQUIRED": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(main.app, raise_server_exceptions=False)

    def test_404(self):
        r = self.client.get("/nonexistent-api-route")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], "not_found")

    def test_401(self):
        with patch.dict(os.environ, {"NSG_AUTH_REQUIRED": "1"}):
            r = self.client.get("/scenarios")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "unauthorized")

    def test_403(self):
        user = {
            "id": "test-user",
            "username": "test-user",
            "role": "user",
            "disabled": False,
        }
        with patch.dict(os.environ, {
            "NSG_AUTH_REQUIRED": "1",
            "NSG_AUTH_SECRET": "api-error-test-secret",
        }):
            token = auth.create_token(
                user["id"], user["username"], user["role"]
            )
            with patch.object(main.store, "get_user", return_value=user):
                r = self.client.get(
                    "/auth/users",
                    headers={"Authorization": f"Bearer {token}"}
                )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "forbidden")

    def test_422_does_not_echo_input(self):
        r = self.client.post(
            "/generate-report",
            json={
                "tput": "sensitive-test-value",
                "power": 1,
                "prb": 50,
                "qos": 80,
            }
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["code"], "validation_error")
        self.assertNotIn("sensitive-test-value", r.text)

    def test_500_does_not_expose_server_details(self):
        with patch.object(
            main.store, "usernames_by_id", return_value={}
        ), patch.object(
            main.store, "load_scenarios",
            side_effect=RuntimeError("database-password-secret")
        ):
            r = self.client.get("/scenarios")

        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.json()["code"], "internal_error")
        self.assertNotIn("database-password-secret", r.text)

    def test_502_does_not_expose_upstream_details(self):
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/test",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("localhost", 80),
        }
        response = asyncio.run(main.api_http_exception(
            Request(scope),
            StarletteHTTPException(
                status_code=502, detail="private-upstream-address"
            )
        ))
        body = json.loads(response.body)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(body["code"], "upstream_error")
        self.assertNotIn("private-upstream-address", str(body))

    def test_503_ai_unavailable(self):
        agent = Mock()
        agent.generate_text.side_effect = main.AIUnavailableError(
            "private-provider-credential"
        )
        with patch.object(main, "_agent", agent):
            r = self.client.post(
                "/generate-report",
                json={"tput": 1, "power": 10, "prb": 50, "qos": 80}
            )
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["code"], "service_unavailable")
        self.assertNotIn("private-provider-credential", r.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
