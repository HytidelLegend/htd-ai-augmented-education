"""Recoverable batch ASR; each media file has its own explicit child run."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.scripts.file_transaction import file_sha256
from utils.scripts.structured_io import read_json
from utils.scripts.timestamp import iso_timestamp, unique_filename_timestamp
from utils.scripts.workflow_state import WorkflowDefinition, WorkflowStateStore
from run_speech_to_text import run_request, resume_run, verify_run, _paths, SUPPORTED_EXTENSIONS

WORKFLOW = "run-speech-to-text"
DEFINITION = WorkflowDefinition.build(name=WORKFLOW, transitions={
    "prepared": ["processing", "paused_error"],
    "processing": ["awaiting_transcript_approval", "paused_error", "completed"],
    "awaiting_transcript_approval": ["processing", "paused_error"],
    "paused_error": ["processing"], "completed": []})


def store_for(root: Path, run_id: str) -> WorkflowStateStore:
    return WorkflowStateStore(root=root, workflow=WORKFLOW, run_id=run_id, definition=DEFINITION,
        schema_path=ROOT / "utils/references/workflow-state-v1.schema.json",
        events_dir=root / "logs" / WORKFLOW / "runs" / run_id / "events")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="批量 ASR 状态机，恢复使用明确的 batch run ID")
    parser.add_argument("command", choices=("start", "resume", "status", "verify", "deliver"))
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--input-dir")
    parser.add_argument("--run-id")
    parser.add_argument("--implementation", choices=("streaming", "recording_file"), default="streaming")
    parser.add_argument("--hotwords-file")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.command == "start":
            if not args.input_dir:
                raise ValueError("start 必须提供 --input-dir")
            input_dir = Path(args.input_dir)
            input_dir = (input_dir if input_dir.is_absolute() else root / input_dir).resolve()
            sources = sorted(p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS)
            if not sources:
                raise ValueError("输入目录没有受支持的音视频")
            logs = root / "logs" / WORKFLOW / "runs"
            run_id = "batch_" + unique_filename_timestamp(p.name.removeprefix("batch_") for p in logs.glob("batch_*"))
            store = store_for(root, run_id)
            now = iso_timestamp()
            state = {"schema_version": "1.0", "workflow": WORKFLOW, "run_id": run_id,
                "status": "prepared", "current_stage": "prepared", "resume_stage": None,
                "current_object_id": None, "current_batch_id": None, "completed_steps": [],
                "pending_decisions": [], "error": None, "created_at": now, "updated_at": now,
                "last_heartbeat_at": now, "event_sequence": 0,
                "inputs": [{"path": str(p), "sha256": file_sha256(p)} for p in sources],
                "implementation": args.implementation, "hotwords_file": args.hotwords_file,
                "children": {}, "results": {}, "errors": {}}
            store.create(state)
        else:
            if not args.run_id or not args.run_id.startswith("batch_") or Path(args.run_id).name != args.run_id:
                raise ValueError("必须提供合法的 --run-id")
            run_id = args.run_id
            store = store_for(root, run_id)
            state = store.load()
        if args.command == "status":
            print(json.dumps(state, ensure_ascii=False))
            return 0
        if args.command in {"verify", "deliver"}:
            all_approved = state["status"] == "completed"
            for entry in state["inputs"]:
                source = Path(entry["path"])
                if file_sha256(source) != entry["sha256"]:
                    raise ValueError("批次输入已变化")
                child = state["children"].get(source.name)
                if not child:
                    all_approved = False
                    continue
                verify_run(root, child)
                all_approved &= read_json(_paths(Path(child))["state"])["status"] == "completed"
            print(json.dumps({"status": "completed" if all_approved else state["status"],
                              "run_id": run_id, "results": state["results"]}, ensure_ascii=False))
            return 0 if all_approved else 3
        with store.lock():
            if state["status"] != "completed":
                store.transition(state, "processing", stage="processing")
            all_approved = True
            for entry in state["inputs"]:
                source = Path(entry["path"])
                if file_sha256(source) != entry["sha256"]:
                    raise ValueError("批次输入已变化，请新建运行")
                child = state["children"].get(source.name)
                if args.command in {"verify", "deliver"} and not child:
                    raise ValueError("批次尚有未识别的输入")
                if child:
                    if args.command in {"verify", "deliver"}:
                        verify_run(root, child)
                    else:
                        resume_run(root, child)
                else:
                    def save_child(child_id, child_dir):
                        state["children"][source.name] = str(child_dir)
                        store.checkpoint(state, event="child_initialized", details={"child_run_id": child_id})
                    request = {"schema_version": "1.0", "input_path": str(source), "implementation": state["implementation"]}
                    if state["hotwords_file"]:
                        request["hotwords_path"] = state["hotwords_file"]
                    run_request(root, request, on_initialized=save_child)
                    child = state["children"][source.name]
                verification = verify_run(root, child)
                child_state = read_json(_paths(Path(child))["state"])
                all_approved &= child_state["status"] == "completed"
                export_dir = Path(child) / ("approved" if child_state["status"] == "completed" else "generated")
                transcript_path = _paths(Path(child))["approved_transcript" if child_state["status"] == "completed" else "transcript"]
                export_dir = export_dir / "markdown" / file_sha256(transcript_path)
                result = subprocess.run([sys.executable, str(Path(__file__).with_name("export_transcript_markdown.py")),
                    "--root", str(root), "--run-dir", child, "--output-dir", str(export_dir)],
                    capture_output=True, text=True, encoding="utf-8", shell=False)
                if result.returncode:
                    raise ValueError(result.stderr.strip())
                state["results"][source.name] = {"run_dir": child, "verification": verification,
                                                "export": json.loads(result.stdout)}
                if state["status"] != "completed":
                    store.checkpoint(state, event="child_verified")
            if state["status"] != "completed":
                store.transition(state, "completed" if all_approved else "awaiting_transcript_approval",
                                 stage="completed" if all_approved else "awaiting_transcript_approval")
        print(json.dumps({"status": state["status"], "run_id": run_id, "results": state["results"]}, ensure_ascii=False))
        return 0 if all_approved else 3
    except Exception as exc:
        if args.command not in {"verify", "deliver"} and 'store' in locals() and 'state' in locals() and state["status"] != "completed":
            store.pause(state, status="paused_error", error_code="batch_error", message=str(exc), resume_stage="processing")
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 4 if args.command in {"verify", "deliver"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
