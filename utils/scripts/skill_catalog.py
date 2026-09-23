"""Shared parsing and validation helpers for the project's Skill catalog."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - the project runtime declares PyYAML.
    yaml = None

ALLOWED_CATEGORIES = {
    "project_function",
    "ai_assisted_learning",
    "ai_assisted_teaching",
    "ai_assisted_research",
    "common_tool",
}


def parse_front_matter(text: str) -> dict[str, Any]:
    """Parse the YAML front matter without interpreting the Markdown body."""
    if not text.startswith("---"):
        return {}
    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.DOTALL)
    if not match:
        return {}
    if yaml is not None:
        parsed = yaml.safe_load(match.group(1))
        return parsed if isinstance(parsed, dict) else {}
    result: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            result[key.strip()] = value.strip().strip("\"'")
    return result


def load_skill_metadata(root: Path) -> dict[str, dict[str, Any]]:
    """Return active Skill metadata keyed by Skill directory name."""
    base = root / "skills"
    if not base.is_dir():
        return {}
    result = {}
    for path in sorted(base.glob("*/SKILL.md")):
        if ".backup" in path.parts:
            continue
        data = parse_front_matter(path.read_text(encoding="utf-8"))
        result[path.parent.name] = {
            "path": path.relative_to(root).as_posix(),
            "name": data.get("name"),
            "category": data.get("category"),
            "description": data.get("description"),
        }
    return result


def parse_scenario_examples(text: str) -> list[dict[str, Any]]:
    """Extract scenario_examples YAML blocks from a Skill detail section."""
    examples: list[dict[str, Any]] = []
    for match in re.finditer(r"```yaml\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE):
        block = match.group(1)
        if "scenario_examples:" not in block:
            continue
        parsed = yaml.safe_load(block) if yaml is not None else None
        if isinstance(parsed, dict) and isinstance(parsed.get("scenario_examples"), list):
            examples.extend(item for item in parsed["scenario_examples"] if isinstance(item, dict))
    return examples


def load_documented_scenarios(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Extract scenarios from `### <skill-name>` detail sections."""
    text = path.read_text(encoding="utf-8")
    sections = re.split(r"(?m)^###\s+([^\n]+)\s*$", text)
    result: dict[str, list[dict[str, Any]]] = {}
    for index in range(1, len(sections), 2):
        name = sections[index].strip().strip("`")
        body = sections[index + 1] if index + 1 < len(sections) else ""
        result[name] = parse_scenario_examples(body)
    return result


def load_documented_categories(path: Path) -> dict[str, str]:
    """Read Skill categories from the overview tables in the catalog document."""
    text = path.read_text(encoding="utf-8")
    overview = re.split(r"(?m)^## 项目功能\s*$", text, maxsplit=1)[0]
    current = None
    result: dict[str, str] = {}
    for line in overview.splitlines():
        heading = re.match(r"^###\s+(.+?)\s*$", line)
        if heading:
            current = heading.group(1).strip()
            continue
        if not current or not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] and cells[0] != "Skill" and len(cells) >= 4:
            result[cells[0]] = current
    return result


def load_documented_category_occurrences(path: Path) -> dict[str, list[str]]:
    """Return all category headings in which each Skill appears."""
    text = path.read_text(encoding="utf-8")
    overview = re.split(r"(?m)^## 项目功能\s*$", text, maxsplit=1)[0]
    current = None
    result: dict[str, list[str]] = {}
    for line in overview.splitlines():
        heading = re.match(r"^###\s+(.+?)\s*$", line)
        if heading:
            current = heading.group(1).strip()
            continue
        if not current or not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] and cells[0] != "Skill" and len(cells) >= 4:
            result.setdefault(cells[0], []).append(current)
    return result


def validate_scenario(example: dict[str, Any]) -> list[str]:
    required = ("id", "user_request", "when_to_call", "invocation", "expected_output")
    return [key for key in required if not example.get(key)]
