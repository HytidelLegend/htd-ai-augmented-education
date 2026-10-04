from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest
from navigation_test_support import finish_ordering

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/build-curriculum-navigation/scripts/build_curriculum_navigation.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("build_curriculum_navigation", SCRIPT)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

from utils.scripts.structured_io import read_json, write_json


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def filled_decisions(template: dict, *, prerequisite: str | None = None) -> dict:
    for index, item in enumerate(template["items"]):
        item.update({
            "include": True,
            "stage": "基础",
            "module": "入门",
            "importance": "required",
            "difficulty": "beginner",
            "purpose": f"掌握 {item['title']}",
            "learning_objectives": [f"能够解释 {item['title']}"],
            "concept_roles": [{"concept": item["title"], "role": "introduce"}],
        })
        if index == 1 and prerequisite:
            item["prerequisites"] = [prerequisite]
    template["student_analysis"].update({
        "inferred_level": "beginner", "application_gaps": ["缺少应用经验"],
        "curriculum_impacts": ["先基础后应用"], "student_change_reviewed": True,
    })
    template["course_overview"] = {"purpose": "建立完整框架", "core_questions": ["如何学习"], "completion_criteria": ["能够应用"]}
    template["stage_overviews"] = [{"title": "基础", "purpose": "掌握基础", "core_questions": ["基础是什么"], "completion_criteria": ["能够解释"]}]
    template["module_overviews"] = [{"stage": "基础", "title": "入门", "purpose": "完成入门", "core_questions": ["如何入门"], "completion_criteria": ["能够实践"]}]
    return template


def test_all_public_interfaces_end_to_end_and_sources_immutable(tmp_path: Path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    source = source_dir / "book.md"
    source.write_text("# 课程基础\n\n基础内容。\n\n## 用户需求\n\n需求内容。\n", encoding="utf-8", newline="\n")
    source_hash = digest(source)
    request = tmp_path / "request.json"
    write_json(request, {
        "schema_version": "2.0",
        "input_paths": ["sources"],
        "navigation_json": "output/navigation.json",
        "navigation_title": "测试课程",
        "student_profile": None,
        "preview_chars": 40,
        "max_heading_depth": 6,
        "max_candidates_per_batch": 30,
        "max_preview_chars_per_batch": 12000,
        "max_total_preview_chars": 120000,
    })
    prepared = runner.prepare(tmp_path, request)
    run_dir = Path(prepared["run_dir"])
    assert runner.main(["status", "--run-dir", str(run_dir)]) == 0
    assert runner.main(["resume", "--run-dir", str(run_dir)]) == 0
    template = read_json(run_dir / "semantic-decisions.template.json")
    first_candidate = template["items"][0]["candidate_id"]
    decisions = tmp_path / "decisions.json"
    write_json(decisions, filled_decisions(template, prerequisite=first_candidate))
    resolved = finish_ordering(runner, run_dir, runner.resolve(run_dir, decisions))
    assert resolved["status"] == "awaiting_approval"
    completed = runner.commit(run_dir, confirmed_by="pytest", preview_sha256=resolved["preview_sha256"])
    navigation = Path(completed["navigation_json"])
    assert runner.verify(tmp_path, navigation)["unit_count"] == 2
    assert runner.main(["verify", "--root", str(tmp_path), "--navigation", str(navigation)]) == 0
    assert digest(source) == source_hash


def test_cli_exposes_all_commands():
    command_parser = runner.parser()
    for command in ("init-request", "prepare", "resolve-source", "resolve", "resolve-ordering", "apply-loop-resolution", "commit", "status", "resume", "verify"):
        assert command in command_parser._subparsers._group_actions[0].choices


def test_default_output_and_public_cli(tmp_path: Path):
    (tmp_path / "source.md").write_text("# 基础概念\n说明。\n", encoding="utf-8")
    request = tmp_path / "request.json"
    write_json(request, {
        "schema_version": "2.0", "input_paths": ["source.md"],
        "navigation_title": "入门导航", "student_profile": None,
        "preview_chars": 40, "max_heading_depth": 6,
        "max_candidates_per_batch": 30, "max_preview_chars_per_batch": 12000,
        "max_total_preview_chars": 120000,
    })
    cli = ROOT / "skills/build-curriculum-navigation/scripts/cli.py"
    process = subprocess.run(
        [sys.executable, str(cli), "prepare", "--root", str(tmp_path), "--request", str(request)],
        capture_output=True, text=True, check=True,
    )
    prepared = json.loads(process.stdout)
    run_dir = Path(prepared["run_dir"])
    assert run_dir == tmp_path / "logs/build-curriculum-navigation/runs" / prepared["run_id"]
    assert prepared['status'] == 'point_division_required'
    sid = read_json(run_dir/'source-inventory.json')[0]['source_id']
    directory = run_dir/'source-work'/sid
    fragments = read_json(directory/'body-fragments.json')['fragments']
    division = read_json(directory/'point-division.template.json')
    division['ignored_fragments'] = [{'fragment_id': fragments[0]['fragment_id'], 'reason': '标题，实质内容在正文。'}]
    division['points'] = [{'summary': '基础概念说明', 'fragment_ids': [f['fragment_id'] for f in fragments[1:]],
                           'track': 'main', 'reason': '课程基础正文'}]
    division_path = run_dir/'test-division.json'; write_json(division_path, division)
    runner.resolve_points(run_dir, sid, division_path)
    decisions = tmp_path / "decisions.json"
    global_decisions = filled_decisions(read_json(run_dir / "semantic-decisions.template.json"))
    source_decisions = read_json(directory/'source-decisions.template.json')
    source_decisions.update(covered_candidate_ids=[x['candidate_id'] for x in global_decisions['items']], items=global_decisions['items'])
    source_path = run_dir/'test-source-decision.json'; write_json(source_path, source_decisions)
    runner.resolve_source(run_dir, sid, source_path)
    write_json(decisions, global_decisions)
    resolved = finish_ordering(runner, run_dir, runner.resolve(run_dir, decisions))
    completed = runner.commit(run_dir, confirmed_by="pytest", preview_sha256=resolved["preview_sha256"])
    navigation = Path(completed["navigation_json"])
    assert navigation == tmp_path / "outputs/build-curriculum-navigation/runs" / prepared["run_id"] / "navigation.json"
    assert runner.verify(tmp_path, navigation)["unit_count"] == 1


def test_prepare_resume_keeps_run_id(tmp_path: Path):
    request = tmp_path / "request.json"
    write_json(request, {
        "schema_version": "2.0", "input_paths": ["source.md"],
        "navigation_title": "入门导航", "student_profile": None,
        "preview_chars": 40, "max_heading_depth": 6,
        "max_candidates_per_batch": 30, "max_preview_chars_per_batch": 12000,
        "max_total_preview_chars": 120000,
    })
    first = runner.prepare(tmp_path, request)
    assert first["status"] == "paused_error"
    (tmp_path / "source.md").write_text("# 基础概念\n说明。\n", encoding="utf-8")
    resumed = runner.resume_run(Path(first["run_dir"]))
    assert resumed["run_id"] == first["run_id"]
    assert resumed["run_dir"] == first["run_dir"]


def test_commit_rejects_concurrent_view_or_fragment_changes(tmp_path: Path):
    (tmp_path / "source.md").write_text("# 基础概念\n说明。\n", encoding="utf-8")
    request = tmp_path / "request.json"
    write_json(request, {
        "schema_version": "2.0", "input_paths": ["source.md"],
        "navigation_title": "入门导航", "student_profile": None,
        "preview_chars": 40, "max_heading_depth": 6,
        "max_candidates_per_batch": 30, "max_preview_chars_per_batch": 12000,
        "max_total_preview_chars": 120000,
    })
    prepared = runner.prepare(tmp_path, request)
    run_dir = Path(prepared["run_dir"])
    decisions = tmp_path / "decisions.json"
    write_json(decisions, filled_decisions(read_json(run_dir / "semantic-decisions.template.json")))
    resolved = finish_ordering(runner, run_dir, runner.resolve(run_dir, decisions))
    target = tmp_path / "outputs/build-curriculum-navigation/runs" / prepared["run_id"] / "navigation.json"
    target.parent.mkdir(parents=True)
    markdown = target.with_suffix(".md")
    markdown.write_text("concurrent edit", encoding="utf-8")
    with pytest.raises(runner.CurriculumNavigationError, match="Markdown"):
        runner.commit(run_dir, confirmed_by="pytest", preview_sha256=resolved["preview_sha256"])
    markdown.unlink()
    fragment_dir = target.with_suffix(".sources")
    fragment_dir.mkdir()
    (fragment_dir / "other.md").write_text("concurrent edit", encoding="utf-8")
    with pytest.raises(runner.CurriculumNavigationError, match="来源片段"):
        runner.commit(run_dir, confirmed_by="pytest", preview_sha256=resolved["preview_sha256"])
