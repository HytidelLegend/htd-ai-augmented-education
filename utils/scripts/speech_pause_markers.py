"""Parse deterministic inline speech pause markers shared by transcript and TTS skills."""

from __future__ import annotations

import re
from typing import Any


MARKER_VERSION = "1.0"
MIN_PAUSE_MS = 1
MAX_PAUSE_MS = 60_000
MARKER_PREFIX = "{{pause"
VALID_MARKER = re.compile(r"\{\{pause:(\d+)ms\}\}")


class SpeechPauseMarkerError(ValueError):
    """Raised when a reserved pause marker is malformed or out of range."""


def _location(text: str, offset: int) -> tuple[int, int]:
    line = text.count("\n", 0, offset) + 1
    previous_newline = text.rfind("\n", 0, offset)
    column = offset - previous_newline
    return line, column


def parse_pause_markers(text: str) -> list[dict[str, Any]]:
    """Return validated pause directives using normalized-text character offsets."""
    markers: list[dict[str, Any]] = []
    cursor = 0
    while True:
        start = text.find(MARKER_PREFIX, cursor)
        if start < 0:
            break
        match = VALID_MARKER.match(text, start)
        if match is None:
            line, column = _location(text, start)
            excerpt = text[start : text.find("\n", start) if "\n" in text[start:] else len(text)]
            raise SpeechPauseMarkerError(
                f"停顿指令格式无效（第 {line} 行，第 {column} 列）：{excerpt!r}；"
                "应使用 {{pause:1500ms}}"
            )
        duration_ms = int(match.group(1))
        if not MIN_PAUSE_MS <= duration_ms <= MAX_PAUSE_MS:
            line, column = _location(text, start)
            raise SpeechPauseMarkerError(
                f"停顿时长必须位于 {MIN_PAUSE_MS}～{MAX_PAUSE_MS} ms"
                f"（第 {line} 行，第 {column} 列）：{match.group(0)}"
            )
        end = match.end()
        markers.append(
            {
                "pause_id": f"pause_{len(markers) + 1:03d}",
                "directive": match.group(0),
                "duration_ms": duration_ms,
                "source_span": {"start": start, "end": end},
            }
        )
        cursor = end
    return markers


def remove_pause_markers(text: str, markers: list[dict[str, Any]] | None = None) -> str:
    """Remove only validated pause directives and preserve every other character."""
    parsed = markers if markers is not None else parse_pause_markers(text)
    parts: list[str] = []
    cursor = 0
    for marker in parsed:
        span = marker["source_span"]
        start, end = int(span["start"]), int(span["end"])
        if text[start:end] != marker["directive"] or start < cursor:
            raise SpeechPauseMarkerError("停顿指令 span 与文本不一致")
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def marker_summary(markers: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": MARKER_VERSION,
        "count": len(markers),
        "total_duration_ms": sum(int(item["duration_ms"]) for item in markers),
    }
