"""Regression checks for complete mathematical expressions and examples."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utils" / "scripts"))
from render_span_examples import extract_spans, render, validate
from span_ops import candidate_spans, masked_text, validate_cloze_quality

SOURCE = ROOT / "skills" / "mark-memory-spans" / "references" / "math-span-examples.json"
MARKDOWN = SOURCE.with_name("span-examples.md")


def test_math_reference_round_trip() -> None:
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    validate(data)
    assert len(data["examples"]) == 3
    assert render(data) in MARKDOWN.read_text(encoding="utf-8")
    for example in data["examples"]:
        spans = extract_spans(example["source"], example["marked"])
        assert masked_text(example["source"], spans) == example["masked"]
        assert example["negative"] != example["marked"]


def test_formula_candidate_and_partial_formula_rejection() -> None:
    example = json.loads(SOURCE.read_text(encoding="utf-8"))["examples"][1]
    source = example["source"]
    assert "b²−4ac" in {item["text"] for item in candidate_spans(source)}
    assert "a(x-h)²+k" in {item["text"] for item in candidate_spans("二次函数写成y=a(x-h)²+k。")}
    assert "b² - 4ac" in {item["text"] for item in candidate_spans("判别式Δ=b² - 4ac。")}
    assert "4−8" in {item["text"] for item in candidate_spans("代入后得到Δ=4−8。")}
    assert "a + b" in {item["text"] for item in candidate_spans("Use x=a + b for the next step.")}
    assert "a + b for the next step" not in {item["text"] for item in candidate_spans("Use x=a + b for the next step.")}
    assert "a+b" in {item["text"] for item in candidate_spans("Use x=a+b.For the next step.")}
    partial_spans = extract_spans(source, example["negative"])
    assert "公式表达式被拆断" in " ".join(validate_cloze_quality(source, partial_spans)["failures"])
