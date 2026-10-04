from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from navigation_test_support import diagnose, finish_ordering

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/build-curriculum-navigation/scripts/build_curriculum_navigation.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("global_graph_runner", SCRIPT)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

from utils.scripts.learning_navigation import stable_id
from utils.scripts.knowledge_boundary import begin, target, add_question, answer
from utils.scripts.structured_io import read_json, write_json


def _diagnostic_context(run_dir: Path) -> dict:
    return diagnose(runner, run_dir)


def _request(root: Path) -> Path:
    path = root / "request.json"
    write_json(path, {
        "schema_version": "2.0", "input_paths": ["sources"],
        "navigation_json": "navigation.json", "navigation_title": "总课程",
        "student_profile": None, "preview_chars": 100, "max_heading_depth": 6,
        "max_candidates_per_batch": 30, "max_preview_chars_per_batch": 12000,
        "max_total_preview_chars": 120000,
    })
    return path


def _filled(run_dir: Path) -> dict:
    decisions = read_json(run_dir / "semantic-decisions.template.json")
    for item in decisions["items"]:
        item.update({
            "include": True, "stage": "基础", "module": "主线", "importance": "required",
            "difficulty": "beginner", "purpose": f"理解{item['title']}",
            "learning_objectives": [f"能够说明{item['title']}"],
            "concept_roles": [{"concept": item["title"], "role": "introduce"}],
        })
    decisions["student_analysis"]["student_change_reviewed"] = True
    decisions["course_overview"] = {"purpose": "形成完整认识", "core_questions": ["先学什么"], "completion_criteria": ["能够应用"]}
    decisions["stage_overviews"] = [{"title": "基础", "purpose": "建立基础", "core_questions": ["基础是什么"], "completion_criteria": ["能够解释"]}]
    decisions["module_overviews"] = [{"stage": "基础", "title": "主线", "purpose": "按顺序学习", "core_questions": ["如何衔接"], "completion_criteria": ["能够实践"]}]
    return decisions


def _prepare(root: Path, contents: dict[str, str]) -> tuple[dict, Path, dict]:
    source_dir = root / "sources"
    source_dir.mkdir()
    for name, content in contents.items():
        (source_dir / name).write_text(content, encoding="utf-8", newline="\n")
    prepared = runner.prepare(root, _request(root))
    run_dir = Path(prepared["run_dir"])
    return prepared, run_dir, _filled(run_dir)


def test_all_sources_become_fragments_before_one_global_graph(tmp_path: Path):
    _, run_dir, decisions = _prepare(tmp_path, {"a.md": "# 基础 A\n正文", "b.md": "# 应用 B\n正文"})
    first, second = decisions["items"]
    decisions["relationship_records"] = [{
        "predecessor_ref": first["candidate_id"], "successor_ref": second["candidate_id"],
        "reason": "应用 B 直接使用了基础 A。", "evidence_refs": ["b.md#应用 B"],
        "confidence": "high", "source_ids": [],
    }]
    path = tmp_path / "decisions.json"
    write_json(path, decisions)
    with pytest.raises(runner.CurriculumNavigationError, match="必须先完成当前资料导航"):
        runner.resolve(run_dir, path)
    while read_json(run_dir / "run-state.json")["status"] == "source_navigation_fragment_required":
        state = read_json(run_dir / "run-state.json")
        source_id = state["current_source_id"]
        source_decisions = read_json(run_dir / "source-work" / source_id / "source-decisions.template.json")
        source_decisions["covered_candidate_ids"] = [item["candidate_id"] for item in source_decisions["items"]]
        global_items = {item["candidate_id"]: item for item in decisions["items"]}
        source_decisions["items"] = [global_items[item["candidate_id"]] for item in source_decisions["items"]]
        source_decisions["relationship_records"] = decisions["relationship_records"]
        source_path = tmp_path / f"{source_id}.json"
        write_json(source_path, source_decisions)
        assert runner.main(["resolve-source", "--run-dir", str(run_dir), "--source-id", source_id, "--decisions", str(source_path)]) == 0
    resolved = finish_ordering(runner, run_dir, runner.resolve(run_dir, path))
    assert resolved["status"] == "awaiting_approval"
    fragments = read_json(run_dir / "source-fragment-index.json")
    assert len(fragments) == 2
    assert all(Path(item["json"]).is_file() and Path(item["markdown"]).is_file() for item in fragments)
    assert read_json(run_dir / "global-graph-analysis.json")["edge_count"] == 1


def test_source_stage_requires_complete_reading_manifest(tmp_path: Path):
    _, run_dir, decisions = _prepare(tmp_path, {"a.md": "# A1\n正文\n# A2\n正文", "b.md": "# B\n正文"})
    state = read_json(run_dir / "run-state.json")
    source_id = state["current_source_id"]
    source_decisions = read_json(run_dir / "source-work" / source_id / "source-decisions.template.json")
    global_items = {item["candidate_id"]: item for item in decisions["items"]}
    source_decisions["items"] = [global_items[item["candidate_id"]] for item in source_decisions["items"]]
    source_decisions["covered_candidate_ids"] = [source_decisions["items"][0]["candidate_id"]]
    path = tmp_path / "incomplete-source.json"
    write_json(path, source_decisions)
    with pytest.raises(runner.CurriculumNavigationError, match="全部章节"):
        runner.resolve_source(run_dir, source_id, path)


def test_agent_selects_one_valid_order_when_multiple_orders_exist(tmp_path: Path):
    _, run_dir, decisions = _prepare(tmp_path, {"book.md": "# A\n正文\n# B\n正文\n# C\n正文"})
    a, b, c = decisions["items"]
    decisions["relationship_records"] = [
        {"predecessor_ref": a["candidate_id"], "successor_ref": c["candidate_id"], "reason": "C 使用 A。", "evidence_refs": [], "confidence": "high", "source_ids": []},
        {"predecessor_ref": b["candidate_id"], "successor_ref": c["candidate_id"], "reason": "C 使用 B。", "evidence_refs": [], "confidence": "high", "source_ids": []},
    ]
    path = tmp_path / "decisions.json"
    write_json(path, decisions)
    resolved = runner.resolve(run_dir, path)
    assert resolved["status"] == "ordering_decision_required"
    ordering = read_json(run_dir / "ordering-decisions.template.json")
    ordering["selected_order"] = [stable_id("UNIT", b["candidate_id"]), stable_id("UNIT", a["candidate_id"]), stable_id("UNIT", c["candidate_id"])]
    ordering["rationale"] = "先从更贴近用户经验的 B 开始，再补 A，最后进入综合应用 C。"
    ordering["learner_context"] = _diagnostic_context(run_dir)
    ordering_path = tmp_path / "ordering.json"
    bad = {**ordering, "learner_context": {**ordering["learner_context"],
           "diagnostic_answers": [{**ordering["learner_context"]["diagnostic_answers"][0], "unit_id": stable_id("UNIT", c["candidate_id"])}]}}
    write_json(ordering_path, bad)
    with pytest.raises(runner.CurriculumNavigationError, match="诊断记录"):
        runner.resolve_ordering(run_dir, ordering_path)
    write_json(ordering_path, ordering)
    assert runner.main(["resolve-ordering", "--run-dir", str(run_dir), "--ordering", str(ordering_path)]) == 0
    assert read_json(run_dir / "run-state.json")["status"] == "awaiting_approval"
    assert [item["title"] for item in read_json(run_dir / "navigation-preview.json")["units"]] == ["B", "A", "C"]
    assert read_json(run_dir / "navigation-preview.json")["planning_profile"]["route_context"] == ordering["learner_context"]
    assert read_json(run_dir / "knowledge-boundary.json")["status"] == "completed"


def test_blocked_learning_order_uses_plain_report_and_can_be_resolved(tmp_path: Path):
    _, run_dir, decisions = _prepare(tmp_path, {"book.md": "# A\n正文\n# B\n正文"})
    a, b = decisions["items"]
    decisions["relationship_records"] = [
        {"predecessor_ref": a["candidate_id"], "successor_ref": b["candidate_id"], "reason": "B 使用 A。", "evidence_refs": [], "confidence": "high", "source_ids": []},
        {"predecessor_ref": b["candidate_id"], "successor_ref": a["candidate_id"], "reason": "A 又要求先理解 B。", "evidence_refs": [], "confidence": "low", "source_ids": []},
    ]
    path = tmp_path / "decisions.json"
    write_json(path, decisions)
    resolved = runner.resolve(run_dir, path)
    assert resolved["status"] == "awaiting_loop_resolution"
    report = (run_dir / "learning-order-blocked.md").read_text(encoding="utf-8")
    assert "学习顺序互相卡住" in report
    assert "优先复核" in report
    assert "强连通分量" not in report and "入度" not in report
    units = read_json(run_dir / "graph-units.json")
    a_id, b_id = stable_id("UNIT", a["candidate_id"]), stable_id("UNIT", b["candidate_id"])
    resolution = {
        "schema_version": "2.0", "graph_revision": read_json(run_dir / "run-state.json")["graph_revision"],
        "confirmed_by": "pytest",
        "actions": [{"action": "remove_requirement", "predecessor_id": b_id, "successor_id": a_id, "reason": "这条关系证据较弱。"}],
    }
    resolution_path = tmp_path / "resolution.json"
    write_json(resolution_path, resolution)
    assert runner.main(["apply-loop-resolution", "--run-dir", str(run_dir), "--resolution", str(resolution_path)]) == 0
    assert read_json(run_dir / "run-state.json")["status"] == "ordering_decision_required"
    finish_ordering(runner, run_dir, {"status":"ordering_decision_required"})
    assert len(units) == 2
