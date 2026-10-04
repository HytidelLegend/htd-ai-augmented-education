"""Prepare and collect compact Agent decisions for curriculum navigation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.scripts.structured_io import read_json, validate_json_schema, write_json

sys.stdout.reconfigure(encoding="utf-8")


def decision_item(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "include": False,
        "title": candidate["title"],
        "stage": "",
        "module": "",
        "importance": "optional",
        "difficulty": "beginner",
        "purpose": "",
        "learning_objectives": [],
        "prerequisites": [],
        "concept_roles": [],
        "content_roles": ["not_yet_classified"],
        "risk_tags": [],
        "teaching_notes": [],
        "case_context": {
            "era": "unknown", "context_scale": "unknown", "domain": "unknown",
            "transferable_principle": "", "changed_conditions": [],
            "needs_current_fact_check": False, "evidence_status": "unknown",
        },
        "visual_reference_uses": [],
    }


def build_template(
    inventory: list[dict[str, Any]], *, title: str, existing_source_ids: set[str]
) -> dict[str, Any]:
    items = [
        decision_item(heading)
        for source in inventory
        if source["source_id"] not in existing_source_ids
        for heading in source["headings"]
    ]
    metadata = [{
        "source_id": source["source_id"],
        "author": str(source.get("frontmatter", {}).get("author", "")),
        "edition": str(source.get("frontmatter", {}).get("edition", "unknown")),
        "theme": "",
        "content_tendency": ["unknown"],
        "scan_confidence": "low",
    } for source in inventory]
    return {
        "schema_version": "2.0",
        "navigation_title": title,
        "student_analysis": {
            "explicit_facts": [], "inferred_level": "unknown", "knowledge_strengths": [], "knowledge_gaps": [],
            "application_gaps": [], "learning_goals": [], "excluded_goals": [], "resource_constraints": [],
            "recommended_entry_level": "unknown", "recommended_difficulty_curve": "由浅入深",
            "evidence_refs": [], "confidence": "low", "curriculum_impacts": [], "student_change_reviewed": False,
        },
        "course_overview": {"purpose": "", "core_questions": [], "completion_criteria": []},
        "stage_overviews": [],
        "module_overviews": [],
        "overview_units": [],
        "source_metadata": metadata,
        "source_relationships": [],
        "relationship_records": [],
        "items": items,
        "existing_unit_updates": [],
        "coverage_gaps": [],
        "deferred_items": [],
        "lesson_generation_policy": {
            "read_scope": "只读取当前单元、必要先修摘要和少量相邻上下文。",
            "locator_failure": "定位失败时报告并暂停，不得改读整本资料。",
            "distinguish_sources": "区分原书观点、Agent 解释、现代案例和待核验事实。",
            "language_style": "使用大白话、生活化类比和贴近学习者场景的案例。",
            "fact_check_current": "涉及可能变化的事实时使用可靠来源核验。",
            "application_exercises": "每课生成面向学习者真实项目的应用练习。",
            "image_policy": "source_pointer_only",
        },
    }


def write_batch_templates(
    run_dir: Path,
    inventory: list[dict[str, Any]],
    existing_source_ids: set[str],
    *,
    max_candidates: int,
    max_preview_chars: int,
) -> None:
    batch_dir = run_dir / "batches"
    for source in inventory:
        if source["source_id"] in existing_source_ids:
            continue
        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_chars = 0
        for section in source["headings"]:
            section_chars = len(section.get("preview", ""))
            if current and (len(current) >= max_candidates or current_chars + section_chars > max_preview_chars):
                chunks.append(current)
                current, current_chars = [], 0
            current.append(section)
            current_chars += section_chars
        if current:
            chunks.append(current)
        for index, sections in enumerate(chunks, start=1):
            batch_id = f"{source['source_id']}-{index:03d}"
            write_json(batch_dir / f"{batch_id}.template.json", {
                "schema_version": "2.0", "batch_id": batch_id, "source_id": source["source_id"],
                "source_path": source["path"], "items": [decision_item(item) for item in sections],
            })
            write_json(batch_dir / f"{batch_id}.agent-input.json", {
                "source_id": source["source_id"], "source_path": source["path"], "source_sha256": source["sha256"],
                "frontmatter": source["frontmatter"], "sections": sections,
            })


def _collect_item_patches(base: dict[str, Any], batch_dir: Path, *, source_id: str | None = None) -> None:
    base_by_id = {item["candidate_id"]: item for item in base["items"]}
    expected = set(base_by_id)
    covered: set[str] = set()
    patched: set[str] = set()
    templates = sorted(batch_dir.glob(f"{source_id}-*.template.json" if source_id else "*.template.json"))
    for template_path in templates:
        template = read_json(template_path)
        batch_ids = {item["candidate_id"] for item in template["items"]} & expected
        if not batch_ids:
            continue
        decisions_path = template_path.with_name(template_path.name.replace(".template.json", ".decisions.json"))
        if not decisions_path.is_file():
            raise ValueError(f"缺少 batch 决策：{decisions_path.name}")
        value = read_json(decisions_path)
        if value.get("source_id") != template.get("source_id"):
            raise ValueError(f"batch 来源不一致：{decisions_path}")
        declared = value.get("covered_candidate_ids")
        batch_covered = declared if declared is not None else [item.get("candidate_id") for item in value.get("items", [])]
        if set(batch_covered) != batch_ids or len(batch_covered) != len(batch_ids):
            raise ValueError(f"batch 章节阅读覆盖不完整：{decisions_path.name}")
        if covered & batch_ids:
            raise ValueError(f"batch 候选重复：{sorted(covered & batch_ids)}")
        covered.update(batch_ids)
        for patch in value.get("items", []):
            candidate_id = patch.get("candidate_id")
            if candidate_id not in batch_ids or candidate_id in patched:
                raise ValueError(f"batch 决策无效或重复：{candidate_id}")
            patched.add(candidate_id)
            item = dict(base_by_id[candidate_id])
            item.update(patch)
            if "case_context" in patch:
                item["case_context"] = {**base_by_id[candidate_id]["case_context"], **patch["case_context"]}
            base_by_id[candidate_id] = item
    if covered != expected:
        raise ValueError(f"batch 未完整覆盖候选；缺少={sorted(expected - covered)}")
    base["items"] = [base_by_id[item["candidate_id"]] for item in base["items"]]


def collect_batches(base_path: Path, batch_dir: Path, output: Path, schema: Path) -> None:
    base = read_json(base_path)
    _collect_item_patches(base, batch_dir)
    validate_json_schema(base, schema)
    write_json(output, base)


def collect_source_batches(base_path: Path, batch_dir: Path, output: Path, schema: Path) -> None:
    base = read_json(base_path)
    source_id = base["source_id"]
    _collect_item_patches(base, batch_dir, source_id=source_id)
    base["covered_candidate_ids"] = [item["candidate_id"] for item in base["items"]]
    validate_json_schema(base, schema)
    write_json(output, base)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="收集或校验课程导航语义决策")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--decisions", type=Path, required=True)
    validate.add_argument("--schema", type=Path, default=ROOT / "utils/references/learning-navigation-decisions-v2.schema.json")
    collect = sub.add_parser("collect")
    collect.add_argument("--base", type=Path, required=True)
    collect.add_argument("--batch-dir", type=Path, required=True)
    collect.add_argument("--output", type=Path, required=True)
    collect.add_argument("--schema", type=Path, default=ROOT / "utils/references/learning-navigation-decisions-v2.schema.json")
    collect_source = sub.add_parser("collect-source")
    collect_source.add_argument("--base", type=Path, required=True)
    collect_source.add_argument("--batch-dir", type=Path, required=True)
    collect_source.add_argument("--output", type=Path, required=True)
    collect_source.add_argument("--schema", type=Path, default=ROOT / "utils/references/learning-source-decisions-v2.schema.json")
    args = parser.parse_args(argv)
    if args.command == "collect-source":
        collect_source_batches(args.base, args.batch_dir, args.output, args.schema)
        print(f"已聚合当前资料：{args.output}")
    elif args.command == "collect":
        collect_batches(args.base, args.batch_dir, args.output, args.schema)
        print(f"已聚合：{args.output}")
    else:
        validate_json_schema(read_json(args.decisions), args.schema)
        print(f"决策校验通过：{args.decisions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
