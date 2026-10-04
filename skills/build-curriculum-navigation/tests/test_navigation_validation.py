from __future__ import annotations

import sys
from pathlib import Path

import pytest
from navigation_test_support import finish_ordering

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.scripts.learning_navigation import LearningNavigationError, _topological_order
from utils.scripts.markdown_structure import extract_markdown_structure
from utils.scripts.student_learning_profile import parse_student_profile, render_student_profile
from utils.scripts.timestamp import iso_timestamp
from utils.scripts.structured_io import write_json


def test_duplicate_headings_have_unique_occurrences_and_fenced_heading_is_ignored(tmp_path: Path):
    source = tmp_path / "duplicate.md"
    source.write_text("# 同名\n```md\n# 伪标题\n```\n# 同名\n", encoding="utf-8")
    result = extract_markdown_structure(source, root=tmp_path, preview_chars=0)
    assert [item["occurrence"] for item in result["headings"]] == [1, 2]
    assert all(item["title"] != "伪标题" for item in result["headings"])


def test_cycle_is_rejected():
    with pytest.raises(LearningNavigationError, match="循环"):
        _topological_order([
            {"unit_id": "A", "prerequisites": ["B"]},
            {"unit_id": "B", "prerequisites": ["A"]},
        ])


def test_project_student_profile_schema_and_renderer(tmp_path: Path):
    profile_path = tmp_path / "student.md"
    schema = ROOT / "utils/references/student-learning-profile-v2.schema.json"
    now = iso_timestamp()
    profile = {
        "profile_type": "student_learning_profile", "schema_version": "2.0",
        "profile_id": "example", "profile_date": now[:10],
        "created_at": now, "updated_at": now, "domain": "通用学习",
        "title": "学习档案", "current_stage": "入门阶段",
        "primary_goals": ["掌握基础"], "secondary_goals": [], "excluded_goals": [],
        "current_projects": [], "experience": [], "constraints": [],
        "priority_topics": [], "deferred_topics": [], "learning_preferences": [],
        "knowledge": {"clear": [], "medium": [], "unclear": []},
    }
    profile_path.write_text(render_student_profile(profile), encoding="utf-8")
    snapshot = parse_student_profile(profile_path, schema)
    assert snapshot["profile"]["profile_date"] == now[:10]
    rendered = render_student_profile(snapshot["profile"])
    assert "# 学习档案" in rendered
    assert "## 知识掌握情况" in rendered
    assert profile_path.read_text(encoding="utf-8") == rendered
