"""Resumable migration of per-word files into the unique JSONL library."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

from utils.scripts.dictionary_jsonl import ensure_buckets, read_all
from utils.scripts.dictionary_records import entries
from utils.scripts.file_transaction import project_lock
from utils.scripts.structured_io import read_json, write_json, write_text_transaction
from utils.scripts.timestamp import iso_timestamp, unique_filename_timestamp
from utils.scripts.workflow_state import WorkflowDefinition, WorkflowStateStore

ROOT = Path(__file__).resolve().parents[2]
NAME = "dictionary-migration"
STAGES = ("prepared", "collecting", "validating", "committing", "verifying", "cleaning", "completed")


def migrate(root: Path, validate=None) -> dict:
    parent = root / "logs" / NAME / "runs"
    pending = [path for path in sorted(parent.glob("*/state.json"), reverse=True) if read_json(path)["status"] != "completed"]
    run_id = pending[0].parent.name if pending else unique_filename_timestamp(path.name for path in parent.iterdir()) if parent.exists() else unique_filename_timestamp([])
    transitions = {stage: [STAGES[i + 1], "paused_retryable_error"] for i, stage in enumerate(STAGES[:-1])}
    transitions["paused_retryable_error"] = list(STAGES[:-1])
    flow = WorkflowStateStore(root=root, workflow=NAME, run_id=run_id,
        definition=WorkflowDefinition.build(name=NAME, transitions=transitions),
        schema_path=ROOT / "utils" / "references" / "workflow-state-v1.schema.json")
    if not pending:
        now = iso_timestamp()
        flow.create({"schema_version": "1.0", "workflow": NAME, "run_id": run_id, "status": "prepared",
            "current_stage": "prepared", "resume_stage": None, "current_object_id": None, "current_batch_id": None,
            "completed_steps": [], "pending_decisions": [], "error": None, "created_at": now, "updated_at": now,
            "last_heartbeat_at": now, "event_sequence": 0})
    directory = root / "outputs" / "词汇星图" / "dicts"
    sources = (root / "outputs" / "英文词典" / "entries", root / "outputs" / "词汇星图" / "entries")
    with flow.lock(), project_lock(root / "logs" / "build-word-entry" / "dictionary.lock", f"{NAME}:{run_id}"):
        state = flow.load()
        if state["status"] == "paused_retryable_error":
            state = flow.transition(state, state["resume_stage"], stage=state["resume_stage"])
        try:
            while state["status"] != "completed":
                stage = state["status"]
                if stage == "collecting":
                    snapshot = []
                    for source in sources:
                        for path in sorted(source.glob("*.json")):
                            raw = path.read_bytes()
                            snapshot.append({"path": str(path.relative_to(root)), "sha256": hashlib.sha256(raw).hexdigest(), "entry": json.loads(raw)})
                    write_json(flow.run_dir / "sources.json", snapshot)
                elif stage == "validating":
                    ensure_buckets(directory)
                    current = read_all(directory, validate)
                    updates = {}
                    identical = 0
                    for item in read_json(flow.run_dir / "sources.json"):
                        entry = item["entry"]
                        if validate:
                            validate(entry)
                        if Path(item["path"]).stem != entry["wordId"]:
                            raise ValueError("迁移文件名与 wordId 不一致")
                        prior = updates.get(entry["wordId"]) or current.get(entry["wordId"])
                        if prior and prior["revision"] == entry["revision"] and prior != entry:
                            raise ValueError("同修订词条迁移冲突")
                        if not prior or prior["revision"] < entry["revision"]:
                            updates[entry["wordId"]] = entry
                        else:
                            identical += 1
                    write_json(flow.run_dir / "updates.json", updates)
                    write_json(flow.run_dir / "baseline.json", current)
                    write_json(flow.run_dir / "result.json", {"copied": len(updates), "identical": identical, "runId": run_id})
                elif stage == "committing":
                    current = read_all(directory, validate)
                    baseline = read_json(flow.run_dir / "baseline.json")
                    updates = read_json(flow.run_dir / "updates.json")
                    for wid, value in updates.items():
                        if current.get(wid) != baseline.get(wid) and current.get(wid) != value:
                            raise ValueError("迁移期间总词条已被修改")
                    write_text_transaction({entries(root) / wid: json.dumps(value, ensure_ascii=False) for wid, value in updates.items()})
                elif stage == "verifying":
                    current = read_all(directory, validate)
                    for wid, value in read_json(flow.run_dir / "updates.json").items():
                        if current.get(wid) != value:
                            raise ValueError("迁移验证失败")
                elif stage == "cleaning":
                    snapshot = read_json(flow.run_dir / "sources.json")
                    # Verify all paths and fingerprints before deleting any old file.
                    paths = []
                    for item in snapshot:
                        path = (root / item["path"]).resolve()
                        if path.parent not in {source.resolve() for source in sources}:
                            raise ValueError("迁移清理路径越界")
                        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                            raise ValueError("旧词条在迁移期间变化，保留原文件")
                        paths.append(path)
                    for path in paths:
                        path.unlink(missing_ok=True)
                    for source in sources:
                        if source.is_dir() and not any(source.iterdir()):
                            source.rmdir()
                target = STAGES[STAGES.index(stage) + 1]
                state = flow.transition(state, target, stage=target, completed_step=stage)
        except Exception as exc:
            flow.pause(state, status="paused_retryable_error", error_code="migration_failed", message=str(exc), resume_stage=state["status"])
            raise
    return read_json(flow.run_dir / "result.json")
