"""
scheduler.py — Orchestration loop and APScheduler wiring for Argus.

This module is the integration hub: it imports all other Argus modules and
wires them into the full pipeline. Two public entry points:

    run_digest(config, db_path)
        Execute one complete monitor-→-summarise-→-email cycle immediately.
        Called by `main.py run` and also by the scheduler job.

    start_scheduler(config, db_path)
        Block the process, running run_digest on the configured cron schedule.
        Called by `main.py schedule`.
"""

import logging
import os
from datetime import datetime, timezone

import openai
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from argus import diff as diff_module
from argus import emailer, scraper, storage, summarizer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_digest(config: dict, db_path: str) -> None:
    """
    Execute a full competitive intelligence digest cycle.

    Pipeline:
        1. Initialise DB connection.
        2. For each configured source: scrape → diff → update snapshot.
        3. For each changed source: call the LLM summarizer.
        4. If any summaries exist: build and send the email digest.
        5. Log the run outcome to the DB.

    Design notes:
    - Snapshots are always updated, even when no change is detected. This keeps
      the baseline current so the next run compares to the most recent content
      rather than the first-ever version.
    - A failed scrape logs an error and skips that source; it never aborts
      the remaining sources or the email send.
    - The OpenAI client is instantiated once per run and reused across all
      summarizer calls for efficiency.

    Args:
        config:  Parsed config.yaml as a dict.
        db_path: Filesystem path to the SQLite database.
    """
    logger.info("Starting Argus digest run")
    conn = storage.init_db(db_path)

    # One OpenAI client per run — reusing a single client is more efficient
    # than instantiating one per URL call.
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "OPENAI_API_KEY is not set. Add it to your .env file or environment."
        )
    client = openai.OpenAI(api_key=api_key)

    sources = config.get("sources", [])
    if not sources:
        logger.warning("No sources configured in config.yaml — nothing to monitor.")
        return

    # -----------------------------------------------------------------------
    # Phase 1: Scrape and diff all sources
    # -----------------------------------------------------------------------

    # Each entry: (DiffResult, source config dict)
    changed_sources: list[tuple[diff_module.DiffResult, dict]] = []

    for source in sources:
        url = source["url"]
        name = source.get("name", url)

        logger.info("Checking: %s (%s)", name, url)
        page = scraper.scrape_url(url)

        if page.error:
            logger.error("Skipping %s — scrape failed: %s", name, page.error)
            continue

        snapshot = storage.get_snapshot(conn, url)
        diff_result = diff_module.compute_diff(page, snapshot)

        # Always persist the freshest content as the new baseline.
        storage.upsert_snapshot(conn, url, diff_result.current_hash, diff_result.current_text)

        if diff_result.has_changed:
            changed_sources.append((diff_result, source))

    logger.info(
        "Scrape phase complete: %d/%d sources changed",
        len(changed_sources),
        len(sources),
    )

    # -----------------------------------------------------------------------
    # Phase 2: Summarise changed sources
    # -----------------------------------------------------------------------

    summaries: list[summarizer.ChangeSummary] = []

    for diff_result, source in changed_sources:
        name = source.get("name", diff_result.url)
        logger.info("Summarising changes for: %s", name)

        try:
            summary = summarizer.summarize_change(
                client=client,
                url=diff_result.url,
                source_name=name,
                previous_text=diff_result.previous_text,
                current_text=diff_result.current_text,
                is_new=diff_result.is_new,
            )
            summaries.append(summary)
        except Exception as exc:  # noqa: BLE001
            # A summarization failure for one source should not prevent the
            # rest of the summaries or the email from being sent.
            logger.error("Summarization failed for %s: %s", name, exc)

    # -----------------------------------------------------------------------
    # Phase 3: Build and send email digest
    # -----------------------------------------------------------------------

    email_sent = False

    if summaries:
        digest = emailer.build_digest(
            summaries=summaries,
            run_date=datetime.now(timezone.utc),
            total_checked=len(sources),
        )

        try:
            _deliver_email(config, digest)
            email_sent = True
        except Exception as exc:  # noqa: BLE001
            logger.error("Email delivery failed: %s", exc)
    else:
        logger.info(
            "No changes detected across %d source%s — no email sent.",
            len(sources),
            "s" if len(sources) != 1 else "",
        )

    # -----------------------------------------------------------------------
    # Phase 4: Log run outcome
    # -----------------------------------------------------------------------

    storage.log_run(
        conn=conn,
        urls_checked=len(sources),
        urls_changed=len(changed_sources),
        email_sent=email_sent,
    )

    conn.close()
    logger.info("Argus digest run complete.")


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def start_scheduler(config: dict, db_path: str) -> None:
    """
    Start the APScheduler blocking scheduler using the cron expression
    defined in config.yaml under `schedule.cron`.

    Blocks until the process receives a KeyboardInterrupt (Ctrl-C).

    Args:
        config:  Parsed config.yaml as a dict.
        db_path: Filesystem path to the SQLite database.
    """
    cron_expr = config.get("schedule", {}).get("cron", "0 6 * * *")
    logger.info("Starting Argus scheduler with cron: '%s'", cron_expr)

    scheduler = BlockingScheduler(timezone="UTC")
    trigger = CronTrigger.from_crontab(cron_expr, timezone="UTC")

    scheduler.add_job(
        func=run_digest,
        trigger=trigger,
        args=[config, db_path],
        id="argus_digest",
        name="Argus Competitive Intel Digest",
        max_instances=1,  # prevent overlap if a run takes longer than the interval
        coalesce=True,  # skip missed runs rather than queuing them
    )

    next_run = scheduler.get_jobs()[0].next_run_time
    logger.info("Next digest scheduled for: %s", next_run)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Argus scheduler stopped.")


def start_background_scheduler(
    config: dict,
    db_path: str,
    run_fn=None,
) -> BackgroundScheduler:
    """
    Start APScheduler in non-blocking (background thread) mode.

    Used when the web server occupies the main thread. The returned scheduler
    object should be stored in app.state so routes can inspect next_run_time.

    Args:
        config:  Parsed config.yaml as a dict.
        db_path: Filesystem path to the SQLite database.
        run_fn:  Optional callable to use as the job function. Defaults to
                 run_digest — pass a wrapper if you need lock-guarded execution.

    Returns:
        A started BackgroundScheduler instance.
    """
    cron_expr = config.get("schedule", {}).get("cron", "0 6 * * *")
    job_fn = run_fn or run_digest

    scheduler = BackgroundScheduler(timezone="UTC")
    trigger = CronTrigger.from_crontab(cron_expr, timezone="UTC")

    scheduler.add_job(
        func=job_fn,
        trigger=trigger,
        args=[config, db_path] if job_fn is run_digest else [],
        id="argus_digest",
        name="Argus Competitive Intel Digest",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    next_run = scheduler.get_jobs()[0].next_run_time
    logger.info("Background scheduler started. Next run: %s", next_run)
    return scheduler


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _deliver_email(config: dict, digest: emailer.DigestEmail) -> None:
    """
    Route email delivery to the configured provider (smtp or sendgrid).

    Args:
        config: Full parsed config.yaml.
        digest: Rendered digest email.

    Raises:
        ValueError:   If an unknown email provider is specified.
        RuntimeError: On delivery failure (propagated from emailer module).
    """
    email_cfg = config.get("email", {})
    provider = email_cfg.get("provider", "smtp").lower()
    from_addr = email_cfg["from"]
    to_addrs = email_cfg["to"] if isinstance(email_cfg["to"], list) else [email_cfg["to"]]

    if provider == "smtp":
        smtp_cfg = config.get("smtp", {})
        emailer.send_email_smtp(
            digest=digest,
            smtp_host=smtp_cfg["host"],
            smtp_port=int(smtp_cfg.get("port", 587)),
            smtp_user=os.environ["SMTP_USER"],
            smtp_password=os.environ["SMTP_PASSWORD"],
            from_addr=from_addr,
            to_addrs=to_addrs,
            use_tls=smtp_cfg.get("use_tls", True),
        )

    elif provider == "sendgrid":
        api_key = os.environ.get("SENDGRID_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "SENDGRID_API_KEY is not set. Add it to your .env file."
            )
        emailer.send_email_sendgrid(
            digest=digest,
            api_key=api_key,
            from_addr=from_addr,
            to_addrs=to_addrs,
        )

    else:
        raise ValueError(
            f"Unknown email provider '{provider}'. "
            "Set email.provider to 'smtp' or 'sendgrid' in config.yaml."
        )
