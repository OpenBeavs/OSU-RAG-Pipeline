"""
Lightweight SQLite persistence for ODK submission tracking.
No ORM — plain sqlite3 so the dashboard has zero heavy dependencies.
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
    """Create all tables if they don't exist."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id               TEXT PRIMARY KEY,
                submitter_email  TEXT NOT NULL,
                department_name  TEXT NOT NULL,
                department_url   TEXT NOT NULL,
                filename         TEXT NOT NULL,
                file_path        TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'pending',
                rejection_reason TEXT,
                created_at       INTEGER NOT NULL,
                updated_at       INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crawl_run (
                submission_id  TEXT PRIMARY KEY,
                doc_status     TEXT NOT NULL DEFAULT 'pending',
                web_status     TEXT NOT NULL DEFAULT 'pending',
                doc_vectors    INTEGER NOT NULL DEFAULT 0,
                pages_found    INTEGER NOT NULL DEFAULT 0,
                pages_indexed  INTEGER NOT NULL DEFAULT 0,
                web_vectors    INTEGER NOT NULL DEFAULT 0,
                error          TEXT,
                started_at     INTEGER,
                finished_at    INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crawl_page (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id  TEXT NOT NULL,
                url            TEXT NOT NULL,
                title          TEXT,
                word_count     INTEGER NOT NULL DEFAULT 0,
                chunks         INTEGER NOT NULL DEFAULT 0,
                vectors        INTEGER NOT NULL DEFAULT 0,
                status         TEXT NOT NULL,
                crawled_at     INTEGER NOT NULL
            )
        """)
        conn.commit()


# ──────────────────────────────────────────────
# Submissions
# ──────────────────────────────────────────────

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
        conn.execute("DELETE FROM crawl_run WHERE submission_id=?", (sub_id,))
        conn.execute("DELETE FROM crawl_page WHERE submission_id=?", (sub_id,))
        conn.commit()
        return cur.rowcount > 0


# ──────────────────────────────────────────────
# Crawl run tracking
# ──────────────────────────────────────────────

def upsert_crawl_run(submission_id: str, **fields) -> None:
    """Create or update the crawl_run row for a submission."""
    with _connect() as conn:
        existing = conn.execute(
            "SELECT submission_id FROM crawl_run WHERE submission_id=?", (submission_id,)
        ).fetchone()
        if existing:
            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                conn.execute(
                    f"UPDATE crawl_run SET {sets} WHERE submission_id=?",
                    (*fields.values(), submission_id),
                )
        else:
            conn.execute(
                "INSERT INTO crawl_run (submission_id) VALUES (?)", (submission_id,)
            )
            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                conn.execute(
                    f"UPDATE crawl_run SET {sets} WHERE submission_id=?",
                    (*fields.values(), submission_id),
                )
        conn.commit()


def increment_crawl_run(submission_id: str, **delta) -> None:
    """Atomically increment numeric fields in crawl_run."""
    with _connect() as conn:
        for col, val in delta.items():
            conn.execute(
                f"UPDATE crawl_run SET {col} = {col} + ? WHERE submission_id=?",
                (val, submission_id),
            )
        conn.commit()


def get_crawl_run(submission_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM crawl_run WHERE submission_id=?", (submission_id,)
        ).fetchone()
        return dict(row) if row else None


# ──────────────────────────────────────────────
# Crawl page log
# ──────────────────────────────────────────────

def add_crawl_page(
    submission_id: str,
    url: str,
    title: Optional[str],
    word_count: int,
    chunks: int,
    vectors: int,
    status: str,
) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO crawl_page
               (submission_id, url, title, word_count, chunks, vectors, status, crawled_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (submission_id, url, title, word_count, chunks, vectors, status, int(time.time())),
        )
        conn.commit()


def get_crawl_pages(submission_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM crawl_page WHERE submission_id=? ORDER BY crawled_at ASC",
            (submission_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_crawl_pages() -> list[dict]:
    """Return all indexed pages across all submissions (for the graph view)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT cp.*, s.department_url FROM crawl_page cp "
            "JOIN submissions s ON cp.submission_id = s.id "
            "WHERE cp.status = 'indexed' ORDER BY cp.crawled_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
