"""
markdown_render.py — Rich, safe HTML rendering for dashboard digest summaries.
"""

from __future__ import annotations

import html
import re

_HEADING = re.compile(r"^(#{1,4})\s+(.+)$")
_ORDERED = re.compile(r"^\d+\.\s+")
_BULLET = re.compile(r"^[-*•]\s+")


def render_digest_markdown(text: str) -> str:
    """
    Convert LLM markdown into structured HTML for the web dashboard.

    Handles headings (###), paragraphs, bullet/numbered lists, bold, italic,
    and inline code. Input is HTML-escaped; known safe tags are emitted.
    """
    text = html.unescape((text or "").strip())
    if not text:
        return ""

    lines = text.split("\n")
    parts: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue

        heading = _HEADING.match(line.strip())
        if heading:
            level = len(heading.group(1))
            parts.append(_heading(level, heading.group(2)))
            i += 1
            continue

        if _is_list_line(line):
            ordered = bool(_ORDERED.match(line.strip()))
            items: list[str] = []
            while i < len(lines) and _is_list_line(lines[i]):
                items.append(_list_item_text(lines[i]))
                i += 1
            tag = "ol" if ordered else "ul"
            lis = "".join(f"<li>{_inline_format(item)}</li>" for item in items)
            parts.append(f'<{tag} class="digest-list">{lis}</{tag}>')
            continue

        para_lines: list[str] = []
        while i < len(lines):
            ln = lines[i].rstrip()
            if not ln.strip():
                break
            if _HEADING.match(ln.strip()) or _is_list_line(ln):
                break
            para_lines.append(ln)
            i += 1
        parts.append(f'<p class="digest-p">{_inline_format(" ".join(para_lines))}</p>')

    return f'<div class="digest-rendered">{"".join(parts)}</div>'


def _is_list_line(line: str) -> bool:
    s = line.strip()
    return bool(_BULLET.match(s) or _ORDERED.match(s))


def _list_item_text(line: str) -> str:
    s = line.strip()
    s = _BULLET.sub("", s)
    s = _ORDERED.sub("", s)
    return s.strip()


def _heading(level: int, title: str) -> str:
    level = max(2, min(4, level))  # h2–h4 for digest hierarchy
    return (
        f'<h{level} class="digest-heading digest-h{level}">'
        f"{_inline_format(title)}</h{level}>"
    )


def _escape_text(text: str) -> str:
    """Escape only characters that break HTML structure (not apostrophes)."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _inline_format(text: str) -> str:
    """Escape text then apply inline markdown (bold, italic, code)."""
    escaped = _escape_text(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", escaped)
    escaped = re.sub(r"`([^`]+)`", r'<code class="digest-code">\1</code>', escaped)
    return escaped
