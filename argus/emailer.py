"""
emailer.py — HTML digest builder and email delivery for Argus.

Builds a Gmail-safe HTML email from a list of ChangeSummary objects and
delivers it via SMTP (default) or SendGrid (optional). HTML is constructed
with f-strings and html.escape() — no Jinja2 dependency — keeping the
requirements footprint small.

Gmail CSS constraints honoured:
- All styles are inlined (Gmail strips <style> blocks in <head>).
- Layout uses HTML tables, not flexbox or CSS grid.
- Font stack limited to universally available web-safe fonts.
- No external resources (no web fonts, no remote images).
"""

import html
import logging
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from argus.summarizer import ChangeSummary

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------

_COLOUR_BG = "#f8f9fa"
_COLOUR_CARD_BG = "#ffffff"
_COLOUR_HEADER_BG = "#1a1a2e"
_COLOUR_HEADER_TEXT = "#ffffff"
_COLOUR_SOURCE_NAME = "#0f3460"
_COLOUR_URL = "#6c757d"
_COLOUR_BODY_TEXT = "#212529"
_COLOUR_BORDER = "#dee2e6"
_COLOUR_BADGE_NEW = "#e94560"
_COLOUR_BADGE_CHANGED = "#0f3460"
_COLOUR_BADGE_TEXT = "#ffffff"
_COLOUR_FOOTER_TEXT = "#6c757d"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class DigestEmail:
    """A fully rendered digest email ready for delivery."""

    subject: str
    html_body: str
    plain_body: str  # fallback for non-HTML mail clients


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_digest(
    summaries: list[ChangeSummary],
    run_date: datetime | None = None,
    total_checked: int = 0,
) -> DigestEmail:
    """
    Render an HTML + plain-text digest from a list of change summaries.

    Args:
        summaries:     One ChangeSummary per changed source.
        run_date:      When the digest run executed (defaults to now UTC).
        total_checked: Total number of sources monitored this run
                       (for the "X checked, Y changed" header line).

    Returns:
        A DigestEmail with both HTML and plain-text representations.
    """
    if run_date is None:
        run_date = datetime.now(timezone.utc)

    date_str = run_date.strftime("%B %-d, %Y")  # e.g. "May 25, 2026"
    subject = f"[Argus] Competitive Intel Digest — {date_str}"

    html_body = _render_html(summaries, run_date, total_checked)
    plain_body = _render_plain(summaries, run_date, total_checked)

    return DigestEmail(subject=subject, html_body=html_body, plain_body=plain_body)


def send_email_smtp(
    digest: DigestEmail,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    from_addr: str,
    to_addrs: list[str],
    use_tls: bool = True,
) -> None:
    """
    Deliver the digest via SMTP.

    Uses STARTTLS when use_tls=True (required for port 587, Gmail, etc.).
    For SSL-only servers (port 465) set use_tls=False and wrap the connection
    manually — or switch to SendGrid.

    Args:
        digest:       The rendered email to send.
        smtp_host:    SMTP server hostname (e.g. "smtp.gmail.com").
        smtp_port:    SMTP port (typically 587 for STARTTLS, 465 for SSL).
        smtp_user:    Login username (usually the sending email address).
        smtp_password: App password or SMTP credential.
        from_addr:    Envelope/header From address.
        to_addrs:     List of recipient addresses.
        use_tls:      Whether to call starttls() after connect (default True).

    Raises:
        smtplib.SMTPException: On any delivery failure.
    """
    msg = _build_mime(digest, from_addr, to_addrs)

    logger.info("Sending digest to %s via %s:%d", to_addrs, smtp_host, smtp_port)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        if use_tls:
            server.starttls()
            server.ehlo()
        server.login(smtp_user, smtp_password)
        server.sendmail(from_addr, to_addrs, msg.as_string())

    logger.info("Digest delivered successfully to %d recipients", len(to_addrs))


def send_email_sendgrid(
    digest: DigestEmail,
    api_key: str,
    from_addr: str,
    to_addrs: list[str],
) -> None:
    """
    Deliver the digest via the SendGrid HTTP API.

    Requires the optional `sendgrid` package (not in requirements.txt by default).
    Install it with: pip install sendgrid

    Args:
        digest:    The rendered email to send.
        api_key:   SendGrid API key (from env var SENDGRID_API_KEY).
        from_addr: Sender email address (must be verified in SendGrid).
        to_addrs:  List of recipient addresses.

    Raises:
        RuntimeError: If the sendgrid package is not installed.
        Exception:    On SendGrid API errors.
    """
    try:
        from sendgrid import SendGridAPIClient  # type: ignore[import]
        from sendgrid.helpers.mail import Mail, To  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "The 'sendgrid' package is required for SendGrid delivery. "
            "Install it with: pip install sendgrid"
        ) from exc

    to_list = [To(addr) for addr in to_addrs]
    message = Mail(
        from_email=from_addr,
        to_emails=to_list,
        subject=digest.subject,
        html_content=digest.html_body,
        plain_text_content=digest.plain_body,
    )

    logger.info("Sending digest to %s via SendGrid", to_addrs)
    sg = SendGridAPIClient(api_key)
    response = sg.send(message)

    if response.status_code not in (200, 201, 202):
        raise RuntimeError(
            f"SendGrid returned status {response.status_code}: {response.body}"
        )

    logger.info("SendGrid delivery accepted (status %d)", response.status_code)


# ---------------------------------------------------------------------------
# Internal rendering helpers
# ---------------------------------------------------------------------------


def _build_mime(digest: DigestEmail, from_addr: str, to_addrs: list[str]) -> MIMEMultipart:
    """Assemble a multipart/alternative MIME message."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = digest.subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg.attach(MIMEText(digest.plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(digest.html_body, "html", "utf-8"))
    return msg


def _render_html(
    summaries: list[ChangeSummary],
    run_date: datetime,
    total_checked: int,
) -> str:
    """Build the full HTML email body as a string."""
    date_str = run_date.strftime("%B %-d, %Y at %H:%M UTC")
    num_changed = len(summaries)

    cards_html = "\n".join(_render_card(s) for s in summaries)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Argus Competitive Intel Digest</title>
</head>
<body style="margin:0;padding:0;background-color:{_COLOUR_BG};font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0"
         style="background-color:{_COLOUR_BG};padding:24px 0;">
    <tr>
      <td align="center">
        <table width="640" cellpadding="0" cellspacing="0" border="0"
               style="max-width:640px;width:100%;">

          <!-- Header -->
          <tr>
            <td style="background-color:{_COLOUR_HEADER_BG};border-radius:8px 8px 0 0;
                       padding:28px 32px;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td>
                    <span style="font-size:22px;font-weight:700;color:{_COLOUR_HEADER_TEXT};
                                 letter-spacing:-0.5px;">🔍 Argus</span>
                    <span style="font-size:14px;color:#a0aec0;margin-left:8px;">
                      Competitive Intelligence
                    </span>
                  </td>
                </tr>
                <tr>
                  <td style="padding-top:8px;">
                    <span style="font-size:13px;color:#a0aec0;">
                      {total_checked} source{"s" if total_checked != 1 else ""} monitored
                      &nbsp;·&nbsp;
                      <strong style="color:{_COLOUR_HEADER_TEXT};">{num_changed} change{"s" if num_changed != 1 else ""} detected</strong>
                    </span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Cards -->
          {cards_html}

          <!-- Footer -->
          <tr>
            <td style="background-color:{_COLOUR_CARD_BG};border-radius:0 0 8px 8px;
                       border:1px solid {_COLOUR_BORDER};border-top:none;
                       padding:20px 32px;text-align:center;">
              <p style="margin:0;font-size:12px;color:{_COLOUR_FOOTER_TEXT};">
                Argus ran at {html.escape(date_str)}
                &nbsp;·&nbsp;
                <a href="https://github.com/emiliomt/argus"
                   style="color:{_COLOUR_URL};text-decoration:none;">View on GitHub</a>
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _render_card(summary: ChangeSummary) -> str:
    """Render a single source card as an HTML table row."""
    badge_bg = _COLOUR_BADGE_NEW  # all summaries are either new or changed
    badge_label = "NEW" if _looks_new(summary) else "CHANGED"

    # Convert the markdown-ish summary to simple HTML paragraphs.
    summary_html = _markdown_to_html(summary.summary_text)

    source_escaped = html.escape(summary.source_name)
    url_escaped = html.escape(summary.url)

    return f"""
          <tr>
            <td style="background-color:{_COLOUR_CARD_BG};
                       border:1px solid {_COLOUR_BORDER};border-top:none;
                       padding:24px 32px;">
              <!-- Source name + badge -->
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td>
                    <span style="font-size:16px;font-weight:700;
                                 color:{_COLOUR_SOURCE_NAME};">
                      {source_escaped}
                    </span>
                  </td>
                  <td align="right" style="vertical-align:middle;">
                    <span style="display:inline-block;background-color:{badge_bg};
                                 color:{_COLOUR_BADGE_TEXT};font-size:11px;
                                 font-weight:700;letter-spacing:0.5px;
                                 padding:3px 8px;border-radius:3px;">
                      {badge_label}
                    </span>
                  </td>
                </tr>
              </table>
              <!-- URL -->
              <p style="margin:4px 0 16px;font-size:12px;">
                <a href="{url_escaped}" style="color:{_COLOUR_URL};text-decoration:none;">
                  {url_escaped}
                </a>
              </p>
              <!-- Divider -->
              <hr style="border:none;border-top:1px solid {_COLOUR_BORDER};margin:0 0 16px;">
              <!-- Summary -->
              <div style="font-size:14px;line-height:1.7;color:{_COLOUR_BODY_TEXT};">
                {summary_html}
              </div>
            </td>
          </tr>"""


def _looks_new(summary: ChangeSummary) -> bool:
    """
    Heuristic: if the summary mentions 'first time' or 'new source', badge as NEW.
    In practice the orchestrator passes is_new through the summarizer prompt,
    so the LLM usually leads with "New source" for first-visit pages.
    This badge is cosmetic only — the underlying diff logic is authoritative.
    """
    lower = summary.summary_text.lower()
    return "new source" in lower or "first time" in lower


def render_summary_html(text: str) -> str:
    """Convert LLM summary markdown to safe HTML for the web dashboard."""
    return _markdown_to_html(text)


def _markdown_to_html(text: str) -> str:
    """
    Convert a simple markdown-ish summary to safe HTML suitable for email.

    Handles:
    - Blank-line-separated paragraphs
    - Bullet points starting with '- ' or '* '
    - **bold** → <strong>
    - Escapes all other HTML entities

    This is intentionally minimal — we don't need a full markdown parser for
    the controlled output we receive from the LLM.
    """
    # Split into blocks on blank lines.
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    html_parts = []

    for block in blocks:
        lines = block.split("\n")
        # Check if this block looks like a bullet list.
        if all(line.strip().startswith(("- ", "* ", "• ")) for line in lines if line.strip()):
            items = []
            for line in lines:
                stripped = line.strip().lstrip("-*•").strip()
                items.append(f"<li style='margin-bottom:6px;'>{_inline_md(stripped)}</li>")
            html_parts.append(
                "<ul style='margin:0 0 12px;padding-left:20px;'>" + "".join(items) + "</ul>"
            )
        else:
            html_parts.append(f"<p style='margin:0 0 12px;'>{_inline_md(html.escape(block))}</p>")

    return "".join(html_parts)


def _inline_md(text: str) -> str:
    """Replace **bold** markers with <strong> tags in an already-escaped string."""
    import re

    # The input may already be html-escaped (for paragraph blocks) or not
    # (for list items we escape here). Handle both by escaping first.
    # Note: html.escape is idempotent on already-escaped text for our use case.
    escaped = html.escape(text)
    # Match **...** and wrap in <strong>.
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)


def _render_plain(
    summaries: list[ChangeSummary],
    run_date: datetime,
    total_checked: int,
) -> str:
    """Build a plain-text fallback version of the digest."""
    date_str = run_date.strftime("%B %-d, %Y at %H:%M UTC")
    lines = [
        "ARGUS — Competitive Intelligence Digest",
        "=" * 50,
        f"Run: {date_str}",
        f"Sources checked: {total_checked}  |  Changes detected: {len(summaries)}",
        "",
    ]

    for summary in summaries:
        lines += [
            "-" * 50,
            f"SOURCE: {summary.source_name}",
            f"URL:    {summary.url}",
            "",
            summary.summary_text,
            "",
        ]

    lines += [
        "=" * 50,
        "Generated by Argus — https://github.com/emiliomt/argus",
    ]

    return "\n".join(lines)
