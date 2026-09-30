"""Shared helpers for separating user-facing outputs from workflow artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .structured_io import write_json


ARTIFACTS_DIRNAME = "artifacts"
LAYOUT_VERSION = "2.0"


def artifacts_dir(run_dir: Path) -> Path:
    """Return the internal artifact directory for a user-facing run directory."""
    return Path(run_dir) / ARTIFACTS_DIRNAME


def artifact_path(run_dir: Path, *parts: str) -> Path:
    """Build a path below the internal artifact directory."""
    return artifacts_dir(run_dir).joinpath(*parts)


def lesson_file_map(units: list[dict[str, Any]]) -> dict[str, str]:
    """Assign deterministic user-facing Markdown names while retaining stable unit IDs."""
    result: dict[str, str] = {}
    lesson_number = 0
    overview_number = 0
    for unit in sorted(units, key=lambda item: (int(item.get("sequence", 0)), str(item["unit_id"]))):
        if unit.get("unit_type") == "agent_overview":
            overview_number += 1
            filename = "overview.md" if overview_number == 1 else f"overview-{overview_number:03d}.md"
        else:
            lesson_number += 1
            filename = f"lesson-{lesson_number:03d}.md"
        if filename in result.values():
            raise ValueError(f"用户讲义文件名重复：{filename}")
        result[str(unit["unit_id"])] = f"lessons/{filename}"
    return result


def write_lesson_index(run_dir: Path, units: list[dict[str, Any]], mapping: dict[str, str]) -> Path:
    """Write the machine-readable unit-to-file index used for recovery and CLI resolution."""
    by_id = {str(item["unit_id"]): item for item in units}
    output = artifact_path(run_dir, "lesson-index.json")
    write_json(output, {
        "schema_version": "1.0",
        "layout_version": LAYOUT_VERSION,
        "lessons": [{
            "unit_id": unit_id,
            "unit_type": by_id[unit_id].get("unit_type"),
            "sequence": by_id[unit_id].get("sequence"),
            "title": by_id[unit_id].get("title"),
            "path": relative,
        } for unit_id, relative in mapping.items()],
    })
    return output
