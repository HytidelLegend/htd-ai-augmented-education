"""Validation gates for preview provenance, approvals, and TTS handoff."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from transcript_renderer import render_transcript, validate_decisions
from utils.scripts.speech_pause_markers import parse_pause_markers


UNHANDLED_PATTERNS = [
    ("Markdown 标题", re.compile(r"(?m)^ {0,3}#{1,6}[ \t]+")),
    ("Markdown 引用行", re.compile(r"(?m)^[ \t]*>")),
    ("display 指令", re.compile(r"\{\{display:\d+ms\}\}")),
    ("Markdown 列表", re.compile(r"(?m)^\s*(?:[-+*]|\d+[.)、])[ \t]+")),
    ("数学公式标记", re.compile(r"\$")),
    ("不可发音括号", re.compile(r"[「」《》【】\[\]（）()]")),
    ("Markdown 标记", re.compile(r"\*\*|__|~~|`")),
    ("未分类的 @ 符号", re.compile(r"@")),
]


def validate_preview(source: str, scan: dict[str, Any], decisions: dict[str, Any], run_id: str, preview: str) -> None:
    semantic = validate_decisions(scan, decisions, run_id)
    expected, _ = render_transcript(source, scan, semantic)
    if preview != expected:
        raise ValueError("逐字稿与登记规则／语义决策无法一致重建，存在丢字、增字或未追溯修改")
    preview_pauses = parse_pause_markers(preview)
    scanned_pauses = list(scan.get("preserved_directives") or [])
    if [item["directive"] for item in preview_pauses] != [
        item["directive"] for item in scanned_pauses
    ]:
        raise ValueError("逐字稿中的停顿指令未按保留内容的原文顺序逐字保留")
    if [
        (item["pause_id"], item["directive"], item["duration_ms"])
        for item in preview_pauses
    ] != [
        (item["pause_id"], item["directive"], item["duration_ms"])
        for item in scanned_pauses
    ]:
        raise ValueError("逐字稿中的停顿指令与规则扫描结果不一致")
    for label, pattern in UNHANDLED_PATTERNS:
        if pattern.search(preview):
            raise ValueError(f"逐字稿仍含未处理的{label}")


def validate_tts_handoff(tts_manifest: dict[str, Any], approved_sha256: str) -> dict[str, Any]:
    if tts_manifest.get("schema_version") not in {2, 3}:
        raise ValueError("只支持 TTS manifest schema 2 或 3")
    if tts_manifest.get("status") != "completed":
        raise ValueError("TTS manifest 尚未完成，拒绝 handoff")
    input_info = tts_manifest.get("input")
    if not isinstance(input_info, dict):
        raise ValueError("TTS manifest 缺少 input")
    actual = input_info.get("file_sha256")
    if actual != approved_sha256:
        mode = tts_manifest.get("mode", "original")
        if mode == "revision":
            raise ValueError("TTS 修订文本哈希与原确认不一致；修订文本必须作为新候选重新确认")
        raise ValueError("TTS 输入哈希与已确认逐字稿不一致，拒绝 handoff")
    copied_path = input_info.get("copied_path")
    if not isinstance(copied_path, str) or not copied_path:
        raise ValueError("TTS manifest input 缺少 copied_path")
    return {
        "schema_version": 2,
        "run_id": tts_manifest.get("run_id"),
        "mode": tts_manifest.get("mode", "original"),
        "copied_path": copied_path,
        "base_run_id": input_info.get("base_run_id") or (tts_manifest.get("revision") or {}).get("base_run_id"),
        "base_input_path": input_info.get("base_input_path"),
        "input_file_sha256": actual,
    }


def read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise ValueError(f"文件不是有效 UTF-8：{path}") from exc
