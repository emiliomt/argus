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
) -> None:
    """
    Append a row to run_log recording the outcome of a digest run.

    Args:
        conn:          Open database connection.
        urls_checked:  Total number of sources attempted.
        urls_changed:  Number of sources where content differed from last run.
        email_sent:    True if a digest email was successfully delivered.
        error_message: Top-level error if the run failed, otherwise None.
    """
    conn.execute(
        """
        INSERT INTO run_log (urls_checked, urls_changed, email_sent, error_message)
        VALUES (?, ?, ?, ?)
        """,
        (urls_checked, urls_changed, int(email_sent), error_message),
    )
    conn.commit()
    logger.info(
        "Run logged: checked=%d changed=%d email_sent=%s",
        urls_checked,
        urls_changed,
        email_sent,
    )
