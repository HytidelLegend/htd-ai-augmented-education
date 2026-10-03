"""Deterministic helpers for locating and applying text-span edits."""

from __future__ import annotations

from typing import Any


def find_literal_spans(text: str, query: str) -> list[tuple[int, int]]:
    if not query:
        raise ValueError("查询文本不能为空")
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        index = text.find(query, start)
        if index < 0:
            return spans
        spans.append((index, index + len(query)))
        start = index + 1


def validate_text_edits(edits: list[dict[str, Any]], text_length: int) -> list[dict[str, Any]]:
    ordered = sorted(edits, key=lambda item: (int(item["start"]), int(item["end"])))
    previous_end = 0
    for index, edit in enumerate(ordered):
        start = int(edit["start"])
        end = int(edit["end"])
        if start < 0 or end <= start or end > text_length:
            raise ValueError(f"文本修改范围无效：[{start}, {end})")
        if index and start < previous_end:
            raise ValueError("文本修改范围不能重叠")
        previous_end = end
    return ordered


def apply_text_edits(text: str, edits: list[dict[str, Any]]) -> str:
    ordered = validate_text_edits(edits, len(text))
    parts: list[str] = []
    cursor = 0
    for edit in ordered:
        start = int(edit["start"])
        end = int(edit["end"])
        parts.append(text[cursor:start])
        parts.append(str(edit["replacement"]))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def spans_overlap(first_start: int, first_end: int, second_start: int, second_end: int) -> bool:
    return max(first_start, second_start) < min(first_end, second_end)
