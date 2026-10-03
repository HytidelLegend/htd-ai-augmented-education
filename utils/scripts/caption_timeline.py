"""Build deterministic caption events from TTS word-level timestamps."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Iterable

from .media_text_overlay import (
    ASS_STYLE_FIELDS,
    parse_ass_style_line,
    render_caption_ass as render_ass,
)


class CaptionTimelineError(ValueError):
    """Raised when transcript timestamps cannot produce safe captions."""


PUNCTUATION = frozenset("，。！？；：、“”‘’（）【】《》…—·,.!?;:\"'()[]<>")
TRAILING_PUNCTUATION_RE = re.compile(
    r"(?:[，。！？；：,.!?;:]+[”’）》】）)\]}>]*)+\s*$"
)
CLOSING_WRAPPERS = frozenset("”’）》】）)]}>")
BOUNDARY_CHARACTERS = {
    "sentence_end": frozenset("。."),
    "question": frozenset("？?"),
    "exclamation": frozenset("！!"),
    "semicolon": frozenset("；;"),
    "colon": frozenset("：:"),
    "comma": frozenset("，,"),
}


def alignment_text(value: str) -> str:
    """Remove whitespace while preserving all spoken punctuation and characters."""
    return "".join(value.split())


def effective_char_count(
    value: str,
    *,
    count_whitespace: bool = False,
    count_punctuation: bool = False,
) -> int:
    """Count caption characters under the configured display policy."""
    return sum(
        1
        for char in value
        if (count_whitespace or not char.isspace())
        and (count_punctuation or char not in PUNCTUATION)
    )


def _boundary_kind(value: str) -> str | None:
    stripped = value.rstrip()
    while stripped and stripped[-1] in CLOSING_WRAPPERS:
        stripped = stripped[:-1].rstrip()
    if not stripped:
        return None
    last = stripped[-1]
    for kind, characters in BOUNDARY_CHARACTERS.items():
        if last in characters:
            return kind
    return None


def _validate_items(items: list[dict[str, Any]], duration_ms: int) -> None:
    if not items:
        raise CaptionTimelineError("full.timestamps.json 的 items 不能为空")
    previous_end = 0
    for index, item in enumerate(items):
        try:
            text = str(item["text"])
            start_ms = int(item["start_ms"])
            end_ms = int(item["end_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CaptionTimelineError(f"items[{index}] 缺少合法文本或时间字段") from exc
        if not text or not alignment_text(text):
            raise CaptionTimelineError(f"items[{index}].text 不能为空")
        if start_ms < previous_end or end_ms <= start_ms or end_ms > duration_ms:
            raise CaptionTimelineError(
                f"items[{index}] 时间必须单调、不重叠、为正且不越界"
            )
        previous_end = end_ms


def _sentence_item_ranges(
    sentences: list[dict[str, Any]], items: list[dict[str, Any]]
) -> list[tuple[int, int]]:
    if not sentences:
        raise CaptionTimelineError("full.timestamps.json 的 sentences 不能为空")
    ranges: list[tuple[int, int]] = []
    item_index = 0
    for sentence_index, sentence in enumerate(sentences):
        target = alignment_text(str(sentence.get("text", "")))
        if not target:
            raise CaptionTimelineError(f"sentences[{sentence_index}].text 不能为空")
        start = item_index
        accumulated = ""
        while item_index < len(items) and len(accumulated) < len(target):
            accumulated += alignment_text(str(items[item_index]["text"]))
            item_index += 1
        if accumulated != target:
            raise CaptionTimelineError(
                f"sentences[{sentence_index}] 无法与词级 items 精确对齐"
            )
        ranges.append((start, item_index))
    if item_index != len(items):
        raise CaptionTimelineError("sentences 未覆盖全部词级 items")
    return ranges


def _punctuation_sentence_ranges(items: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """Derive stable sentence ranges when API sentence text disagrees with word items."""
    ranges: list[tuple[int, int]] = []
    start = 0
    for index, item in enumerate(items, 1):
        if _boundary_kind(str(item["text"])) in {"sentence_end", "question", "exclamation"}:
            if (
                ranges
                and start == index - 1
                and effective_char_count(str(item["text"])) == 0
            ):
                previous_start, _ = ranges[-1]
                ranges[-1] = (previous_start, index)
                start = index
                continue
            ranges.append((start, index))
            start = index
    if start < len(items):
        ranges.append((start, len(items)))
    if not ranges:
        raise CaptionTimelineError("无法从词级 items 推导句子边界")
    return ranges


def has_trailing_sentence_punctuation(value: str) -> bool:
    """Return whether the display text still ends in removable sentence punctuation."""
    return TRAILING_PUNCTUATION_RE.search(value) is not None


def _choose_end(
    items: list[dict[str, Any]],
    start: int,
    end: int,
    *,
    max_chars: int,
    soft_limit: int,
    priority: list[str],
    count_whitespace: bool,
    count_punctuation: bool,
) -> tuple[int, str]:
    rank = {kind: index for index, kind in enumerate(priority)}
    total = 0
    last_fit: int | None = None
    candidates: list[tuple[int, str]] = []
    for index in range(start, end):
        item_count = effective_char_count(
            str(items[index]["text"]),
            count_whitespace=count_whitespace,
            count_punctuation=count_punctuation,
        )
        if item_count > max_chars:
            raise CaptionTimelineError(
                f"items[{index}] 单项有效字符数 {item_count} 超过限制 {max_chars}，"
                "无法在不估算词内时间戳的前提下切分"
            )
        if total + item_count > max_chars:
            break
        total += item_count
        last_fit = index + 1
        boundary = _boundary_kind(str(items[index]["text"]))
        if total >= soft_limit and boundary:
            candidates.append((index + 1, boundary))
    if last_fit is None:
        raise CaptionTimelineError("无法为字幕找到合法的词级切分边界")
    if last_fit == end:
        return end, "sentence_end"
    if candidates:
        selected_end, kind = min(
            candidates,
            key=lambda item: (rank.get(item[1], len(rank)), -item[0]),
        )
        return selected_end, kind
    return last_fit, "item"


def build_caption_events(
    transcript: str,
    timestamps: dict[str, Any],
    *,
    max_chars: int = 20,
    count_whitespace: bool = False,
    count_punctuation: bool = False,
    soft_limit_ratio: float = 0.7,
    remove_trailing_punctuation: bool = True,
    boundary_priority: Iterable[str] = (
        "sentence_end",
        "question",
        "exclamation",
        "semicolon",
        "colon",
        "comma",
        "item",
    ),
) -> list[dict[str, Any]]:
    """Return caption events aligned to the original TTS word boundaries."""
    if not transcript.strip():
        raise CaptionTimelineError("逐字稿不能为空")
    if not isinstance(max_chars, int) or max_chars < 1:
        raise CaptionTimelineError("max_chars 必须为正整数")
    if not isinstance(soft_limit_ratio, (int, float)) or not math.isfinite(soft_limit_ratio):
        raise CaptionTimelineError("soft_limit_ratio 必须是有限数值")
    if not 0 < float(soft_limit_ratio) <= 1:
        raise CaptionTimelineError("soft_limit_ratio 必须位于 (0, 1]")
    if timestamps.get("schema_version") != "1.1":
        raise CaptionTimelineError("时间戳只支持 schema_version 1.1")
    if timestamps.get("time_unit") != "ms":
        raise CaptionTimelineError("时间戳 time_unit 必须为 ms")
    if timestamps.get("interval_semantics") != "[start_ms, end_ms)":
        raise CaptionTimelineError("时间戳必须使用半开区间 [start_ms, end_ms)")
    try:
        duration_ms = int(timestamps["duration_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CaptionTimelineError("时间戳 duration_ms 必须为正整数") from exc
    if duration_ms <= 0:
        raise CaptionTimelineError("时间戳 duration_ms 必须为正整数")
    items = timestamps.get("items")
    sentences = timestamps.get("sentences")
    if not isinstance(items, list) or not isinstance(sentences, list):
        raise CaptionTimelineError("时间戳必须包含 items 和 sentences 数组")
    _validate_items(items, duration_ms)
    if alignment_text("".join(str(item["text"]) for item in items)) != alignment_text(transcript):
        raise CaptionTimelineError("词级 items 拼接后与逐字稿正文不一致")
    try:
        sentence_ranges = _sentence_item_ranges(sentences, items)
    except CaptionTimelineError:
        # Some TTS responses shift quote marks or short words between sentence entries
        # while their word-level items remain complete and exact. Word items are the
        # authoritative timing source, so derive boundaries from their punctuation.
        sentence_ranges = _punctuation_sentence_ranges(items)

    priority = list(boundary_priority)
    if not priority or len(priority) != len(set(priority)):
        raise CaptionTimelineError("boundary_priority 不能为空或包含重复值")
    soft_limit = max(1, math.floor(max_chars * float(soft_limit_ratio)))
    events: list[dict[str, Any]] = []
    for sentence_index, (sentence_start, sentence_end) in enumerate(sentence_ranges, 1):
        cursor = sentence_start
        while cursor < sentence_end:
            selected_end, boundary = _choose_end(
                items,
                cursor,
                sentence_end,
                max_chars=max_chars,
                soft_limit=soft_limit,
                priority=priority,
                count_whitespace=count_whitespace,
                count_punctuation=count_punctuation,
            )
            source_text = "".join(str(item["text"]) for item in items[cursor:selected_end])
            display_text = (
                TRAILING_PUNCTUATION_RE.sub("", source_text)
                if remove_trailing_punctuation
                else source_text.rstrip()
            )
            if not display_text:
                raise CaptionTimelineError(
                    "移除句尾标点后字幕为空："
                    f"sentence_index={sentence_index}，"
                    f"item_range=[{cursor}, {selected_end})，"
                    f"source_text={source_text!r}"
                )
            char_count = effective_char_count(
                display_text,
                count_whitespace=count_whitespace,
                count_punctuation=count_punctuation,
            )
            if char_count > max_chars:
                raise CaptionTimelineError("字幕切分后仍超过 max_chars")
            events.append(
                {
                    "caption_index": len(events) + 1,
                    "sentence_index": sentence_index,
                    "text": display_text,
                    "source_text": source_text,
                    "start_ms": int(items[cursor]["start_ms"]),
                    "end_ms": int(items[selected_end - 1]["end_ms"]),
                    "item_range": {"start": cursor, "end": selected_end},
                    "char_count": char_count,
                    "boundary": boundary,
                }
            )
            cursor = selected_end
    if not events:
        raise CaptionTimelineError("未生成任何字幕事件")
    if alignment_text("".join(event["source_text"] for event in events)) != alignment_text(transcript):
        raise CaptionTimelineError("字幕事件无法重建逐字稿")
    return events
