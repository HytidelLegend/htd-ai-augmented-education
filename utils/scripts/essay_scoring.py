"""Deterministic, rubric-oriented scoring for short English essays."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from essay_metrics import metrics, sentences

CONNECTORS = {
    "first", "firstly", "second", "secondly", "moreover", "however",
    "therefore", "thus", "in addition", "for example", "for instance",
    "in brief", "in conclusion", "to sum up", "on the one hand",
    "on the other hand",
}
STANCE_PATTERNS = (r"\bin my opinion\b", r"\bas far as i am concerned\b", r"\bi (?:agree|disagree)\b", r"\bi believe\b")
EXAMPLE_PATTERNS = (r"\bfor example\b", r"\bfor instance\b", r"\bsuch as\b", r"\bto illustrate\b")
COUNTER_PATTERNS = (r"\bhowever\b", r"\balthough\b", r"\bon the other hand\b", r"\bwhile\b")
CONCLUSION_PATTERNS = (r"\bin brief\b", r"\bin conclusion\b", r"\bto sum up\b", r"\btherefore\b")
ADVANCED_PHRASES = (
    "postgraduate study", "master's degree", "competitive advantage", "guarantee success",
    "think critically", "practical skills", "rewarding job", "in the long run",
)


def _has(patterns: tuple[str, ...], text: str) -> bool:
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in patterns)


def _count_connectors(text: str) -> int:
    lowered = text.lower()
    return sum(lowered.count(item) for item in CONNECTORS)


def _topic_keywords(topic: str) -> set[str]:
    words = re.findall(r"[A-Za-z]{5,}", (topic or "").lower())
    stop = {"directions", "allowed", "minutes", "write", "essay", "commenting", "saying", "today", "world", "where", "college", "population", "grows", "rapidly", "essential", "wish", "compete", "rewarding", "should", "words", "than", "more", "cite", "example", "illustrate", "point", "view"}
    return {word for word in words if word not in stop}


def _topic_hit(topic: str, essay: str) -> bool:
    keywords = _topic_keywords(topic)
    if not keywords:
        return True
    lowered = essay.lower()
    return any(keyword in lowered for keyword in keywords)


def integer_score(value: Any, *, field: str, minimum: int = 0, round_fraction: bool = False) -> int:
    """Return a canonical integer score; optionally half-up round computed fractions."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} 必须是整数分数") from exc
    if not number.is_finite() or number < minimum:
        raise ValueError(f"{field} 必须是不小于 {minimum} 的整数分数")
    if not round_fraction and number != number.to_integral_value():
        raise ValueError(f"{field} 必须是整数分数")
    return int(number.to_integral_value(rounding=ROUND_HALF_UP))


def build_evidence(essay: str, *, topic: str = "") -> dict[str, Any]:
    """Prepare mechanical evidence; semantic judgments belong to the Agent."""
    info = metrics(essay)
    sentence_list = sentences(essay)
    lower = essay.lower()
    return {
        "metrics": info,
        "sentence_count": len(sentence_list),
        "paragraph_count": info["paragraph_count"],
        "topic_keywords_present": sorted(keyword for keyword in _topic_keywords(topic) if keyword in lower),
        "marker_examples": {
            "connectors": sorted(item for item in CONNECTORS if item in lower),
            "stance": _has(STANCE_PATTERNS, essay),
            "example": _has(EXAMPLE_PATTERNS, essay),
            "counterargument": _has(COUNTER_PATTERNS, essay),
            "conclusion": _has(CONCLUSION_PATTERNS, essay),
        },
        "organization_evidence": build_organization_evidence(essay),
        "notice": "以上仅为机械证据，不得直接作为内容、结构、语言或逻辑评分。",
    }


def build_organization_evidence(essay: str, *, genre: str = "essay") -> dict[str, Any]:
    """Prepare paragraph-level evidence without assigning semantic judgments."""
    paragraph_texts = [part.strip() for part in re.split(r"\n\s*\n", essay.strip()) if part.strip()]
    paragraph_items = []
    for index, paragraph in enumerate(paragraph_texts, 1):
        paragraph_sentences = sentences(paragraph)
        paragraph_items.append({
            "paragraph_index": index,
            "sentence_count": len(paragraph_sentences),
            "text": paragraph,
            "first_sentence": paragraph_sentences[0] if paragraph_sentences else "",
            "last_sentence": paragraph_sentences[-1] if paragraph_sentences else "",
        })
    return {
        "genre": genre,
        "paragraph_count": len(paragraph_items),
        "paragraphs": paragraph_items,
        "recommended_roles": ["引入话题并提出立场", "展开论证或提供例证", "总结观点并提出建议"],
    }


def score_from_agent_review(review: dict[str, Any], *, full_score: int = 15, dimensions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Validate and normalize an Agent-authored review; never infer a score."""
    full_score = integer_score(full_score, field="full_score", minimum=1)
    dimensions = dimensions or [
        {"id": "content", "label": "内容", "max_score": 5},
        {"id": "organization", "label": "结构", "max_score": 5},
        {"id": "language", "label": "语言", "max_score": 5},
    ]
    scores = review.get("dimension_scores")
    if not isinstance(scores, list):
        raise ValueError("agent_review.dimension_scores 必须由 Agent 提供")
    by_id = {item.get("id"): item for item in scores if isinstance(item, dict)}
    dimension_scores = []
    for dimension in dimensions:
        key = dimension.get("id", "")
        max_score = integer_score(dimension.get("max_score", 5), field=f"{key}.max_score", minimum=1)
        item = by_id.get(key)
        if not item:
            raise ValueError(f"agent_review 缺少 {key} 维度判断")
        score = integer_score(item.get("score"), field=f"agent_review.{key}.score", minimum=0)
        if score > max_score:
            raise ValueError(f"agent_review.{key}.score 超出维度满分")
        reason = str(item.get("reason", "")).strip()
        if not reason:
            raise ValueError(f"agent_review.{key}.reason 不能为空")
        dimension_scores.append({
            "id": key,
            "dimension": dimension.get("label", key),
            "score": score,
            "max_score": max_score,
            "reason": reason,
            "evidence": item.get("evidence", []),
        })
    value = integer_score(review.get("value"), field="agent_review.value", minimum=0)
    if value > full_score:
        raise ValueError("agent_review.value 超出满分范围")
    return {
        "value": value,
        "full_score": full_score,
        "dimension_scores": dimension_scores,
        "reason": "总分与维度判断由 Agent 依据题目、工具规则和作文内容给出；脚本仅校验结构。",
        "agent_evidence": review.get("evidence", []),
    }


def parse_target(value: Any, full_score: int) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return integer_score(value, field="target_score", minimum=0)
    match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/\s*[0-9]+(?:\.[0-9]+)?)?\s*$", str(value))
    if not match:
        raise ValueError(f"无法解析目标分数：{value}")
    return integer_score(match.group(1), field="target_score", minimum=0)


def target_band(value: Any, full_score: int, tolerance: int = 1) -> dict[str, int] | None:
    target = parse_target(value, full_score)
    if target is None:
        return None
    full = integer_score(full_score, field="full_score", minimum=1)
    margin = integer_score(tolerance, field="target_tolerance", minimum=0)
    return {"target": target, "lower": max(0, target - margin), "upper": min(full, target + margin), "tolerance": margin}
