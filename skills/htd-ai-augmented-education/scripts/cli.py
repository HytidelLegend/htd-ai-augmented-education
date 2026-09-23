"""Script-first project information and task-routing workflow."""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "utils" / "scripts"))

from structured_io import read_json, validate_json_schema, write_json  # noqa: E402
from timestamp import iso_timestamp  # noqa: E402
from workflow_state import WorkflowDefinition, WorkflowStateStore  # noqa: E402

WORKFLOW = "htd-ai-augmented-education"
SKILL_ROOT = Path(__file__).resolve().parents[1]
REQUEST_SCHEMA = SKILL_ROOT / "references" / "request.schema.json"
RESPONSE_SCHEMA = SKILL_ROOT / "references" / "response.schema.json"
BASE_SOURCES = (
    "AGENTS.md",
    "CODE_OF_CONDUCT.md",
    "SOURCE_OF_TRUTH.md",
    "docs/PRDs",
    "docs/architecture",
    "docs/Skills_说明书.md",
    "docs/contributing",
    "docs/security",
    "docs/更新历史.md",
    ".claude-plugin/plugin.json",
)
TRANSITIONS = {
    "prepared": {"validating_request"},
    "validating_request": {"classifying_intent", "paused_input"},
    "classifying_intent": {"resolving_authoritative_sources", "paused_intent_clarification"},
    "resolving_authoritative_sources": {"building_evidence_packet", "paused_missing_source", "paused_source_conflict"},
    "building_evidence_packet": {"validating_evidence_packet"},
    "validating_evidence_packet": {"paused_agent_response", "paused_missing_source"},
    "paused_agent_response": {"validating_agent_response"},
    "validating_agent_response": {"rendering_markdown", "paused_response_validation"},
    "rendering_markdown": {"verifying_delivery"},
    "verifying_delivery": {"publishing", "paused_response_validation"},
    "publishing": {"completed", "paused_output_conflict"},
    "paused_input": {"prepared"},
    "paused_intent_clarification": {"classifying_intent"},
    "paused_missing_source": {"resolving_authoritative_sources"},
    "paused_source_conflict": {"resolving_authoritative_sources"},
    "paused_response_validation": {"validating_agent_response"},
    "paused_output_conflict": {"publishing"},
}
DEFINITION = WorkflowDefinition.build(name=WORKFLOW, transitions=TRANSITIONS)


def run_dir(root: Path, run_id: str) -> Path:
    return root / "logs" / WORKFLOW / "runs" / run_id


def output_dir(root: Path, run_id: str) -> Path:
    return root / "outputs" / WORKFLOW / "runs" / run_id


def state_store(root: Path, run_id: str) -> WorkflowStateStore:
    return WorkflowStateStore(
        root=root,
        workflow=WORKFLOW,
        run_id=run_id,
        definition=DEFINITION,
        run_dir=run_dir(root, run_id),
        schema_path=PROJECT_ROOT / "utils" / "references" / "workflow-state-v1.schema.json",
    )


def new_state(run_id: str) -> dict[str, Any]:
    now = iso_timestamp()
    return {
        "schema_version": "1.0",
        "workflow": WORKFLOW,
        "run_id": run_id,
        "status": "prepared",
        "current_stage": "prepared",
        "resume_stage": None,
        "current_object_id": None,
        "current_batch_id": None,
        "completed_steps": [],
        "pending_decisions": [],
        "error": None,
        "created_at": now,
        "updated_at": now,
        "last_heartbeat_at": now,
        "event_sequence": 0,
    }


def advance(root: Path, state: dict[str, Any], target: str, **updates: Any) -> dict[str, Any]:
    return state_store(root, state["run_id"]).transition(
        state, target, stage=target, completed_step=target, updates=updates
    )


def pause(root: Path, state: dict[str, Any], status: str, message: str, resume_stage: str) -> dict[str, Any]:
    return state_store(root, state["run_id"]).pause(
        state,
        status=status,
        error_code=status.removeprefix("paused_"),
        message=message,
        resume_stage=resume_stage,
    )


def classify(query: str, requested_mode: str) -> str:
    if requested_mode != "auto":
        return requested_mode
    task_pattern = re.compile(r"(?:实现|开发|新增|添加|修改|修复|重构|完成|做一个|创建|接入|调用).{0,40}")
    return "task_routing" if task_pattern.search(query) else "project_info"


def registered_skills(root: Path) -> list[dict[str, str]]:
    manifest_path = root / ".claude-plugin" / "plugin.json"
    manifest = read_json(manifest_path)
    entries: list[dict[str, str]] = []
    for raw in manifest.get("skills", []):
        rel_dir = PurePosixPath(str(raw).removeprefix("./"))
        skill_file = Path(*rel_dir.parts) / "SKILL.md"
        absolute = root / skill_file
        if not absolute.is_file():
            raise ValueError(f"已注册 Skill 缺少公开契约：{skill_file.as_posix()}")
        entries.append({"name": rel_dir.name, "source": skill_file.as_posix()})
    return entries


def expand_source(root: Path, relative: str) -> list[str]:
    path = root / Path(*PurePosixPath(relative).parts)
    if path.is_file():
        return [relative]
    if path.is_dir():
        return sorted(item.relative_to(root).as_posix() for item in path.rglob("*.md") if item.is_file())
    raise ValueError(f"缺少权威来源：{relative}")


def build_source_manifest(root: Path, mode: str) -> dict[str, Any]:
    sources: list[str] = []
    for relative in BASE_SOURCES:
        sources.extend(expand_source(root, relative))
    skills = registered_skills(root)
    sources.extend(item["source"] for item in skills)
    unique_sources = list(dict.fromkeys(sources))
    return {
        "mode": mode,
        "sources": [{"path": path} for path in unique_sources],
        "registered_skills": skills,
        "routing_policy": {
            "registered_only": True,
            "recommend_external_skills": False,
            "allow_empty_sequence": True,
        },
    }


def response_skeleton(query: str, mode: str) -> dict[str, Any]:
    common: dict[str, Any] = {
        "response_type": mode,
        "normalized_request": query.strip(),
        "assumptions": [],
        "evidence": [],
    }
    if mode == "project_info":
        common.update({"answer_points": [], "limitations": []})
    else:
        common.update({
            "normalized_task": query.strip(),
            "skill_sequence": [],
            "prompt_example": {
                "objective": query.strip(),
                "context": [],
                "constraints": [],
                "acceptance_criteria": [],
            },
            "uncovered_requirements": [],
        })
    return common


def start(root: Path, request_path: Path) -> dict[str, Any]:
    request = read_json(request_path)
    validate_json_schema(request, REQUEST_SCHEMA)
    run_id = uuid.uuid4().hex
    store = state_store(root, run_id)
    state = store.create(new_state(run_id))
    state = advance(root, state, "validating_request")
    query = request["query"].strip()
    if not query:
        state = pause(root, state, "paused_input", "query 不能为空", "prepared")
        return state
    state = advance(root, state, "classifying_intent")
    mode = classify(query, request.get("mode", "auto"))
    state = advance(root, state, "resolving_authoritative_sources", request_mode=mode)
    try:
        manifest = build_source_manifest(root, mode)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return pause(root, state, "paused_missing_source", str(exc), "resolving_authoritative_sources")
    state = advance(root, state, "building_evidence_packet")
    directory = run_dir(root, run_id)
    write_json(directory / "request.json", {"query": query, "mode": mode})
    write_json(directory / "source_manifest.json", manifest)
    draft_path = directory / "response-draft.json"
    write_json(draft_path, response_skeleton(query, mode))
    state = advance(root, state, "validating_evidence_packet")
    if not manifest["sources"]:
        return pause(root, state, "paused_missing_source", "没有可用的权威来源", "resolving_authoritative_sources")
    state = pause(root, state, "paused_agent_response", "需要 Agent 根据权威来源填写响应草稿", "validating_agent_response")
    return {
        "status": state["status"],
        "run_id": run_id,
        "mode": mode,
        "source_manifest_path": str(directory / "source_manifest.json"),
        "response_draft_path": str(draft_path),
    }


def safe_relative_source(root: Path, value: str) -> str:
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"来源必须是仓库内相对路径：{value}")
    path = root / Path(*candidate.parts)
    if not path.is_file():
        raise ValueError(f"引用来源不存在：{value}")
    return candidate.as_posix()


def validate_response(root: Path, run_id: str, response: dict[str, Any]) -> None:
    validate_json_schema(response, RESPONSE_SCHEMA)
    request = read_json(run_dir(root, run_id) / "request.json")
    if response["response_type"] != request["mode"]:
        raise ValueError("response_type 与运行模式不一致")
    for item in response["evidence"]:
        safe_relative_source(root, item["source"])
    if response["response_type"] == "project_info":
        for point in response["answer_points"]:
            for source in point["sources"]:
                safe_relative_source(root, source)
        return
    registered = {item["name"]: item["source"] for item in registered_skills(root)}
    orders = [item["order"] for item in response["skill_sequence"]]
    if orders != list(range(1, len(orders) + 1)):
        raise ValueError("skill_sequence.order 必须从 1 开始连续递增")
    for item in response["skill_sequence"]:
        if item["skill"] not in registered:
            raise ValueError(f"不得推荐未注册或外部 Skill：{item['skill']}")
        if safe_relative_source(root, item["source"]) != registered[item["skill"]]:
            raise ValueError(f"Skill 来源与插件注册不一致：{item['skill']}")
    if not response["skill_sequence"] and not response["uncovered_requirements"]:
        raise ValueError("无可用 Skill 时必须明确说明未覆盖需求")


def bullet_list(values: list[str], empty: str) -> str:
    return "\n".join(f"- {value}" for value in values) if values else f"- {empty}"


def render_evidence(response: dict[str, Any]) -> str:
    return "\n".join(f"- `{item['source']}`：{item['relevance']}" for item in response["evidence"])


def render_response(response: dict[str, Any]) -> str:
    if response["response_type"] == "project_info":
        template = (SKILL_ROOT / "templates" / "project-info.template.md").read_text(encoding="utf-8")
        answer = "\n\n".join(
            f"- {item['statement']}\n  - 来源：" + "、".join(f"`{source}`" for source in item["sources"])
            for item in response["answer_points"]
        )
        values = {
            "answer_points": answer,
            "evidence": render_evidence(response),
            "limitations": bullet_list(response["limitations"], "未发现需要额外说明的边界。"),
        }
    else:
        template = (SKILL_ROOT / "templates" / "task-routing.template.md").read_text(encoding="utf-8")
        if response["skill_sequence"]:
            rows = ["| 顺序 | Skill | 用途 | 前置条件 | 依据 |", "|---:|---|---|---|---|"]
            for item in response["skill_sequence"]:
                prerequisite = "；".join(item["prerequisite"]) or "无"
                rows.append(f"| {item['order']} | `{item['skill']}` | {item['purpose']} | {prerequisite} | `{item['source']}` |")
            sequence = "\n".join(rows)
        else:
            sequence = "当前已注册的项目 Skills 无法实现该需求。"
        prompt = response["prompt_example"]
        prompt_text = (
            "```text\n"
            f"目标：{prompt['objective']}\n"
            f"上下文：{'；'.join(prompt['context']) or '无额外上下文'}\n"
            f"约束：{'；'.join(prompt['constraints']) or '遵循项目文档与对应 Skill 契约'}\n"
            f"验收标准：{'；'.join(prompt['acceptance_criteria']) or '按对应 Skill 的完成门禁验证'}\n"
            "```"
        )
        values = {
            "normalized_task": response["normalized_task"],
            "skill_sequence": sequence,
            "prompt_example": prompt_text,
            "uncovered_requirements": bullet_list(response["uncovered_requirements"], "现有 Skills 已覆盖任务要求。"),
            "evidence": render_evidence(response),
        }
    for key, value in values.items():
        template = template.replace("{{ " + key + " }}", value)
    return template.rstrip() + "\n"


def resume(root: Path, run_id: str, response_path: Path) -> dict[str, Any]:
    store = state_store(root, run_id)
    state = store.load()
    if state["status"] not in {"paused_agent_response", "paused_response_validation"}:
        raise ValueError(f"当前状态不能提交响应：{state['status']}")
    state = store.resume(state)
    try:
        response = read_json(response_path)
        validate_response(root, run_id, response)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return pause(root, state, "paused_response_validation", str(exc), "validating_agent_response")
    write_json(run_dir(root, run_id) / "response.json", response)
    state = advance(root, state, "rendering_markdown")
    markdown = render_response(response)
    state = advance(root, state, "verifying_delivery")
    if "{{ " in markdown or not markdown.strip():
        return pause(root, state, "paused_response_validation", "Markdown 模板渲染不完整", "validating_agent_response")
    target = output_dir(root, run_id)
    if target.exists() and any(target.iterdir()):
        return pause(root, state, "paused_output_conflict", f"输出目录非空，不覆盖：{target}", "publishing")
    state = advance(root, state, "publishing")
    target.mkdir(parents=True, exist_ok=True)
    write_json(target / "result.json", response)
    (target / "result.md").write_text(markdown, encoding="utf-8", newline="\n")
    state = advance(root, state, "completed", output_path=str(target))
    return {"status": state["status"], "run_id": run_id, "output_path": str(target)}


def verify(root: Path, run_id: str) -> dict[str, Any]:
    state = state_store(root, run_id).load()
    if state["status"] != "completed":
        raise ValueError(f"运行尚未完成：{state['status']}")
    target = output_dir(root, run_id)
    response = read_json(target / "result.json")
    validate_response(root, run_id, response)
    expected = render_response(response)
    actual = (target / "result.md").read_text(encoding="utf-8")
    if actual != expected:
        raise ValueError("result.md 与 result.json 渲染结果不一致")
    return {"status": "verified", "run_id": run_id, "result_path": str(target / "result.md")}


def create_request(path: Path, query: str, mode: str) -> dict[str, Any]:
    value = {"query": query, "mode": mode}
    validate_json_schema(value, REQUEST_SCHEMA)
    if path.exists():
        raise ValueError(f"请求文件已存在，不覆盖：{path}")
    write_json(path, value)
    return {"status": "created", "request_path": str(path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="htd-ai-augmented-education project router")
    parser.add_argument("command", choices=["create-request", "start", "status", "resume", "deliver", "verify"])
    parser.add_argument("--root", default=".")
    parser.add_argument("--input")
    parser.add_argument("--run-id")
    parser.add_argument("--request-file")
    parser.add_argument("--query", default="")
    parser.add_argument("--mode", choices=["auto", "project_info", "task_routing"], default="auto")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.command == "create-request":
            if not args.request_file or not args.query:
                raise ValueError("create-request 需要 --request-file 和 --query")
            result = create_request(Path(args.request_file), args.query, args.mode)
        elif args.command == "start":
            if not args.input:
                raise ValueError("start 需要 --input")
            result = start(root, Path(args.input))
        elif args.command == "status":
            if not args.run_id:
                raise ValueError("status 需要 --run-id")
            result = state_store(root, args.run_id).load()
        elif args.command == "resume":
            if not args.run_id or not args.input:
                raise ValueError("resume 需要 --run-id 和 --input")
            result = resume(root, args.run_id, Path(args.input))
        elif args.command == "verify":
            if not args.run_id:
                raise ValueError("verify 需要 --run-id")
            result = verify(root, args.run_id)
        else:
            if not args.run_id:
                raise ValueError("deliver 需要 --run-id")
            verify(root, args.run_id)
            sys.stdout.write((output_dir(root, args.run_id) / "result.md").read_text(encoding="utf-8"))
            return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and str(result.get("status", "")).startswith("paused_"):
            return 3
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 4 if args.command == "verify" else 2


if __name__ == "__main__":
    raise SystemExit(main())
