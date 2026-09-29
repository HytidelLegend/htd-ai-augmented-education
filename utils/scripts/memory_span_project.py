"""Shared validation and interval operations for text recall projects."""
from __future__ import annotations

import hashlib
import colorsys
import re
import unicodedata
from typing import Any

COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


def next_span_color(existing: list[str]) -> str:
    """Choose a stable, visually spaced color for a newly created memory span."""
    used = {color.lower() for color in existing}
    for index in range(len(existing), len(existing) + 10001):
        hue = ((index * 137.508) % 360) / 360
        red, green, blue = colorsys.hls_to_rgb(hue, 0.57, 0.58)
        color = f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"
        if color not in used:
            return color
    raise ValueError("无法分配新的记忆要点颜色")


def normalize_text(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("原文必须是纯文本")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def source_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def compact_intervals(indices: list[int]) -> list[dict[str, int]]:
    """Turn selected Python code point positions into sorted half-open intervals."""
    values = sorted(set(indices))
    if not values:
        return []
    out: list[dict[str, int]] = []
    start = previous = values[0]
    for index in values[1:]:
        if index != previous + 1:
            out.append({"start": start, "end": previous + 1})
            start = index
        previous = index
    out.append({"start": start, "end": previous + 1})
    return out


def is_grapheme_boundary(text: str, index: int) -> bool:
    """Reject boundaries inside common Unicode grapheme sequences."""
    if index <= 0 or index >= len(text):
        return True
    left, right = text[index - 1], text[index]
    if left == "\u200d" or right == "\u200d":
        return False
    if unicodedata.category(right).startswith("M") or 0x1F3FB <= ord(right) <= 0x1F3FF:
        return False
    if "VIRAMA" in unicodedata.name(left, ""):
        return False
    if 0x1F1E6 <= ord(left) <= 0x1F1FF and 0x1F1E6 <= ord(right) <= 0x1F1FF:
        preceding = 0
        for character in reversed(text[:index]):
            if not 0x1F1E6 <= ord(character) <= 0x1F1FF:
                break
            preceding += 1
        return preceding % 2 == 0
    return True


def validate_project(project: Any, text: str) -> dict:
    text = normalize_text(text)
    if not text.strip() or len(text) > 100_000:
        raise ValueError("原文必须为 1 至 100000 字符")
    if not isinstance(project, dict) or project.get("schemaVersion") != 1:
        raise ValueError("项目 schemaVersion 无效")
    if not UUID.fullmatch(str(project.get("projectId", ""))):
        raise ValueError("项目 ID 无效")
    title = project.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError("项目标题无效")
    for key in ("createdAt", "updatedAt", "lastOpenedAt"):
        if not STAMP.fullmatch(str(project.get(key, ""))):
            raise ValueError(f"{key} 格式无效")
    if type(project.get("revision")) is not int or project["revision"] < 0:
        raise ValueError("修订号无效")
    if not UUID.fullmatch(str(project.get("lastWriterId", ""))):
        raise ValueError("写入者 ID 无效")
    source = project.get("source")
    if not isinstance(source, dict) or source.get("path") != "source.txt" or source.get("sha256") != source_hash(text):
        raise ValueError("原文哈希或路径无效")
    extraction = project.get("extraction")
    if extraction is not None and (not isinstance(extraction, dict) or extraction.get("source") != "mark-memory-spans" or extraction.get("status") != "verified" or not isinstance(extraction.get("runId"), str) or not extraction["runId"]):
        raise ValueError("提取来源无效")
    spans = project.get("memorySpans")
    if not isinstance(spans, list) or len(spans) > 10_000:
        raise ValueError("记忆要点列表无效")
    seen_ids: set[str] = set()
    occupied: list[tuple[int, int]] = []
    for span in spans:
        if not isinstance(span, dict) or not UUID.fullmatch(str(span.get("id", ""))) or span["id"] in seen_ids:
            raise ValueError("记忆要点 ID 无效或重复")
        seen_ids.add(span["id"])
        if not COLOR.fullmatch(str(span.get("color", ""))):
            raise ValueError("记忆要点颜色无效")
        if not isinstance(span.get("role"), str) or len(span["role"]) > 120:
            raise ValueError("记忆要点角色无效")
        segments = span.get("segments")
        if not isinstance(segments, list) or not segments:
            raise ValueError("记忆要点至少需要一个区间")
        previous_end = -1
        for segment in segments:
            if not isinstance(segment, dict):
                raise ValueError("记忆要点区间无效")
            start, end = segment.get("start"), segment.get("end")
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text) or start < previous_end:
                raise ValueError("记忆要点区间越界或乱序")
            if text[start:end].isspace() or "\n" in text[start:end]:
                raise ValueError("记忆要点不可只包含空白或跨越换行")
            if not is_grapheme_boundary(text, start):
                raise ValueError("区间拆分了组合字符")
            if not is_grapheme_boundary(text, end):
                raise ValueError("区间拆分了组合字符")
            occupied.append((start, end))
            previous_end = end
    occupied.sort()
    if any(right[0] < left[1] for left, right in zip(occupied, occupied[1:])):
        raise ValueError("记忆要点区间重叠")
    return project
