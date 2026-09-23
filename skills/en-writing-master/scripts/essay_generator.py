"""Prepare Agent writing context and validate Agent-produced essays."""

from __future__ import annotations

from typing import Any

from essay_metrics import metrics
from essay_scoring import build_evidence, target_band


def build_generation_packet(topic: str, requirements: dict[str, Any], tool: dict[str, Any]) -> dict[str, Any]:
    """Build the compact, machine-readable context handed to the writing Agent."""
    language_items = [item for values in tool.get("language_bank", {}).values() for item in values]
    normalized_requirements = dict(requirements)
    normalized_requirements.setdefault("word_count_min", 120)
    normalized_requirements.setdefault("word_count_max", 180)
    return {
        "tool_id": tool["tool_id"],
        "topic": topic,
        "requirements": normalized_requirements,
        "writing_rules": [
            {"title": rule.get("title", ""), "content": rule.get("content", "")}
            for rule in tool.get("writing_rules", [])
        ],
        "scoring_dimensions": tool.get("scoring", {}).get("dimensions", []),
        "language_items": language_items,
        "output_contract": {
            "required_fields": ["essay"],
            "instruction": "根据题目和工具规则生成完整作文；不得返回评分或 Markdown 包装。",
        },
    }


def validate_agent_draft(
    essay: str,
    topic: str,
    requirements: dict[str, Any],
    full_score: int,
    target_score: Any,
) -> dict[str, Any]:
    """Prepare deterministic evidence for an essay supplied during resume."""
    draft = str(essay or "").strip()
    if not draft:
        raise ValueError("Agent 生成的 essay 不能为空")
    evidence = build_evidence(draft, topic=topic)
    band = target_band(target_score, full_score, tolerance=1.0)
    quality = metrics(draft)
    minimum = int(requirements.get("word_count_min", 120) or 120)
    maximum = int(requirements.get("word_count_max", 180) or 180)
    if minimum and quality["word_count"] < minimum:
        raise ValueError(f"Agent 生成的作文词数不足：{quality['word_count']} < {minimum}")
    if maximum and quality["word_count"] > maximum:
        raise ValueError(f"Agent 生成的作文词数超限：{quality['word_count']} > {maximum}")
    return {
        "essay": draft,
        "score_preview": {"evidence": evidence, "notice": "未评分；内容、结构、语言和逻辑评分必须由 Agent 完成。"},
        "target_band": band,
        "target_match": band is None,
        "word_count": quality["word_count"],
    }
