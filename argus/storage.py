"""
storage.py — SQLite persistence layer for Argus.

Manages two tables:
- page_snapshots: stores the last-seen content hash and raw text for each URL.
- run_log: audit trail of every digest run and its outcome.

All functions accept a sqlite3.Connection so callers control the connection
lifecycle. No business logic lives here — this is pure data access.
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    """Represents the last-seen state for a single monitored URL."""

    url: str
    content_hash: str  # SHA-256 hex digest of the extracted page text
    raw_text: str  # stripped body text (fed to summarizer as "before" context)
    captured_at: datetime


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS page_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    raw_text     TEXT NOT NULL,
    captured_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_CREATE_RUN_LOG = """
CREATE TABLE IF NOT EXISTS run_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    urls_checked  INTEGER NOT NULL DEFAULT 0,
    urls_changed  INTEGER NOT NULL DEFAULT 0,
    email_sent    INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);
"""

_CREATE_RUN_SUMMARIES = """
CREATE TABLE IF NOT EXISTS run_summaries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL,
    url          TEXT NOT NULL,
    source_name  TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES run_log(id) ON DELETE CASCADE
);
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_db(db_path: str) -> sqlite3.Connection:
    """
    Open (or create) the SQLite database at db_path.

    Creates the required tables if they don't exist and enables WAL mode for
    safer concurrent access. Returns an open connection; caller is responsible
    for closing it.

    Args:
        db_path: Filesystem path to the SQLite file (e.g. "argus.db").

    Returns:
        An open sqlite3.Connection with row_factory set to sqlite3.Row.
    """
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row

    # WAL mode allows concurrent reads while a write is in progress.
    conn.execute("PRAGMA journal_mode=WAL;")

    conn.execute(_CREATE_SNAPSHOTS)
    conn.execute(_CREATE_RUN_LOG)
    conn.execute(_CREATE_RUN_SUMMARIES)
    conn.commit()

    logger.debug("Database initialised at %s", db_path)
    return conn


def get_snapshot(conn: sqlite3.Connection, url: str) -> Snapshot | None:
    """
    Fetch the stored snapshot for a URL, or None if this is a first visit.

    Args:
        conn: Open database connection.
        url:  The exact URL string used as the primary key.

    Returns:
        A Snapshot dataclass, or None if the URL has never been seen.
    """
    row = conn.execute(
        "SELECT url, content_hash, raw_text, captured_at FROM page_snapshots WHERE url = ?",
        (url,),
    ).fetchone()

    if row is None:
        return None

    return Snapshot(
        url=row["url"],
        content_hash=row["content_hash"],
        raw_text=row["raw_text"],
        captured_at=row["captured_at"],
    )


def upsert_snapshot(
    conn: sqlite3.Connection,
    url: str,
    content_hash: str,
    raw_text: str,
) -> None:
    """
    Insert or replace the snapshot for a URL.

    Always called after a successful scrape, regardless of whether content
    changed. This keeps the baseline current so the next run compares against
    the most recently seen version — not the first-ever version.

    Args:
        conn:         Open database connection.
        url:          The monitored URL.
        content_hash: SHA-256 hex digest of the extracted page text.
        raw_text:     The boilerplate-stripped body text.
    """
    conn.execute(
        """
        INSERT INTO page_snapshots (url, content_hash, raw_text, captured_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(url) DO UPDATE SET
            content_hash = excluded.content_hash,
            raw_text     = excluded.raw_text,
            captured_at  = excluded.captured_at
        """,
        (url, content_hash, raw_text),
    )
    conn.commit()
    logger.debug("Snapshot updated for %s", url)


def log_run(
    conn: sqlite3.Connection,
    urls_checked: int,
    urls_changed: int,
    email_sent: bool,
    error_message: str | None = None,
) -> int:
    """
    Append a row to run_log recording the outcome of a digest run.

    Args:
        conn:          Open database connection.
        urls_checked:  Total number of sources attempted.
        urls_changed:  Number of sources where content differed from last run.
        email_sent:    True if a digest email was successfully delivered.
        error_message: Top-level error if the run failed, otherwise None.

    Returns:
        The id of the newly inserted run_log row.
    """
    cursor = conn.execute(
        """
        INSERT INTO run_log (urls_checked, urls_changed, email_sent, error_message)
        VALUES (?, ?, ?, ?)
        """,
        (urls_checked, urls_changed, int(email_sent), error_message),
    )
    conn.commit()
    run_id = cursor.lastrowid
    logger.info(
        "Run logged: id=%d checked=%d changed=%d email_sent=%s",
        run_id,
        urls_checked,
        urls_changed,
        email_sent,
    )
    return run_id


def save_run_summaries(
    conn: sqlite3.Connection,
    run_id: int,
    summaries: list,
) -> None:
    """
    Persist LLM summaries for a digest run (shown in the web dashboard).

    Args:
        conn:      Open database connection.
        run_id:    run_log.id for this run.
        summaries: Iterable of ChangeSummary objects from summarizer.py.
    """
    if not summaries:
        return

    conn.executemany(
        """
        INSERT INTO run_summaries (run_id, url, source_name, summary_text)
        VALUES (?, ?, ?, ?)
        """,
        [
            (run_id, s.url, s.source_name, s.summary_text)
            for s in summaries
        ],
    )
    conn.commit()
    logger.info("Saved %d summaries for run %d", len(summaries), run_id)


def get_summaries_for_run(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """Return all summaries for a given run_log id, oldest first."""
    rows = conn.execute(
        """
        SELECT url, source_name, summary_text
        FROM run_summaries
        WHERE run_id = ?
        ORDER BY id ASC
        """,
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_latest_digest(conn: sqlite3.Connection) -> dict | None:
    """
    Return the most recent run that has at least one stored summary.

    Returns:
        Dict with run metadata and a summaries list, or None if no digest exists.
    """
    row = conn.execute(
        """
        SELECT r.id, r.run_at, r.urls_checked, r.urls_changed
        FROM run_log r
        WHERE EXISTS (SELECT 1 FROM run_summaries s WHERE s.run_id = r.id)
        ORDER BY r.run_at DESC
        LIMIT 1
        """
    ).fetchone()

    if row is None:
        return None

    summaries = get_summaries_for_run(conn, row["id"])
    return {
        "run_id": row["id"],
        "run_at": row["run_at"],
        "urls_checked": row["urls_checked"],
        "urls_changed": row["urls_changed"],
        "summaries": summaries,
    }
