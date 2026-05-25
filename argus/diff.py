"""
diff.py — Change detection for Argus.

Pure Python module: no I/O, no network, no database. Takes a ScrapedPage and
an optional stored Snapshot, and returns a DiffResult describing what (if
anything) changed. Keeping this logic I/O-free makes it trivial to unit test.

Change detection strategy:
    We hash the boilerplate-stripped body text (not the raw HTML). This means:
    - A new blog post → hash changes → detected
    - Pricing table update → hash changes → detected
    - Nav link counter increments → not detected (nav is stripped)
    - Cookie banner text swap → not detected (stripped)

    The full extracted text is stored in the snapshot, so the summarizer
    receives the actual "before" and "after" prose for context.
"""

import hashlib
import logging
from dataclasses import dataclass

from argus.scraper import ScrapedPage
from argus.storage import Snapshot

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class DiffResult:
    """
    Outcome of comparing a freshly-scraped page to its last-seen snapshot.

    Attributes:
        url:           The monitored URL.
        has_changed:   True when content differs from the stored snapshot,
                       or when no snapshot exists (first time seen).
        is_new:        True when there was no prior snapshot — semantically
                       distinct from a change, because the summarizer prompt
                       will differ ("new source" vs "content updated").
        previous_hash: Hash from the stored snapshot; None if is_new.
        current_hash:  SHA-256 of the freshly-scraped text.
        previous_text: Raw text from the stored snapshot; None if is_new.
        current_text:  Freshly-scraped body text.
    """

    url: str
    has_changed: bool
    is_new: bool
    previous_hash: str | None
    current_hash: str
    previous_text: str | None
    current_text: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_diff(scraped: ScrapedPage, snapshot: Snapshot | None) -> DiffResult:
    """
    Compare a freshly-scraped page to its stored snapshot.

    Args:
        scraped:  The just-fetched page. Must not have an error set — callers
                  should check ScrapedPage.error before calling this function.
        snapshot: The last-stored snapshot for this URL, or None on first visit.

    Returns:
        A DiffResult describing what changed (or that this is a new source).
    """
    current_hash = compute_text_hash(scraped.main_text)
    is_new = snapshot is None

    if is_new:
        logger.info("New source detected: %s", scraped.url)
        return DiffResult(
            url=scraped.url,
            has_changed=True,
            is_new=True,
            previous_hash=None,
            current_hash=current_hash,
            previous_text=None,
            current_text=scraped.main_text,
        )

    has_changed = current_hash != snapshot.content_hash

    if has_changed:
        logger.info("Content change detected: %s", scraped.url)
    else:
        logger.debug("No change: %s", scraped.url)

    return DiffResult(
        url=scraped.url,
        has_changed=has_changed,
        is_new=False,
        previous_hash=snapshot.content_hash,
        current_hash=current_hash,
        previous_text=snapshot.raw_text,
        current_text=scraped.main_text,
    )


def compute_text_hash(text: str) -> str:
    """
    Return the SHA-256 hex digest of a UTF-8 encoded text string.

    Used to produce stable, compact fingerprints for page content comparison.

    Args:
        text: The normalised body text of a scraped page.

    Returns:
        A 64-character lowercase hex string.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
