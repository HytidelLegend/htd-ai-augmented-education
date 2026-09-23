"""Shared deterministic metrics for short English essays."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def word_tokens(text: str) -> list[str]:
    return WORD_RE.findall(text or "")


def sentences(text: str) -> list[str]:
    return [item.strip() for item in SENTENCE_RE.split((text or "").strip()) if item.strip()]


def paragraphs(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"\n\s*\n+", (text or "").strip()) if item.strip()]


def metrics(text: str) -> dict[str, Any]:
    tokens = word_tokens(text)
    lowered = [token.lower() for token in tokens]
    counts = Counter(lowered)
    return {
        "word_count": len(tokens),
        "sentence_count": len(sentences(text)),
        "paragraph_count": len(paragraphs(text)),
        "unique_word_count": len(counts),
        "lexical_diversity": round(len(counts) / len(tokens), 3) if tokens else 0.0,
    }
