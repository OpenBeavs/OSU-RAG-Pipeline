"""
Lightweight SQLite persistence for ODK submission tracking.
No ORM — just plain sqlite3 so the dashboard has zero heavy dependencies.
"""

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "odk.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id              TEXT PRIMARY KEY,
                submitter_email TEXT NOT NULL,
                department_name TEXT NOT NULL,
                department_url  TEXT NOT NULL,
                filename        TEXT NOT NULL,
                file_path       TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                rejection_reason TEXT,
                created_at      INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL
            )
        """)
        conn.commit()


def create_submission(
    submitter_email: str,
    department_name: str,
    department_url: str,
    filename: str,
    file_path: str,
) -> dict:
    now = int(time.time())
    sub_id = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            """INSERT INTO submissions
               (id, submitter_email, department_name, department_url,
                filename, file_path, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (sub_id, submitter_email, department_name, department_url,
             filename, file_path, "pending", now, now),
        )
        conn.commit()
    return get_submission(sub_id)


def get_submission(sub_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
        return dict(row) if row else None


def list_submissions(status: Optional[str] = None) -> list[dict]:
    with _connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM submissions WHERE status=? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM submissions ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def update_status(sub_id: str, status: str, rejection_reason: Optional[str] = None) -> Optional[dict]:
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            "UPDATE submissions SET status=?, rejection_reason=?, updated_at=? WHERE id=?",
            (status, rejection_reason, now, sub_id),
        )
        conn.commit()
    return get_submission(sub_id)


def delete_submission(sub_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM submissions WHERE id=?", (sub_id,))
        conn.commit()
        return cur.rowcount > 0
