import sqlite3
from pathlib import Path
from typing import List, Dict, Any


class TaskHistoryDatabase:
    storage_root = Path(__file__).resolve().parent.parent
    DB_PATH = storage_root / "task_history.db"

    @classmethod
    def _get_connection(cls) -> sqlite3.Connection:
        conn = sqlite3.connect(str(cls.DB_PATH), timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    @classmethod
    def initialize(cls):
        with cls._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task TEXT NOT NULL,
                    succeeded BOOLEAN NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    @classmethod
    def record_task(cls, task_text: str, succeeded: bool):
        cleaned_task = (task_text or "").strip()
        if not cleaned_task:
            return
        cls.initialize()
        with cls._get_connection() as conn:
            conn.execute(
                "INSERT INTO task_history (task, succeeded) VALUES (?, ?)",
                (cleaned_task, 1 if succeeded else 0),
            )
            # Permanently forget any records beyond the latest 10
            conn.execute("""
                DELETE FROM task_history 
                WHERE id NOT IN (
                    SELECT id FROM task_history ORDER BY id DESC LIMIT 10
                )
            """)
            conn.commit()

    @classmethod
    def get_recent_tasks(cls, limit: int = 10) -> List[Dict[str, Any]]:
        cls.initialize()
        with cls._get_connection() as conn:
            cursor = conn.execute(
                "SELECT id, task, succeeded, created_at FROM task_history ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = cursor.fetchall()
            return [
                {
                    "id": row["id"],
                    "task": row["task"],
                    "succeeded": bool(row["succeeded"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    @classmethod
    def clear_all(cls):
        cls.initialize()
        with cls._get_connection() as conn:
            conn.execute("DELETE FROM task_history")
            conn.commit()
