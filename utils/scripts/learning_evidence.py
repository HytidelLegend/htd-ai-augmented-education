"""Build stable, bounded evidence bundles for interactive lessons."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from utils.scripts.markdown_structure import extract_markdown_structure


class LearningEvidenceError(ValueError):
    pass


class SourceChangedError(LearningEvidenceError):
    pass


class LocatorInvalidError(LearningEvidenceError):
    pass


def resolve_heading_locator(root: Path, source_path: Path, locator: dict[str, Any]) -> dict[str, Any]:
    """Resolve a navigation locator against the current source instead of trusting line hints."""
    structure = extract_markdown_structure(source_path, root=root, preview_chars=0)
    if locator.get("source_sha256") and structure["sha256"] != locator["source_sha256"]:
        raise SourceChangedError(f"学习资料哈希已变化：{structure['path']}")
    matches = [
        item for item in structure["headings"]
        if item["title"] == locator["heading_text"]
        and item["occurrence"] == locator["heading_occurrence"]
        and item["parent_heading_chain"] == locator["parent_heading_chain"]
    ]
    if len(matches) != 1:
        raise LocatorInvalidError(f"章节定位不能唯一解析：{locator['heading_text']}")
    return matches[0]


def build_evidence(root: Path, source_path: Path, *, heading: dict | None = None) -> list[dict]:
    structure = extract_markdown_structure(source_path, root=root, preview_chars=0)
    target = heading or structure["headings"][0]
    lines = source_path.read_text(encoding="utf-8-sig").splitlines()
    start = max(1, int(target["start_line"]))
    end = min(len(lines), int(target["end_line"]))
    text = "\n".join(lines[start - 1:end]).strip()
    key = f"{structure['path']}|{start}|{end}|{text}"
    return [{
        "evidence_id": f"EVD-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}",
        "evidence_kind": "source_excerpt",
        "source_path": structure["path"],
        "source_sha256": structure["sha256"],
        "locator": {
            "heading": target["title"],
            "heading_occurrence": target["occurrence"],
            "start_line": start,
            "end_line": end,
            "parent_heading_chain": target["parent_heading_chain"],
        },
        "text": text,
    }]


def build_navigation_context_evidence(unit: dict[str, Any], navigation: dict[str, Any]) -> list[dict[str, Any]]:
    """Create explicit non-source evidence for an agent-authored overview unit."""
    payload = "\n".join([
        f"课程目的：{navigation['course_overview']['purpose']}",
        f"单元目的：{unit['purpose']}",
        *(f"学习目标：{item}" for item in unit["learning_objectives"]),
        *(f"教学备注：{item}" for item in unit["teaching_notes"]),
    ])
    key = f"{unit['unit_id']}|{payload}"
    return [{
        "evidence_id": f"NAV-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}",
        "evidence_kind": "navigation_context",
        "source_path": None,
        "source_sha256": None,
        "locator": {"unit_id": unit["unit_id"], "stage": unit["stage"], "module": unit["module"]},
        "text": payload,
    }]
