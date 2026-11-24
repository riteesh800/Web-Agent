import unittest
import os
import sys
from starlette.testclient import TestClient

suite_directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if suite_directory not in sys.path:
    sys.path.insert(0, suite_directory)

from backend.gateway import build_server_app


class TestHttpGateway(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway_app = build_server_app()
        cls.http_tester = TestClient(cls.gateway_app)

    def test_index_serves_html(self):
        http_packet = self.http_tester.get("/")
        self.assertEqual(http_packet.status_code, 200)
        self.assertIn("text/html", http_packet.headers["content-type"])
        self.assertIn("<title>Web Agent", http_packet.text)

    def test_brand_mark_endpoint(self):
        http_packet = self.http_tester.get("/media/brand_mark.png")
        self.assertEqual(http_packet.status_code, 200)
        self.assertEqual(http_packet.headers["content-type"], "image/png")

    def test_system_health_endpoint(self):
        http_packet = self.http_tester.get("/api/health")
        self.assertEqual(http_packet.status_code, 200)
        health_payload = http_packet.json()
        self.assertEqual(health_payload["status"], "online")
        self.assertIn("providers", health_payload)

    def test_save_token_guard(self):
        http_packet = self.http_tester.post("/api/save-key", json={"key": "   "})
        self.assertEqual(http_packet.status_code, 400)
        self.assertIn("error", http_packet.json())

    def test_chat_empty_query_guard(self):
        http_packet = self.http_tester.post("/api/chat", json={"message": ""})
        self.assertEqual(http_packet.status_code, 400)
        self.assertIn("error", http_packet.json())

    def test_browser_run_missing_directive_guard(self):
        http_packet = self.http_tester.get("/api/run?task=")
        self.assertEqual(http_packet.status_code, 200)
        self.assertIn("text/event-stream", http_packet.headers["content-type"])
        self.assertIn("Task directive is required", http_packet.text)


if __name__ == "__main__":
    unittest.main()
