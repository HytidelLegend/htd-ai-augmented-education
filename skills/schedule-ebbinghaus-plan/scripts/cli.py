"""Recoverable CLI for deterministic day-based recall schedules."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from utils.scripts.spaced_repetition import (
    ScheduleInfeasible, build_schedule, choose_batch_size, render_markdown,
)
from utils.scripts.structured_io import (
    json_digest, read_json, validate_json_schema, write_json, write_text_transaction,
)
from utils.scripts.timestamp import iso_timestamp, unique_filename_timestamp
from utils.scripts.workflow_state import WorkflowDefinition, WorkflowStateError, WorkflowStateStore


NAME = "schedule-ebbinghaus-plan"
SKILL_ROOT = Path(__file__).resolve().parents[1]
REQUEST_SCHEMA = SKILL_ROOT / "references" / "request.schema.json"
RESULT_SCHEMA = SKILL_ROOT / "references" / "result.schema.json"
STAGES = (
    "prepared", "validating_request", "selecting_batch_size", "building_schedule",
    "validating_schedule", "rendering_outputs", "verifying_outputs", "completed",
)
PAUSES = ("paused_invalid_request", "paused_infeasible", "paused_runtime")
TRANSITIONS = {stage: [STAGES[i + 1], *PAUSES] for i, stage in enumerate(STAGES[:-1])}
for stage in STAGES[2:-1]:
    TRANSITIONS[stage].append("validating_request")
TRANSITIONS.update({pause: ["validating_request"] for pause in PAUSES})
DEFINITION = WorkflowDefinition.build(name=NAME, transitions=TRANSITIONS)


def paths(root: Path, run_id: str) -> tuple[Path, Path]:
    return root / "logs" / NAME / "runs" / run_id, root / "outputs" / NAME / "runs" / run_id


def store_for(root: Path, run_id: str) -> WorkflowStateStore:
    log_dir, _ = paths(root, run_id)
    return WorkflowStateStore(
        root=root, workflow=NAME, run_id=run_id, definition=DEFINITION,
        schema_path=ROOT / "utils" / "references" / "workflow-state-v1.schema.json",
        events_dir=log_dir / "events",
    )


def receipt(run_id: str, status: str, **extra: Any) -> None:
    print(json.dumps({"run_id": run_id, "status": status, **extra}, ensure_ascii=False))


def verify_files(root: Path, run_id: str, *, require_complete: bool = True) -> dict[str, Any]:
    store = store_for(root, run_id)
    state = store.load()
    if require_complete and state["status"] != "completed":
        raise ValueError("运行尚未完成")
    log_dir, out_dir = paths(root, run_id)
    request = read_json(log_dir / "request.json")
    validate_json_schema(request, REQUEST_SCHEMA)
    if json_digest(request) != state.get("request_digest"):
        raise ValueError("请求快照与状态不一致")
    result = read_json(out_dir / "result.json")
    validate_json_schema(result, RESULT_SCHEMA)
    expected = build_schedule(request)
    if state.get("selected_batch_size") != expected["batch_size"]:
        raise ValueError("batch 大小与状态检查点不一致")
    if result != expected:
        raise ValueError("JSON 与请求或排程规则不一致")
    if (out_dir / "result.md").read_text(encoding="utf-8") != render_markdown(expected):
        raise ValueError("Markdown 与 JSON 不一致")
    return result


def pause(store: WorkflowStateStore, state: dict[str, Any], status: str, message: str, code: int) -> int:
    store.pause(
        state, status=status, error_code=status.removeprefix("paused_"),
        message=message, resume_stage="validating_request",
    )
    receipt(store.run_id, status, error=message)
    return code


def process(root: Path, run_id: str) -> int:
    store = store_for(root, run_id)
    log_dir, out_dir = paths(root, run_id)
    with store.lock():
        state = store.load()
        if state["status"] == "completed":
            receipt(run_id, "completed", result=str(out_dir / "result.json"))
            return 0
        if state["status"] in PAUSES:
            state = store.resume(state)
        elif state["status"] == "prepared":
            state = store.transition(state, "validating_request", stage="validating_request")
        elif state["status"] != "validating_request":
            # Restart deterministic work from its persisted request in one resume call.
            state = store.transition(
                state, "validating_request", stage="validating_request",
                details={"recovered_from": state["status"]},
            )

        try:
            request = read_json(log_dir / "request.json")
            validate_json_schema(request, REQUEST_SCHEMA)
            # Schema's integer type accepts booleans; the algorithm rejects them.
            if type(request["n"]) is not int or any(type(request[key]) is not int for key in ("d", "daily_items") if key in request):
                raise ValueError("n、d、daily_items 必须是正整数，不能为布尔值")
            if any(type(value) is not int for value in request.get("review_offsets", [])):
                raise ValueError("review_offsets 必须是正整数数组")
            if json_digest(request) != state.get("request_digest"):
                raise ValueError("请求快照与状态不一致")
        except (ValueError, KeyError, OSError) as exc:
            return pause(store, state, "paused_invalid_request", str(exc), 2)

        state = store.transition(state, "selecting_batch_size", stage="selecting_batch_size")
        try:
            batch_size = choose_batch_size(request)
            state = store.checkpoint(state, event="batch_size_selected", updates={"selected_batch_size": batch_size})
            state = store.transition(state, "building_schedule", stage="building_schedule")
            result = build_schedule(request)
            if result["batch_size"] != batch_size:
                raise ValueError("batch 大小与检查点不一致")
        except ScheduleInfeasible as exc:
            return pause(store, state, "paused_infeasible", str(exc), 3)
        except ValueError as exc:
            return pause(store, state, "paused_invalid_request", str(exc), 2)
        except Exception as exc:
            return pause(store, state, "paused_runtime", str(exc), 5)

        try:
            state = store.transition(state, "validating_schedule", stage="validating_schedule")
            validate_json_schema(result, RESULT_SCHEMA)
            state = store.transition(state, "rendering_outputs", stage="rendering_outputs")
            result_text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
            write_text_transaction({
                out_dir / "result.json": result_text,
                out_dir / "result.md": render_markdown(result),
            })
            state = store.transition(state, "verifying_outputs", stage="verifying_outputs")
            verify_files(root, run_id, require_complete=False)
            state = store.transition(state, "completed", stage="completed")
        except Exception as exc:
            return pause(store, state, "paused_runtime", str(exc), 5)
        receipt(run_id, "completed", result=str(out_dir / "result.json"), markdown=str(out_dir / "result.md"))
        return 0


def start(root: Path, input_path: Path) -> int:
    try:
        request = read_json(input_path)
        if not isinstance(request, dict):
            raise ValueError("请求必须是 JSON 对象")
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "invalid_request", "error": str(exc)}, ensure_ascii=False))
        return 2
    runs_root = root / "logs" / NAME / "runs"
    existing = [path.name for path in runs_root.iterdir()] if runs_root.is_dir() else []
    run_id = unique_filename_timestamp(existing)
    log_dir, _ = paths(root, run_id)
    store = store_for(root, run_id)
    now = iso_timestamp()
    state = {
        "schema_version": "1.0", "workflow": NAME, "run_id": run_id,
        "status": "prepared", "current_stage": "prepared", "resume_stage": None,
        "current_object_id": None, "current_batch_id": None,
        "completed_steps": ["prepared"], "pending_decisions": [], "error": None,
        "created_at": now, "updated_at": now, "last_heartbeat_at": now,
        "event_sequence": 0, "request_digest": json_digest(request),
    }
    with store.lock():
        store.create(state)
        write_json(log_dir / "request.json", request)
    return process(root, run_id)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="艾宾浩斯批次背诵计划")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "status", "resume", "verify", "deliver"):
        item = sub.add_parser(command)
        item.add_argument("--root", default=".")
        if command == "start":
            item.add_argument("--input", required=True)
        else:
            item.add_argument("--run-id", required=True)
            if command == "resume":
                item.add_argument("--input")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if args.command == "start":
        return start(root, Path(args.input))
    run_id = args.run_id
    if not re.fullmatch(r"\d{8}T\d{6}(?:_\d+)?", run_id):
        parser.error("无效的 run-id")
    store = store_for(root, run_id)
    try:
        state = store.load()
        if args.command == "status":
            receipt(run_id, state["status"], error=state.get("error"))
            return 0
        if args.command == "resume":
            if args.input:
                request = read_json(Path(args.input))
                if not isinstance(request, dict):
                    raise ValueError("请求必须是 JSON 对象")
                if state["status"] not in PAUSES:
                    raise ValueError("只有暂停的运行可以更新请求")
                log_dir, _ = paths(root, run_id)
                with store.lock():
                    write_json(log_dir / "request.json", request)
                    state["request_digest"] = json_digest(request)
                    store.save(state)
            return process(root, run_id)
        if args.command == "verify":
            result = verify_files(root, run_id)
            receipt(run_id, "verified", days=len(result["days"]))
            return 0
        verify_files(root, run_id)
        _, out_dir = paths(root, run_id)
        sys.stdout.write((out_dir / "result.md").read_text(encoding="utf-8"))
        return 0
    except (OSError, ValueError, WorkflowStateError, KeyError) as exc:
        print(json.dumps({"run_id": run_id, "status": "error", "error": str(exc)}, ensure_ascii=False))
        return 4 if args.command in ("verify", "deliver") else 2


if __name__ == "__main__":
    raise SystemExit(main())
