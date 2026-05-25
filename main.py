"""
main.py — CLI entrypoint for Argus.

Usage:
    python main.py run                    # Run one digest cycle immediately
    python main.py schedule               # Start the cron scheduler (blocks)
    python main.py run --config my.yaml   # Use a custom config file

Environment variables (put them in a .env file — see .env.example):
    ANTHROPIC_API_KEY   Required for LLM summarization
    SMTP_USER           Required when email.provider = smtp
    SMTP_PASSWORD       Required when email.provider = smtp
    SENDGRID_API_KEY    Required when email.provider = sendgrid
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from argus import scheduler


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


def _load_config(config_path: str) -> dict:
    """
    Load and minimally validate config.yaml.

    Args:
        config_path: Path to the YAML config file.

    Returns:
        Parsed config as a dict.

    Raises:
        SystemExit: On missing file or invalid YAML.
    """
    path = Path(config_path)
    if not path.exists():
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        print("Copy config.yaml.example to config.yaml and fill in your settings.", file=sys.stderr)
        sys.exit(1)

    try:
        with path.open() as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        print(f"Error: could not parse {config_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    if not config:
        print(f"Error: {config_path} is empty.", file=sys.stderr)
        sys.exit(1)

    # Required fields check — fail early with a clear message.
    _require(config, "sources", config_path)
    _require(config, "email", config_path)
    _require(config["email"], "from", config_path)
    _require(config["email"], "to", config_path)

    return config


def _require(obj: dict, key: str, config_path: str) -> None:
    if key not in obj:
        print(f"Error: '{key}' is required in {config_path}", file=sys.stderr)
        sys.exit(1)


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
  schedule   Start the scheduler (runs on the cron from config.yaml). Blocks.

examples:
  python main.py run
  python main.py run --config /etc/argus/config.yaml
  python main.py schedule
        """,
    )
    parser.add_argument(
        "command",
        choices=["run", "schedule"],
        help="'run' for a one-shot digest; 'schedule' to start the cron loop.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        metavar="PATH",
        help="Path to config.yaml (default: ./config.yaml)",
    )

    args = parser.parse_args()
    config = _load_config(args.config)
    db_path = config.get("database", {}).get("path", "argus.db")

    if args.command == "run":
        scheduler.run_digest(config, db_path)
    elif args.command == "schedule":
        scheduler.start_scheduler(config, db_path)


if __name__ == "__main__":
    main()
