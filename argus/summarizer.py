"""
summarizer.py — LLM-powered change summarization for Argus.

Calls the OpenAI API (gpt-4o) to turn raw before/after page text into
concise, actionable competitive intelligence summaries.

Key design decisions:
- Model: gpt-4o — best balance of reasoning quality and cost for this task.
- OpenAI caches prompt prefixes automatically for prompts > 1024 tokens;
  no explicit cache_control markers are needed. Cached token counts are
  surfaced in usage.prompt_tokens_details.cached_tokens when available.
- Rate limit and transient server errors are retried with exponential backoff
  (up to 4 attempts) before propagating.
"""

import logging
import time
from dataclasses import dataclass

import openai
from openai import APIStatusError, RateLimitError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = "gpt-4o"
MAX_TOKENS = 1024
MAX_RETRIES = 4

# Maximum characters of page text we include in the prompt. Long pages are
# truncated from the middle to preserve both the opening context and the most
# recent additions (which typically appear at the bottom of blog/news feeds).
_MAX_TEXT_CHARS = 12_000

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a competitive intelligence analyst embedded in a B2B SaaS GTM team.

Your job: review changes on competitor websites, news sources, and industry blogs, then \
produce concise, actionable summaries that help the sales, marketing, and ops team respond quickly.

Guidelines for every summary:
- Lead with WHAT specifically changed (pricing, product features, positioning, hiring signals, \
partnerships, new content, etc.)
- In 1-2 sentences explain WHY it matters to a GTM or ops team
- Explicitly flag competitive threats (e.g. "they now undercut our entry plan") or \
opportunities (e.g. "their blog post signals a pivot away from SMB — potential mid-market gap")
- If the change is genuinely trivial (minor copy tweaks, date stamps, formatting), say so \
briefly — don't over-analyse noise
- Be direct and businesslike; no filler phrases like "It's worth noting that..."
- Format: use ### headings for each distinct topic, then short paragraphs or bullet lists \
under each heading — keep it scannable for a busy GTM reader
- Do not wrap your response in markdown code blocks"""

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ChangeSummary:
    """LLM-generated summary for a single changed source."""

    url: str
    source_name: str
    summary_text: str  # markdown-formatted summary from the model
    input_tokens: int
    cached_tokens: int  # tokens served from OpenAI's automatic prefix cache
    output_tokens: int


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def summarize_change(
    client: openai.OpenAI,
    url: str,
    source_name: str,
    previous_text: str | None,
    current_text: str,
    is_new: bool,
) -> ChangeSummary:
    """
    Call the OpenAI API to summarize what changed on a monitored page.

    Retries up to MAX_RETRIES times on rate-limit or transient server errors,
    using exponential backoff and the `Retry-After` header when available.

    Args:
        client:        An instantiated openai.OpenAI client.
        url:           The URL that changed.
        source_name:   Human-readable name for the source (from config.yaml).
        previous_text: Body text from the last stored snapshot; None if new.
        current_text:  Freshly-scraped body text.
        is_new:        True if this source has no prior snapshot.

    Returns:
        A ChangeSummary with the model's analysis and token usage metadata.

    Raises:
        RuntimeError: If all retries are exhausted.
        openai.APIError: On non-retryable API errors.
    """
    user_message = _build_user_prompt(source_name, url, previous_text, current_text, is_new)

    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
            )

            usage = response.usage
            # OpenAI surfaces cached prefix tokens here when available.
            cached = 0
            if usage and hasattr(usage, "prompt_tokens_details") and usage.prompt_tokens_details:
                cached = getattr(usage.prompt_tokens_details, "cached_tokens", 0) or 0

            logger.info(
                "Summarised %s — tokens: input=%d cached=%d output=%d",
                source_name,
                usage.prompt_tokens if usage else 0,
                cached,
                usage.completion_tokens if usage else 0,
            )

            return ChangeSummary(
                url=url,
                source_name=source_name,
                summary_text=response.choices[0].message.content or "",
                input_tokens=usage.prompt_tokens if usage else 0,
                cached_tokens=cached,
                output_tokens=usage.completion_tokens if usage else 0,
            )

        except RateLimitError as exc:
            # Respect the server's Retry-After header when present.
            retry_after = int(exc.response.headers.get("retry-after", "30"))
            logger.warning(
                "Rate limited on attempt %d/%d — sleeping %ds",
                attempt,
                MAX_RETRIES,
                retry_after,
            )
            last_exc = exc
            time.sleep(retry_after + 1)

        except APIStatusError as exc:
            if exc.status_code >= 500 and attempt < MAX_RETRIES:
                wait = 2**attempt  # 2s, 4s, 8s
                logger.warning(
                    "Server error %d on attempt %d/%d — retrying in %ds",
                    exc.status_code,
                    attempt,
                    MAX_RETRIES,
                    wait,
                )
                last_exc = exc
                time.sleep(wait)
            else:
                raise

    raise RuntimeError(
        f"Summarizer exhausted {MAX_RETRIES} retries for {url}"
    ) from last_exc


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_user_prompt(
    source_name: str,
    url: str,
    previous_text: str | None,
    current_text: str,
    is_new: bool,
) -> str:
    """
    Build the user-turn message for the OpenAI API call.

    Args:
        source_name:   Human-readable label for this source.
        url:           The monitored URL.
        previous_text: Last-seen body text; None if this is a new source.
        current_text:  Freshly-scraped body text (may be truncated).
        is_new:        True when there is no prior baseline.

    Returns:
        A formatted string to use as the user message content.
    """
    current_excerpt = _truncate_text(current_text)

    if is_new:
        return f"""New source added to monitoring: **{source_name}**
URL: {url}

This is the first time we've captured this page. Please summarise what this source covers \
and highlight any immediately notable competitive signals (pricing, positioning, product claims, \
recent announcements, etc.) that the GTM team should be aware of.

--- PAGE CONTENT ---
{current_excerpt}
--- END ---"""

    previous_excerpt = _truncate_text(previous_text or "")
    return f"""Source: **{source_name}**
URL: {url}

Content has changed since the last check. Please analyse what specifically changed and what \
it means for our competitive positioning.

--- PREVIOUS CONTENT ---
{previous_excerpt}
--- END PREVIOUS ---

--- CURRENT CONTENT ---
{current_excerpt}
--- END CURRENT ---"""


def _truncate_text(text: str, max_chars: int = _MAX_TEXT_CHARS) -> str:
    """
    Truncate long text, preserving both the start and end.

    The beginning of a page typically contains the most important content
    (headlines, value propositions). The end of a blog/news feed contains
    the newest entries. We keep both and drop the middle.

    Args:
        text:      The text to truncate.
        max_chars: Maximum number of characters to keep.

    Returns:
        The original text if within the limit, or a truncated version with
        a "[... content truncated ...]" marker in the middle.
    """
    if len(text) <= max_chars:
        return text

    half = max_chars // 2
    return (
        text[:half]
        + "\n\n[... content truncated for length ...]\n\n"
        + text[-half:]
    )
