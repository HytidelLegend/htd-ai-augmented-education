"""Regression checks for the published geography span boundaries."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utils" / "scripts"))
from render_span_examples import extract_spans, render, validate
from span_ops import masked_text, validate_cloze_quality

SOURCE = ROOT / "skills" / "mark-memory-spans" / "references" / "geography-span-examples.json"
MARKDOWN = SOURCE.with_name("span-examples.md")


def examples() -> dict:
    return json.loads(SOURCE.read_text(encoding="utf-8"))


def test_published_cases_are_aligned_and_rendered() -> None:
    data = examples()
    validate(data)
    assert len(data["examples"]) == 3
    for example in data["examples"]:
        spans = extract_spans(example["source"], example["marked"])
        assert masked_text(example["source"], spans) == example["masked"]
        assert validate_cloze_quality(example["source"], spans)["pass"]
        assert example["marked"] != example["negative"]
    assert render(data) in MARKDOWN.read_text(encoding="utf-8")


def test_renderer_rejects_drifted_cloze_text() -> None:
    data = examples()
    data["examples"][0]["masked"] += "____"
    with pytest.raises(ValueError, match="挖空文本"):
        validate(data)


def test_renderer_rejects_partial_negative_example() -> None:
    data = examples()
    data["examples"][1]["negative"] = "「水土保持」"
    with pytest.raises(ValueError, match="原文不一致"):
        validate(data)


def test_quality_check_uses_original_offsets_for_multiple_spans() -> None:
    text = "甲，乙，丙。"
    spans = [{"start": 0, "end": 1}, {"start": 2, "end": 3}, {"start": 4, "end": 5}]
    result = validate_cloze_quality(text, spans)
    assert not result["pass"]
    assert not result["sentence_checks"][0]["main_clause_remaining"]
