"""Populate curriculum-navigation batch decisions from a compact review profile."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.scripts.structured_io import read_json, write_json

sys.stdout.reconfigure(encoding="utf-8")


def merged(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in updates.items():
        if key == "case_context":
            result[key] = {**result.get(key, {}), **value}
        else:
            result[key] = value
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--promote-selected-concepts", action="store_true")
    args = parser.parse_args(argv)

    profile = read_json(args.profile)
    source_id = profile["source_id"]
    selected = profile.get("selected", [])
    selected_by_id = {item["candidate_id"]: item for item in selected}
    written = 0

    for template_path in sorted(args.batch_dir.glob(f"{source_id}-*.template.json")):
        batch = read_json(template_path)
        populated: list[dict[str, Any]] = []
        for item in batch["items"]:
            candidate_id = item["candidate_id"]
            decision = merged(item, profile["excluded_defaults"])
            if candidate_id in selected_by_id:
                choice = selected_by_id[candidate_id]
                concept = choice.get("concept", item["title"])
                prerequisites = choice.get("prerequisites", [])
                decision = merged(decision, {
                    "include": True,
                    "stage": choice["stage"],
                    "module": choice["module"],
                    "importance": choice.get("importance", "recommended"),
                    "difficulty": choice.get("difficulty", "beginner"),
                    "purpose": choice.get("purpose", f"理解“{item['title']}”的核心内容及适用条件。"),
                    "learning_objectives": choice.get("learning_objectives", [f"能解释“{item['title']}”并举出一个适用例子。"]),
                    "prerequisites": prerequisites,
                    "concept_roles": choice.get("concept_roles", [{
                        "concept": concept,
                        "role": "introduce" if args.promote_selected_concepts else choice.get("concept_role", "review"),
                    }]),
                    "content_roles": choice.get("content_roles", profile["included_defaults"]["content_roles"]),
                    "risk_tags": choice.get("risk_tags", profile["included_defaults"]["risk_tags"]),
                    "teaching_notes": choice.get("teaching_notes", profile["included_defaults"]["teaching_notes"]),
                    "case_context": {**profile["included_defaults"]["case_context"], **choice.get("case_context", {})},
                    "visual_reference_uses": choice.get("visual_reference_uses", []),
                })
            populated.append(decision)
        batch["items"] = populated
        output = template_path.with_name(template_path.name.replace(".template.json", ".decisions.json"))
        write_json(output, batch)
        written += 1

    expected = set(selected_by_id)
    found: set[str] = set()
    for path in args.batch_dir.glob(f"{source_id}-*.decisions.json"):
        found.update(item["candidate_id"] for item in read_json(path)["items"] if item["include"])
    if found != expected:
        raise ValueError(f"selected candidates mismatch: missing={sorted(expected - found)} extra={sorted(found - expected)}")
    print(f"populated {written} batches for {source_id}; included={len(found)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
