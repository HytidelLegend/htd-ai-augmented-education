"""Deterministically locate mechanical transforms and bounded semantic decisions."""

from __future__ import annotations

import re
from typing import Any

from utils.scripts.markdown_transcript_filter import (
    mask_exclusions,
    scan_markdown_transcript_exclusions,
)
from utils.scripts.speech_pause_markers import MARKER_VERSION, parse_pause_markers


RULES_VERSION = "1.4"
UNSPOKEN_SYMBOLS = "「」《》【】[]（）()"
EMAIL_DOMAIN_AFTER_AT = re.compile(
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}\.)+[A-Za-z]{2,63}(?![A-Za-z0-9-])"
)
WRITTEN_REPLACEMENTS = {
    "因此": ["因此", "所以"],
    "然而": ["然而", "不过"],
    "此外": ["此外", "另外"],
    "例如": ["例如", "比如"],
}


def _spoken_integer(value: str) -> str:
    """Render a positive list index in ordinary spoken Chinese."""
    number = int(value)
    if number <= 0 or number > 9999:
        return value
    digits = "零一二三四五六七八九"
    units = ("", "十", "百", "千")
    source = str(number)
    parts: list[str] = []
    pending_zero = False
    for index, character in enumerate(source):
        digit = int(character)
        position = len(source) - index - 1
        if digit == 0:
            pending_zero = bool(parts)
            continue
        if pending_zero:
            parts.append("零")
            pending_zero = False
        if not (digit == 1 and position == 1 and not parts):
            parts.append(digits[digit])
        parts.append(units[position])
    return "".join(parts)


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(max(start, left) < min(end, right) for left, right in spans)


def _at_sign_replacement(text: str, start: int) -> str:
    """Speak email-domain at-signs and drop social-handle at-signs."""
    return "艾特" if EMAIL_DOMAIN_AFTER_AT.match(text, start) else ""


def _normalize_at_signs(text: str) -> str:
    """Apply the deterministic at-sign policy inside a replacement string."""
    return "".join(
        _at_sign_replacement(text, index) if character == "@" else character
        for index, character in enumerate(text)
    )


def _line_edits(text: str, protected: list[tuple[int, int]]) -> list[dict[str, Any]]:
    edits: list[dict[str, Any]] = []
    patterns = [
        ("markdown_heading", re.compile(r"(?m)^( {0,3})#{1,6}[ \t]+"), lambda match: match.group(1)),
        ("unordered_list_marker", re.compile(r"(?m)^(\s*)[-+*][ \t]+"), lambda match: match.group(1)),
        (
            "ordered_list_marker",
            re.compile(r"(?m)^(\s*)(\d+)[.)、][ \t]+"),
            lambda match: f"{match.group(1)}第{_spoken_integer(match.group(2))}，",
        ),
        ("horizontal_rule", re.compile(r"(?m)^[ \t]*(?:---+|___+|\*\*\*+)[ \t]*$"), lambda match: ""),
        (
            "spoken_summary_label",
            re.compile(r"(?m)^([ \t]*)总结[：:]"),
            lambda match: f"{match.group(1)}总结一下，",
        ),
    ]
    for rule, pattern, replacement in patterns:
        for match in pattern.finditer(text):
            if _overlaps(match.start(), match.end(), protected):
                continue
            edits.append(
                {
                    "rule": rule,
                    "start": match.start(),
                    "end": match.end(),
                    "original": match.group(0),
                    "replacement": replacement(match),
                }
            )
    return edits


def scan_text(text: str) -> dict[str, Any]:
    """Return non-overlapping mechanical edits and semantic candidates."""
    content_selection = scan_markdown_transcript_exclusions(text)
    exclusions = list(content_selection["exclusions"])
    exclusion_spans = [(int(item["start"]), int(item["end"])) for item in exclusions]
    pause_directives = parse_pause_markers(mask_exclusions(text, exclusions))
    pause_spans = [
        (int(item["source_span"]["start"]), int(item["source_span"]["end"]))
        for item in pause_directives
    ]
    mechanical = exclusions + _line_edits(text, exclusion_spans)
    occupied = [(item["start"], item["end"]) for item in mechanical] + pause_spans
    semantic: list[dict[str, Any]] = []

    formula_spans: list[tuple[int, int]] = []
    formula_pattern = re.compile(r"\${1,2}([^$\n]+?)\${1,2}")
    for match in formula_pattern.finditer(text):
        if _overlaps(match.start(), match.end(), occupied):
            continue
        formula_spans.append((match.start(), match.end()))
        semantic.append(
            {
                "kind": "formula",
                "start": match.start(),
                "end": match.end(),
                "original": match.group(0),
                "allowed_replacements": [],
                "custom_replacement": True,
                "hint": "填写完整、可朗读且不含换行的公式读法",
            }
        )

    link_pattern = re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")
    link_spans: list[tuple[int, int]] = []
    for match in link_pattern.finditer(text):
        if _overlaps(match.start(), match.end(), occupied + formula_spans):
            continue
        link_spans.append((match.start(), match.end()))
        mechanical.append(
            {
                "rule": "markdown_link",
                "start": match.start(),
                "end": match.end(),
                "original": match.group(0),
                "replacement": _normalize_at_signs(match.group(1)),
            }
        )

    protected = occupied + formula_spans + link_spans
    marker_pattern = re.compile(r"(?:\*\*|__|~~|`|(?<!\*)\*(?!\*)|(?<!_)_(?!_))")
    for match in marker_pattern.finditer(text):
        if not _overlaps(match.start(), match.end(), protected):
            mechanical.append(
                {
                    "rule": "markdown_marker",
                    "start": match.start(),
                    "end": match.end(),
                    "original": match.group(0),
                    "replacement": "",
                }
            )

    for index, character in enumerate(text):
        if character in UNSPOKEN_SYMBOLS and not _overlaps(index, index + 1, protected):
            mechanical.append(
                {
                    "rule": "unspoken_symbol",
                    "start": index,
                    "end": index + 1,
                    "original": character,
                    "replacement": "",
                }
            )

    for index, character in enumerate(text):
        if character == "@" and not _overlaps(index, index + 1, protected):
            replacement = _at_sign_replacement(text, index)
            mechanical.append(
                {
                    "rule": "email_at_sign" if replacement else "social_handle_at_sign",
                    "start": index,
                    "end": index + 1,
                    "original": character,
                    "replacement": replacement,
                }
            )

    semantic_protected = protected + pause_spans
    candidates = [
        (
            "number",
            re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?%?(?![A-Za-z0-9])"),
            lambda value: ([value, "二", "两"] if value == "2" else [value]),
            True,
            "填写适合当前语境的中文读法；数字 2 可按语境选择‘二’或‘两’",
        ),
        (
            "pronoun_ta",
            re.compile(r"(?i)(?<![A-Za-z])ta(?![A-Za-z])"),
            lambda value: [value, "他", "她", "它", "他们", "她们", "它们"],
            False,
            "按上下文选择可朗读代词",
        ),
        (
            "abbreviation",
            re.compile(r"(?<![A-Za-z])[A-Z]{2,}(?![A-Za-z])"),
            lambda value: [value],
            False,
            "专有缩写默认保留原拼写，由 TTS 逐字母读取",
        ),
    ]
    for kind, pattern, allowed, custom, hint in candidates:
        for match in pattern.finditer(text):
            if _overlaps(match.start(), match.end(), semantic_protected):
                continue
            value = match.group(0)
            semantic.append(
                {
                    "kind": kind,
                    "start": match.start(),
                    "end": match.end(),
                    "original": value,
                    "allowed_replacements": allowed(value),
                    "custom_replacement": custom,
                    "hint": hint,
                }
            )

    for written, replacements in WRITTEN_REPLACEMENTS.items():
        for match in re.finditer(re.escape(written), text):
            if _overlaps(match.start(), match.end(), semantic_protected):
                continue
            semantic.append(
                {
                    "kind": "written_expression",
                    "start": match.start(),
                    "end": match.end(),
                    "original": written,
                    "allowed_replacements": replacements,
                    "custom_replacement": False,
                    "hint": "可保留原词，或选择登记的极少量口语化表达",
                }
            )

    mechanical.sort(key=lambda item: (item["start"], item["end"]))
    for left, right in zip(mechanical, mechanical[1:]):
        if left["end"] > right["start"]:
            raise ValueError(f"机械规则范围重叠：{left['rule']} 与 {right['rule']}")

    semantic.sort(key=lambda item: (item["start"], item["end"], item["kind"]))
    for index, item in enumerate(semantic, start=1):
        item["candidate_id"] = f"SEM-{index:03d}"
    return {
        "schema_version": "1.0",
        "rules_version": RULES_VERSION,
        "pause_marker_version": MARKER_VERSION,
        "content_selection": content_selection,
        "preserved_directives": pause_directives,
        "mechanical_edits": mechanical,
        "semantic_candidates": semantic,
        "summary": {
            "mechanical_edits": len(mechanical),
            "excluded_ranges": len(exclusions),
            "semantic_candidates": len(semantic),
            "preserved_directives": len(pause_directives),
        },
    }
