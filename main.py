"""
main.py — CLI entrypoint for Argus.

Usage:
    python main.py run                    # Run one digest cycle immediately
    python main.py schedule               # Start the cron scheduler (blocks)
    python main.py web                    # Start the web dashboard + scheduler
    python main.py run --config my.yaml   # Use a custom config file

Environment variables (put them in a .env file — see .env.example):
    OPENAI_API_KEY      Required for LLM summarization
    SMTP_USER           Required when email.provider = smtp
    SMTP_PASSWORD       Required when email.provider = smtp
    SENDGRID_API_KEY    Required when email.provider = sendgrid
    PORT                Web server port (default 8000; Railway sets this automatically)
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from argus import scheduler as scheduler_module


def _configure_logging() -> None:
    """Set up structured console logging for the lifetime of the process."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stdout,
    )
    # Quiet down noisy third-party loggers.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _load_config(config_path: str, require_email: bool = True) -> dict:
    """
    Load and minimally validate config.yaml.

    Args:
        config_path:   Path to the YAML config file.
        require_email: Whether to require email settings (not needed for web-only mode).

    Returns:
        Parsed config as a dict.

    Raises:
        SystemExit: On missing file or invalid YAML.
    """
    path = Path(config_path)
    if not path.exists():
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        print("Copy config.yaml and fill in your settings.", file=sys.stderr)
        sys.exit(1)

    try:
        with path.open() as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        print(f"Error: could not parse {config_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    if not config:
        # Empty config is allowed in web mode (user can add sources via the UI).
        config = {}

    if require_email:
        _require(config, "email", config_path)
        _require(config["email"], "from", config_path)
        _require(config["email"], "to", config_path)

    # Ensure sources key always exists so the web app can safely append to it.
    config.setdefault("sources", [])

    return config


def _require(obj: dict, key: str, config_path: str) -> None:
    if key not in obj:
        print(f"Error: '{key}' is required in {config_path}", file=sys.stderr)
        sys.exit(1)


def _start_web(config: dict, db_path: str, config_path: str) -> None:
    """
    Start the Argus web dashboard.

    Launches the APScheduler background scheduler (non-blocking) so digests
    still run on the configured cron, then hands the main thread to uvicorn.
    Railway (and other PaaS providers) set the PORT environment variable;
    we fall back to 8000 for local development.

    Args:
        config:      Parsed config dict (mutated in-place by source CRUD).
        db_path:     SQLite file path.
        config_path: Path to config.yaml (for persisting source changes).
    """
    import uvicorn

    from argus.web import create_app, _run_digest_safe

    log = logging.getLogger(__name__)
    port = int(os.environ.get("PORT", 8000))

    # Build the FastAPI app first so we have app.state to wire things into.
    app = create_app(config, db_path, config_path)

    # Start the background cron scheduler.
    # We pass a lambda that calls _run_digest_safe(app) so the scheduler
    # respects the same run_lock that the "Run Now" button uses — preventing
    # concurrent runs between scheduled and manual triggers.
    def scheduled_run():
        _run_digest_safe(app)

    bg_scheduler = scheduler_module.start_background_scheduler(
        config=config,
        db_path=db_path,
        run_fn=scheduled_run,
    )
    app.state.bg_scheduler = bg_scheduler

    log.info("Argus web dashboard starting on http://0.0.0.0:%d", port)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",  # uvicorn access logs are noisy; our own logger handles info
    )


def main() -> None:
    load_dotenv()
    _configure_logging()

    parser = argparse.ArgumentParser(
        prog="argus",
        description="Argus — Competitive Intelligence Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
commands:
  run        Execute one digest cycle immediately and exit.
  schedule   Start the cron scheduler (blocks; no web UI).
  web        Start the web dashboard + background scheduler (Railway-ready).

examples:
  python main.py run
  python main.py web
  python main.py run --config /etc/argus/config.yaml
  python main.py schedule
        """,
    )
    parser.add_argument(
        "command",
        choices=["run", "schedule", "web"],
        help="Command to execute.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        metavar="PATH",
        help="Path to config.yaml (default: ./config.yaml)",
    )

    args = parser.parse_args()
    db_path = None  # resolved after config load

    if args.command == "web":
        # Web mode: email settings are optional (user may only want the dashboard).
        config = _load_config(args.config, require_email=False)
        db_path = config.get("database", {}).get("path", "argus.db")
        _start_web(config, db_path, args.config)

    elif args.command == "run":
        config = _load_config(args.config, require_email=False)
        db_path = config.get("database", {}).get("path", "argus.db")
        scheduler_module.run_digest(config, db_path)

    elif args.command == "schedule":
        config = _load_config(args.config, require_email=False)
        db_path = config.get("database", {}).get("path", "argus.db")
        scheduler_module.start_scheduler(config, db_path)


if __name__ == "__main__":
    main()
