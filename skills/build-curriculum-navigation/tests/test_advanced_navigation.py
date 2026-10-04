from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from navigation_test_support import finish_ordering

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/build-curriculum-navigation/scripts/build_curriculum_navigation.py"
DECISION_SCRIPT = ROOT / "skills/build-curriculum-navigation/scripts/prepare_navigation_decisions.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("advanced_navigation_runner", SCRIPT)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

decision_spec = importlib.util.spec_from_file_location("prepare_navigation_decisions", DECISION_SCRIPT)
assert decision_spec and decision_spec.loader
decision_runner = importlib.util.module_from_spec(decision_spec)
decision_spec.loader.exec_module(decision_runner)

from utils.scripts.learning_navigation import render_navigation_markdown
from utils.scripts.markdown_structure import extract_markdown_structure
from utils.scripts.structured_io import read_json, write_json
from utils.scripts.student_learning_profile import render_student_profile
from utils.scripts.timestamp import iso_timestamp


def request(root: Path, *, student: str | None = None, max_candidates: int = 30) -> Path:
    path = root / "request.json"
    write_json(path, {
        "schema_version": "2.0", "input_paths": ["sources"], "navigation_json": "navigation.json",
        "navigation_title": "课程", "student_profile": student, "preview_chars": 80, "max_heading_depth": 6,
        "max_candidates_per_batch": max_candidates, "max_preview_chars_per_batch": 1000,
        "max_total_preview_chars": 10000,
    })
    return path


def fill(template: dict, *, prerequisite_by_candidate: dict[str, list[str]] | None = None) -> dict:
    prerequisite_by_candidate = prerequisite_by_candidate or {}
    for item in template["items"]:
        item.update({
            "include": True, "stage": "基础", "module": "模块", "importance": "required",
            "difficulty": "beginner", "purpose": "学习本节", "learning_objectives": ["能够应用"],
            "prerequisites": prerequisite_by_candidate.get(item["candidate_id"], []),
            "concept_roles": [{"concept": item["title"], "role": "introduce"}],
        })
    template["student_analysis"]["student_change_reviewed"] = True
    template["course_overview"] = {"purpose": "建立框架", "core_questions": ["学什么"], "completion_criteria": ["能够应用"]}
    template["stage_overviews"] = [{"title": "基础", "purpose": "阶段导览", "core_questions": ["为什么"], "completion_criteria": ["完成"]}]
    template["module_overviews"] = [{"stage": "基础", "title": "模块", "purpose": "模块导览", "core_questions": ["怎么做"], "completion_criteria": ["会做"]}]
    return template


def resolve_and_commit(root: Path, prepared: dict, decisions: dict) -> dict:
    run_dir = Path(prepared["run_dir"])
    path = root / f"{prepared['run_id']}-decisions.json"
    write_json(path, decisions)
    resolved = finish_ordering(runner, run_dir, runner.resolve(run_dir, path))
    runner.commit(run_dir, confirmed_by="pytest", preview_sha256=resolved["preview_sha256"])
    return read_json(root / "navigation.json")


def test_images_are_located_without_persisting_remote_urls(tmp_path: Path):
    source = tmp_path / "book.md"
    source.write_text(
        "# 图示章节\n说明\n![远程图](https://invalid.example/a.png)\n![引用图][x]\n"
        "<img src=\"https://invalid.example/b.png\" alt=\"HTML 图\">\n![[local.png|本地图]]\n"
        "```md\n![伪图](https://invalid.example/fake.png)\n```\n[x]: https://invalid.example/c.png\n",
        encoding="utf-8", newline="\n",
    )
    structure = extract_markdown_structure(source, root=tmp_path, preview_chars=0)
    assert len(structure["images"]) == 4
    assert [item["image_index_in_section"] for item in structure["images"]] == [1, 2, 3, 4]
    assert "https://" not in json.dumps(structure["images"], ensure_ascii=False)


def test_visual_reference_renders_as_source_pointer_only(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "book.md").write_text("# 图示\n![图](https://invalid.example/a.png)\n", encoding="utf-8")
    prepared = runner.prepare(tmp_path, request(tmp_path))
    run_dir = Path(prepared["run_dir"])
    template = fill(read_json(run_dir / "semantic-decisions.template.json"))
    figure = read_json(run_dir / "source-inventory.json")[0]["images"][0]
    template["items"][0]["visual_reference_uses"] = [{"figure_id": figure["figure_id"], "purpose": "理解结构", "placement_hint": "概念解释后"}]
    navigation = resolve_and_commit(tmp_path, prepared, template)
    rendered = render_navigation_markdown(navigation)
    assert "查看本节第 1 张图" in rendered
    assert "https://" not in rendered and "![" not in rendered


def test_exact_duplicates_keep_distinct_sources_and_relationship_candidate(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    for name in ("trial.md", "full.md"):
        (sources / name).write_text("# 相同目录\n正文", encoding="utf-8")
    prepared = runner.prepare(tmp_path, request(tmp_path))
    inventory = read_json(Path(prepared["run_dir"]) / "source-inventory.json")
    relations = read_json(Path(prepared["run_dir"]) / "source-relationship-candidates.json")
    assert len({item["source_id"] for item in inventory}) == 2
    assert relations[0]["exact_duplicate"] is True


def test_batches_are_split_by_candidate_limit(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "book.md").write_text("\n".join(f"# 第{i}节\n正文" for i in range(5)), encoding="utf-8")
    prepared = runner.prepare(tmp_path, request(tmp_path, max_candidates=2))
    assert prepared["batch_count"] == 3


def test_new_unit_can_become_prerequisite_of_existing_unit(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "old.md").write_text("# 高级内容\n正文", encoding="utf-8")
    request_path = request(tmp_path)
    first_prepared = runner.prepare(tmp_path, request_path)
    first = resolve_and_commit(tmp_path, first_prepared, fill(read_json(Path(first_prepared["run_dir"]) / "semantic-decisions.template.json")))
    old_id = first["units"][0]["unit_id"]
    (sources / "new.md").write_text("# 基础内容\n正文", encoding="utf-8")
    second_prepared = runner.prepare(tmp_path, request_path)
    decisions = fill(read_json(Path(second_prepared["run_dir"]) / "semantic-decisions.template.json"))
    new_candidate = decisions["items"][0]["candidate_id"]
    decisions["existing_unit_updates"] = [{"unit_id": old_id, "prerequisites": [new_candidate]}]
    second = resolve_and_commit(tmp_path, second_prepared, decisions)
    assert [item["title"] for item in second["units"]] == ["基础内容", "高级内容"]
    assert second["units"][1]["prerequisites"] == [second["units"][0]["unit_id"]]


def test_student_change_requires_explicit_review(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "book.md").write_text("# 课程\n正文", encoding="utf-8")
    profile = tmp_path / "student.md"
    now = iso_timestamp()
    profile.write_text(render_student_profile({
        "profile_type": "student_learning_profile", "schema_version": "2.0",
        "profile_id": "example", "profile_date": now[:10], "created_at": now,
        "updated_at": now, "domain": "通用学习", "title": "学习档案",
        "current_stage": "入门阶段", "primary_goals": ["掌握基础"],
        "secondary_goals": [], "excluded_goals": [], "current_projects": [],
        "experience": [], "constraints": [], "priority_topics": [],
        "deferred_topics": [], "learning_preferences": [],
        "knowledge": {"clear": [], "medium": [], "unclear": []},
    }), encoding="utf-8")
    request_path = request(tmp_path, student="student.md")
    initial = runner.prepare(tmp_path, request_path)
    resolve_and_commit(tmp_path, initial, fill(read_json(Path(initial["run_dir"]) / "semantic-decisions.template.json")))
    profile.write_text(profile.read_text(encoding="utf-8").replace("入门阶段", "进阶阶段", 1), encoding="utf-8")
    changed = runner.prepare(tmp_path, request_path)
    assert read_json(Path(changed["run_dir"]) / "student-profile-diff.json")["changed"] is True
    decisions = read_json(Path(changed["run_dir"]) / "semantic-decisions.template.json")
    assert decisions["student_analysis"]["student_change_reviewed"] is False
    decision_path = tmp_path / "changed.json"
    write_json(decision_path, decisions)
    with pytest.raises(runner.CurriculumNavigationError, match="学生信息已变化"):
        runner.resolve(Path(changed["run_dir"]), decision_path)


def test_batch_collector_and_validator_interfaces(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "book.md").write_text("# 一\n正文\n# 二\n正文\n# 三\n正文", encoding="utf-8")
    prepared = runner.prepare(tmp_path, request(tmp_path, max_candidates=2))
    run_dir = Path(prepared["run_dir"])
    base = fill(read_json(run_dir / "semantic-decisions.template.json"))
    base_path = tmp_path / "base.json"
    write_json(base_path, base)
    for template_path in (run_dir / "batches").glob("*.template.json"):
        batch = read_json(template_path)
        for item in batch["items"]:
            source_item = next(value for value in base["items"] if value["candidate_id"] == item["candidate_id"])
            item.update(source_item)
        write_json(template_path.with_name(template_path.name.replace(".template.json", ".decisions.json")), batch)
    output = tmp_path / "collected.json"
    assert decision_runner.main(["collect", "--base", str(base_path), "--batch-dir", str(run_dir / "batches"), "--output", str(output)]) == 0
    assert decision_runner.main(["validate", "--decisions", str(output)]) == 0
    assert len(read_json(output)["items"]) == 3
    source_id = read_json(run_dir / "source-inventory.json")[0]["source_id"]
    source_output = tmp_path / "source-collected.json"
    assert decision_runner.main([
        "collect-source", "--base", str(run_dir / "source-work" / source_id / "source-decisions.template.json"),
        "--batch-dir", str(run_dir / "batches"), "--output", str(source_output),
    ]) == 0
    assert len(read_json(source_output)["covered_candidate_ids"]) == 3


def test_compact_batch_patches_expand_from_templates(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "book.md").write_text("# 概念一\n正文\n# 概念二\n正文", encoding="utf-8")
    prepared = runner.prepare(tmp_path, request(tmp_path))
    run_dir = Path(prepared["run_dir"])
    base = fill(read_json(run_dir / "semantic-decisions.template.json"))
    for item in base["items"]:
        item["include"] = False
    base_path = tmp_path / "base.json"
    write_json(base_path, base)
    batch_template_path = next((run_dir / "batches").glob("*.template.json"))
    batch_template = read_json(batch_template_path)
    ids = [item["candidate_id"] for item in batch_template["items"]]
    patch = {
        "source_id": batch_template["source_id"],
        "covered_candidate_ids": ids,
        "items": [{"candidate_id": ids[0], "include": True, "difficulty": "advanced"}],
    }
    write_json(batch_template_path.with_name(batch_template_path.name.replace(".template.json", ".decisions.json")), patch)
    output = tmp_path / "collected.json"
    decision_runner.collect_batches(base_path, run_dir / "batches", output, ROOT / "utils/references/learning-navigation-decisions-v2.schema.json")
    result = read_json(output)
    assert len(result["items"]) == 2
    assert result["items"][0]["difficulty"] == "advanced"
    assert result["items"][1]["include"] is False


def test_resume_retries_failed_resolve_with_corrected_decisions(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "book.md").write_text("# 概念\n正文", encoding="utf-8")
    prepared = runner.prepare(tmp_path, request(tmp_path))
    run_dir = Path(prepared["run_dir"])
    decisions = fill(read_json(run_dir / "semantic-decisions.template.json"))
    decisions["items"][0]["concept_roles"] = [{"concept": "概念", "role": "review"}]
    decisions_path = tmp_path / "resume-decisions.json"
    write_json(decisions_path, decisions)
    assert runner.main(["resolve", "--run-dir", str(run_dir), "--decisions", str(decisions_path)]) == 2
    paused = read_json(run_dir / "run-state.json")
    assert paused["status"] == "paused_error" and paused["resume_stage"] == "resolve"
    decisions["items"][0]["concept_roles"] = [{"concept": "概念", "role": "introduce"}]
    write_json(decisions_path, decisions)
    assert runner.main(["resume", "--run-dir", str(run_dir)]) == 0
    assert read_json(run_dir / "run-state.json")["status"] == "ordering_decision_required"
    finish_ordering(runner, run_dir, {"status":"ordering_decision_required"})
