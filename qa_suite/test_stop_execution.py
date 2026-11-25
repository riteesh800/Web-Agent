import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from starlette.testclient import TestClient

suite_directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if suite_directory not in sys.path:
    sys.path.insert(0, suite_directory)

from agent_core.navigation_driver import (
    BrowserSessionManager,
    HumanVerificationCoordinator,
    TaskExecutionCoordinator,
)
from core_config.history_db import TaskHistoryDatabase
from backend.gateway import build_server_app


class TestStopExecution(unittest.TestCase):
    def setUp(self):
        self._original_db_path = TaskHistoryDatabase.DB_PATH
        self._temp_dir = tempfile.TemporaryDirectory()
        TaskHistoryDatabase.DB_PATH = Path(self._temp_dir.name) / "test_stop_history.db"
        TaskExecutionCoordinator.get_coordinator().reset()
        HumanVerificationCoordinator.get_coordinator().reset()

    def tearDown(self):
        TaskExecutionCoordinator.get_coordinator().reset()
        HumanVerificationCoordinator.get_coordinator().reset()
        TaskHistoryDatabase.DB_PATH = self._original_db_path
        self._temp_dir.cleanup()

    def test_stop_endpoint_when_idle(self):
        client = TestClient(build_server_app())
        resp = client.post("/api/stop", json={})
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertFalse(payload["success"])
        self.assertIn("No active task", payload["message"])

    def test_coordinator_stops_active_task_verification_and_browser(self):
        async def _test():
            coordinator = TaskExecutionCoordinator.get_coordinator()
            verif = HumanVerificationCoordinator.get_coordinator()
            manager = BrowserSessionManager.obtain_manager()

            mock_browser = MagicMock()
            mock_browser.stop = AsyncMock()
            manager.active_session = mock_browser

            run_id, stop_event = coordinator.begin_run()

            async def _long_running_agent():
                await verif.pause_for_verification("CAPTCHA pause")
                await asyncio.sleep(60)

            worker = asyncio.create_task(_long_running_agent())
            coordinator.attach_task(run_id, worker)
            await asyncio.sleep(0.05)

            self.assertTrue(verif.is_waiting)
            self.assertFalse(worker.done())

            stopped = await coordinator.request_stop(terminate_browser=True)
            self.assertTrue(stopped)
            self.assertTrue(stop_event.is_set())
            self.assertTrue(worker.done())
            self.assertFalse(verif.is_waiting)
            mock_browser.stop.assert_awaited_once()
            self.assertIsNone(manager.active_session)

            coordinator.finish_run(run_id)
            self.assertFalse(coordinator.is_running)

        asyncio.run(_test())

    def test_stop_endpoint_cancels_running_sse_task_immediately(self):
        started_event = asyncio.Event()
        cancelled_flag = {"cancelled": False}

        class FakeAgent:
            def __init__(self, **kwargs):
                pass

            async def run(self):
                started_event.set()
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    cancelled_flag["cancelled"] = True
                    raise

        mock_browser = MagicMock()
        mock_browser.stop = AsyncMock()
        manager = BrowserSessionManager.obtain_manager()
        manager.active_session = None

        async def _fake_acquire_session(background_operation=False):
            manager.active_session = mock_browser
            return mock_browser

        async def _run_scenario():
            with (
                patch("backend.gateway.construct_agent_llm", return_value=MagicMock()),
                patch("backend.gateway.Agent", FakeAgent),
                patch.object(manager, "acquire_session", side_effect=_fake_acquire_session),
            ):
                from starlette.requests import Request
                from backend.gateway import browser_agent_stream, stop_browser_task_action

                scope = {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/run",
                    "query_string": b"task=test+long+task&background_mode=true",
                    "headers": [],
                }
                req = Request(scope)
                response = await browser_agent_stream(req)

                collected_events = []

                async def _consume_stream():
                    async for chunk in response.body_iterator:
                        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                        for line in text.splitlines():
                            if line.startswith("data: "):
                                collected_events.append(json.loads(line[6:]))

                stream_task = asyncio.create_task(_consume_stream())
                await asyncio.wait_for(started_event.wait(), timeout=2.0)

                stop_scope = {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/stop",
                    "query_string": b"",
                    "headers": [],
                }
                stop_resp = await stop_browser_task_action(Request(stop_scope))
                stop_payload = json.loads(stop_resp.body.decode("utf-8"))
                self.assertTrue(stop_payload["success"])

                await asyncio.wait_for(stream_task, timeout=3.0)

                event_types = [evt["type"] for evt in collected_events]
                self.assertIn("status", event_types)
                self.assertIn("stopped", event_types)
                self.assertEqual(event_types[-1], "done")
                self.assertTrue(cancelled_flag["cancelled"])
                mock_browser.stop.assert_awaited()

        asyncio.run(_run_scenario())

    def test_frontend_stop_button_html_and_behavior(self):
        client = TestClient(build_server_app())
        index_resp = client.get("/")
        self.assertEqual(index_resp.status_code, 200)
        self.assertIn('id="startExecutionButton"', index_resp.text)
        self.assertIn('id="stopExecutionButton"', index_resp.text)
        self.assertIn('onclick="stopBrowserWorkflow()"', index_resp.text)

        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("Node.js runtime not available for frontend stop verification")

        script_path = os.path.join(suite_directory, "frontend", "scripts", "orchestrator_ui.js")
        node_harness = r"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const scriptPath = process.argv[1];
const source = fs.readFileSync(scriptPath, 'utf8');

function createElement(id) {
    return {
        id,
        value: '',
        checked: false,
        disabled: id === 'stopExecutionButton',
        _innerHTML: '',
        get innerHTML() { return this._innerHTML; },
        set innerHTML(val) {
            this._innerHTML = val;
            if (val === '') this.children = [];
        },
        textContent: '',
        className: '',
        title: '',
        style: { display: 'none' },
        children: [],
        scrollTop: 0,
        scrollHeight: 0,
        appendChild(child) { this.children.push(child); },
        addEventListener() {},
        focus() {},
        scrollIntoView() {},
    };
}

const elements = {};
for (const id of [
    'taskDirectiveInputBox', 'backgroundModeCheckbox', 'startExecutionButton',
    'stopExecutionButton', 'liveTerminalCard', 'summaryDisplayCard',
    'summaryContentContainer', 'summaryStatusHeading', 'humanVerificationCard',
    'verificationPromptMessage', 'btnConfirmVerification', 'logsDisplaySection',
    'taskHistoryContainer', 'taskHistoryList', 'chatQueryInputBox',
]) {
    elements[id] = createElement(id);
}

let activeStreams = [];
let stopRequests = [];

class MockEventSource {
    constructor(url) {
        this.url = url;
        this.closed = false;
        this.onmessage = null;
        this.onerror = null;
        activeStreams.push(this);
    }
    close() { this.closed = true; }
}

const sandbox = {
    console,
    JSON,
    encodeURIComponent,
    setTimeout,
    clearTimeout,
    alert(msg) { throw new Error('Unexpected alert: ' + msg); },
    marked: { parse: (txt) => txt },
    EventSource: MockEventSource,
    fetch: async (url, opts) => {
        if (url === '/api/stop') {
            stopRequests.push(opts);
            return {
                ok: true,
                json: async () => ({ success: true, message: 'Task and browser agent stopped.' }),
            };
        }
        if (url === '/api/history') {
            return {
                ok: true,
                json: async () => ({ history: [{ task: 'Stopped task', succeeded: false }] }),
            };
        }
        throw new Error('Unexpected fetch: ' + url);
    },
    document: {
        getElementById: (id) => elements[id] || null,
        createElement: (tag) => createElement(tag),
        addEventListener: () => {},
    },
};

vm.runInNewContext(source, sandbox);

(async () => {
    // Before running, stop button is disabled
    assert.strictEqual(elements.stopExecutionButton.disabled, true);

    // Start a task
    elements.taskDirectiveInputBox.value = 'Navigate to example.com';
    sandbox.triggerBrowserWorkflow();

    assert.strictEqual(elements.startExecutionButton.disabled, true);
    assert.strictEqual(elements.stopExecutionButton.disabled, false);
    assert.strictEqual(activeStreams.length, 1);
    assert.strictEqual(activeStreams[0].closed, false);

    // Simulate verification card open before user hits Stop
    elements.humanVerificationCard.style.display = 'block';

    // Press Stop button
    await sandbox.stopBrowserWorkflow();

    assert.strictEqual(activeStreams[0].closed, true);
    assert.strictEqual(stopRequests.length, 1);
    assert.strictEqual(stopRequests[0].method, 'POST');
    assert.strictEqual(elements.humanVerificationCard.style.display, 'none');
    assert.strictEqual(elements.startExecutionButton.disabled, false);
    assert.strictEqual(elements.stopExecutionButton.disabled, true);
    assert.ok(elements.startExecutionButton.innerHTML.includes('Run Task'));
    assert.ok(elements.stopExecutionButton.innerHTML.includes('Stop'));
    assert.ok(elements.logsDisplaySection.children.some((row) => row.textContent.includes('stopped')));
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
"""
        import subprocess
        proc = subprocess.run(
            [node_bin, "-e", node_harness, script_path],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, f"Frontend stop harness failed:\n{proc.stderr}\n{proc.stdout}")


if __name__ == "__main__":
    unittest.main()
