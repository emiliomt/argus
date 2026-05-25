"""
scraper.py — HTTP fetch and content extraction for Argus.

Fetches a URL with requests, strips navigation/boilerplate with BeautifulSoup,
and returns a ScrapedPage dataclass. This module never raises on network
errors — failures are captured in ScrapedPage.error so the orchestrator
can decide how to handle them without crashing the whole run.
"""

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Rotate through a small set of user-agent strings to reduce trivial bot blocks.
# We identify ourselves honestly in the primary UA; the others are fallbacks for
# sites that reject requests from bots they don't recognise.
_USER_AGENTS = [
    "Mozilla/5.0 (compatible; Argus/1.0; +https://github.com/emiliomt/argus)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# Tags that contain navigation, ads, and other boilerplate — strip these before
# hashing and extracting text, so minor nav updates don't trigger false alarms.
_BOILERPLATE_TAGS = [
    "nav",
    "header",
    "footer",
    "aside",
    "script",
    "style",
    "noscript",
    "iframe",
    "form",
    "button",
]

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ScrapedPage:
    """Result of fetching and parsing a single URL."""

    url: str
    title: str  # <title> tag content, or empty string
    main_text: str  # boilerplate-stripped body text, whitespace-normalised
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status_code: int = 0  # HTTP status; 0 means no response (network error)
    error: str | None = None  # non-None on any failure; orchestrator decides next step


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scrape_url(url: str, timeout: int = 15) -> ScrapedPage:
    """
    Fetch a URL and extract its meaningful text content.

    Strips boilerplate (nav, header, footer, scripts, etc.) before returning,
    so minor site-wide changes don't generate spurious diffs.

    On any error — network failure, HTTP error, parse error — returns a
    ScrapedPage with `error` set rather than raising, so one bad URL can't
    abort the whole monitoring run.

    Args:
        url:     The URL to fetch.
        timeout: Request timeout in seconds (default 15).

    Returns:
        A ScrapedPage; check `.error` before trusting `.main_text`.
    """
    headers = {"User-Agent": random.choice(_USER_AGENTS)}

    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        logger.warning("Timeout fetching %s", url)
        return ScrapedPage(url=url, title="", main_text="", error=f"Timeout after {timeout}s")
    except requests.exceptions.ConnectionError as exc:
        logger.warning("Connection error fetching %s: %s", url, exc)
        return ScrapedPage(url=url, title="", main_text="", error=str(exc))
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        logger.warning("HTTP %d for %s", status, url)
        return ScrapedPage(
            url=url,
            title="",
            main_text="",
            status_code=status,
            error=f"HTTP {status}",
        )

    try:
        soup = BeautifulSoup(response.text, "lxml")
    except Exception as exc:  # noqa: BLE001 — catch-all is intentional here
        logger.warning("Parse error for %s: %s", url, exc)
        return ScrapedPage(
            url=url,
            title="",
            main_text="",
            status_code=response.status_code,
            error=f"Parse error: {exc}",
        )

    title = soup.title.get_text(strip=True) if soup.title else ""
    main_text = extract_main_text(soup)

    logger.debug("Scraped %s — %d chars of text", url, len(main_text))

    return ScrapedPage(
        url=url,
        title=title,
        main_text=main_text,
        status_code=response.status_code,
    )


def extract_main_text(soup: BeautifulSoup) -> str:
    """
    Strip boilerplate elements from a parsed page and return normalised text.

    Removes tags that typically contain navigation, ads, and other site chrome
    that changes frequently without signalling meaningful content updates.
    Collapses all whitespace runs to single spaces and strips leading/trailing
    whitespace — making the output stable for hashing.

    Args:
        soup: A BeautifulSoup object for the full page.

    Returns:
        A clean string of the page's meaningful text content.
    """
    # Work on a copy so we don't mutate the caller's soup object.
    soup_copy = BeautifulSoup(str(soup), "lxml")

    for tag_name in _BOILERPLATE_TAGS:
        for tag in soup_copy.find_all(tag_name):
            tag.decompose()

    # Also remove elements that are semantically navigation even without a <nav> tag.
    for tag in soup_copy.find_all(attrs={"role": ["navigation", "banner", "contentinfo"]}):
        tag.decompose()

    raw_text = soup_copy.get_text(separator=" ")

    # Collapse all whitespace (spaces, tabs, newlines) to single spaces.
    normalised = " ".join(raw_text.split())

    return normalised
