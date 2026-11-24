import asyncio
import os
import sys
import unittest
from starlette.testclient import TestClient

suite_directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if suite_directory not in sys.path:
    sys.path.insert(0, suite_directory)

from agent_core.navigation_driver import (
    HumanVerificationCoordinator,
    build_verification_controller,
)
from backend.gateway import build_server_app


class TestHumanVerificationFlow(unittest.TestCase):
    def setUp(self):
        self.coordinator = HumanVerificationCoordinator.get_coordinator()
        self.coordinator.reset()

    def tearDown(self):
        self.coordinator.reset()

    def test_singleton_coordination(self):
        c1 = HumanVerificationCoordinator.get_coordinator()
        c2 = HumanVerificationCoordinator.get_coordinator()
        self.assertIs(c1, c2)

    def test_empty_resolve_returns_false(self):
        self.assertFalse(self.coordinator.resolve_verification())

    def test_pause_and_resolve_flow(self):
        async def _test():
            notified_events = []

            def _listener(evt):
                notified_events.append(evt)

            self.coordinator.register_listener(_listener)

            async def _worker():
                return await self.coordinator.pause_for_verification("reCAPTCHA traffic lights detected")

            worker_task = asyncio.create_task(_worker())
            await asyncio.sleep(0.05)

            self.assertTrue(self.coordinator.is_waiting)
            self.assertEqual(len(notified_events), 1)
            self.assertEqual(notified_events[0]["type"], "verification_required")
            self.assertIn("reCAPTCHA traffic lights", notified_events[0]["reason"])

            # Resolve verification
            resolved = self.coordinator.resolve_verification()
            self.assertTrue(resolved)

            outcome_msg = await worker_task
            self.assertIn("Human verification confirmed", outcome_msg)
            self.assertFalse(self.coordinator.is_waiting)

        asyncio.run(_test())

    def test_controller_action_creation(self):
        controller = build_verification_controller()
        self.assertIsNotNone(controller)

    def test_resume_endpoint_without_active_verification(self):
        client = TestClient(build_server_app())
        resp = client.post("/api/resume-verification", json={})
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertFalse(payload["success"])
        self.assertIn("No active verification", payload["message"])

    def test_resume_endpoint_with_active_verification(self):
        async def _test():
            client = TestClient(build_server_app())

            async def _worker():
                return await self.coordinator.pause_for_verification("Cloudflare challenge")

            worker_task = asyncio.create_task(_worker())
            await asyncio.sleep(0.05)

            # Send POST to resume-verification
            resp = client.post("/api/resume-verification", json={})
            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            self.assertTrue(payload["success"])
            self.assertIn("confirmed", payload["message"])

            result_str = await worker_task
            self.assertIn("confirmed", result_str)

        asyncio.run(_test())


if __name__ == "__main__":
    unittest.main()
