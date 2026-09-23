"""Deterministic extraction of reusable bilingual writing language items."""

from __future__ import annotations

import re
from typing import Iterable

from source_normalization import SourceBlock, normalize_for_matching


_CHINESE = r"[\u3400-\u9fff]"
_NUMBER_PREFIX = r"(?:[①②③④⑤⑥⑦⑧⑨⑩]|\d+[.、)]\s*)?"


def _strip_prefix(value: str) -> str:
    return re.sub(rf"^{_NUMBER_PREFIX}", "", value.strip())


def _split_bilingual(line: str) -> tuple[str, str] | None:
    value = _strip_prefix(normalize_for_matching(line))
    match = re.match(rf"^(.+?)[ \t]*({_CHINESE}.*)$", value)
    if not match:
        return None
    expression = re.sub(r"\s+", " ", match.group(1)).strip(" -")
    meaning = match.group(2).strip()
    if not expression or not re.search(r"[A-Za-z]", expression):
        return None
    return expression, meaning


def _kind(expression: str, section: str) -> str:
    if section == "vocabulary":
        return "word" if len(expression.split()) == 1 else "phrase"
    return "sentence_pattern"


def _example_for(expression: str, lines: list[str], section: str) -> str:
    if section == "sentence":
        return expression
    lowered = expression.lower()
    for line in lines:
        clean = normalize_for_matching(line)
        if re.search(rf"(?<![A-Za-z]){re.escape(lowered)}(?![A-Za-z])", clean.lower()) and len(clean.split()) >= 3:
            return clean
    return f"源词条：{expression}"


def extract_language_bank(blocks: Iterable[SourceBlock]) -> dict[str, list[dict]]:
    bank: dict[str, list[dict]] = {
        "advanced_words": [],
        "phrases": [],
        "sentence_patterns": [],
        "expressions": [],
    }
    seen: set[tuple[str, str]] = set()
    for block in blocks:
        lines = block.content.splitlines()
        bilingual_vocab_candidates = [
            pair for pair in (_split_bilingual(line) for line in lines)
            if pair and len(pair[0].split()) <= 8 and not re.search(r"[.!?]$", pair[0])
        ]
        section: str | None = "sentence" if "框架" in block.title else ("vocabulary" if len(bilingual_vocab_candidates) >= 2 else None)
        for raw in lines:
            line = normalize_for_matching(raw)
            if not line:
                continue
            if "亮点词汇" in line or "核心词汇" in line:
                section = "vocabulary"
                continue
            if "句型拓展" in line or "万能框架" in line:
                section = "sentence"
                continue
            if not section or line.startswith("#"):
                continue
            pair = _split_bilingual(line)
            if pair:
                expression, meaning = pair
            elif section == "sentence" and re.search(r"[A-Za-z]", line):
                expression, meaning = _strip_prefix(line), "写作句型"
            else:
                continue
            item_type = _kind(expression, section)
            key = (item_type, expression.casefold())
            if key in seen or len(expression) > 240 or len(meaning) > 500:
                continue
            seen.add(key)
            target = "advanced_words" if item_type == "word" else "phrases" if item_type == "phrase" else "sentence_patterns"
            bank[target].append(
                {
                    "type": item_type,
                    "expression": expression,
                    "meaning_zh": meaning,
                    "usage_case": _example_for(expression, lines, section),
                    "source_locator": block.source_locator,
                    "source_section": block.title,
                }
            )
    if not any(bank.values()):
        bank["expressions"].append(
            {
                "type": "expression",
                "expression": "from my perspective",
                "meaning_zh": "在我看来",
                "usage_case": "From my perspective, practical skills are essential.",
                "source_locator": "fallback",
                "source_section": "fallback",
            }
        )
    return bank
