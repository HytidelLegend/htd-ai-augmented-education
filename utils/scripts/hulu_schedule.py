"""Deterministic first-pass-only vocabulary scheduling."""
from __future__ import annotations

from math import ceil
from typing import Any
from pathlib import Path


def build_schedule(request: dict[str, int]) -> dict[str, Any]:
    n = request["n"]
    if type(n) is not int or n < 1 or ("daily_items" in request) == ("d" in request):
        raise ValueError("需要正整数 n，且 daily_items 与 d 恰好指定一个")
    value = request.get("daily_items", request.get("d"))
    if type(value) is not int or value < 1 or ("d" in request and value > n):
        raise ValueError("每日词数或天数无效")
    days = ceil(n / value) if "daily_items" in request else value
    batch_size = value if "daily_items" in request else ceil(n / days)
    batches = []
    cursor = 1
    for index in range(days):
        size = min(batch_size, n - cursor + 1) if "daily_items" in request else (n // days + (index < n % days))
        ids = list(range(cursor, cursor + size))
        batches.append({"batch": index + 1, "item_ids": ids})
        cursor += size
    return {"method": "hulu", "batch_size": batch_size, "batches": batches,
            "days": [{"day": row["batch"], "item_ids": row["item_ids"],
                      "new_item_ids": row["item_ids"], "review_item_ids": []} for row in batches]}


def run_schedule(root: Path, request: dict[str, int], run_id: str | None = None) -> dict:
    """Application-owned state machine; resume a failed run with its run_id."""
    from utils.scripts.timestamp import iso_timestamp, unique_filename_timestamp
    from utils.scripts.structured_io import read_json, write_json, validate_json_schema
    from utils.scripts.workflow_state import WorkflowDefinition, WorkflowStateStore
    parent = root / "logs" / "hulu-schedule" / "runs"
    stages = ("prepared", "validating_request", "building_schedule", "verifying_outputs", "completed")
    transitions = {stage: [stages[i + 1], "paused_retryable_error"] for i, stage in enumerate(stages[:-1])}
    transitions["paused_retryable_error"] = list(stages[:-1])
    resume = run_id is not None
    if run_id is None:
        run_id = unique_filename_timestamp(path.name for path in parent.iterdir()) if parent.exists() else unique_filename_timestamp([])
    project_root = Path(__file__).resolve().parents[2]
    flow = WorkflowStateStore(root=root, workflow="hulu-schedule", run_id=run_id,
        definition=WorkflowDefinition.build(name="hulu-schedule", transitions=transitions),
        schema_path=project_root / "utils" / "references" / "workflow-state-v1.schema.json")
    if not resume:
        now = iso_timestamp()
        flow.create({"schema_version": "1.0", "workflow": "hulu-schedule", "run_id": run_id,
            "status": "prepared", "current_stage": "prepared", "resume_stage": None,
            "current_object_id": None, "current_batch_id": None, "completed_steps": [],
            "pending_decisions": [], "error": None, "created_at": now, "updated_at": now,
            "last_heartbeat_at": now, "event_sequence": 0})
        write_json(flow.run_dir / "request.json", request)
    with flow.lock():
        state = flow.load()
        request = read_json(flow.run_dir / "request.json")
        if state["status"] == "paused_retryable_error":
            state = flow.transition(state, state["resume_stage"], stage=state["resume_stage"])
        try:
            while state["status"] != "completed":
                stage = state["status"]
                if stage == "validating_request":
                    build_schedule(request)
                elif stage == "building_schedule":
                    write_json(flow.run_dir / "schedule.json", build_schedule(request))
                elif stage == "verifying_outputs":
                    schedule = read_json(flow.run_dir / "schedule.json")
                    validate_json_schema(schedule, project_root / "utils" / "references" / "hulu-schedule-v1.schema.json")
                    ids = [item for day in schedule["days"] for item in day["item_ids"]]
                    if ids != list(range(1, request["n"] + 1)):
                        raise ValueError("葫芦排程未恰好覆盖全部单词")
                target = stages[stages.index(stage) + 1]
                state = flow.transition(state, target, stage=target, completed_step=stage)
        except Exception as exc:
            flow.pause(state, status="paused_retryable_error", error_code="schedule_failed", message=str(exc), resume_stage=state["status"])
            raise
    return read_json(flow.run_dir / "schedule.json")
