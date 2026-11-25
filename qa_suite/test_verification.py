import asyncio
import os
import sys
import unittest
from starlette.testclient import TestClient

from unittest.mock import AsyncMock, MagicMock, patch

suite_directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if suite_directory not in sys.path:
    sys.path.insert(0, suite_directory)

from agent_core.navigation_driver import (
    BrowserSessionManager,
    HumanVerificationCoordinator,
    build_verification_controller,
    format_browser_connection_guidance,
    normalize_browser_mode,
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

    def test_browser_session_manager_creates_chromium_edge_browser(self):
        async def _test():
            manager = BrowserSessionManager()
            browser = await manager.acquire_session(background_operation=True)
            self.assertEqual(browser.kind, "chromium")
            self.assertTrue(browser.headless)
            self.assertTrue(browser.keep_alive)

        asyncio.run(_test())

    def test_browser_session_manager_attached_open_browser_mode_and_switch(self):
        async def _test():
            manager = BrowserSessionManager()
            with patch("agent_core.navigation_driver.find_edge_devtools_port", return_value=9222):
                attached = await manager.acquire_session(background_operation=False, browser_mode="attached")
                self.assertEqual(attached.kind, "cdp")
                self.assertEqual(attached.cdp_url, "http://127.0.0.1:9222")
                self.assertTrue(attached.keep_alive)
                self.assertEqual(manager.active_mode, "attached")

                # Switching back to isolated stops prior session cleanly
                attached.stop = AsyncMock()
                isolated = await manager.acquire_session(background_operation=False, browser_mode="isolated")
                attached.stop.assert_awaited_once()
                self.assertEqual(isolated.kind, "chromium")
                self.assertFalse(isolated.headless)
                self.assertEqual(manager.active_mode, "isolated")

        asyncio.run(_test())

    def test_normalize_browser_mode(self):
        self.assertEqual(normalize_browser_mode("attached"), "attached")
        self.assertEqual(normalize_browser_mode("open_browser"), "attached")
        self.assertEqual(normalize_browser_mode("edge"), "attached")
        self.assertEqual(normalize_browser_mode("isolated"), "isolated")
        self.assertEqual(normalize_browser_mode(""), "isolated")
        self.assertEqual(normalize_browser_mode(None), "isolated")

        # Chrome is strictly forbidden
        with self.assertRaises(ValueError):
            normalize_browser_mode("chrome")
        with self.assertRaises(ValueError):
            normalize_browser_mode("google-chrome")

    def test_format_browser_connection_guidance(self):
        setup_msg = format_browser_connection_guidance(
            "remote-debugging-setup: DevToolsActivePort not found for Microsoft Edge"
        )
        self.assertIsNotNone(setup_msg)
        self.assertIn("edge://inspect/#remote-debugging", setup_msg)
        self.assertNotIn("chrome://inspect", setup_msg)

        perm_msg = format_browser_connection_guidance("permission-blocked: Edge did not approve")
        self.assertIsNotNone(perm_msg)
        self.assertIn("Allow", perm_msg)

        not_running = format_browser_connection_guidance("edge-not-running: no running microsoft edge")
        self.assertIsNotNone(not_running)
        self.assertIn("Microsoft Edge", not_running)

        self.assertIsNone(format_browser_connection_guidance("Some unrelated ValueError"))

    def test_extract_task_conclusion_formats_browser_connection_guidance(self):
        fake_result = MagicMock()
        fake_result.final_result.return_value = None
        fake_result.text = None
        fake_result.output = None
        fake_result.messages = None
        fake_result.status = "error"
        fake_result.has_errors.return_value = True
        fake_result.error = "remote-debugging-setup: DevToolsActivePort not found for Microsoft Edge"

        summary, is_valid = BrowserSessionManager.extract_task_conclusion(fake_result)
        self.assertFalse(is_valid)
        self.assertIn("Open-Browser Attachment Setup Needed", summary)
        self.assertIn("edge://inspect/#remote-debugging", summary)


if __name__ == "__main__":
    unittest.main()
