import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from starlette.testclient import TestClient

from unittest.mock import AsyncMock, MagicMock, patch

suite_directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if suite_directory not in sys.path:
    sys.path.insert(0, suite_directory)

from agent_core.navigation_driver import (
    BrowserSessionManager,
    HumanVerificationCoordinator,
    build_verification_controller,
    ensure_attached_working_tab,
    ensure_edge_debugging_ready,
    find_edge_devtools_endpoint,
    find_edge_devtools_port,
    format_browser_connection_guidance,
    normalize_browser_mode,
    resolve_edge_cdp_url,
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
                self.assertTrue(attached.cdp_url.startswith("ws://127.0.0.1:9222") or attached.cdp_url == "http://127.0.0.1:9222")
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

        ws_timeout_msg = format_browser_connection_guidance("CDP WS handshake failed: timed out during opening handshake")
        self.assertIsNotNone(ws_timeout_msg)
        self.assertIn("Allow", ws_timeout_msg)

        not_running = format_browser_connection_guidance("edge-not-running: no running microsoft edge")
        self.assertIsNotNone(not_running)
        self.assertIn("Microsoft Edge", not_running)

        self.assertIsNone(format_browser_connection_guidance("Some unrelated ValueError"))

    def test_find_edge_devtools_endpoint_and_resolve_ws_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            edge_dir = Path(tmpdir)
            (edge_dir / "DevToolsActivePort").write_text("9333\n/devtools/browser/abc-123\n", encoding="utf-8")

            fake_conn = MagicMock()
            fake_conn.__enter__.return_value = fake_conn
            fake_conn.__exit__.return_value = False

            with (
                patch("agent_core.navigation_driver.get_edge_user_data_dirs", return_value=[edge_dir]),
                patch("socket.create_connection", return_value=fake_conn),
            ):
                endpoint = find_edge_devtools_endpoint()
                self.assertEqual(endpoint, (9333, "/devtools/browser/abc-123", edge_dir))
                self.assertEqual(find_edge_devtools_port(), 9333)
                self.assertEqual(resolve_edge_cdp_url(9333), "ws://127.0.0.1:9333/devtools/browser/abc-123")
                self.assertEqual(resolve_edge_cdp_url(9222), "http://127.0.0.1:9222")

    def test_ensure_edge_debugging_ready_launches_when_closed_with_toggle(self):
        async def _test():
            ports = iter([None, None, 9444])
            statuses = []
            with (
                patch("agent_core.navigation_driver.find_edge_devtools_port", side_effect=lambda: next(ports, 9444)),
                patch("agent_core.navigation_driver.is_edge_running", return_value=False),
                patch("agent_core.navigation_driver.has_edge_remote_debugging_toggle", return_value=True),
                patch("agent_core.navigation_driver.launch_user_edge", return_value=True) as mock_launch,
            ):
                port = await ensure_edge_debugging_ready(on_status=statuses.append, wait_timeout=2.0)
                self.assertEqual(port, 9444)
                mock_launch.assert_called_once_with()
                self.assertTrue(any("Microsoft Edge" in s for s in statuses))

        asyncio.run(_test())

    def test_ensure_edge_debugging_ready_fails_fast_when_toggle_off(self):
        async def _test():
            with (
                patch("agent_core.navigation_driver.find_edge_devtools_port", return_value=None),
                patch("agent_core.navigation_driver.is_edge_running", return_value=True),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    await ensure_edge_debugging_ready(wait_timeout=0.5)
                self.assertIn("remote-debugging-setup", str(ctx.exception))

        asyncio.run(_test())

    def test_ensure_edge_debugging_ready_cancels_on_stop_event(self):
        async def _test():
            stop_event = asyncio.Event()
            stop_event.set()
            with (
                patch("agent_core.navigation_driver.find_edge_devtools_port", return_value=None),
                patch("agent_core.navigation_driver.is_edge_running", return_value=True),
                patch("agent_core.navigation_driver.open_edge_inspect_page", return_value=True),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await ensure_edge_debugging_ready(stop_event=stop_event, wait_timeout=2.0)

        asyncio.run(_test())

    def test_ensure_attached_working_tab_protects_ui_tab(self):
        async def _test():
            fake_browser = MagicMock()
            fake_browser.kind = "cdp"
            fake_browser.start = AsyncMock()
            fake_browser._daemon_name = MagicMock(return_value="bu1234")

            cdp_calls = []
            req_calls = []

            def _fake_request(name, req, timeout=30):
                req_calls.append((name, req))
                if req.get("meta") == "current_tab":
                    return {"targetId": "ui-tab-1", "url": "http://127.0.0.1:5000/"}
                return {"session_id": "new-sid-2"}

            def _fake_cdp(name, method, session_id=None, **params):
                cdp_calls.append((method, params))
                if method == "Target.createTarget":
                    return {"targetId": "work-tab-2"}
                if method == "Target.attachToTarget":
                    return {"sessionId": "new-sid-2"}
                return {}

            with (
                patch("browser_use.browser.browser._daemon_request", side_effect=_fake_request),
                patch("browser_use.browser.browser._daemon_cdp", side_effect=_fake_cdp),
            ):
                await ensure_attached_working_tab(fake_browser, ui_netloc="127.0.0.1:5000")

            fake_browser.start.assert_awaited_once()
            self.assertEqual(fake_browser._agent_tab, "work-tab-2")
            self.assertEqual(cdp_calls[0], ("Target.createTarget", {"url": "about:blank", "background": False}))
            self.assertEqual(cdp_calls[1], ("Target.attachToTarget", {"targetId": "work-tab-2", "flatten": True}))
            self.assertEqual(cdp_calls[2], ("Target.activateTarget", {"targetId": "work-tab-2"}))
            self.assertEqual(req_calls[1], ("bu1234", {"meta": "set_session", "session_id": "new-sid-2", "target_id": "work-tab-2"}))

        asyncio.run(_test())

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
