from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
CLI = ROOT / "skills" / "htd-ai-augmented-education" / "scripts" / "cli.py"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, encoding="utf-8")


def start_run(tmp_path: Path, query: str, mode: str) -> tuple[str, Path]:
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"query": query, "mode": mode}, ensure_ascii=False), encoding="utf-8")
    started = run_cli("start", "--root", str(ROOT), "--input", str(request))
    assert started.returncode == 3, started.stderr
    receipt = json.loads(started.stdout)
    return receipt["run_id"], Path(receipt["response_draft_path"])


def test_project_info_all_interfaces(tmp_path: Path) -> None:
    request_file = tmp_path / "created-request.json"
    created = run_cli("create-request", "--request-file", str(request_file), "--query", "这个项目的目标是什么？")
    assert created.returncode == 0, created.stderr
    run_id, draft_path = start_run(tmp_path, "这个项目的目标是什么？", "project_info")
    status = run_cli("status", "--root", str(ROOT), "--run-id", run_id)
    assert status.returncode == 3 and json.loads(status.stdout)["status"] == "paused_agent_response"
    response = {
        "response_type": "project_info",
        "normalized_request": "说明项目目标。",
        "assumptions": [],
        "evidence": [{"source": "docs/PRDs/AI辅助教育项目.md", "relevance": "项目目标与验收来源"}],
        "answer_points": [{"statement": "项目目标以 PRD 为准。", "sources": ["docs/PRDs/AI辅助教育项目.md"]}],
        "limitations": [],
    }
    draft_path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
    resumed = run_cli("resume", "--root", str(ROOT), "--run-id", run_id, "--input", str(draft_path))
    assert resumed.returncode == 0, resumed.stderr
    verified = run_cli("verify", "--root", str(ROOT), "--run-id", run_id)
    assert verified.returncode == 0, verified.stderr
    delivered = run_cli("deliver", "--root", str(ROOT), "--run-id", run_id)
    assert delivered.returncode == 0 and delivered.stdout.startswith("# 项目信息\n")


def test_task_routing_can_admit_no_registered_skill(tmp_path: Path) -> None:
    run_id, draft_path = start_run(tmp_path, "实现一个当前项目不支持的需求", "task_routing")
    response = {
        "response_type": "task_routing",
        "normalized_request": "判断项目是否能够完成该需求并给出调用建议。",
        "assumptions": [],
        "evidence": [{"source": ".claude-plugin/plugin.json", "relevance": "已注册 Skill 清单"}],
        "normalized_task": "实现一个当前项目不支持的需求。",
        "skill_sequence": [],
        "prompt_example": {
            "objective": "确认需求缺口。",
            "context": ["仅检查项目已注册 Skills"],
            "constraints": ["不推荐外部 Skills"],
            "acceptance_criteria": ["明确说明未覆盖能力"],
        },
        "uncovered_requirements": ["当前已注册 Skills 没有公开契约声明可实现该需求。"],
    }
    draft_path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
    resumed = run_cli("resume", "--root", str(ROOT), "--run-id", run_id, "--input", str(draft_path))
    assert resumed.returncode == 0, resumed.stderr
    delivered = run_cli("deliver", "--root", str(ROOT), "--run-id", run_id)
    assert "无法实现该需求" in delivered.stdout
    assert "不推荐外部 Skills" in delivered.stdout


def test_rejects_external_skill(tmp_path: Path) -> None:
    run_id, draft_path = start_run(tmp_path, "实现一个功能", "task_routing")
    response = {
        "response_type": "task_routing",
        "normalized_request": "实现一个功能。",
        "assumptions": [],
        "evidence": [{"source": ".claude-plugin/plugin.json", "relevance": "注册信息"}],
        "normalized_task": "实现一个功能。",
        "skill_sequence": [{"order": 1, "skill": "external-skill", "purpose": "尝试实现", "prerequisite": [], "source": ".claude-plugin/plugin.json"}],
        "prompt_example": {"objective": "实现功能", "context": [], "constraints": [], "acceptance_criteria": []},
        "uncovered_requirements": [],
    }
    draft_path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
    resumed = run_cli("resume", "--root", str(ROOT), "--run-id", run_id, "--input", str(draft_path))
    assert resumed.returncode == 3
    assert json.loads(resumed.stdout)["status"] == "paused_response_validation"
