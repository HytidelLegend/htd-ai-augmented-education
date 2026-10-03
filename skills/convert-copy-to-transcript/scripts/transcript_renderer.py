"""Render a transcript and traceable Markdown diff from registered edits only."""

from __future__ import annotations

from typing import Any


def validate_decisions(scan: dict[str, Any], decisions: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    if decisions.get("schema_version") != "1.0" or decisions.get("run_id") != run_id:
        raise ValueError("语义决策的 schema_version 或 run_id 不匹配")
    candidates = {item["candidate_id"]: item for item in scan["semantic_candidates"]}
    provided = decisions.get("decisions")
    if not isinstance(provided, list):
        raise ValueError("decisions 必须是数组")
    by_id: dict[str, dict[str, Any]] = {}
    for item in provided:
        candidate_id = item.get("candidate_id")
        if candidate_id not in candidates:
            raise ValueError(f"决策引用了扫描器未登记的候选：{candidate_id}")
        if candidate_id in by_id:
            raise ValueError(f"候选决策重复：{candidate_id}")
        replacement = item.get("replacement")
        if not isinstance(replacement, str) or not replacement.strip():
            raise ValueError(f"候选 {candidate_id} 缺少可朗读 replacement")
        if "\n" in replacement or "\r" in replacement:
            raise ValueError(f"候选 {candidate_id} 的 replacement 不得改变断句")
        candidate = candidates[candidate_id]
        allowed = candidate["allowed_replacements"]
        if not candidate["custom_replacement"] and replacement not in allowed:
            raise ValueError(f"候选 {candidate_id} 的 replacement 不在允许范围内")
        by_id[candidate_id] = item
    missing = sorted(set(candidates) - set(by_id))
    if missing:
        raise ValueError("仍有未决语义候选：" + "、".join(missing))
    return [
        {
            "rule": f"semantic:{candidate['kind']}",
            "candidate_id": candidate_id,
            "start": candidate["start"],
            "end": candidate["end"],
            "original": candidate["original"],
            "replacement": by_id[candidate_id]["replacement"],
        }
        for candidate_id, candidate in candidates.items()
    ]


def render_transcript(source: str, scan: dict[str, Any], semantic_edits: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    edits = [dict(item) for item in scan["mechanical_edits"]] + semantic_edits
    edits.sort(key=lambda item: (item["start"], item["end"]))
    parts: list[str] = []
    cursor = 0
    for edit in edits:
        start, end = int(edit["start"]), int(edit["end"])
        if start < cursor or end <= start or end > len(source):
            raise ValueError(f"转换范围无效或重叠：[{start}, {end})")
        if source[start:end] != edit["original"]:
            raise ValueError(f"转换范围原文不匹配：[{start}, {end})")
        parts.extend([source[cursor:start], edit["replacement"]])
        cursor = end
    parts.append(source[cursor:])
    return "".join(parts), edits


def render_diff_report(
    run_id: str,
    source_sha256: str,
    preview_sha256: str,
    edits: list[dict[str, Any]],
    preserved_directives: list[dict[str, Any]] | None = None,
) -> str:
    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", "↵") or "（空）"

    lines = [
        "# 逐字稿差异报告",
        "",
        f"- 运行 ID：`{run_id}`",
        f"- 原始输入 SHA-256：`{source_sha256}`",
        f"- 预览 SHA-256：`{preview_sha256}`",
        f"- 可追溯转换数：`{len(edits)}`",
        "",
        "| 规则／决策 | 原文范围 | 原文 | 逐字稿 |",
        "|---|---:|---|---|",
    ]
    for edit in edits:
        label = edit.get("candidate_id") or edit["rule"]
        lines.append(
            f"| {cell(label)}（{cell(edit['rule'])}） | "
            f"`[{edit['start']}, {edit['end']})` | {cell(edit['original'])} | {cell(edit['replacement'])} |"
        )
    if not edits:
        lines.append("| 无 | — | — | — |")
    excluded = [item for item in edits if str(item["rule"]).startswith("excluded_")]
    lines.extend(
        [
            "",
            "## 排除的非朗读内容",
            "",
            "| 规则 | 原文范围 | 原文 |",
            "|---|---:|---|",
        ]
    )
    for edit in excluded:
        lines.append(
            f"| {cell(edit['rule'])} | `[{edit['start']}, {edit['end']})` | {cell(edit['original'])} |"
        )
    if not excluded:
        lines.append("| 无 | — | — |")
    directives = list(preserved_directives or [])
    lines.extend(
        [
            "",
            "## 保留的控制指令",
            "",
            "| pause_id | 原文范围 | 指令 | 时长 |",
            "|---|---:|---|---:|",
        ]
    )
    for directive in directives:
        span = directive["source_span"]
        lines.append(
            f"| {cell(directive['pause_id'])} | `[{span['start']}, {span['end']})` | "
            f"{cell(directive['directive'])} | {directive['duration_ms']} ms |"
        )
    if not directives:
        lines.append("| 无 | — | — | — |")
    lines.extend(["", "本报告由确定性 renderer 生成；表中每一项均对应扫描规则或已登记语义决策。", ""])
    return "\n".join(lines)
