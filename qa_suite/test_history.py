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


if __name__ == "__main__":
    unittest.main()
