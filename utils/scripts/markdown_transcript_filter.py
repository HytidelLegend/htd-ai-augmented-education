"""Deterministic selection of speakable content from Markdown-like copy."""

from __future__ import annotations

import re
from typing import Any


START_HEADING = "开头"
_ATX_HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})(?:[ \t]+(.*?)[ \t]*|[ \t]*)$")
_BLOCKQUOTE_LINE = re.compile(r"^[ \t]*>")
_DISPLAY_DIRECTIVE = re.compile(r"\{\{display:\d+ms\}\}")


def _line_records(text: str) -> list[tuple[int, int, str]]:
    records: list[tuple[int, int, str]] = []
    for match in re.finditer(r"[^\n]*(?:\n|$)", text):
        if match.start() == match.end():
            continue
        raw = match.group(0)
        records.append((match.start(), match.end(), raw[:-1] if raw.endswith("\n") else raw))
    return records


def _heading_text(line: str) -> tuple[int, str] | None:
    match = _ATX_HEADING.fullmatch(line)
    if not match:
        return None
    body = (match.group(2) or "").strip()
    body = re.sub(r"[ \t]+#+[ \t]*$", "", body).strip()
    return len(match.group(1)), body


def _overlaps(start: int, end: int, edits: list[dict[str, Any]]) -> bool:
    return any(max(start, int(item["start"])) < min(end, int(item["end"])) for item in edits)


def scan_markdown_transcript_exclusions(
    text: str,
    *,
    start_heading: str = START_HEADING,
) -> dict[str, Any]:
    """Return traceable deletion edits for content that must not be spoken.

    The first level-one ATX heading whose text exactly matches ``start_heading``
    acts as an optional start anchor. If it is absent, the whole document remains
    eligible. Headings and blockquote lines are deleted as complete lines. A
    standalone display directive is deleted with its line; an inline directive
    is deleted without removing the surrounding prose.
    """

    lines = _line_records(text)
    anchor: dict[str, Any] | None = None
    for start, end, line in lines:
        heading = _heading_text(line)
        if heading == (1, start_heading):
            anchor = {"heading": start_heading, "start": start, "end": end}
            break

    exclusions: list[dict[str, Any]] = []

    def add(rule: str, start: int, end: int) -> None:
        if start >= end or _overlaps(start, end, exclusions):
            return
        exclusions.append(
            {
                "rule": rule,
                "start": start,
                "end": end,
                "original": text[start:end],
                "replacement": "",
            }
        )

    content_start = 0
    if anchor is not None:
        content_start = int(anchor["end"])
        add("excluded_before_start_heading", 0, content_start)

    standalone_display = re.compile(r"^[ \t]*\{\{display:\d+ms\}\}[ \t]*$")
    for start, end, line in lines:
        if end <= content_start:
            continue
        if _heading_text(line) is not None:
            add("excluded_markdown_heading", start, end)
        elif _BLOCKQUOTE_LINE.match(line):
            add("excluded_blockquote_line", start, end)
        elif standalone_display.fullmatch(line):
            add("excluded_display_line", start, end)

    for match in _DISPLAY_DIRECTIVE.finditer(text, content_start):
        add("excluded_display_directive", match.start(), match.end())

    exclusions.sort(key=lambda item: (int(item["start"]), int(item["end"])))
    return {
        "schema_version": "1.0",
        "start_heading": start_heading,
        "start_heading_found": anchor is not None,
        "start_anchor": anchor,
        "exclusions": exclusions,
    }


def mask_exclusions(text: str, exclusions: list[dict[str, Any]]) -> str:
    """Mask excluded characters while retaining source offsets and newlines."""

    characters = list(text)
    for item in exclusions:
        for index in range(int(item["start"]), int(item["end"])):
            if characters[index] != "\n":
                characters[index] = " "
    return "".join(characters)
