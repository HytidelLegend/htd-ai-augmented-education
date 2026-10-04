from __future__ import annotations

import importlib.util
import sys
from navigation_test_support import finish_ordering
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/build-curriculum-navigation/scripts/build_curriculum_navigation.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("incremental_runner", SCRIPT)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

from utils.scripts.structured_io import read_json, write_json
from utils.scripts.knowledge_boundary import begin, target, add_question, answer


def _diagnostic_context(run_dir: Path) -> dict:
    units = {item["unit_id"]: item for item in read_json(run_dir / "graph-units.json")}
    ordered = [units[unit_id] for unit_id in read_json(run_dir / "global-graph-analysis.json")["recommended_order"]
               if units[unit_id]["importance"] not in {"deferred", "not_recommended_now"}]
    assessment = begin(ordered, "初学者", "掌握新增内容")
    records = []
    while wanted := target(assessment, ordered):
        record = {"unit_id": wanted["unit_id"], "prompt": "哪项符合资料？",
                  "options": ["甲", "乙", "丙", "丁"], "correct_index": 0,
                  "choice": None, "correct": False}
        add_question(assessment, ordered, record)
        answer(assessment, ordered, None)
        records.append(record)
    return {"current_level": "初学者", "learning_goal": "掌握新增内容",
            "diagnostic_answers": records, "estimated_boundary": "基础单元附近"}


def _request(root: Path) -> Path:
    path = root / "request.json"
    write_json(path, {
        "schema_version": "2.0", "input_paths": ["sources"],
        "navigation_json": "navigation.json", "navigation_title": "增量课程",
        "student_profile": None, "preview_chars": 20, "max_heading_depth": 6,
        "max_candidates_per_batch": 30, "max_preview_chars_per_batch": 12000,
        "max_total_preview_chars": 120000,
    })
    return path


def _complete(root: Path, request: Path, prerequisite: str | None = None) -> dict:
    prepared = runner.prepare(root, request)
    run_dir = Path(prepared["run_dir"])
    decisions = read_json(run_dir / "semantic-decisions.template.json")
    for item in decisions["items"]:
        item.update({
            "include": True, "stage": "阶段", "module": "模块", "importance": "required",
            "difficulty": "beginner", "purpose": "学习", "learning_objectives": ["会应用"],
            "concept_roles": [{"concept": item["title"], "role": "introduce"}],
            "prerequisites": [prerequisite] if prerequisite else [],
        })
    decisions["student_analysis"]["student_change_reviewed"] = True
    decisions["course_overview"] = {"purpose": "建立框架", "core_questions": ["学什么"], "completion_criteria": ["会应用"]}
    decisions["stage_overviews"] = [{"title": "阶段", "purpose": "阶段导览", "core_questions": ["为什么"], "completion_criteria": ["完成"]}]
    decisions["module_overviews"] = [{"stage": "阶段", "title": "模块", "purpose": "模块导览", "core_questions": ["怎么做"], "completion_criteria": ["会做"]}]
    decision_path = root / f"{prepared['run_id']}-decisions.json"
    write_json(decision_path, decisions)
    resolved = finish_ordering(runner, run_dir, runner.resolve(run_dir, decision_path))
    if resolved["status"] == "ordering_decision_required":
        ordering_path = root / f"{prepared['run_id']}-ordering.json"
        ordering = read_json(run_dir / "ordering-decisions.template.json")
        ordering["learner_context"] = _diagnostic_context(run_dir)
        write_json(ordering_path, ordering)
        resolved = runner.resolve_ordering(run_dir, ordering_path)
    runner.commit(run_dir, confirmed_by="pytest", preview_sha256=resolved["preview_sha256"])
    return read_json(root / "navigation.json")


def test_new_markdown_is_merged_and_retains_existing_unit_id(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "one.md").write_text("# 原有章节\n\n正文", encoding="utf-8")
    request = _request(tmp_path)
    first = _complete(tmp_path, request)
    original_id = first["units"][0]["unit_id"]
    (sources / "two.md").write_text("# 新章节\n\n正文", encoding="utf-8")
    second = _complete(tmp_path, request, prerequisite=original_id)
    assert len(second["units"]) == 2
    assert second["units"][0]["unit_id"] == original_id
    assert second["units"][1]["prerequisites"] == [original_id]


def test_changed_source_is_reprocessed_instead_of_reusing_stale_units(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "book.md"
    source.write_text("# 原有章节\n\n正文", encoding="utf-8")
    request = _request(tmp_path)
    first = _complete(tmp_path, request)
    original_id = first["units"][0]["unit_id"]
    source.write_text("# 原有章节\n\n正文已更新\n\n# 新增章节\n\n新增正文", encoding="utf-8")
    second = _complete(tmp_path, request)
    assert {item["title"] for item in second["units"]} == {"原有章节", "新增章节"}
    assert next(item["unit_id"] for item in second["units"] if item["title"] == "原有章节") == original_id
