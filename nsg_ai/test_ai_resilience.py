import logging
import unittest
from unittest.mock import Mock
import httpx
from fastapi.testclient import TestClient
from openai import AuthenticationError

import main
from llm_agent import NSGAgent, ai_configured, decide_xapp_actions

logging.disable(logging.CRITICAL)


class AIResilienceTests(unittest.TestCase):

    def setUp(self):
        main._agent = NSGAgent()
        self.client = TestClient(main.app)

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)

    def test_ai_unconfigured(self):
        self.assertFalse(ai_configured())
        r = self.client.get("/ai/status")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["configured"])

    def test_ai_modify_without_key(self):
        r = self.client.post(
            "/ai/modify",
            json={"prompt": "Generate a simulation"}
        )
        self.assertEqual(r.status_code, 503)

    def test_report_without_key(self):
        r = self.client.post(
            "/generate-report",
            json={"tput": 1, "power": 10, "prb": 50, "qos": 80}
        )
        self.assertEqual(r.status_code, 503)

    def test_invalid_key(self):
        response = httpx.Response(
            401,
            request=httpx.Request(
                "POST",
                "https://openrouter.ai/api/v1/chat/completions"
            )
        )

        agent = Mock()
        agent.modify_configuration.side_effect = AuthenticationError(
            "Invalid API key",
            response=response,
            body={"error": "invalid_api_key"}
        )
        main._agent = agent

        r = self.client.post(
            "/ai/modify",
            json={"prompt": "Generate a simulation"}
        )
        self.assertEqual(r.status_code, 503)

    def test_normal_api_without_ai(self):
        r = self.client.get("/schema")
        self.assertEqual(r.status_code, 200)

    def test_xapp_without_key(self):
        result = decide_xapp_actions("Optimize QoS", {})
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
