"""Stateful runner for initial and incremental curriculum navigation builds."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from utils.scripts.learning_navigation import (
    build_navigation,
    json_text,
    prepare_navigation_units,
    render_navigation_markdown,
    stable_id,
    validate_navigation,
)
from utils.scripts.learning_navigation_bundle import render_source_fragment_markdown
from utils.scripts.dependency_graph import (
    analyze_dependency_graph,
    build_plain_language_loop_report,
    edges_from_units,
    render_plain_language_loop_report,
    validate_selected_order,
)
from utils.scripts.markdown_structure import (
    extract_markdown_structure,
    resolve_markdown_inputs,
    sha256_file,
)
from utils.scripts.structured_io import (
    read_json,
    structured_error_receipt,
    validate_json_schema,
    write_json,
    write_text_transaction,
)
from utils.scripts.student_learning_profile import parse_student_profile
from utils.scripts.timestamp import iso_timestamp, unique_filename_timestamp
from utils.scripts import adaptive_assessment
from utils.scripts.learning_config import freeze_config, acknowledge_config
from utils.scripts.structured_io import write_text_atomic

from prepare_navigation_decisions import build_template, write_batch_templates

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

SKILL_NAME = "build-curriculum-navigation"
NAV_SCHEMA = ROOT / "utils/references/learning-navigation-v2.schema.json"
DECISION_SCHEMA = ROOT / "utils/references/learning-navigation-decisions-v2.schema.json"
STATE_SCHEMA = ROOT / "utils/references/learning-navigation-run-state-v2.schema.json"
PROFILE_SCHEMA = ROOT / "utils/references/student-learning-profile-v2.schema.json"
REQUEST_SCHEMA = ROOT / "utils/references/learning-navigation-request-v2.schema.json"
ORDERING_SCHEMA = ROOT / "utils/references/learning-ordering-decisions-v2.schema.json"
LOOP_RESOLUTION_SCHEMA = ROOT / "utils/references/learning-loop-resolution-v2.schema.json"
SOURCE_FRAGMENT_SCHEMA = ROOT / "utils/references/learning-navigation-source-fragment-v2.schema.json"
SOURCE_DECISIONS_SCHEMA = ROOT / "utils/references/learning-source-decisions-v2.schema.json"
REQUEST_TEMPLATE = Path(__file__).resolve().parents[1] / "templates/navigation-request.template.json"


class CurriculumNavigationError(ValueError):
    pass


def _path(root: Path, value: str | Path) -> Path:
    candidate = Path(value)
    return candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise CurriculumNavigationError(f"路径必须位于项目目录内：{path}") from exc


def _source_fragment_hashes(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        return {}
    return {
        path.relative_to(directory).as_posix(): sha256_file(path)
        for path in sorted(directory.rglob("*")) if path.is_file()
    }


def _state(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "run-state.json"
    if not path.is_file():
        raise CurriculumNavigationError(f"运行状态不存在：{path}")
    value = read_json(path)
    validate_json_schema(value, STATE_SCHEMA)
    return value


def _save_state(run_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = iso_timestamp()
    validate_json_schema(state, STATE_SCHEMA)
    write_json(run_dir / "run-state.json", state)


def init_request(output: Path) -> dict[str, Any]:
    if output.exists():
        raise CurriculumNavigationError(f"默认不覆盖已有请求：{output}")
    value = read_json(REQUEST_TEMPLATE)
    write_json(output, value)
    return {"status": "created", "request": str(output)}


def _load_request(root: Path, request_path: Path) -> dict[str, Any]:
    request = read_json(request_path)
    validate_json_schema(request, REQUEST_SCHEMA)
    if request.get("navigation_json"):
        _relative(root, _path(root, request["navigation_json"]))
    return request


def prepare(root: Path, request_path: Path, *, existing_run_dir: Path | None = None, require_body_points: bool = False) -> dict[str, Any]:
    root = root.resolve()
    request_path = request_path.resolve()
    request = _load_request(root, request_path)
    runs_root = root / "logs" / SKILL_NAME / "runs"
    existing_stamps = [path.name for path in runs_root.iterdir()] if runs_root.is_dir() else []
    run_id = existing_run_dir.name if existing_run_dir else unique_filename_timestamp(existing_stamps)
    run_dir = existing_run_dir.resolve() if existing_run_dir else runs_root / run_id
    navigation_json = (_path(root, request["navigation_json"]) if request.get("navigation_json")
                       else root / "outputs" / SKILL_NAME / "runs" / run_id / "navigation.json")
    navigation_markdown = navigation_json.with_suffix(".md")
    existing = read_json(navigation_json) if navigation_json.is_file() else None
    if existing:
        validate_navigation(existing, schema_path=NAV_SCHEMA, root=root, verify_sources=False)
    if existing_run_dir is None:
        run_dir.mkdir(parents=True, exist_ok=False)
    write_json(run_dir / "request.json", request)
    freeze_config('build-curriculum-navigation', run_dir)
    state = {
        "schema_version": "2.0",
        "run_id": run_id,
        "status": "prepared", "current_stage": "prepared",
        "created_at": iso_timestamp(),
        "updated_at": iso_timestamp(),
        "completed_steps": [], "pending_decisions": [], "errors": [],
        "request_path": str(run_dir / "request.json"), "project_root": str(root),
        "original_request_path": str(request_path),
        "navigation_json": str(navigation_json), "navigation_markdown": str(navigation_markdown),
        "expected_navigation_sha256": sha256_file(navigation_json) if navigation_json.is_file() else None,
        "expected_navigation_markdown_sha256": sha256_file(navigation_markdown) if navigation_markdown.is_file() else None,
        "expected_source_fragment_hashes": _source_fragment_hashes(navigation_json.with_suffix(".sources")),
        "source_hashes": {}, "student_profile_hash": None, "preview_sha256": None, "confirmed_by": None,
        "resume_stage": None, "last_decisions_path": None, "student_profile_changed": False,
        "current_source_id": None, "source_queue": [], "completed_source_ids": [],
        "source_navigation_fragments": [], "graph_revision": None,
    }
    if existing_run_dir is not None:
        old_state = _state(run_dir)
        if old_state.get("project_root") != str(root) or old_state.get("original_request_path") != str(request_path):
            raise CurriculumNavigationError("恢复请求与原运行不一致")
        state["created_at"] = old_state["created_at"]
        require_body_points = require_body_points or old_state.get('body_point_protocol', False)
    state['body_point_protocol'] = require_body_points or request['schema_version'] == '3.0'
    _save_state(run_dir, state)
    try:
        student_value = request.get("student_profile")
        student_path = _path(root, student_value) if student_value else None
        explicit_inputs = resolve_markdown_inputs(
            [Path(value) for value in request["input_paths"]], root=root,
            exclude=[path for path in (navigation_markdown, student_path) if path is not None],
        )
        inputs = set(explicit_inputs)
        missing_existing: list[dict[str, Any]] = []
        for source in (existing or {}).get("sources", []):
            source_path = root / source["path"]
            if source_path.is_file():
                inputs.add(source_path.resolve())
            else:
                missing_existing.append(source)
        inventory = [extract_markdown_structure(
            path, root=root, preview_chars=request["preview_chars"], max_heading_depth=request["max_heading_depth"]
        ) for path in sorted(inputs)]
        old_hash_matches: dict[str, list[dict[str, Any]]] = {}
        for old_source in (existing or {}).get("sources", []):
            old_hash_matches.setdefault(old_source["sha256"], []).append(old_source)
        new_hash_counts: dict[str, int] = {}
        for item in inventory:
            new_hash_counts[item["sha256"]] = new_hash_counts.get(item["sha256"], 0) + 1
        old_paths = {item["path"] for item in (existing or {}).get("sources", [])}
        for item in inventory:
            matches = old_hash_matches.get(item["sha256"], [])
            if item["path"] not in old_paths and len(matches) == 1 and new_hash_counts[item["sha256"]] == 1:
                item["source_id"] = matches[0]["source_id"]
        remaining_preview = request["max_total_preview_chars"]
        for source in inventory:
            for section in source["headings"]:
                preview = section.get("preview", "")
                section["preview"] = preview[:remaining_preview]
                section["preview_truncated"] = len(preview) > remaining_preview
                remaining_preview = max(0, remaining_preview - len(section["preview"]))
        current_ids = {item["source_id"] for item in inventory}
        unresolved_missing = [item["path"] for item in missing_existing if item["source_id"] not in current_ids]
        old_by_path = {item["path"]: item for item in (existing or {}).get("sources", [])}
        new_by_path = {item["path"]: item for item in inventory}
        source_diff = {
            "added": sorted(path for path in new_by_path if path not in old_by_path),
            "removed": sorted(unresolved_missing),
            "changed": sorted(path for path in new_by_path if path in old_by_path and new_by_path[path]["sha256"] != old_by_path[path]["sha256"]),
            "unchanged": sorted(path for path in new_by_path if path in old_by_path and new_by_path[path]["sha256"] == old_by_path[path]["sha256"]),
            "moved": [],
        }
        old_by_id = {item["source_id"]: item for item in (existing or {}).get("sources", [])}
        for item in inventory:
            old = old_by_id.get(item["source_id"])
            if old and old["path"] != item["path"]:
                source_diff["moved"].append({"from": old["path"], "to": item["path"]})
        write_json(run_dir / "source-inventory.json", inventory)
        write_json(run_dir / "source-diff.json", source_diff)
        state["source_hashes"] = {item["path"]: item["sha256"] for item in inventory}
        if unresolved_missing:
            state.update({"status": "paused_source_conflict", "current_stage": "paused_source_conflict", "errors": [f"已有导航来源缺失：{', '.join(unresolved_missing)}"], "pending_decisions": [{"type": "source_conflict", "paths": unresolved_missing}]})
            _save_state(run_dir, state)
            return {"status": state["status"], "run_id": run_id, "run_dir": str(run_dir), "missing_sources": unresolved_missing}
        student_snapshot = None
        if student_path:
            student_snapshot = parse_student_profile(student_path, PROFILE_SCHEMA)
            student_snapshot["path"] = _relative(root, student_path)
            write_json(run_dir / "student-profile-snapshot.json", student_snapshot)
        old_profile = (existing or {}).get("student_profile_ref")
        new_profile_hash = student_snapshot["sha256"] if student_snapshot else None
        profile_changed = bool(existing) and (old_profile or {}).get("sha256") != new_profile_hash
        write_json(run_dir / "student-profile-diff.json", {"changed": profile_changed, "old": old_profile, "new": {key: student_snapshot[key] for key in ("path", "sha256") } if student_snapshot else None})
        old_sources_by_id = {item["source_id"]: item for item in (existing or {}).get("sources", [])}
        reusable_source_ids = {
            item["source_id"] for item in inventory
            if item["source_id"] in old_sources_by_id and old_sources_by_id[item["source_id"]]["sha256"] == item["sha256"]
        }
        template = build_template(inventory, title=request["navigation_title"], existing_source_ids=reusable_source_ids)
        if existing:
            prior = existing.get("planning_profile", {})
            template["student_analysis"] = {key: prior.get(key, value) for key, value in template["student_analysis"].items()}
            template["student_analysis"]["student_change_reviewed"] = not profile_changed
            template["course_overview"] = existing.get("course_overview", template["course_overview"])
            template["source_relationships"] = existing.get("source_relationships", [])
            template["coverage_gaps"] = existing.get("coverage_gaps", [])
            template["deferred_items"] = existing.get("deferred_items", [])
            template["lesson_generation_policy"] = existing.get("lesson_generation_policy", template["lesson_generation_policy"])
            template["stage_overviews"] = [{"title": stage["title"], **stage["overview"]} for stage in existing.get("stages", [])]
            template["module_overviews"] = [
                {"stage": stage["title"], "title": module["title"], **module["overview"]}
                for stage in existing.get("stages", []) for module in stage.get("modules", [])
            ]
            old_metadata = {item["source_id"]: item for item in existing.get("sources", [])}
            for item in template["source_metadata"]:
                old = old_metadata.get(item["source_id"])
                if old:
                    for key in ("author", "edition", "theme", "content_tendency", "scan_confidence"):
                        item[key] = old[key]
        write_json(run_dir / "semantic-decisions.template.json", template)
        write_batch_templates(run_dir, inventory, reusable_source_ids, max_candidates=request["max_candidates_per_batch"], max_preview_chars=request["max_preview_chars_per_batch"])
        source_queue = [item["source_id"] for item in inventory if item["source_id"] not in reusable_source_ids]
        template_by_candidate = {item["candidate_id"]: item for item in template["items"]}
        for source in inventory:
            if source["source_id"] not in source_queue:
                continue
            candidate_ids = [item["candidate_id"] for item in source["headings"]]
            source_dir = run_dir / "source-work" / source["source_id"]
            write_json(source_dir / "source-decisions.template.json", {
                "schema_version": "2.0", "source_id": source["source_id"],
                "covered_candidate_ids": [],
                "items": [template_by_candidate[candidate_id] for candidate_id in candidate_ids],
                "relationship_records": [],
            })
            write_json(source_dir / "reading-manifest.json", {
                "source_id": source["source_id"], "source_path": source["path"], "source_sha256": source["sha256"],
                "required_sections": [{
                    "candidate_id": item["candidate_id"], "title": item["title"],
                    "start_line": item["start_line"], "end_line": item["end_line"],
                } for item in source["headings"]],
            })
        relationships = []
        for index, left in enumerate(inventory):
            left_titles = {item["title"] for item in left["headings"]}
            for right in inventory[index + 1:]:
                right_titles = {item["title"] for item in right["headings"]}
                overlap = sorted(left_titles & right_titles)
                if left["sha256"] == right["sha256"] or overlap:
                    relationships.append({"source_id": left["source_id"], "related_source_id": right["source_id"], "exact_duplicate": left["sha256"] == right["sha256"], "overlapping_titles": overlap})
        write_json(run_dir / "source-relationship-candidates.json", relationships)
        sequential = len(source_queue) > 1
        next_status = "source_navigation_fragment_required" if sequential else "source_navigation_fragments_required"
        state.update({
            "status": next_status, "current_stage": next_status,
            "completed_steps": ["sources_scanned", "source_diff_generated", "batches_prepared"],
            "pending_decisions": ([{
                "type": "source_navigation_fragment", "source_id": source_queue[0],
                "template": str(run_dir / "source-work" / source_queue[0] / "source-decisions.template.json"),
            }] if sequential else [{"type": "source_navigation_fragments", "template": "semantic-decisions.template.json"}]),
            "student_profile_hash": student_snapshot["sha256"] if student_snapshot else None,
            "student_profile_changed": profile_changed,
            "source_queue": source_queue,
            "current_source_id": source_queue[0] if sequential else None,
            "completed_source_ids": sorted(reusable_source_ids),
        })
        if state['body_point_protocol']:
            from utils.scripts.learning_content import fragments
            state['point_division_sources'] = [s['source_id'] for s in inventory]
            state['post_division_status'] = next_status
            for source in inventory:
                directory = run_dir / 'source-work' / source['source_id']
                write_json(directory / 'body-fragments.json', {
                    'source_id': source['source_id'], 'source_sha256': source['sha256'],
                    'fragments': fragments(root / source['path'], source['source_id'], source['sha256'])})
                write_json(directory / 'point-division.template.json', {
                    'source_id': source['source_id'], 'source_sha256': source['sha256'],
                    'points': [], 'ignored_fragments': []})
            state.update(status='point_division_required', current_stage='point_division_required',
                         pending_decisions=[{'type': 'body_point_division', 'source_ids': state['point_division_sources']}])
        _save_state(run_dir, state)
        return {"status": state["status"], "run_id": run_id, "run_dir": str(run_dir), "candidate_count": len(template["items"]), "batch_count": len(list((run_dir / "batches").glob("*.template.json")))}
    except Exception as exc:
        state.update({"status": "paused_error", "current_stage": "paused_error", "resume_stage": "prepare", "errors": [str(exc)]})
        _save_state(run_dir, state)
        return {"status": "paused_error", "run_id": run_id, "run_dir": str(run_dir), "error": str(exc)}


def resolve_points(run_dir, source_id, decisions_path):
    """Replace heading candidates with agent-divided, hash-bound body points."""
    from utils.scripts.learning_content import validate_division
    import copy
    state = _state(run_dir)
    if state['status'] == 'paused_error' and state.get('resume_from_status') == 'point_division_required':
        state.update(status='point_division_required', current_stage='point_division_required')
    if state['status'] != 'point_division_required' or source_id not in state['point_division_sources']:
        raise ValueError('当前不能提交该资料的正文要点划分')
    inventory = read_json(run_dir / 'source-inventory.json')
    source = next(s for s in inventory if s['source_id'] == source_id)
    root = Path(state['project_root'])
    if sha256_file(root / source['path']) != source['sha256']: raise ValueError('来源正文已变化')
    directory = run_dir / 'source-work' / source_id
    value = read_json(decisions_path)
    from utils.scripts.learning_content import fragments
    actual_body = fragments(root / source['path'], source_id, source['sha256'])
    if read_json(directory / 'body-fragments.json')['fragments'] != actual_body:
        raise ValueError('正文片段清单已变化，请恢复完整来源清单')
    points = validate_division(source, {'fragments': actual_body}, value)
    original = source.get('original_headings', source['headings'])
    source['original_headings'] = original
    headings = []
    for point in points:
        anchor = next((h for h in reversed(original) if h['start_line'] <= point['start_line']), original[0])
        candidate = 'SEC-' + point['point_id'][6:]
        headings.append({**anchor, 'candidate_id': candidate, 'preview': point['summary'],
                         'content_span': {'start_line': point['start_line'], 'end_line': point['end_line']}})
        point['candidate_id'] = candidate
    source['headings'] = headings; source['material_points'] = points
    template = read_json(run_dir / 'semantic-decisions.template.json')
    old_ids = {h['candidate_id'] for h in original}
    base = next((x for x in template['items'] if x['candidate_id'] in old_ids), None)
    if base is None:
        base = build_template([source], title=template['navigation_title'], existing_source_ids=set())['items'][0]
    items = []
    for point in points:
        item = copy.deepcopy(base)
        item.update(candidate_id=point['candidate_id'], include=True, title=point['summary'],
                    importance='required' if point['track'] == 'main' else 'optional')
        items.append(item)
    template['items'] = [x for x in template['items'] if x['candidate_id'] not in old_ids] + items
    if source_id in state['completed_source_ids']: state['completed_source_ids'].remove(source_id)
    if source_id not in state['source_queue']: state['source_queue'].append(source_id)
    remaining = [x for x in state['point_division_sources'] if x != source_id]
    state['point_division_sources'] = remaining
    state['completed_steps'].append('body_points_ready:' + source_id)
    if not remaining:
        state.update(status='source_navigation_fragment_required', current_stage='source_navigation_fragment_required',
                     current_source_id=state['source_queue'][0], pending_decisions=[{'type': 'source_navigation_fragment', 'source_id': state['source_queue'][0]}])
    write_text_transaction({run_dir / 'source-inventory.json': json_text(inventory),
                            run_dir / 'semantic-decisions.template.json': json_text(template),
                            directory / 'point-division.json': json_text(value),
                            directory / 'source-decisions.template.json': json_text({
                                'schema_version': '2.0', 'source_id': source_id,
                                'covered_candidate_ids': [], 'items': items, 'relationship_records': []})})
    request = read_json(Path(state['request_path']))
    write_batch_templates(run_dir, inventory, set(), max_candidates=request['max_candidates_per_batch'], max_preview_chars=request['max_preview_chars_per_batch'])
    _save_state(run_dir, state)
    return {'status': state['status'], 'remaining_source_ids': remaining, 'point_count': len(points)}


def _graph_revision(units: list[dict[str, Any]]) -> str:
    payload = [{"unit_id": item["unit_id"], "prerequisites": item.get("prerequisites", [])} for item in units]
    return hashlib.sha256(json_text(payload).encode("utf-8")).hexdigest()


def _validate_source_items(items: list[dict[str, Any]]) -> None:
    decision_schema = read_json(DECISION_SCHEMA)
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "array", "items": {"$ref": "#/$defs/item"}, "$defs": decision_schema["$defs"],
    }
    errors = sorted(Draft202012Validator(schema).iter_errors(items), key=lambda item: list(item.absolute_path))
    if errors:
        rendered = [f"{'/'.join(str(value) for value in error.absolute_path) or '$'}: {error.message}" for error in errors]
        raise CurriculumNavigationError("单资料决策校验失败：" + "；".join(rendered))


def resolve_source(run_dir: Path, source_id: str, decisions_path: Path) -> dict[str, Any]:
    """Complete exactly one source before the next source becomes available."""
    run_dir = run_dir.resolve()
    state = _state(run_dir)
    if state["status"] != "source_navigation_fragment_required":
        raise CurriculumNavigationError(f"当前状态不能提交单资料导航：{state['status']}")
    if source_id != state.get("current_source_id"):
        raise CurriculumNavigationError(f"当前应先完成资料：{state.get('current_source_id')}")
    value = read_json(decisions_path.resolve())
    validate_json_schema(value, SOURCE_DECISIONS_SCHEMA)
    if value["source_id"] != source_id:
        raise CurriculumNavigationError("单资料决策中的 source_id 与当前资料不一致")
    _validate_source_items(value["items"])
    inventory = read_json(run_dir / "source-inventory.json")
    source = next((item for item in inventory if item["source_id"] == source_id), None)
    if source is None:
        raise CurriculumNavigationError(f"当前资料不在来源清单中：{source_id}")
    expected = {item["candidate_id"] for item in source["headings"]}
    covered = set(value["covered_candidate_ids"])
    actual = {item["candidate_id"] for item in value["items"]}
    if covered != expected:
        raise CurriculumNavigationError(f"必须确认已阅读当前资料的全部章节；缺少={sorted(expected - covered)}；多余={sorted(covered - expected)}")
    if actual != expected or len(actual) != len(value["items"]):
        raise CurriculumNavigationError("单资料决策必须逐项覆盖当前资料的全部章节，且不得重复")
    heading_by_id = {item["candidate_id"]: item for item in source["headings"]}
    if source.get('material_points') and any(not item['include'] for item in value['items']):
        raise ValueError('所有实质原文要点必须建立知识点；暂缓内容安排为支线')
    points = []
    for item in value["items"]:
        if not item["include"]:
            continue
        heading = heading_by_id[item["candidate_id"]]
        points.append({
            "point_id": stable_id("UNIT", item["candidate_id"]), "title": item["title"],
            "difficulty": item["difficulty"], "purpose": item["purpose"],
            "source_locator": {
                "source_id": source_id, "file": source["path"], "source_sha256": source["sha256"],
                "heading_text": heading["title"], "heading_level": heading["level"],
                "heading_occurrence": heading["occurrence"], "parent_heading_chain": heading["parent_heading_chain"],
                "start_line_hint": heading["start_line"], "end_before_heading": heading["end_before_heading"],
                "end_line_hint": heading["end_line"], "locator_status": "valid",
                **({'content_span': heading['content_span']} if heading.get('content_span') else {}),
            },
        })
    records = []
    seen: set[tuple[str, str]] = set()
    for record in value["relationship_records"]:
        before = stable_id("UNIT", record["predecessor_ref"]) if record["predecessor_ref"].startswith(("SEC-", "DOC-")) else record["predecessor_ref"]
        after = stable_id("UNIT", record["successor_ref"]) if record["successor_ref"].startswith(("SEC-", "DOC-")) else record["successor_ref"]
        seen.add((before, after))
        records.append({
            "predecessor_id": before, "successor_id": after, "reason": record["reason"],
            "evidence_refs": record["evidence_refs"], "confidence": record["confidence"],
            "source_ids": record["source_ids"] or [source_id],
        })
    title_by_candidate = {item["candidate_id"]: item["title"] for item in value["items"]}
    for item in value["items"]:
        if not item["include"]:
            continue
        after = stable_id("UNIT", item["candidate_id"])
        for prerequisite in item["prerequisites"]:
            before = stable_id("UNIT", prerequisite) if prerequisite.startswith(("SEC-", "DOC-")) else prerequisite
            if (before, after) in seen:
                continue
            records.append({
                "predecessor_id": before, "successor_id": after,
                "reason": f"“{item['title']}”将“{title_by_candidate.get(prerequisite, prerequisite)}”列为需要先掌握的内容。",
                "evidence_refs": [], "confidence": "unknown", "source_ids": [source_id],
            })
    fragment = {
        "schema_version": "2.0",
        "source": {"source_id": source_id, "path": source["path"], "sha256": source["sha256"]},
        "learning_points": points, "relationship_records": records,
    }
    validate_json_schema(fragment, SOURCE_FRAGMENT_SCHEMA)
    source_output = run_dir / "source-fragments" / f"{source_id}.json"
    write_text_transaction({source_output: json_text(fragment), source_output.with_suffix(".md"): _render_source_fragment(fragment)})
    stored = run_dir / "source-decisions" / f"{source_id}.json"
    write_json(stored, value)
    completed = [*state.get("completed_source_ids", []), source_id]
    remaining = [item for item in state.get("source_queue", []) if item != source_id]
    fragment_refs = [
        item for item in state.get("source_navigation_fragments", []) if item.get("source_id") != source_id
    ]
    fragment_refs.append({"source_id": source_id, "json": str(source_output), "markdown": str(source_output.with_suffix('.md'))})
    if remaining:
        next_id = remaining[0]
        state.update({
            "status": "source_navigation_fragment_required", "current_stage": "source_navigation_fragment_required",
            "current_source_id": next_id, "source_queue": remaining, "completed_source_ids": completed,
            "source_navigation_fragments": fragment_refs,
            "pending_decisions": [{"type": "source_navigation_fragment", "source_id": next_id, "template": str(run_dir / "source-work" / next_id / "source-decisions.template.json")}],
        })
    else:
        state.update({
            "status": "all_source_navigation_fragments_ready", "current_stage": "all_source_navigation_fragments_ready",
            "current_source_id": None, "source_queue": [], "completed_source_ids": completed,
            "source_navigation_fragments": fragment_refs,
            "pending_decisions": [{"type": "global_navigation_decisions", "template": str(run_dir / "semantic-decisions.template.json")}],
        })
    state["completed_steps"] = [*state["completed_steps"], f"source_fragment_ready:{source_id}"]
    _save_state(run_dir, state)
    return {"status": state["status"], "run_dir": str(run_dir), "completed_source_id": source_id, "next_source_id": state.get("current_source_id")}


def _merge_completed_source_decisions(run_dir: Path, decisions: dict[str, Any]) -> dict[str, Any]:
    merged = dict(decisions)
    items_by_id = {item["candidate_id"]: item for item in decisions["items"]}
    relationships = list(decisions.get("relationship_records", []))
    seen_relationships = {(item["predecessor_ref"], item["successor_ref"], item["reason"]) for item in relationships}
    for path in sorted((run_dir / "source-decisions").glob("*.json")):
        source_value = read_json(path)
        for item in source_value["items"]:
            items_by_id[item["candidate_id"]] = item
        for record in source_value["relationship_records"]:
            key = (record["predecessor_ref"], record["successor_ref"], record["reason"])
            if key not in seen_relationships:
                seen_relationships.add(key)
                relationships.append(record)
    merged["items"] = [items_by_id[item["candidate_id"]] for item in decisions["items"]]
    merged["relationship_records"] = relationships
    return merged


def _resolved_relationship_records(
    units: list[dict[str, Any]], decisions: dict[str, Any], aliases: dict[str, str]
) -> list[dict[str, Any]]:
    by_id = {item["unit_id"]: item for item in units}
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for record in decisions.get("relationship_records", []):
        before = aliases.get(record["predecessor_ref"], record["predecessor_ref"])
        after = aliases.get(record["successor_ref"], record["successor_ref"])
        seen.add((before, after))
        result.append({
            "predecessor_id": before,
            "successor_id": after,
            "reason": record["reason"],
            "evidence_refs": record["evidence_refs"],
            "confidence": record["confidence"],
            "source_ids": record["source_ids"],
        })
    for unit in units:
        for before in unit.get("prerequisites", []):
            key = (before, unit["unit_id"])
            if key in seen:
                continue
            seen.add(key)
            source_ids = []
            for related in (by_id[before], unit):
                source = related.get("source")
                if source and source["source_id"] not in source_ids:
                    source_ids.append(source["source_id"])
            result.append({
                "predecessor_id": before,
                "successor_id": unit["unit_id"],
                "reason": f"“{unit['title']}”将“{by_id[before]['title']}”列为需要先掌握的内容。",
                "evidence_refs": [],
                "confidence": "unknown",
                "source_ids": source_ids,
            })
    return result


def _render_source_fragment(fragment: dict[str, Any]) -> str:
    return render_source_fragment_markdown(fragment)


def _write_source_fragments(
    run_dir: Path, inventory: list[dict[str, Any]], units: list[dict[str, Any]], records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    output_dir = run_dir / "source-fragments"
    references: list[dict[str, Any]] = []
    for source in inventory:
        source_id = source["source_id"]
        points = []
        point_ids: set[str] = set()
        for unit in units:
            locator = unit.get("source")
            if not locator or locator["source_id"] != source_id:
                continue
            point_ids.add(unit["unit_id"])
            points.append({
                "point_id": unit["unit_id"], "title": unit["title"], "difficulty": unit["difficulty"],
                "purpose": unit["purpose"], "source_locator": locator,
            })
        local_records = [item for item in records if item["predecessor_id"] in point_ids or item["successor_id"] in point_ids]
        fragment = {
            "schema_version": "2.0",
            "source": {"source_id": source_id, "path": source["path"], "sha256": source["sha256"]},
            "learning_points": points,
            "relationship_records": local_records,
        }
        validate_json_schema(fragment, SOURCE_FRAGMENT_SCHEMA)
        json_path = output_dir / f"{source_id}.json"
        md_path = output_dir / f"{source_id}.md"
        write_text_transaction({json_path: json_text(fragment), md_path: _render_source_fragment(fragment)})
        references.append({"source_id": source_id, "json": str(json_path), "markdown": str(md_path)})
    write_json(run_dir / "source-fragment-index.json", references)
    return references


def _assemble_edges_from_fragments(run_dir: Path, units: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Assemble the global graph from every completed source fragment."""
    unit_edges = edges_from_units(units)
    by_id = {item["unit_id"]: item for item in units}
    fragment_pairs: set[tuple[str, str]] = set()
    for path in sorted((run_dir / "source-fragments").glob("*.json")):
        fragment = read_json(path)
        validate_json_schema(fragment, SOURCE_FRAGMENT_SCHEMA)
        for record in fragment["relationship_records"]:
            fragment_pairs.add((record["predecessor_id"], record["successor_id"]))
    required_source_pairs = {
        (edge["predecessor_id"], edge["successor_id"])
        for edge in unit_edges
        if by_id[edge["predecessor_id"]].get("source") is not None or by_id[edge["successor_id"]].get("source") is not None
    }
    missing = sorted(required_source_pairs - fragment_pairs)
    if missing:
        raise CurriculumNavigationError(f"总图关系没有完整来源于单资料导航片段：{missing}")
    pairs = set(fragment_pairs)
    pairs.update(
        (edge["predecessor_id"], edge["successor_id"])
        for edge in unit_edges
        if by_id[edge["predecessor_id"]].get("source") is None and by_id[edge["successor_id"]].get("source") is None
    )
    return [{"predecessor_id": before, "successor_id": after} for before, after in sorted(pairs)]


def _write_ordering_materials(
    run_dir: Path, revision: str, analysis: dict[str, Any], units: list[dict[str, Any]],
    records: list[dict[str, Any]], student_analysis: dict[str, Any],
) -> None:
    by_id = {item["unit_id"]: item for item in units}
    outgoing_count: dict[str, int] = {unit_id: 0 for unit_id in by_id}
    for edge in analysis["edges"]:
        outgoing_count[edge["predecessor_id"]] += 1
    related_reasons: dict[str, list[str]] = {unit_id: [] for unit_id in by_id}
    for record in records:
        text = record.get("reason", "")
        for unit_id in (record["predecessor_id"], record["successor_id"]):
            if text and text not in related_reasons[unit_id]:
                related_reasons[unit_id].append(text)
    choice_points = []
    for point in analysis["choice_points"]:
        choice_points.append({
            "position": point["position"],
            "recommended_id": point["recommended_id"],
            "candidates": [{
                "point_id": unit_id, "title": by_id[unit_id]["title"],
                "difficulty": by_id[unit_id]["difficulty"], "stage": by_id[unit_id]["stage"],
                "module": by_id[unit_id]["module"], "purpose": by_id[unit_id]["purpose"],
                "unlocks_directly": outgoing_count[unit_id],
                "relationship_reasons": related_reasons[unit_id],
            } for unit_id in point["candidate_ids"]],
        })
    write_json(run_dir / "ordering-decisions.template.json", {
        "schema_version": "2.0", "graph_revision": revision,
        "selected_order": analysis["recommended_order"],
        "rationale": "先确认当前水平与学习目的，逐题四选一粗测知识边界；记录目标、边界与关键路线取舍，再确认此顺序。",
        "learner_context": {"current_level": "待确认", "learning_goal": "待确认",
                            "diagnostic_answers": [], "estimated_boundary": "待测评"},
    })
    write_json(run_dir / "ordering-choice-points.json", {
        "student_learning_goals": student_analysis.get("learning_goals", []),
        "student_knowledge_strengths": student_analysis.get("knowledge_strengths", []),
        "student_knowledge_gaps": student_analysis.get("knowledge_gaps", []),
        "diagnostic_guidance": {
            "question_mode": "one_four_choice_question_at_a_time",
            "search": "goal_first_adaptive", "max_questions": freeze_config(SKILL_NAME, run_dir)["config"]["assessment"]["max_multiple_choice_questions"],
            "record": ["tested_unit_id", "user_choice", "estimated_boundary"],
            "note": "单题答对不代表掌握；目的和边界只用于合法拓扑序中的路线选择。",
        },
        "selection_priorities": [
            "不得违反任何前置要求", "符合用户学习目标", "贴近诊断所得知识边界", "难度平缓上升",
            "主题衔接自然", "优先解锁更多后续内容", "减少无意义的资料或主题跳转",
        ],
        "choice_points": choice_points,
    })


def _write_preview(
    run_dir: Path, state: dict[str, Any], decisions_path: Path, decisions: dict[str, Any],
    inventory: list[dict[str, Any]], existing: dict[str, Any] | None,
    student_snapshot: dict[str, Any] | None, ordered_unit_ids: list[str],
    prerequisite_overrides: dict[str, list[str]] | None = None,
    learner_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    navigation = build_navigation(
        inventory=inventory, decisions=decisions, existing=existing, student_snapshot=student_snapshot,
        ordered_unit_ids=ordered_unit_ids, prerequisite_overrides=prerequisite_overrides,
    )
    if learner_context:
        navigation["planning_profile"]["route_context"] = learner_context
    if (run_dir / "candidate-history.json").is_file():
        navigation["planning_profile"]["ordering_history"] = read_json(run_dir / "candidate-history.json")
    if (run_dir / 'knowledge-boundary.json').is_file():
        navigation['planning_profile']['assessment'] = read_json(run_dir / 'knowledge-boundary.json')
    if (run_dir / 'candidate-orders.json').is_file():
        navigation['planning_profile']['ordering'] = read_json(run_dir / 'candidate-orders.json')
    project_root = Path(state["project_root"])
    report = validate_navigation(navigation, schema_path=NAV_SCHEMA, root=project_root, verify_sources=True)
    preview_json = run_dir / "navigation-preview.json"
    preview_md = run_dir / "navigation-preview.md"
    write_text_transaction({preview_json: json_text(navigation), preview_md: render_navigation_markdown(navigation)})
    write_json(run_dir / "validation-report.json", report)
    write_json(run_dir / "student-planning-analysis.json", decisions["student_analysis"])
    old_by_id = {item["unit_id"]: item for item in (existing or {}).get("units", [])}
    new_by_id = {item["unit_id"]: item for item in navigation["units"]}
    tracked = ("sequence", "stage", "module", "importance", "difficulty", "prerequisites", "concept_roles", "visual_references")
    changed_units = []
    for unit_id in sorted(set(old_by_id) & set(new_by_id)):
        changes = {key: {"old": old_by_id[unit_id].get(key), "new": new_by_id[unit_id].get(key)} for key in tracked if old_by_id[unit_id].get(key) != new_by_id[unit_id].get(key)}
        if changes:
            changed_units.append({"unit_id": unit_id, "changes": changes})
    write_json(run_dir / "navigation-diff.json", {
        "added_unit_ids": sorted(set(new_by_id) - set(old_by_id)), "removed_unit_ids": sorted(set(old_by_id) - set(new_by_id)),
        "retained_unit_ids": sorted(set(old_by_id) & set(new_by_id)), "changed_units": changed_units,
        "source_diff": read_json(run_dir / "source-diff.json"),
    })
    write_json(run_dir / "student-impact-diff.json", {
        "student_profile_changed": bool(state.get("student_profile_changed")),
        "affected_units": [item["unit_id"] for item in changed_units],
        "curriculum_impacts": decisions["student_analysis"]["curriculum_impacts"],
    })
    state.update({
        "status": "awaiting_approval", "current_stage": "awaiting_approval",
        "completed_steps": [*state["completed_steps"], "global_order_validated", "preview_rendered"],
        "pending_decisions": [{"type": "approval", "preview": str(preview_md)}],
        "preview_sha256": sha256_file(preview_json), "last_decisions_path": str(decisions_path.resolve()),
    })
    _save_state(run_dir, state)
    return {**report, "status": state["status"], "run_dir": str(run_dir), "preview_sha256": state["preview_sha256"]}


def resolve(run_dir: Path, decisions_path: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    state = _state(run_dir)
    if state["status"] == "source_navigation_fragment_required":
        raise CurriculumNavigationError(f"必须先完成当前资料导航：{state.get('current_source_id')}")
    if state["status"] not in {"source_navigation_fragments_required", "all_source_navigation_fragments_ready", "semantic_decisions_required", "paused_error"}:
        raise CurriculumNavigationError(f"当前状态不能提交语义决策：{state['status']}")
    decisions = read_json(decisions_path.resolve())
    if state["status"] == "all_source_navigation_fragments_ready":
        decisions = _merge_completed_source_decisions(run_dir, decisions)
    validate_json_schema(decisions, DECISION_SCHEMA)
    if state.get("student_profile_changed") and not decisions["student_analysis"]["student_change_reviewed"]:
        raise CurriculumNavigationError("学生信息已变化，必须明确完成课程影响复核")
    template = read_json(run_dir / "semantic-decisions.template.json")
    expected = {item["candidate_id"] for item in template["items"]}
    actual = {item["candidate_id"] for item in decisions["items"]}
    if expected != actual or len(actual) != len(decisions["items"]):
        raise CurriculumNavigationError("语义决策必须逐项覆盖模板中的全部候选，且不得重复")
    inventory = read_json(run_dir / "source-inventory.json")
    navigation_path = Path(state["navigation_json"])
    existing = read_json(navigation_path) if navigation_path.is_file() else None
    student_snapshot = read_json(run_dir / "student-profile-snapshot.json") if (run_dir / "student-profile-snapshot.json").is_file() else None
    units, aliases = prepare_navigation_units(inventory=inventory, decisions=decisions, existing=existing)
    records = _resolved_relationship_records(units, decisions, aliases)
    fragments = _write_source_fragments(run_dir, inventory, units, records)
    edges = _assemble_edges_from_fragments(run_dir, units)
    analysis = analyze_dependency_graph([item["unit_id"] for item in units], edges)
    revision = _graph_revision(units)
    write_json(run_dir / "graph-units.json", units)
    write_json(run_dir / "relationship-records.json", records)
    write_json(run_dir / "global-graph-analysis.json", analysis)
    write_json(run_dir / "resolved-decisions.json", decisions)
    state.update({
        "completed_steps": [*state["completed_steps"], "source_fragments_created", "global_graph_built"],
        "source_navigation_fragments": fragments,
        "graph_revision": revision,
        "last_decisions_path": str(decisions_path.resolve()),
    })
    if not analysis["is_dag"]:
        report = build_plain_language_loop_report(analysis, units, records)
        write_text_transaction({
            run_dir / "learning-order-blocked.json": json_text(report),
            run_dir / "learning-order-blocked.md": render_plain_language_loop_report(report),
        })
        state.update({
            "status": "awaiting_loop_resolution", "current_stage": "awaiting_loop_resolution",
            "pending_decisions": [{"type": "learning_order_adjustment", "report": str(run_dir / "learning-order-blocked.md")}],
        })
        _save_state(run_dir, state)
        return {"status": state["status"], "run_dir": str(run_dir), "report": str(run_dir / "learning-order-blocked.md")}
    if analysis["is_dag"]:
        draft = build_navigation(inventory=inventory, decisions=decisions, existing=existing,
                                 student_snapshot=student_snapshot, ordered_unit_ids=analysis['recommended_order'])
        validate_navigation(draft, schema_path=NAV_SCHEMA, root=Path(state['project_root']), verify_sources=True)
        _write_ordering_materials(run_dir, revision, analysis, units, records, decisions["student_analysis"])
        state.update({
            "status": "ordering_decision_required", "current_stage": "ordering_decision_required",
            "pending_decisions": [{"type": "ordering_decision", "template": str(run_dir / "ordering-decisions.template.json")}],
        })
        _save_state(run_dir, state)
        return {"status": state["status"], "run_dir": str(run_dir), "choice_point_count": len(analysis["choice_points"])}
    return _write_preview(
        run_dir, state, decisions_path, decisions, inventory, existing, student_snapshot,
        analysis["recommended_order"],
    )


def resolve_ordering(run_dir: Path, ordering_path: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    state = _state(run_dir)
    if state["status"] != "ordering_decision_required":
        raise CurriculumNavigationError(f"当前状态不能提交学习顺序选择：{state['status']}")
    ordering = read_json(ordering_path.resolve())
    validate_json_schema(ordering, ORDERING_SCHEMA)
    if ordering["graph_revision"] != state.get("graph_revision"):
        raise CurriculumNavigationError("学习关系已变化，请基于最新模板重新选择顺序")
    units = read_json(run_dir / "graph-units.json")
    edges = edges_from_units(units)
    validate_selected_order([item["unit_id"] for item in units], edges, ordering["selected_order"])
    context = ordering["learner_context"]
    if context["current_level"] == "待确认" or context["learning_goal"] == "待确认" or context["estimated_boundary"] == "待测评":
        raise CurriculumNavigationError("请先确认学习水平、学习目的并完成逐题知识边界诊断")
    cfg = freeze_config(SKILL_NAME, run_dir)['config']
    diagnostic_path = run_dir / 'knowledge-boundary.json'
    if not diagnostic_path.is_file():
        raise CurriculumNavigationError('先调用 diagnostic-start 并逐题完成诊断')
    assessment = read_json(diagnostic_path)
    if assessment['status'] != 'completed' or assessment['graph_revision'] != adaptive_assessment.begin(units, context['current_level'], context['learning_goal'], cfg['assessment']['max_multiple_choice_questions'])['graph_revision']:
        raise CurriculumNavigationError('诊断尚未结束或依赖图已变化')
    if context['diagnostic_answers'] != assessment['answers']:
        raise CurriculumNavigationError('诊断记录与已归档回答不一致')
    context['diagnostic_answers'] = assessment['answers']
    context['goal_unit_ids'] = assessment['goal_unit_ids']
    ranked = adaptive_assessment.candidates(units, context, cfg)
    if ordering['selected_order'] not in [r['unit_ids'] for r in ranked['candidates']]:
        raise CurriculumNavigationError('请选择已生成候选路线；不得丢弃候选后另写序列')
    ranked['selected_order_id'] = next(r['candidate_id'] for r in ranked['candidates'] if r['unit_ids'] == ordering['selected_order'])
    retain_candidates(run_dir, ranked)
    write_json(run_dir / "ordering-decisions.json", ordering)
    decisions = read_json(run_dir / "resolved-decisions.json")
    inventory = read_json(run_dir / "source-inventory.json")
    navigation_path = Path(state["navigation_json"])
    existing = read_json(navigation_path) if navigation_path.is_file() else None
    student_snapshot = read_json(run_dir / "student-profile-snapshot.json") if (run_dir / "student-profile-snapshot.json").is_file() else None
    overrides = {item["unit_id"]: item.get("prerequisites", []) for item in units}
    return _write_preview(
        run_dir, state, Path(state["last_decisions_path"]), decisions, inventory, existing,
        student_snapshot, ordering["selected_order"], overrides, context,
    )


def apply_loop_resolution(run_dir: Path, resolution_path: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    state = _state(run_dir)
    if state["status"] != "awaiting_loop_resolution":
        raise CurriculumNavigationError(f"当前状态不能调整学习关系：{state['status']}")
    resolution = read_json(resolution_path.resolve())
    validate_json_schema(resolution, LOOP_RESOLUTION_SCHEMA)
    if resolution["graph_revision"] != state.get("graph_revision"):
        raise CurriculumNavigationError("学习关系已变化，请基于最新报告重新确认调整")
    units = read_json(run_dir / "graph-units.json")
    by_id = {item["unit_id"]: item for item in units}
    for action in resolution["actions"]:
        before, after = action["predecessor_id"], action["successor_id"]
        if before not in by_id or after not in by_id or before not in by_id[after].get("prerequisites", []):
            raise CurriculumNavigationError(f"待调整的学习要求不存在：{before} -> {after}")
        by_id[after]["prerequisites"].remove(before)
        if action["action"] == "reverse_requirement" and after not in by_id[before]["prerequisites"]:
            by_id[before]["prerequisites"].append(after)
    revision = _graph_revision(units)
    edges = edges_from_units(units)
    analysis = analyze_dependency_graph([item["unit_id"] for item in units], edges)
    write_json(run_dir / "graph-units.json", units)
    write_json(run_dir / "global-graph-analysis.json", analysis)
    write_json(run_dir / "loop-resolution.json", resolution)
    state.update({"graph_revision": revision, "completed_steps": [*state["completed_steps"], "learning_order_adjustment_applied"]})
    if not analysis["is_dag"]:
        records = read_json(run_dir / "relationship-records.json")
        report = build_plain_language_loop_report(analysis, units, records)
        write_text_transaction({
            run_dir / "learning-order-blocked.json": json_text(report),
            run_dir / "learning-order-blocked.md": render_plain_language_loop_report(report),
        })
        state.update({
            "status": "awaiting_loop_resolution", "current_stage": "awaiting_loop_resolution",
            "pending_decisions": [{"type": "learning_order_adjustment", "report": str(run_dir / "learning-order-blocked.md")}],
        })
        _save_state(run_dir, state)
        return {"status": state["status"], "run_dir": str(run_dir), "report": str(run_dir / "learning-order-blocked.md")}
    if analysis["is_dag"]:
        decisions = read_json(run_dir / "resolved-decisions.json")
        records = read_json(run_dir / "relationship-records.json")
        _write_ordering_materials(run_dir, revision, analysis, units, records, decisions["student_analysis"])
        state.update({
            "status": "ordering_decision_required", "current_stage": "ordering_decision_required",
            "pending_decisions": [{"type": "ordering_decision", "template": str(run_dir / "ordering-decisions.template.json")}],
        })
        _save_state(run_dir, state)
        return {"status": state["status"], "run_dir": str(run_dir), "choice_point_count": len(analysis["choice_points"])}
    decisions = read_json(run_dir / "resolved-decisions.json")
    inventory = read_json(run_dir / "source-inventory.json")
    navigation_path = Path(state["navigation_json"])
    existing = read_json(navigation_path) if navigation_path.is_file() else None
    student_snapshot = read_json(run_dir / "student-profile-snapshot.json") if (run_dir / "student-profile-snapshot.json").is_file() else None
    overrides = {item["unit_id"]: item.get("prerequisites", []) for item in units}
    return _write_preview(
        run_dir, state, Path(state["last_decisions_path"]), decisions, inventory, existing,
        student_snapshot, analysis["recommended_order"], overrides,
    )


def _verify_unchanged_inputs(root: Path, state: dict[str, Any]) -> None:
    for relative, expected in state["source_hashes"].items():
        path = root / relative
        if not path.is_file() or sha256_file(path) != expected:
            raise CurriculumNavigationError(f"输入教材在运行期间发生变化：{relative}")
    request = read_json(Path(state["request_path"]))
    profile = request.get("student_profile")
    if profile and sha256_file(_path(root, profile)) != state["student_profile_hash"]:
        raise CurriculumNavigationError("学生信息在运行期间发生变化")


def commit(run_dir: Path, *, confirmed_by: str, preview_sha256: str) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    state = _state(run_dir)
    if state["status"] != "awaiting_approval":
        raise CurriculumNavigationError(f"当前状态不能提交：{state['status']}")
    if preview_sha256 != state["preview_sha256"]:
        raise CurriculumNavigationError("批准的预览 SHA-256 与当前预览不一致")
    state["confirmed_by"] = confirmed_by
    state["approved_preview_sha256"] = preview_sha256
    _save_state(run_dir, state)
    root = Path(state["project_root"])
    _verify_unchanged_inputs(root, state)
    target_json = Path(state["navigation_json"])
    expected = state["expected_navigation_sha256"]
    actual = sha256_file(target_json) if target_json.is_file() else None
    if actual != expected:
        raise CurriculumNavigationError("正式导航在运行期间发生变化，禁止覆盖")
    markdown_target = Path(state["navigation_markdown"])
    markdown_actual = sha256_file(markdown_target) if markdown_target.is_file() else None
    if markdown_actual != state.get("expected_navigation_markdown_sha256"):
        raise CurriculumNavigationError("正式导航 Markdown 在运行期间发生变化，禁止覆盖")
    source_target_dir = target_json.with_suffix(".sources")
    if _source_fragment_hashes(source_target_dir) != state.get("expected_source_fragment_hashes", {}):
        raise CurriculumNavigationError("正式来源片段在运行期间发生变化，禁止覆盖")
    navigation = read_json(run_dir / "navigation-preview.json")
    validate_navigation(navigation, schema_path=NAV_SCHEMA, root=root, verify_sources=True)
    outputs = {
        target_json: json_text(navigation),
        Path(state["navigation_markdown"]): render_navigation_markdown(navigation),
    }
    for source_json in sorted((run_dir / "source-fragments").glob("*.json")):
        fragment = read_json(source_json)
        validate_json_schema(fragment, SOURCE_FRAGMENT_SCHEMA)
        outputs[source_target_dir / source_json.name] = json_text(fragment)
        outputs[source_target_dir / source_json.with_suffix(".md").name] = _render_source_fragment(fragment)
    for source in navigation['sources']:
        if any(p['source_id'] == source['source_id'] for p in navigation.get('material_points', [])):
            division = run_dir / 'source-work' / source['source_id'] / 'point-division.json'
            from utils.scripts.learning_content import fragments, validate_division
            value = read_json(division)
            expected_points = validate_division(source, {'fragments': fragments(root / source['path'], source['source_id'], source['sha256'])}, value)
            fields = ('point_id', 'fragment_ids', 'summary', 'track', 'reason')
            expected_points = [{k: p[k] for k in fields} for p in expected_points]
            actual_points = [{k: p[k] for k in fields} for p in navigation['material_points'] if p['source_id'] == source['source_id']]
            if actual_points != expected_points: raise CurriculumNavigationError('正文划分记录与批准导航不一致')
            outputs[source_target_dir / source['source_id'] / 'point-division.json'] = json_text(value)
    write_text_transaction(outputs)
    state.update({
        "status": "completed",
        "current_stage": "completed",
        "completed_steps": [*state["completed_steps"], "approved", "committed", "verified"],
        "pending_decisions": [],
        "confirmed_by": confirmed_by,
    })
    _save_state(run_dir, state)
    return {"status": "completed", "run_id": state["run_id"], "navigation_json": str(target_json), "navigation_markdown": state["navigation_markdown"], "source_navigation_dir": str(source_target_dir)}


def resume_run(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    state = _state(run_dir)
    if state["status"] in {"paused_source_conflict"} or (state["status"] == "paused_error" and state.get("resume_stage") == "prepare"):
        return prepare(Path(state["project_root"]), Path(state["original_request_path"]), existing_run_dir=run_dir)
    if state['status'] == 'paused_error' and state.get('resume_stage') == 'resolve-points':
        state.update(status='point_division_required', current_stage='point_division_required', errors=[])
        _save_state(run_dir, state)
        return resolve_points(run_dir, state['last_source_decisions_id'], Path(state['last_source_decisions_path']))
    if state["status"] == "paused_error" and state.get("resume_stage") == "resolve-source":
        state.update({"status": "source_navigation_fragment_required", "current_stage": "source_navigation_fragment_required", "errors": []})
        _save_state(run_dir, state)
        return resolve_source(run_dir, state["last_source_decisions_id"], Path(state["last_source_decisions_path"]))
    if state["status"] == "paused_error" and state.get("resume_stage") == "resolve":
        restored = state.get("resume_from_status") or "source_navigation_fragments_required"
        if restored not in {"source_navigation_fragments_required", "all_source_navigation_fragments_ready", "semantic_decisions_required"}:
            restored = "source_navigation_fragments_required"
        state.update({"status": restored, "current_stage": restored, "errors": []})
        _save_state(run_dir, state)
        return resolve(run_dir, Path(state["last_decisions_path"]))
    if state["status"] == "paused_error" and state.get("resume_stage") == "resolve-ordering":
        state.update({"status": "ordering_decision_required", "current_stage": "ordering_decision_required", "errors": []})
        _save_state(run_dir, state)
        return resolve_ordering(run_dir, Path(state["last_ordering_path"]))
    if state["status"] == "paused_error" and state.get("resume_stage") == "apply-loop-resolution":
        state.update({"status": "awaiting_loop_resolution", "current_stage": "awaiting_loop_resolution", "errors": []})
        _save_state(run_dir, state)
        return apply_loop_resolution(run_dir, Path(state["last_loop_resolution_path"]))
    if state["status"] == "paused_error" and state.get("resume_stage") == "commit":
        state.update({"status": "awaiting_approval", "current_stage": "awaiting_approval", "errors": []})
        _save_state(run_dir, state)
        return commit(run_dir, confirmed_by=state["confirmed_by"], preview_sha256=state["approved_preview_sha256"])
    return state


def verify(root: Path, navigation_path: Path, material_project: Path | None = None, *, refresh_config: bool = True) -> dict[str, Any]:
    navigation_path = navigation_path.resolve()
    navigation = read_json(navigation_path)
    assessment = navigation.get("planning_profile", {}).get("assessment")
    if assessment and (assessment.get("status") != "completed" or assessment.get("graph_revision") != adaptive_assessment.graph_revision(navigation["units"])):
        raise CurriculumNavigationError("导航诊断与当前知识点图不一致")
    from utils.scripts.learning_material_backup import verify_backup
    paths = verify_backup(root.resolve(), material_project, navigation) if material_project is not None else None
    report = validate_navigation(navigation, schema_path=NAV_SCHEMA, root=root.resolve(), verify_sources=True, material_paths=paths)
    markdown_path = navigation_path.with_suffix(".md")
    if not markdown_path.is_file() or markdown_path.read_text(encoding="utf-8") != render_navigation_markdown(navigation):
        raise CurriculumNavigationError("Markdown 导航不是当前 JSON 的确定性视图")
    source_dir = navigation_path.with_suffix(".sources")
    expected_sources = {item["source_id"]: item for item in navigation["sources"]}
    if not source_dir.is_dir():
        raise CurriculumNavigationError("缺少单资料导航片段目录")
    for source_id, source in expected_sources.items():
        fragment_path = source_dir / f"{source_id}.json"
        fragment_md = source_dir / f"{source_id}.md"
        if not fragment_path.is_file() or not fragment_md.is_file():
            raise CurriculumNavigationError(f"单资料导航片段不完整：{source_id}")
        fragment = read_json(fragment_path)
        validate_json_schema(fragment, SOURCE_FRAGMENT_SCHEMA)
        if fragment["source"]["path"] != source["path"] or fragment["source"]["sha256"] != source["sha256"]:
            raise CurriculumNavigationError(f"单资料导航片段与总导航来源不一致：{source_id}")
        if fragment_md.read_text(encoding="utf-8") != _render_source_fragment(fragment):
            raise CurriculumNavigationError(f"单资料导航 Markdown 不是 JSON 的确定性视图：{source_id}")
    report["source_fragment_count"] = len(expected_sources)
    if navigation.get('material_points') or any(source_dir.glob('*/point-division.json')):
        from utils.scripts.learning_navigation_bundle import load_navigation_bundle
        load_navigation_bundle(root, navigation_path, material_project)
    runs = root.resolve() / 'logs' / SKILL_NAME / 'runs'
    if refresh_config and runs.is_dir():
        for state_path in sorted(runs.glob('*/run-state.json'), reverse=True):
            saved = read_json(state_path)
            if Path(saved.get('navigation_json', '')).resolve() == navigation_path:
                report['config_message'] = freeze_config(SKILL_NAME, state_path.parent)['message']
                acknowledge_config(state_path.parent)
                break
    return report


def retain_candidates(run_dir, ranked):
    path = run_dir / 'candidate-orders.json'
    history_path = run_dir / 'candidate-history.json'
    if path.is_file():
        previous = read_json(path)
        history = read_json(history_path) if history_path.is_file() else []
        if previous != ranked and previous not in history:
            history.append(previous)
            write_json(history_path, history)
    write_json(path, ranked)


def diagnostic_command(run_dir, command, level=None, goal=None, question=None, choice=None, goal_ids=None):
    run_dir = run_dir.resolve()
    state = _state(run_dir)
    if state['status'] != 'ordering_decision_required':
        raise CurriculumNavigationError('诊断必须在知识点依赖图就绪后执行')
    config = freeze_config(SKILL_NAME, run_dir)
    units = read_json(run_dir / 'graph-units.json')
    path = run_dir / 'knowledge-boundary.json'
    limit = config['config']['assessment']['max_multiple_choice_questions']
    if command == 'diagnostic-start':
        data = adaptive_assessment.begin(units, level, goal, limit, goal_ids)
        if path.is_file():
            previous = read_json(path)
            if previous['graph_revision'] == data['graph_revision'] and previous['current_level'] == level and previous['learning_goal'] == goal and set(previous['goal_unit_ids']) == set(data['goal_unit_ids']):
                data = previous
                data['max_questions'] = limit
            else:
                write_json(run_dir / 'assessment-history' / (previous['graph_revision'] + '.json'), previous)
    else:
        data = read_json(path)
        data['max_questions'] = limit
        if command == 'diagnostic-question':
            adaptive_assessment.add_question(data, units, read_json(question))
        elif command == 'diagnostic-answer':
            adaptive_assessment.answer(data, units, None if choice == 'unsure' else int(choice))
        elif data['status'] != 'completed' and not data['pending_question'] and adaptive_assessment.target(data, units) is None:
            data['status'] = 'completed'
            data['stop_reason'] = 'budget_reached'
    write_json(path, data)
    validate_json_schema(data, ROOT/'utils/references/adaptive-learning-assessment-v2.schema.json')
    wanted = adaptive_assessment.target(data, units)
    if data['status'] == 'completed':
        context = {'current_level': data['current_level'], 'learning_goal': data['learning_goal'], 'goal_unit_ids': data['goal_unit_ids'], 'diagnostic_answers': data['answers']}
        ranked = adaptive_assessment.candidates(units, context, config['config'])
        retain_candidates(run_dir, ranked)
        template = read_json(run_dir / 'ordering-decisions.template.json')
        template['learner_context'] = {**context, 'estimated_boundary': '以逐知识点观察为准，未测区域保持未知'}
        template['selected_order'] = ranked['candidates'][0]['unit_ids']
        write_json(run_dir / 'ordering-decisions.template.json', template)
    public = None
    if data['pending_question']:
        q = data['pending_question']
        public = {k: q[k] for k in ('unit_id', 'prompt', 'options')}
        write_text_atomic(run_dir / 'diagnostic-question.md', q['prompt'] + '\n\n' + '\n'.join(f'{chr(65+i)}. {o}' for i,o in enumerate(q['options'])) + '\n')
    return {'status': data['status'], 'next_target': wanted, 'question': public, 'config_message': config['message']}


def parser() -> argparse.ArgumentParser:
    root_default = Path.cwd()
    result = argparse.ArgumentParser(description="构建或增量维护课程学习导航")
    sub = result.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-request")
    init.add_argument("--file", type=Path, required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--root", type=Path, default=root_default)
    prepare_parser.add_argument("--request", type=Path, required=True)
    resolve_parser = sub.add_parser("resolve")
    resolve_parser.add_argument("--run-dir", type=Path, required=True)
    resolve_parser.add_argument("--decisions", type=Path, required=True)
    source_parser = sub.add_parser("resolve-source")
    source_parser.add_argument("--run-dir", type=Path, required=True)
    source_parser.add_argument("--source-id", required=True)
    source_parser.add_argument("--decisions", type=Path, required=True)
    points_parser = sub.add_parser('resolve-points')
    points_parser.add_argument('--run-dir', type=Path, required=True)
    points_parser.add_argument('--source-id', required=True)
    points_parser.add_argument('--decisions', type=Path, required=True)
    ordering_parser = sub.add_parser("resolve-ordering")
    ordering_parser.add_argument("--run-dir", type=Path, required=True)
    ordering_parser.add_argument("--ordering", type=Path, required=True)
    loop_parser = sub.add_parser("apply-loop-resolution")
    loop_parser.add_argument("--run-dir", type=Path, required=True)
    loop_parser.add_argument("--resolution", type=Path, required=True)
    commit_parser = sub.add_parser("commit")
    commit_parser.add_argument("--run-dir", type=Path, required=True)
    commit_parser.add_argument("--confirmed-by", required=True)
    commit_parser.add_argument("--preview-sha256", required=True)
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--run-dir", type=Path, required=True)
    resume_parser = sub.add_parser("resume")
    resume_parser.add_argument("--run-dir", type=Path, required=True)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--root", type=Path, default=root_default)
    verify_parser.add_argument("--navigation", type=Path, required=True)
    verify_parser.add_argument("--material-project", type=Path, help="读取项目内学习材料副本进行来源校验")
    for name in ('diagnostic-start', 'diagnostic-question', 'diagnostic-answer', 'diagnostic-status'):
        item = sub.add_parser(name)
        item.add_argument('--run-dir', type=Path, required=True)
        if name == 'diagnostic-start':
            item.add_argument('--level', required=True)
            item.add_argument('--goal', required=True)
            item.add_argument('--goal-ids', nargs='*')
        elif name == 'diagnostic-question': item.add_argument('--question', type=Path, required=True)
        elif name == 'diagnostic-answer': item.add_argument('--choice', required=True, choices=['0','1','2','3','4','unsure'])
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if getattr(args, 'run_dir', None):
            config_receipt = freeze_config(SKILL_NAME, args.run_dir)
        if args.command.startswith('diagnostic-'):
            receipt = diagnostic_command(args.run_dir, args.command, getattr(args, 'level', None), getattr(args, 'goal', None), getattr(args, 'question', None), getattr(args, 'choice', None), getattr(args, 'goal_ids', None))
        elif args.command == "init-request":
            receipt = init_request(args.file)
        elif args.command == "prepare":
            receipt = prepare(args.root, args.request, require_body_points=True)
        elif args.command == "resolve":
            receipt = resolve(args.run_dir, args.decisions)
        elif args.command == 'resolve-points':
            receipt = resolve_points(args.run_dir, args.source_id, args.decisions)
        elif args.command == "resolve-source":
            receipt = resolve_source(args.run_dir, args.source_id, args.decisions)
        elif args.command == "resolve-ordering":
            receipt = resolve_ordering(args.run_dir, args.ordering)
        elif args.command == "apply-loop-resolution":
            receipt = apply_loop_resolution(args.run_dir, args.resolution)
        elif args.command == "commit":
            receipt = commit(args.run_dir, confirmed_by=args.confirmed_by, preview_sha256=args.preview_sha256)
        elif args.command == "status":
            receipt = _state(args.run_dir.resolve())
        elif args.command == "resume":
            receipt = resume_run(args.run_dir)
        else:
            receipt = verify(args.root, args.navigation, args.material_project)
        if getattr(args, "run_dir", None): receipt["config_message"] = config_receipt["message"]
        print(json.dumps(receipt, ensure_ascii=False))
        if getattr(args, 'run_dir', None): acknowledge_config(args.run_dir)
        return 0
    except Exception as exc:
        run_dir_value = getattr(args, "run_dir", None)
        if run_dir_value:
            candidate = Path(run_dir_value).resolve()
            state_path = candidate / "run-state.json"
            if state_path.is_file():
                try:
                    state = read_json(state_path)
                    previous_status = state.get("status")
                    retry = args.command == 'resume' and previous_status == 'paused_error'
                    state.update({"status": "paused_error", "current_stage": "paused_error", "resume_stage": state.get('resume_stage') if retry else args.command, "resume_from_status": state.get('resume_from_status') if retry else previous_status, "errors": [str(exc)]})
                    if args.command == "resolve":
                        state["last_decisions_path"] = str(args.decisions.resolve())
                    if args.command in ("resolve-source", "resolve-points"):
                        state["last_source_decisions_path"] = str(args.decisions.resolve())
                        state["last_source_decisions_id"] = args.source_id
                    if args.command == "resolve-ordering":
                        state["last_ordering_path"] = str(args.ordering.resolve())
                    if args.command == "apply-loop-resolution":
                        state["last_loop_resolution_path"] = str(args.resolution.resolve())
                    if args.command == "commit":
                        state["confirmed_by"] = args.confirmed_by
                        state["approved_preview_sha256"] = args.preview_sha256
                    _save_state(candidate, state)
                except Exception:
                    pass
        print(json.dumps(structured_error_receipt(exc), ensure_ascii=False))
        if args.command == "verify":
            return 4
        if isinstance(exc, (ImportError, ModuleNotFoundError)):
            return 6
        if isinstance(exc, OSError):
            return 5
        if isinstance(exc, ValueError):
            return 2
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
