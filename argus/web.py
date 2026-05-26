"""
web.py — FastAPI application for the Argus web dashboard.

Provides:
  GET  /              — HTML dashboard (server-rendered via Jinja2)
  POST /api/run       — Trigger an immediate digest run (non-blocking)
  GET  /api/status    — Current run state + next scheduled run time
  GET  /api/runs      — Recent run history from run_log
  GET  /api/digest    — Latest run summaries (dashboard digest)
  GET  /api/runs/{id}/summaries — Summaries for a specific run
  GET  /api/sources   — Source list with last-checked metadata
  POST /api/sources   — Add a new source (persisted to config.yaml)
  DELETE /api/sources/{index} — Remove a source by list index

The app is created via create_app() and mounted to app.state so all routes
share the same config dict (mutated in-place when sources are added/removed)
and the same threading.Lock that prevents concurrent digest runs.
"""

import logging
import os
import threading
from pathlib import Path
from typing import Any

import yaml
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from argus import emailer, storage
from argus.scheduler import run_digest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class SourceIn(BaseModel):
    """Payload for adding a new monitored source."""

    name: str
    url: str


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(config: dict, db_path: str, config_path: str) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Args:
        config:      Parsed config.yaml dict (mutated in-place by source CRUD).
        db_path:     Path to the SQLite database file.
        config_path: Path to config.yaml (used when persisting source changes).

    Returns:
        A configured FastAPI instance ready to hand to uvicorn.
    """
    app = FastAPI(
        title="Argus — Competitive Intelligence Monitor",
        description="Dashboard for monitoring competitor sites and industry news.",
        docs_url=None,  # disable /docs in production; add back during dev if needed
        redoc_url=None,
    )

    # Shared state accessible from all route handlers.
    app.state.config = config
    app.state.db_path = db_path
    app.state.config_path = config_path
    app.state.run_lock = threading.Lock()
    # bg_scheduler is set externally (in main.py) after the app is created.
    app.state.bg_scheduler = None

    # Resolve the templates directory relative to this file, so it works
    # regardless of the current working directory.
    templates_dir = Path(__file__).parent.parent / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))

    # -----------------------------------------------------------------------
    # HTML dashboard
    # -----------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        """Render the main dashboard page."""
        conn = storage.init_db(app.state.db_path)
        runs = _get_recent_runs(conn, limit=15)
        sources = _get_sources_with_status(app.state.config, conn)
        digest = _format_digest(storage.get_latest_digest(conn))
        digest_hint = _digest_hint(runs, digest)
        conn.close()

        cron = app.state.config.get("schedule", {}).get("cron", "0 6 * * *")

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "sources": sources,
                "runs": runs,
                "digest": digest,
                "digest_hint": digest_hint,
                "openai_configured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
                "is_running": app.state.run_lock.locked(),
                "cron": cron,
            },
        )

    # -----------------------------------------------------------------------
    # Run trigger
    # -----------------------------------------------------------------------

    @app.post("/api/run")
    async def trigger_run(background_tasks: BackgroundTasks) -> dict[str, str]:
        """
        Start a digest run in a background thread.

        Returns immediately with {"status": "started"} or {"status":
        "already_running"} (HTTP 409) if a run is already in progress.
        """
        if app.state.run_lock.locked():
            return JSONResponse(
                {"status": "already_running", "message": "A run is already in progress."},
                status_code=409,
            )
        background_tasks.add_task(_run_digest_safe, app)
        return {"status": "started"}

    # -----------------------------------------------------------------------
    # Status
    # -----------------------------------------------------------------------

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        """
        Return the current run state and next scheduled run time.

        The dashboard polls this endpoint every few seconds during an active
        run to detect completion.
        """
        next_run: str | None = None
        if app.state.bg_scheduler is not None:
            jobs = app.state.bg_scheduler.get_jobs()
            if jobs and jobs[0].next_run_time:
                next_run = jobs[0].next_run_time.isoformat()

        conn = storage.init_db(app.state.db_path)
        last_runs = _get_recent_runs(conn, limit=1)
        conn.close()

        return {
            "is_running": app.state.run_lock.locked(),
            "next_run": next_run,
            "last_run": last_runs[0] if last_runs else None,
            "openai_configured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
        }

    # -----------------------------------------------------------------------
    # Run history
    # -----------------------------------------------------------------------

    @app.get("/api/runs")
    async def get_runs() -> list[dict[str, Any]]:
        """Return the 20 most recent run records from run_log."""
        conn = storage.init_db(app.state.db_path)
        runs = _get_recent_runs(conn, limit=20)
        conn.close()
        return runs

    @app.get("/api/digest")
    async def get_digest() -> dict[str, Any] | None:
        """Return the latest digest (summaries from the most recent run with changes)."""
        conn = storage.init_db(app.state.db_path)
        digest = _format_digest(storage.get_latest_digest(conn))
        conn.close()
        return digest

    @app.get("/api/runs/{run_id}/summaries")
    async def get_run_summaries(run_id: int) -> list[dict[str, Any]]:
        """Return summaries for a specific run."""
        conn = storage.init_db(app.state.db_path)
        exists = conn.execute(
            "SELECT 1 FROM run_log WHERE id = ?", (run_id,)
        ).fetchone()
        if not exists:
            conn.close()
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found.")
        summaries = storage.get_summaries_for_run(conn, run_id)
        conn.close()
        return [
            {**s, "summary_html": emailer.render_summary_html(s["summary_text"])}
            for s in summaries
        ]

    # -----------------------------------------------------------------------
    # Source management
    # -----------------------------------------------------------------------

    @app.get("/api/sources")
    async def get_sources() -> list[dict[str, Any]]:
        """Return all configured sources with their last-checked timestamps."""
        conn = storage.init_db(app.state.db_path)
        sources = _get_sources_with_status(app.state.config, conn)
        conn.close()
        return sources

    @app.post("/api/sources", status_code=201)
    async def add_source(source: SourceIn) -> dict[str, Any]:
        """
        Add a new source to the monitored list.

        Mutates app.state.config in-place (so the next scheduled run picks it
        up) and persists the change to config.yaml on disk.
        """
        new_entry = {"name": source.name.strip(), "url": source.url.strip()}
        app.state.config.setdefault("sources", []).append(new_entry)
        _save_config(app.state.config, app.state.config_path)
        logger.info("Source added: %s (%s)", new_entry["name"], new_entry["url"])
        return {"status": "added", "source": new_entry}

    @app.delete("/api/sources/{index}")
    async def delete_source(index: int) -> dict[str, Any]:
        """
        Remove a source by its position in the sources list.

        Mutates app.state.config in-place and persists to config.yaml.
        """
        sources = app.state.config.get("sources", [])
        if index < 0 or index >= len(sources):
            raise HTTPException(status_code=404, detail=f"Source index {index} not found.")
        removed = sources.pop(index)
        _save_config(app.state.config, app.state.config_path)
        logger.info("Source removed: %s (%s)", removed.get("name"), removed.get("url"))
        return {"status": "removed", "source": removed}

    return app


# ---------------------------------------------------------------------------
# Background run helper
# ---------------------------------------------------------------------------


def _run_digest_safe(app: FastAPI) -> None:
    """
    Acquire the run lock and execute a full digest cycle.

    Using `blocking=False` means we return immediately if another run holds
    the lock, rather than queuing behind it — preventing pile-ups if manual
    triggers arrive while a scheduled run is in progress.
    """
    acquired = app.state.run_lock.acquire(blocking=False)
    if not acquired:
        logger.info("Skipping run — lock already held by another run.")
        return
    try:
        run_digest(app.state.config, app.state.db_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("Digest run failed: %s", exc, exc_info=True)
    finally:
        app.state.run_lock.release()


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _get_recent_runs(conn, limit: int = 15) -> list[dict[str, Any]]:
    """Query the run_log table and return rows as plain dicts."""
    rows = conn.execute(
        """
        SELECT r.id, r.run_at, r.urls_checked, r.urls_changed, r.email_sent,
               r.error_message,
               (SELECT COUNT(*) FROM run_summaries s WHERE s.run_id = r.id) AS summary_count
        FROM run_log r
        ORDER BY r.run_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_serialize_run(dict(row)) for row in rows]


def _serialize_run(row: dict) -> dict:
    """Normalize run_log rows for JSON and the dashboard."""
    run_at = row.get("run_at")
    if hasattr(run_at, "isoformat"):
        row["run_at"] = run_at.isoformat()
    row["summary_count"] = int(row.get("summary_count") or 0)
    return row


def _digest_hint(runs: list[dict], digest: dict | None) -> str | None:
    """Explain why Latest Digest is empty when the last run detected changes."""
    if digest or not runs:
        return None
    latest = runs[0]
    if int(latest.get("urls_changed") or 0) == 0:
        return None
    if int(latest.get("summary_count") or 0) > 0:
        return None
    return (
        latest.get("error_message")
        or "Changes were detected but summarization produced no output. "
        "Set OPENAI_API_KEY and click Run Now again."
    )


def _format_digest(raw: dict | None) -> dict | None:
    """Add pre-rendered HTML for each summary in a digest payload."""
    if raw is None:
        return None
    return {
        **raw,
        "run_at": (
            raw["run_at"].isoformat()
            if hasattr(raw["run_at"], "isoformat")
            else raw["run_at"]
        ),
        "summaries": [
            {
                **s,
                "summary_html": emailer.render_summary_html(s["summary_text"]),
            }
            for s in raw["summaries"]
        ],
    }


def _get_sources_with_status(config: dict, conn) -> list[dict[str, Any]]:
    """
    Combine the source list from config with last-checked timestamps from
    the page_snapshots table.
    """
    result = []
    for source in config.get("sources", []):
        url = source["url"]
        snapshot = storage.get_snapshot(conn, url)
        result.append(
            {
                "name": source.get("name", url),
                "url": url,
                # Return ISO-8601 string so the JS can parse it uniformly.
                "last_checked": (
                    snapshot.captured_at.isoformat() if snapshot else None
                ),
            }
        )
    return result


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------


def _save_config(config: dict, config_path: str) -> None:
    """
    Write the in-memory config dict back to config.yaml.

    Note: yaml.dump does not preserve the original comments or key ordering.
    This is acceptable for a tool where sources are the primary user-editable
    field — the comments in the example config serve as documentation, not
    runtime state.
    """
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    logger.debug("Config saved to %s", config_path)
