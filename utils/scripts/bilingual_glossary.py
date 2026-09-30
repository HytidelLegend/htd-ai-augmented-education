"""Parse and render reusable bilingual glossary tables."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(.*?)\s*\|$")


def parse_markdown(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"双语术语表不存在：{path}")
    terms: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        match = ROW.match(line.strip())
        if not match:
            if line.strip().startswith("|"):
                raise ValueError(f"双语术语表第 {number} 行不是三列表格")
            continue
        if set(match.group(1).strip()) == {"-"}:
            continue
        source, target, note = (item.strip() for item in match.groups())
        if source.lower() in {"中文", "source", "source term"}:
            continue
        if source and target:
            key = source.casefold()
            if key in seen:
                raise ValueError(f"双语术语表术语重复：{source}")
            seen.add(key)
            terms.append({
                "source_term": source,
                "target_term": target,
                "source_language": "auto",
                "target_language": "auto",
                "domain": "",
                "note": note,
                "status": "approved",
            })
    return terms


def render_markdown(terms: list[dict[str, Any]]) -> str:
    lines = ["# 术语表", "", "| Source term | Target term | Note |", "|---|---|---|"]
    for item in terms:
        source = item["source_term"].replace("|", "\\|")
        target = item["target_term"].replace("|", "\\|")
        note = item.get("note", "").replace("|", "\\|")
        lines.append(f"| {source} | {target} | {note} |")
    return "\n".join(lines) + "\n"
