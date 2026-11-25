import shutil
import subprocess
import unittest
import os
import sys
from starlette.testclient import TestClient

suite_directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if suite_directory not in sys.path:
    sys.path.insert(0, suite_directory)

from core_config.history_db import TaskHistoryDatabase
from backend.gateway import build_server_app


class TestTaskHistory(unittest.TestCase):
    def setUp(self):
        TaskHistoryDatabase.clear_all()

    def tearDown(self):
        TaskHistoryDatabase.clear_all()

    def test_record_and_fetch_recent_tasks(self):
        TaskHistoryDatabase.record_task("Find top 3 laptops on amazon", True)
        TaskHistoryDatabase.record_task("Check weather in New York", False)

        recent = TaskHistoryDatabase.get_recent_tasks()
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0]["task"], "Check weather in New York")
        self.assertFalse(recent[0]["succeeded"])
        self.assertEqual(recent[1]["task"], "Find top 3 laptops on amazon")
        self.assertTrue(recent[1]["succeeded"])

    def test_forget_eleventh_task_permanently(self):
        # Insert 15 tasks
        for idx in range(1, 16):
            TaskHistoryDatabase.record_task(f"Task Directive #{idx}", idx % 2 == 0)

        # Database must only have the latest 10 tasks
        recent = TaskHistoryDatabase.get_recent_tasks(limit=50)
        self.assertEqual(len(recent), 10)

        # Latest should be Task Directive #15 down to #6
        self.assertEqual(recent[0]["task"], "Task Directive #15")
        self.assertEqual(recent[-1]["task"], "Task Directive #6")

        # Verify directly in SQLite that earlier tasks 1 to 5 are permanently deleted
        with TaskHistoryDatabase._get_connection() as conn:
            total_count = conn.execute("SELECT COUNT(*) FROM task_history").fetchone()[0]
            self.assertEqual(total_count, 10)

            cursor = conn.execute("SELECT task FROM task_history WHERE task = 'Task Directive #1'")
            self.assertIsNone(cursor.fetchone())

    def test_http_history_endpoints(self):
        app = build_server_app()
        client = TestClient(app)

        # POST /api/history
        resp = client.post("/api/history", json={"task": "Search AI trends 2026", "succeeded": True})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(len(data["history"]), 1)
        self.assertEqual(data["history"][0]["task"], "Search AI trends 2026")
        self.assertTrue(data["history"][0]["succeeded"])

        # GET /api/history
        get_resp = client.get("/api/history")
        self.assertEqual(get_resp.status_code, 200)
        get_data = get_resp.json()
        self.assertEqual(len(get_data["history"]), 1)
        self.assertEqual(get_data["history"][0]["task"], "Search AI trends 2026")

    def test_frontend_history_hidden_while_running_until_outcome(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("Node.js runtime not available for frontend behavior verification")

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
        disabled: false,
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
    'liveTerminalCard', 'summaryDisplayCard', 'summaryContentContainer',
    'summaryStatusHeading', 'humanVerificationCard', 'verificationPromptMessage',
    'btnConfirmVerification', 'logsDisplaySection', 'taskHistoryContainer',
    'taskHistoryList', 'chatQueryInputBox',
]) {
    elements[id] = createElement(id);
}

let currentHistory = [{ task: 'Previous finished task', succeeded: true }];
let fetchCallCount = 0;
let activeStreams = [];

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
    fetch: async (url) => {
        if (url === '/api/history') {
            fetchCallCount += 1;
            return {
                ok: true,
                json: async () => ({ history: currentHistory }),
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
    // 1. Initial load shows existing history
    await sandbox.fetchTaskHistory();
    assert.strictEqual(elements.taskHistoryContainer.style.display, 'flex');
    assert.strictEqual(elements.taskHistoryList.children.length, 1);

    // 2. Starting a task hides the history box while running (and across logs / verification)
    elements.taskDirectiveInputBox.value = 'Run a new task';
    sandbox.triggerBrowserWorkflow();
    assert.strictEqual(elements.taskHistoryContainer.style.display, 'none');

    const stream1 = activeStreams[0];
    stream1.onmessage({ data: JSON.stringify({ type: 'status', text: 'Starting...' }) });
    stream1.onmessage({ data: JSON.stringify({ type: 'log', text: 'Step 1' }) });
    stream1.onmessage({ data: JSON.stringify({ type: 'verification_required', reason: 'CAPTCHA' }) });
    stream1.onmessage({ data: JSON.stringify({ type: 'verification_completed', text: 'Resumed' }) });
    assert.strictEqual(elements.taskHistoryContainer.style.display, 'none');

    // Even if renderTaskHistory is called mid-run, history stays hidden
    sandbox.renderTaskHistory(currentHistory);
    assert.strictEqual(elements.taskHistoryContainer.style.display, 'none');

    // 3. Once the task succeeds ('result'), history is shown again with updated items
    const fetchesBeforeResult = fetchCallCount;
    currentHistory = [
        { task: 'Run a new task', succeeded: true },
        { task: 'Previous finished task', succeeded: true },
    ];
    stream1.onmessage({ data: JSON.stringify({ type: 'result', text: 'Done!', success: true }) });
    await new Promise((r) => setTimeout(r, 10));
    assert.strictEqual(elements.taskHistoryContainer.style.display, 'flex');
    assert.strictEqual(elements.taskHistoryList.children.length, 2);
    stream1.onmessage({ data: JSON.stringify({ type: 'done' }) });
    await new Promise((r) => setTimeout(r, 10));
    assert.strictEqual(fetchCallCount, fetchesBeforeResult + 1);

    // 4. Starting a failing task hides history until error/api_limit outcome arrives
    elements.taskDirectiveInputBox.value = 'Run a failing task';
    sandbox.triggerBrowserWorkflow();
    assert.strictEqual(elements.taskHistoryContainer.style.display, 'none');

    const stream2 = activeStreams[1];
    stream2.onmessage({ data: JSON.stringify({ type: 'log', text: 'Navigating...' }) });
    assert.strictEqual(elements.taskHistoryContainer.style.display, 'none');

    currentHistory = [
        { task: 'Run a failing task', succeeded: false },
        { task: 'Run a new task', succeeded: true },
    ];
    stream2.onmessage({ data: JSON.stringify({ type: 'error', text: 'Boom' }) });
    await new Promise((r) => setTimeout(r, 10));
    assert.strictEqual(elements.taskHistoryContainer.style.display, 'flex');
    assert.strictEqual(elements.taskHistoryList.children.length, 2);
    stream2.onmessage({ data: JSON.stringify({ type: 'done' }) });

    // 5. Stream onerror also restores history
    elements.taskDirectiveInputBox.value = 'Run interrupted task';
    sandbox.triggerBrowserWorkflow();
    assert.strictEqual(elements.taskHistoryContainer.style.display, 'none');
    const stream3 = activeStreams[2];
    stream3.onerror();
    await new Promise((r) => setTimeout(r, 10));
    assert.strictEqual(elements.taskHistoryContainer.style.display, 'flex');
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
"""
        proc = subprocess.run([node_bin, "-e", node_harness, script_path], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Frontend history test failed:\n{proc.stderr}\n{proc.stdout}")


if __name__ == "__main__":
    unittest.main()
