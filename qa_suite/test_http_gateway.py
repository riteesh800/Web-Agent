import asyncio
import json
import shutil
import subprocess
import unittest
import os
import sys
from unittest.mock import MagicMock, patch
from starlette.testclient import TestClient

suite_directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if suite_directory not in sys.path:
    sys.path.insert(0, suite_directory)

from agent_core.navigation_driver import BrowserSessionManager
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

    def test_get_keys_endpoint(self):
        http_packet = self.http_tester.get("/api/keys")
        self.assertEqual(http_packet.status_code, 200)
        payload = http_packet.json()
        self.assertIn("openrouter", payload)
        self.assertIn("gemini", payload)
        self.assertIn("groq", payload)

    def test_test_key_empty_guard(self):
        http_packet = self.http_tester.post("/api/test-key", json={"provider": "groq", "key": ""})
        self.assertEqual(http_packet.status_code, 200)
        payload = http_packet.json()
        self.assertFalse(payload["success"])
        self.assertIn("Please enter an API key", payload["error"])

    def test_test_key_invalid_provider_returns_error(self):
        http_packet = self.http_tester.post("/api/test-key", json={"provider": "unknown_provider", "key": "some_key"})
        self.assertEqual(http_packet.status_code, 200)
        payload = http_packet.json()
        self.assertFalse(payload["success"])
        self.assertIn("Unknown provider", payload["error"])

    def test_save_key_with_provider_endpoint(self):
        original_groq = os.getenv("GROQ_API_KEY", "")
        try:
            http_packet = self.http_tester.post("/api/save-key", json={"provider": "groq", "key": "gsk_unittest_sample_key"})
            self.assertEqual(http_packet.status_code, 200)
            payload = http_packet.json()
            self.assertTrue(payload["success"])
            self.assertEqual(payload["provider"], "Groq")
        finally:
            if original_groq:
                self.http_tester.post("/api/save-key", json={"provider": "groq", "key": original_groq})

    def test_frontend_script_served_and_valid_syntax(self):
        http_packet = self.http_tester.get("/static/scripts/orchestrator_ui.js")
        self.assertEqual(http_packet.status_code, 200)
        self.assertIn("function triggerBrowserWorkflow()", http_packet.text)
        node_bin = shutil.which("node")
        if node_bin:
            script_path = os.path.join(suite_directory, "frontend", "scripts", "orchestrator_ui.js")
            proc = subprocess.run([node_bin, "--check", script_path], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"JS syntax check failed:\n{proc.stderr}")

    def test_browser_status_endpoint(self):
        with patch(
            "backend.gateway.detect_open_browser_debugging",
            return_value={"ready": True, "ambiguous": False, "browsers": ["Microsoft Edge"], "dirs": ["C:/Edge"]},
        ):
            resp = self.http_tester.get("/api/browser-status")
            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            self.assertTrue(payload["ready"])
            self.assertFalse(payload["ambiguous"])
            self.assertEqual(payload["browsers"], ["Microsoft Edge"])

    def test_browser_run_attached_mode_wires_attached_session(self):
        captured_calls = []

        class FakeOutcome:
            status = "completed"
            steps = 1
            error = None

            def final_result(self):
                return "Gmail inbox checked."

            def has_errors(self):
                return False

        class FakeAgent:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def run(self):
                return FakeOutcome()

        manager = BrowserSessionManager.obtain_manager()
        original_session = manager.active_session
        original_mode = manager.active_mode
        manager.active_session = None
        manager.active_mode = None

        async def _fake_acquire(background_operation=False, browser_mode="isolated"):
            captured_calls.append((background_operation, browser_mode))
            return MagicMock()

        async def _run():
            with (
                patch("backend.gateway.construct_agent_llm", return_value=MagicMock()),
                patch("backend.gateway.Agent", FakeAgent),
                patch("backend.gateway.TaskHistoryDatabase.record_task"),
                patch.object(manager, "acquire_session", side_effect=_fake_acquire),
            ):
                from starlette.requests import Request
                from backend.gateway import browser_agent_stream

                scope = {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/run",
                    "query_string": b"task=open+gmail&background_mode=true&browser_mode=attached",
                    "headers": [],
                }
                response = await browser_agent_stream(Request(scope))
                events = []
                async for chunk in response.body_iterator:
                    text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                    for line in text.splitlines():
                        if line.startswith("data: "):
                            events.append(json.loads(line[6:]))

                self.assertEqual(captured_calls, [(False, "attached")])
                self.assertTrue(any("Attaching to your open browser session" in evt.get("text", "") for evt in events))
                self.assertTrue(any(evt.get("type") == "result" and evt.get("success") for evt in events))

        try:
            asyncio.run(_run())
        finally:
            manager.active_session = original_session
            manager.active_mode = original_mode

    def test_frontend_open_browser_toggle_behavior(self):
        index_resp = self.http_tester.get("/")
        self.assertEqual(index_resp.status_code, 200)
        self.assertIn('id="useOpenBrowserCheckbox"', index_resp.text)
        self.assertIn('id="openBrowserSetupNotice"', index_resp.text)

        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("Node.js runtime not available for frontend open-browser verification")

        script_path = os.path.join(suite_directory, "frontend", "scripts", "orchestrator_ui.js")
        node_harness = r"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync(process.argv[1], 'utf8');

function createElement(id) {
    return {
        id,
        value: '',
        checked: false,
        disabled: false,
        dataset: {},
        textContent: '',
        innerHTML: '',
        className: '',
        style: { display: 'none' },
        children: [],
        scrollTop: 0,
        scrollHeight: 0,
        appendChild(c) { this.children.push(c); },
        addEventListener() {},
        focus() {},
        scrollIntoView() {},
    };
}

const elements = {};
for (const id of [
    'taskDirectiveInputBox', 'backgroundModeCheckbox', 'useOpenBrowserCheckbox',
    'openBrowserSetupNotice', 'openBrowserStatusBadge', 'startExecutionButton',
    'stopExecutionButton', 'liveTerminalCard', 'summaryDisplayCard',
    'summaryContentContainer', 'summaryStatusHeading', 'humanVerificationCard',
    'verificationPromptMessage', 'btnConfirmVerification', 'logsDisplaySection',
    'taskHistoryContainer', 'taskHistoryList', 'chatQueryInputBox',
]) {
    elements[id] = createElement(id);
}

const streams = [];
class MockEventSource {
    constructor(url) { this.url = url; streams.push(this); }
    close() {}
}

const sandbox = {
    console, JSON, encodeURIComponent, setTimeout, clearTimeout,
    alert(m) { throw new Error(m); },
    marked: { parse: (t) => t },
    EventSource: MockEventSource,
    fetch: async (url) => {
        if (url === '/api/browser-status') {
            return { ok: true, json: async () => ({ ready: true, ambiguous: false, browsers: ['Microsoft Edge'] }) };
        }
        return { ok: true, json: async () => ({ history: [] }) };
    },
    document: {
        getElementById: (id) => elements[id] || null,
        createElement: (tag) => createElement(tag),
        addEventListener: () => {},
    },
};

vm.runInNewContext(source, sandbox);

(async () => {
    elements.backgroundModeCheckbox.checked = true;
    elements.useOpenBrowserCheckbox.checked = true;
    sandbox.onOpenBrowserModeChanged();
    await new Promise((r) => setTimeout(r, 15));

    assert.strictEqual(elements.backgroundModeCheckbox.checked, false);
    assert.strictEqual(elements.backgroundModeCheckbox.disabled, true);
    assert.strictEqual(elements.openBrowserSetupNotice.style.display, 'flex');
    assert.ok(elements.openBrowserStatusBadge.textContent.includes('Microsoft Edge'));

    elements.taskDirectiveInputBox.value = 'Open Gmail';
    sandbox.triggerBrowserWorkflow();
    assert.strictEqual(streams.length, 1);
    assert.ok(streams[0].url.includes('browser_mode=attached'));
    assert.ok(streams[0].url.includes('background_mode=false'));

    elements.useOpenBrowserCheckbox.checked = false;
    sandbox.onOpenBrowserModeChanged();
    assert.strictEqual(elements.backgroundModeCheckbox.disabled, false);
    assert.strictEqual(elements.backgroundModeCheckbox.checked, true);
    assert.strictEqual(elements.openBrowserSetupNotice.style.display, 'none');
    console.log('FRONTEND_OPEN_BROWSER_OK');
})().catch((err) => { console.error(err); process.exit(1); });
"""
        proc = subprocess.run([node_bin, "-e", node_harness, script_path], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Frontend verification failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("FRONTEND_OPEN_BROWSER_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
