"""Build, validate and render machine-readable curriculum navigation."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from utils.scripts.dependency_graph import validate_selected_order
from utils.scripts.markdown_structure import extract_markdown_structure
from utils.scripts.structured_io import validate_json_schema
from utils.scripts.timestamp import iso_timestamp


class LearningNavigationError(ValueError):
    pass


def stable_id(prefix: str, value: str) -> str:
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def _topological_order(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {item["unit_id"]: item for item in units}
    if len(by_id) != len(units):
        raise LearningNavigationError("阅读单元 ID 重复")
    indegree = {unit_id: 0 for unit_id in by_id}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for item in units:
        for dependency in item.get("prerequisites", []):
            if dependency not in by_id:
                raise LearningNavigationError(f"先修单元不存在：{dependency}")
            outgoing[dependency].append(item["unit_id"])
            indegree[item["unit_id"]] += 1
    base_order = {item["unit_id"]: index for index, item in enumerate(units)}
    ready = sorted((key for key, value in indegree.items() if value == 0), key=base_order.get)
    ordered: list[dict[str, Any]] = []
    while ready:
        current = ready.pop(0)
        ordered.append(by_id[current])
        for child in sorted(outgoing[current], key=base_order.get):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort(key=base_order.get)
    if len(ordered) != len(units):
        cyclic = sorted(key for key, value in indegree.items() if value > 0)
        raise LearningNavigationError(f"课程依赖存在循环：{', '.join(cyclic)}")
    for index, item in enumerate(ordered, start=1):
        item["sequence"] = index
    return ordered


def _resolve_refs(raw: list[str], aliases: dict[str, str], valid_ids: set[str]) -> list[str]:
    result: list[str] = []
    for value in raw:
        resolved = aliases.get(value, value)
        if resolved not in valid_ids:
            raise LearningNavigationError(f"未知先修引用：{value}")
        if resolved not in result:
            result.append(resolved)
    return result


def _default_overview() -> dict[str, Any]:
    return {"purpose": "", "core_questions": [], "completion_criteria": []}


def _visual_references(source: dict[str, Any], uses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    images = {item["figure_id"]: item for item in source.get("images", [])}
    result: list[dict[str, Any]] = []
    for use in uses:
        image = images.get(use["figure_id"])
        if image is None:
            raise LearningNavigationError(f"图片引用不存在：{use['figure_id']}")
        result.append({
            **image,
            "source_id": source["source_id"],
            "source_path": source["path"],
            "purpose": use["purpose"],
            "placement_hint": use["placement_hint"],
            "locator_status": "valid",
        })
    return result


def prepare_navigation_units(
    *, inventory: list[dict[str, Any]], decisions: dict[str, Any], existing: dict[str, Any] | None,
    prerequisite_overrides: dict[str, list[str]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Build unresolved navigation units before global graph ordering."""
    existing = existing or {}
    inventory_by_id = {item["source_id"]: item for item in inventory}
    existing_sources = {item["source_id"]: item for item in existing.get("sources", [])}
    changed_source_ids = {
        source_id for source_id, source in inventory_by_id.items()
        if source_id in existing_sources and existing_sources[source_id].get("sha256") != source.get("sha256")
    }
    retained_units = [
        {**item, "source": dict(item["source"]) if item.get("source") else None}
        for item in existing.get("units", [])
        if item.get("source") is None or (
            item["source"].get("source_id") in inventory_by_id
            and item["source"].get("source_id") not in changed_source_ids
        )
    ]
    for unit in retained_units:
        if unit["source"]:
            source = inventory_by_id[unit["source"]["source_id"]]
            unit["source"]["file"] = source["path"]
            unit["source"]["source_sha256"] = source["sha256"]

    candidates = {heading["candidate_id"]: (source, heading) for source in inventory for heading in source["headings"]}
    selected = [item for item in decisions["items"] if item["include"]]
    aliases = {item["candidate_id"]: stable_id("UNIT", item["candidate_id"]) for item in selected}
    for overview in decisions["overview_units"]:
        unit_id = stable_id("OVERVIEW", f"{overview['stage']}|{overview['module']}|{overview['title']}")
        aliases[overview["title"]] = unit_id
        retained_units.append({
            "unit_id": unit_id, "title": overview["title"], "unit_type": "agent_overview",
            "stage": overview["stage"], "module": overview["module"], "sequence": 0,
            "importance": "required", "difficulty": "beginner", "purpose": overview["purpose"],
            "learning_objectives": overview["learning_objectives"], "prerequisites": overview["prerequisites"],
            "concept_roles": overview["concept_roles"], "content_roles": [], "risk_tags": [],
            "teaching_notes": ["本单元是 Agent 导学，不是原书内容。"], "case_context": None,
            "visual_references": [], "source": None,
        })
    for item in selected:
        if item["candidate_id"] not in candidates:
            raise LearningNavigationError(f"决策引用未知候选章节：{item['candidate_id']}")
        source, heading = candidates[item["candidate_id"]]
        retained_units.append({
            "unit_id": aliases[item["candidate_id"]], "title": item["title"], "unit_type": "source_reading",
            "stage": item["stage"], "module": item["module"], "sequence": 0,
            "importance": item["importance"], "difficulty": item["difficulty"], "purpose": item["purpose"],
            "learning_objectives": item["learning_objectives"], "prerequisites": item["prerequisites"],
            "concept_roles": item["concept_roles"], "content_roles": item["content_roles"],
            "risk_tags": item["risk_tags"], "teaching_notes": item["teaching_notes"],
            "case_context": item["case_context"], "visual_references": _visual_references(source, item["visual_reference_uses"]),
            "source": {
                "source_id": source["source_id"], "file": source["path"], "source_sha256": source["sha256"],
                "heading_text": heading["title"], "heading_level": heading["level"], "heading_occurrence": heading["occurrence"],
                "parent_heading_chain": heading["parent_heading_chain"], "start_line_hint": heading["start_line"],
                "end_before_heading": heading["end_before_heading"], "end_line_hint": heading["end_line"], "locator_status": "valid",
            },
        })

    by_id = {item["unit_id"]: item for item in retained_units}
    for update in decisions["existing_unit_updates"]:
        unit = by_id.get(update["unit_id"])
        if unit is None:
            raise LearningNavigationError(f"要更新的已有单元不存在：{update['unit_id']}")
        for key, value in update.items():
            if key != "unit_id":
                unit[key] = value
    valid_ids = set(by_id)
    for unit in retained_units:
        unit["prerequisites"] = _resolve_refs(unit.get("prerequisites", []), aliases, valid_ids)

    for record in decisions.get("relationship_records", []):
        predecessor = aliases.get(record["predecessor_ref"], record["predecessor_ref"])
        successor = aliases.get(record["successor_ref"], record["successor_ref"])
        if predecessor not in valid_ids or successor not in valid_ids:
            raise LearningNavigationError(
                f"学习关系引用未知要点：{record['predecessor_ref']} -> {record['successor_ref']}"
            )
        if predecessor not in by_id[successor]["prerequisites"]:
            by_id[successor]["prerequisites"].append(predecessor)

    if prerequisite_overrides is not None:
        if set(prerequisite_overrides) != valid_ids:
            raise LearningNavigationError("调整后的先修关系必须完整覆盖全部学习单元")
        for unit_id, prerequisites in prerequisite_overrides.items():
            by_id[unit_id]["prerequisites"] = _resolve_refs(prerequisites, aliases, valid_ids)

    stage_order = {item["title"]: index for index, item in enumerate(decisions["stage_overviews"])}
    module_order = {(item["stage"], item["title"]): index for index, item in enumerate(decisions["module_overviews"])}
    retained_units.sort(key=lambda item: (
        stage_order.get(item["stage"], 10_000), module_order.get((item["stage"], item["module"]), 10_000),
        0 if item["unit_type"] == "agent_overview" else 1, item.get("sequence", 0), item["unit_id"],
    ))
    return retained_units, aliases


def build_navigation(
    *, inventory: list[dict[str, Any]], decisions: dict[str, Any], existing: dict[str, Any] | None,
    student_snapshot: dict[str, Any] | None, ordered_unit_ids: list[str] | None = None,
    prerequisite_overrides: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    now = iso_timestamp()
    existing = existing or {}
    retained_units, _ = prepare_navigation_units(
        inventory=inventory,
        decisions=decisions,
        existing=existing,
        prerequisite_overrides=prerequisite_overrides,
    )
    if ordered_unit_ids is None:
        ordered = _topological_order(retained_units)
    else:
        edges = [
            {"predecessor_id": prerequisite, "successor_id": unit["unit_id"]}
            for unit in retained_units for prerequisite in unit.get("prerequisites", [])
        ]
        node_ids = [unit["unit_id"] for unit in retained_units]
        validate_selected_order(node_ids, edges, ordered_unit_ids)
        by_id = {unit["unit_id"]: unit for unit in retained_units}
        ordered = [by_id[unit_id] for unit_id in ordered_unit_ids]
        for index, unit in enumerate(ordered, start=1):
            unit["sequence"] = index

    stage_overviews = {item["title"]: item for item in decisions["stage_overviews"]}
    module_overviews = {(item["stage"], item["title"]): item for item in decisions["module_overviews"]}
    stage_names: list[str] = []
    modules: dict[str, list[str]] = defaultdict(list)
    for unit in ordered:
        if unit["stage"] not in stage_names:
            stage_names.append(unit["stage"])
        if unit["module"] not in modules[unit["stage"]]:
            modules[unit["stage"]].append(unit["module"])
    stages = []
    for stage_index, name in enumerate(stage_names, start=1):
        stage_data = stage_overviews.get(name, {})
        if name not in stage_overviews:
            raise LearningNavigationError(f"阶段缺少 Overview：{name}")
        missing_modules = [module for module in modules[name] if (name, module) not in module_overviews]
        if missing_modules:
            raise LearningNavigationError(f"模块缺少 Overview：{name}／{', '.join(missing_modules)}")
        stages.append({
            "stage_id": stable_id("STAGE", name), "title": name, "sequence": stage_index,
            "overview": {key: stage_data.get(key, default) for key, default in _default_overview().items()},
            "modules": [{
                "module_id": stable_id("MODULE", f"{name}|{module}"), "title": module, "sequence": module_index,
                "overview": {key: module_overviews.get((name, module), {}).get(key, default) for key, default in _default_overview().items()},
            } for module_index, module in enumerate(modules[name], start=1)],
        })

    concept_index: dict[str, str] = {}
    for unit in ordered:
        for concept_role in unit["concept_roles"]:
            concept, role = concept_role["concept"], concept_role["role"]
            if role == "introduce":
                if concept in concept_index:
                    raise LearningNavigationError(f"核心概念存在多个首次教学单元：{concept}")
                concept_index[concept] = unit["unit_id"]
            elif concept in concept_index:
                intro_sequence = next(item["sequence"] for item in ordered if item["unit_id"] == concept_index[concept])
                if intro_sequence >= unit["sequence"]:
                    raise LearningNavigationError(f"概念在首次教学之前被使用：{concept}")

    metadata = {item["source_id"]: item for item in decisions["source_metadata"]}
    sources = []
    for item in inventory:
        details = metadata.get(item["source_id"], {})
        sources.append({
            "source_id": item["source_id"], "path": item["path"], "sha256": item["sha256"],
            "title": str(item.get("frontmatter", {}).get("title") or Path(item["path"]).stem), "size_bytes": item["size_bytes"],
            "author": details.get("author", ""), "edition": details.get("edition", "unknown"),
            "theme": details.get("theme", ""), "content_tendency": details.get("content_tendency", ["unknown"]),
            "scan_confidence": details.get("scan_confidence", "low"),
        })
    profile_ref = None
    if student_snapshot:
        profile = student_snapshot["profile"]
        profile_ref = {"path": student_snapshot["path"], "sha256": student_snapshot["sha256"], "profile_date": profile["profile_date"]}
    return {
        "schema_version": "2.0", "title": decisions["navigation_title"],
        "created_at": existing.get("created_at", now), "updated_at": now,
        "student_profile_ref": profile_ref, "planning_profile": decisions["student_analysis"],
        "course_overview": decisions["course_overview"], "lesson_generation_policy": decisions["lesson_generation_policy"],
        "sources": sources, "source_relationships": decisions["source_relationships"], "stages": stages,
        "units": ordered, "concept_index": concept_index, "coverage_gaps": decisions["coverage_gaps"],
        "deferred_items": decisions["deferred_items"],
    }


def validate_navigation(navigation: dict[str, Any], *, schema_path: Path, root: Path, verify_sources: bool = True, material_paths: dict[str, Path] | None = None) -> dict[str, Any]:
    validate_json_schema(navigation, schema_path)
    _topological_order([dict(item) for item in navigation["units"]])
    source_map = {item["source_id"]: item for item in navigation["sources"]}
    errors: list[str] = []
    if len(source_map) != len(navigation["sources"]):
        errors.append("来源 ID 重复")
    if navigation["lesson_generation_policy"].get("image_policy") != "source_pointer_only":
        errors.append("讲义图片策略必须是 source_pointer_only")
    structures: dict[str, dict[str, Any]] = {}
    if verify_sources:
        for source_id, source in source_map.items():
            path = material_paths[source['path']] if material_paths is not None else root / source["path"]
            if not path.is_file():
                errors.append(f"来源不存在：{source['path']}")
                continue
            structure = extract_markdown_structure(path, root=root, preview_chars=0, identity_path=source['path'])
            structures[source_id] = structure
            if structure["sha256"] != source["sha256"]:
                errors.append(f"来源哈希已变化：{source['path']}")
    introduced: dict[str, str] = {}
    unit_positions = {item["unit_id"]: item["sequence"] for item in navigation["units"]}
    for unit in navigation["units"]:
        for concept in unit["concept_roles"]:
            if concept["role"] == "introduce":
                if concept["concept"] in introduced:
                    errors.append(f"概念存在多个首次教学单元：{concept['concept']}")
                introduced[concept["concept"]] = unit["unit_id"]
    for unit in navigation["units"]:
        for concept in unit["concept_roles"]:
            intro_id = introduced.get(concept["concept"])
            if concept["role"] != "introduce":
                if intro_id is None:
                    errors.append(f"概念缺少首次教学单元：{concept['concept']}")
                elif unit_positions[intro_id] >= unit["sequence"]:
                    errors.append(f"概念在首次教学之前被使用：{concept['concept']}")
        locator = unit["source"]
        if locator is None:
            if unit["unit_type"] != "agent_overview":
                errors.append(f"非导学单元缺少来源：{unit['unit_id']}")
            continue
        source = source_map.get(locator["source_id"])
        if source is None:
            errors.append(f"阅读单元引用未知来源：{unit['unit_id']}")
            continue
        if locator["file"] != source["path"] or locator["source_sha256"] != source["sha256"]:
            errors.append(f"阅读单元来源定位与来源清单不一致：{unit['unit_id']}")
        structure = structures.get(locator["source_id"])
        if structure:
            matches = [heading for heading in structure["headings"] if heading["title"] == locator["heading_text"] and heading["occurrence"] == locator["heading_occurrence"] and heading["parent_heading_chain"] == locator["parent_heading_chain"]]
            if len(matches) != 1:
                errors.append(f"定位不能唯一解析：{unit['unit_id']}")
            images = {item["figure_id"]: item for item in structure["images"]}
            for visual in unit["visual_references"]:
                if visual['source_id'] != source['source_id'] or visual['source_path'] != source['path']:
                    errors.append(f"图片来源与知识点来源不一致：{visual['figure_id']}")
                image = images.get(visual["figure_id"])
                if image is None or image["reference_hash"] != visual["reference_hash"]:
                    errors.append(f"图片定位失效：{visual['figure_id']}")
                validate_json_schema(visual, schema_path.with_name("learning-navigation-visual-reference-v2.schema.json"))
    for relation in navigation["source_relationships"]:
        if relation.get("source_id") not in source_map or relation.get("related_source_id") not in source_map:
            errors.append("来源版本关系引用未知来源")
    if navigation["concept_index"] != introduced:
        errors.append("概念索引与首次教学单元不一致")
    if errors:
        raise LearningNavigationError("；".join(errors))
    return {"status": "verified", "source_count": len(navigation["sources"]), "stage_count": len(navigation["stages"]), "unit_count": len(navigation["units"]), "visual_reference_count": sum(len(item["visual_references"]) for item in navigation["units"]), "cycle_count": 0}


def render_navigation_markdown(navigation: dict[str, Any]) -> str:
    lines = [f"# {navigation['title']}", "", "> 本文件由结构化导航 JSON 确定性生成，请勿直接编辑。", "", "## 课程导览", "", navigation["course_overview"]["purpose"], ""]
    profile = navigation.get("student_profile_ref")
    if profile:
        lines.extend(["## 学生信息", "", f"- 来源：`{profile['path']}`", f"- 档案日期：{profile['profile_date']}", f"- SHA-256：`{profile['sha256']}`", ""])
    lines.extend(["## 课程总览", ""])
    for stage in navigation["stages"]:
        lines.append(f"- {stage['sequence']}. {stage['title']}：{stage['overview']['purpose']}")
        for module in stage["modules"]:
            lines.append(f"  - {module['sequence']}. {module['title']}：{module['overview']['purpose']}")
    lines.extend(["", "## 学习顺序", ""])
    title_by_id = {u["unit_id"]: u["title"] for u in navigation["units"]}
    for unit in navigation["units"]:
        display_type = "资料知识点" if unit["unit_type"] == "source_reading" else "导学知识点"
        lines.extend([f"### {unit['sequence']}. {unit['title']}", "", f"- 类型：{display_type}", f"- 阶段：{unit['stage']}／{unit['module']}", f"- 重要性：`{unit['importance']}`；难度：`{unit['difficulty']}`", f"- 目的：{unit['purpose']}", f"- 先修：{', '.join(title_by_id[item] for item in unit['prerequisites']) or '无'}"])
        if unit["source"]:
            lines.extend([f"- 来源：`{unit['source']['file']}`", f"- 定位：`{unit['source']['heading_text']}`（第 {unit['source']['heading_occurrence']} 次）", f"- 行号提示：{unit['source']['start_line_hint']}～{unit['source']['end_line_hint']}"])
        for visual in unit["visual_references"]:
            chain = "／".join([*visual["parent_heading_chain"], visual["heading_text"]])
            lines.append(f"- 图片提示：请到 `{visual['source_path']}` 的“{chain}”查看本节第 {visual['image_index_in_section']} 张图；用途：{visual['purpose']}；讲义位置：{visual['placement_hint']}")
        lines.append("")
    lines.extend(["## 概念首次教学索引", "", "| 概念 | 阅读单元 |", "|---|---|"])
    for concept, unit_id in navigation["concept_index"].items():
        lines.append(f"| {concept.replace('|', '&#124;')} | {title_by_id[unit_id].replace('|', '&#124;')} |")
    lines.extend(["", "## 课程缺口", ""])
    lines.extend(f"- {item['topic']}：{item['reason']}" for item in navigation["coverage_gaps"])
    lines.extend(["", "## 延后内容", ""])
    lines.extend(f"- {item['topic']}：{item['reason']}；重新纳入条件：{item['reentry_condition']}" for item in navigation["deferred_items"])
    ordering = navigation.get('planning_profile', {}).get('ordering')
    if ordering:
        from utils.scripts.markdown_report import markdown_table
        titles = {u['unit_id']: u['title'] for u in navigation['units']}
        lines.extend(['', '## 候选学习顺序（按分数非升序）', '', markdown_table(
            ['候选', '分数', '知识点顺序', '推荐理由'],
            [[str(i+1), str(r['score']), ' → '.join(titles[x] for x in r['unit_ids']),
              ('推荐；' if r['candidate_id'] in ordering['recommended_order_ids'] else '') + '；'.join(r['recommendation_reasons'])]
             for i,r in enumerate(ordering['candidates'])])])
    text = "\n".join(lines).rstrip() + "\n"
    return text.replace('阅读单元', '知识点').replace('首次教学单元', '首次讲解知识点')


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"
