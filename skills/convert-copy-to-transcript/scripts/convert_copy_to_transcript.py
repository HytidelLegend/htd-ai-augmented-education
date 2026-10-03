"""Public recoverable runner for copy-to-transcript conversion."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PROJECT_DIR = next(p for p in Path(__file__).resolve().parents if (p / 'utils/scripts/timestamp.py').is_file())
if str(DEFAULT_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(DEFAULT_PROJECT_DIR))

from utils.scripts.artifact_manifest import ArtifactManifestStore, artifact_entry
from utils.scripts.file_transaction import file_sha256, project_lock
from utils.scripts.structured_io import read_json, structured_error_receipt
from utils.scripts.run_state import write_json
from utils.scripts.speech_pause_markers import SpeechPauseMarkerError, marker_summary
from utils.scripts.text_input import TextInputError, read_text_file
from utils.scripts.timestamp import iso_timestamp
from utils.scripts.speech_handoff import publish_handoff
from utils.scripts.speech_cli import CommandStateError
from utils.scripts.tts_workspace import (
    TtsWorkspace,
    create_workspace,
    input_metadata,
    update_component,
    open_workspace,
)
from utils.scripts.workflow_state import WorkflowDefinition, WorkflowStateError, WorkflowStateStore

from rule_scanner import RULES_VERSION, scan_text
from transcript_renderer import render_diff_report, render_transcript, validate_decisions
from transcript_validator import read_utf8, validate_preview, validate_tts_handoff


WORKFLOW = "convert-copy-to-transcript"
DEFINITION = WorkflowDefinition.build(
    name=WORKFLOW,
    transitions={
        "prepared": ["deterministic_transform", "paused_error"],
        "deterministic_transform": ["awaiting_semantic_decisions", "paused_error"],
        "awaiting_semantic_decisions": ["preview_ready", "revision_required", "paused_error"],
        "preview_ready": ["awaiting_transcript_approval", "paused_error"],
        "awaiting_transcript_approval": ["approved", "revision_required", "paused_error"],
        "revision_required": ["awaiting_semantic_decisions", "paused_error"],
        "approved": ["verified", "revision_required", "paused_error"],
        "verified": ["completed", "paused_error"],
        "paused_error": [
            "prepared",
            "deterministic_transform",
            "awaiting_semantic_decisions",
            "preview_ready",
            "awaiting_transcript_approval",
            "revision_required",
            "approved",
            "verified",
        ],
        "completed": [],
    },
    terminal_statuses=("completed",),
)


class TranscriptWorkflowError(ValueError):
    pass


def state_store(root: Path, run_id: str) -> WorkflowStateStore:
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise TranscriptWorkflowError("run ID 无效")
    return WorkflowStateStore(root=root, workflow=WORKFLOW, run_id=run_id, definition=DEFINITION,
                              schema_path=DEFAULT_PROJECT_DIR / "utils/references/workflow-state-v1.schema.json",
                              events_dir=root / "logs" / WORKFLOW / "runs" / run_id / "events")


def normalize_text(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise TranscriptWorkflowError("输入文本不能为空")
    return text


def _paths(state: dict[str, Any]) -> dict[str, Path]:
    output = Path(state["output_dir"])
    generated = Path(state["input"]["copied_path"]).parent.parent / "generated"
    return {
        "output": output,
        "source": Path(state["input"]["copied_path"]),
        "scan": generated / "rule-scan.json",
        "decisions": generated / "semantic-decisions.json",
        "preview": output / "generated" / "transcript-preview.txt",
        "diff": output / "generated" / "difference-report.md",
        "approved": output / "approved" / "transcript.txt",
        "approval": output / "approved" / "approval-receipt.json",
        "handoff": generated / "tts-validation.json",
        "manifest": output / "manifest.json",
    }


def _manifest(root: Path, state: dict[str, Any]) -> ArtifactManifestStore:
    return ArtifactManifestStore(root=root, path=_paths(state)["manifest"])


def _default_decisions(run_id: str, scan: dict[str, Any]) -> dict[str, Any]:
    decisions = []
    for candidate in scan["semantic_candidates"]:
        allowed = candidate["allowed_replacements"]
        decisions.append(
            {
                "candidate_id": candidate["candidate_id"],
                "replacement": allowed[0] if allowed and not candidate["custom_replacement"] else "",
            }
        )
    return {"schema_version": "1.0", "run_id": run_id, "decisions": decisions}


def prepare(
    *,
    root: Path,
    input_file: Path | None,
    text: str | None,
    output_root: Path | None = None,
    workspace_dir: Path | None = None,
    workspace_run_id: str | None = None,
    lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if (input_file is None) == (text is None):
        raise TranscriptWorkflowError("--input-file 与 --text 必须且只能提供一个")
    root = root.resolve()
    if workspace_dir is None:
        output_root = (output_root or root / "outputs" / WORKFLOW / "runs").resolve()
        output_root.mkdir(parents=True, exist_ok=True)
    if input_file is not None:
        source_path = input_file.resolve()
        try:
            raw = read_text_file(source_path)
        except TextInputError as exc:
            raise TranscriptWorkflowError(str(exc)) from exc
        source_name = source_path.name
        input_kind = "file"
    else:
        raw = str(text)
        source_path = None
        source_name = "input.txt"
        input_kind = "text"
    normalized = normalize_text(raw)

    workspace = create_workspace(
        root,
        output_root,
        workspace_dir=workspace_dir,
        run_id=workspace_run_id,
        layout_profile="slides-video" if workspace_dir is not None else "standalone",
    )
    init_lock = root / "logs" / WORKFLOW / "initialize.lock"
    with project_lock(init_lock, f"{WORKFLOW}:initialize"):
        run_id = workspace.run_id
        output_dir = workspace.transcript_dir
        inputs_dir = workspace.inputs_dir
        generated_dir = output_dir / "generated"
        copied = inputs_dir / source_name
        if source_path is not None:
            shutil.copy2(source_path, copied)
        else:
            copied.write_text(normalized, encoding="utf-8", newline="\n")
        metadata = input_metadata(copied)
        metadata.update({"kind": input_kind, "source_path": str(source_path) if source_path else None})
        write_json(inputs_dir / "input-metadata.json", metadata)
        now = iso_timestamp()
        state = {
            "schema_version": "1.0",
            "workflow": WORKFLOW,
            "run_id": run_id,
            "status": "prepared",
            "current_stage": "prepared",
            "resume_stage": None,
            "current_object_id": None,
            "current_batch_id": None,
            "completed_steps": ["input_copied"],
            "pending_decisions": [],
            "error": None,
            "created_at": now,
            "updated_at": now,
            "last_heartbeat_at": now,
            "event_sequence": 0,
            "output_dir": str(output_dir),
            "workspace_dir": str(workspace.root),
            "input": {
                "kind": input_kind,
                "source_path": str(source_path) if source_path else None,
                "copied_path": str(copied),
                "file_sha256": file_sha256(copied),
                "normalized_text_sha256": metadata["normalized_text_sha256"],
            },
            "rules_version": RULES_VERSION,
            "lineage": lineage or {},
        }
        store = state_store(root, run_id)
        store.create(state)

        manifest_store = _manifest(root, state)
        manifest = manifest_store.create(
            workflow=WORKFLOW, run_id=run_id, status="prepared", lineage=lineage
        )
        manifest_store.upsert(
            manifest,
            artifact_entry(root, copied, role="source_copy", sources=[]),
        )
        state = store.transition(
            state,
            "deterministic_transform",
            stage="deterministic_transform",
            completed_step="prepared",
        )
        try:
            scan = scan_text(normalized)
        except SpeechPauseMarkerError as exc:
            state = store.pause(
                state,
                status="paused_error",
                error_code="invalid_pause_marker",
                message=str(exc),
                resume_stage="deterministic_transform",
            )
            manifest_store.set_status(manifest_store.load(), state["status"])
            update_component(
                workspace,
                "transcript",
                status=state["status"],
                manifest_path=_paths(state)["manifest"],
            )
            return status(root=root, run_id=run_id)
        scan.update({"run_id": run_id, "source_sha256": state["input"]["file_sha256"]})
        state["pause_markers"] = marker_summary(scan["preserved_directives"])
        paths = _paths(state)
        write_json(paths["scan"], scan)
        write_json(paths["decisions"], _default_decisions(run_id, scan))
        manifest = manifest_store.load()
        manifest_store.upsert(
            manifest,
            artifact_entry(root, paths["scan"], role="rule_scan", schema_version="1.0", sources=["source_copy"]),
        )
        manifest = manifest_store.load()
        manifest_store.upsert(
            manifest,
            artifact_entry(root, paths["decisions"], role="semantic_decisions", schema_version="1.0", sources=["rule_scan"]),
        )
        state = store.transition(
            state,
            "awaiting_semantic_decisions",
            stage="awaiting_semantic_decisions",
            completed_step="rule_scan_completed",
        )
        state = store.checkpoint(
            state,
            event="semantic_decisions_requested",
            updates={
                "pending_decisions": [
                    {
                        "type": "semantic_decisions",
                        "path": str(paths["decisions"]),
                        "candidate_count": scan["summary"]["semantic_candidates"],
                    }
                ]
            },
        )
        manifest_store.set_status(manifest_store.load(), state["status"])
        update_component(
            workspace,
            "transcript",
            status=state["status"],
            manifest_path=paths["manifest"],
        )
    if not scan["semantic_candidates"]:
        return apply_decisions(root=root, run_id=run_id)
    return status(root=root, run_id=run_id)


def _load_and_verify_input(root: Path, run_id: str) -> tuple[WorkflowStateStore, dict[str, Any], dict[str, Path], str]:
    store = state_store(root, run_id)
    state = store.load()
    paths = _paths(state)
    source = read_utf8(paths["source"])
    if file_sha256(paths["source"]) != state["input"]["file_sha256"]:
        raise TranscriptWorkflowError("原始输入副本哈希已变化")
    return store, state, paths, source


def apply_decisions(*, root: Path, run_id: str, decisions_file: Path | None = None) -> dict[str, Any]:
    store, state, paths, source = _load_and_verify_input(root, run_id)
    if state["status"] == "awaiting_transcript_approval":
        state = store.transition(
            state,
            "revision_required",
            stage="revision_required",
            details={"reason": "用户退回预览并重新应用语义决策"},
        )
    if state["status"] == "revision_required":
        state = store.transition(state, "awaiting_semantic_decisions", stage="awaiting_semantic_decisions")
    if state["status"] != "awaiting_semantic_decisions":
        raise CommandStateError(f"当前状态不能应用语义决策：{state['status']}")
    with store.lock():
        if decisions_file is not None:
            incoming = read_json(decisions_file.resolve())
            write_json(paths["decisions"], incoming)
        scan = read_json(paths["scan"])
        decisions = read_json(paths["decisions"])
        semantic = validate_decisions(scan, decisions, run_id)
        preview, edits = render_transcript(source, scan, semantic)
        paths["preview"].write_text(preview, encoding="utf-8", newline="\n")
        preview_hash = file_sha256(paths["preview"])
        report = render_diff_report(
            run_id,
            state["input"]["file_sha256"],
            preview_hash,
            edits,
            list(scan.get("preserved_directives") or []),
        )
        paths["diff"].write_text(report, encoding="utf-8", newline="\n")
        validate_preview(source, scan, decisions, run_id, preview)
        state = store.transition(
            state,
            "preview_ready",
            stage="preview_ready",
            completed_step="preview_rendered",
            updates={"preview_sha256": preview_hash},
        )
        manifest_store = _manifest(root, state)
        manifest = manifest_store.load()
        for role, path, sources in [
            ("semantic_decisions", paths["decisions"], ["rule_scan"]),
            ("transcript_preview", paths["preview"], ["source_copy", "semantic_decisions"]),
            ("difference_report", paths["diff"], ["transcript_preview"]),
        ]:
            manifest_store.upsert(
                manifest_store.load(), artifact_entry(root, path, role=role, sources=sources)
            )
        state = store.transition(
            state,
            "awaiting_transcript_approval",
            stage="awaiting_transcript_approval",
            completed_step="preview_validated",
        )
        state = store.checkpoint(
            state,
            event="transcript_approval_requested",
            updates={
                "pending_decisions": [
                    {
                        "type": "transcript_approval",
                        "preview_path": str(paths["preview"]),
                        "preview_sha256": preview_hash,
                        "diff_report_path": str(paths["diff"]),
                    }
                ]
            },
        )
        manifest_store.set_status(manifest_store.load(), state["status"])
        update_component(
            open_workspace(Path(state["workspace_dir"])),
            "transcript",
            status=state["status"],
            manifest_path=paths["manifest"],
        )
    return status(root=root, run_id=run_id)


def approve(*, root: Path, run_id: str, confirmed_by: str, preview_sha256: str) -> dict[str, Any]:
    store, state, paths, _ = _load_and_verify_input(root, run_id)
    if state["status"] in {"approved", "verified", "completed"}:
        receipt = read_json(paths["approval"])
        if receipt["preview_sha256"] != preview_sha256:
            raise TranscriptWorkflowError("重复 approve 的预览哈希与原确认不一致")
        return receipt
    if state["status"] != "awaiting_transcript_approval":
        raise CommandStateError(f"当前状态不能确认逐字稿：{state['status']}")
    if not confirmed_by.strip():
        raise TranscriptWorkflowError("confirmed_by 不能为空")
    actual_hash = file_sha256(paths["preview"])
    if preview_sha256 != actual_hash or preview_sha256 != state.get("preview_sha256"):
        raise TranscriptWorkflowError("预览哈希不一致，拒绝确认过期或被修改的逐字稿")
    with store.lock():
        paths["approved"].parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(paths["preview"], paths["approved"])
        approved_hash = file_sha256(paths["approved"])
        receipt = {
            "schema_version": "1.0",
            "run_id": run_id,
            "status": "approved",
            "approved_transcript_path": paths["approved"].resolve().relative_to(root.resolve()).as_posix(),
            "approved_transcript_sha256": approved_hash,
            "preview_sha256": preview_sha256,
            "confirmed_by": confirmed_by.strip(),
            "confirmed_at": iso_timestamp(),
            "rules_version": state["rules_version"],
        }
        write_json(paths["approval"], receipt)
        state = store.transition(
            state,
            "approved",
            stage="approved",
            completed_step="transcript_approved",
            updates={"approval": receipt},
        )
        manifest_store = _manifest(root, state)
        manifest_store.upsert(
            manifest_store.load(),
            artifact_entry(root, paths["approved"], role="approved_transcript", sources=["transcript_preview"]),
        )
        manifest_store.upsert(
            manifest_store.load(),
            artifact_entry(root, paths["approval"], role="approval_receipt", schema_version="1.0", sources=["approved_transcript"]),
        )
        manifest_store.set_status(manifest_store.load(), "approved")
        update_component(
            open_workspace(Path(state["workspace_dir"])),
            "transcript",
            status="approved",
            manifest_path=paths["manifest"],
        )
    return receipt


def verify(*, root: Path, run_id: str, tts_manifest: Path | None = None) -> dict[str, Any]:
    store, state, paths, source = _load_and_verify_input(root, run_id)
    if state["status"] not in {"approved", "completed"}:
        raise TranscriptWorkflowError("未确认逐字稿不能验证或完成")
    scan = read_json(paths["scan"])
    decisions = read_json(paths["decisions"])
    preview = read_utf8(paths["preview"])
    validate_preview(source, scan, decisions, run_id, preview)
    receipt = read_json(paths["approval"])
    approved_hash = file_sha256(paths["approved"])
    if approved_hash != receipt["approved_transcript_sha256"] or approved_hash != file_sha256(paths["preview"]):
        raise TranscriptWorkflowError("权威逐字稿、确认回执与预览哈希不一致")
    handoff = None
    if tts_manifest is not None:
        handoff = validate_tts_handoff(read_json(tts_manifest.resolve()), approved_hash)
        handoff["validated_at"] = iso_timestamp()
        if state["status"] != "completed":
            write_json(paths["handoff"], handoff)
    with store.lock():
        manifest_store = _manifest(root, state)
        manifest_store.verify_files(manifest_store.load())
        if state["status"] == "completed":
            return status(root=root, run_id=run_id)
        if handoff is not None:
            manifest_store.upsert(
                manifest_store.load(),
                artifact_entry(root, paths["handoff"], role="tts_handoff", schema_version="1.0", sources=["approval_receipt"]),
            )
        state = store.transition(state, "verified", stage="verified", completed_step="artifacts_verified")
        state = store.transition(state, "completed", stage="completed", completed_step="workflow_completed")
        manifest_store.set_status(manifest_store.load(), "completed")
        update_component(
            open_workspace(Path(state["workspace_dir"])),
            "transcript",
            status="completed",
            manifest_path=paths["manifest"],
        )
    if open_workspace(Path(state["workspace_dir"])).layout_profile == "education":
        publish_handoff(root, WORKFLOW, run_id, Path(state["workspace_dir"]) / "handoff.json")
    return status(root=root, run_id=run_id)


def resume(*, root: Path, run_id: str) -> dict[str, Any]:
    store = state_store(root, run_id)
    state = store.load()
    if state["status"] != "paused_error":
        raise CommandStateError(f"当前运行不在 paused_error：{state['status']}")
    with store.lock():
        pending = list(state.get("pending_decisions", []))
        state = store.resume(state)
        if pending:
            store.checkpoint(
                state,
                event="pending_decisions_restored",
                updates={"pending_decisions": pending},
            )
    return status(root=root, run_id=run_id)


def status(*, root: Path, run_id: str) -> dict[str, Any]:
    state = state_store(root.resolve(), run_id).load()
    paths = _paths(state)
    return {
        "status": state["status"],
        "run_id": run_id,
        "current_stage": state["current_stage"],
        "output_dir": state["output_dir"],
        "workspace_dir": state["workspace_dir"],
        "transcript_dir": state["output_dir"],
        "pipeline_run_id": run_id,
        "completed_steps": state["completed_steps"],
        "pending_decisions": state["pending_decisions"],
        "error": state["error"],
        "preview_path": str(paths["preview"]) if paths["preview"].is_file() else None,
        "preview_sha256": state.get("preview_sha256"),
        "approved_transcript_path": str(paths["approved"]) if paths["approved"].is_file() else None,
        "approval": state.get("approval"),
        "manifest_path": str(paths["manifest"]),
        "scan_path": str(paths["scan"]), "decisions_path": str(paths["decisions"]),
        "handoff_path": str(Path(state["workspace_dir"]) / "handoff.json"),
        "pause_markers": state.get("pause_markers", {"version": "1.0", "count": 0, "total_duration_ms": 0}),
    }


def _pause_after_error(root: Path, run_id: str, exc: Exception) -> None:
    try:
        store = state_store(root, run_id)
        state = store.load()
        if state["status"] in {"paused_error", "completed"}:
            return
        with store.lock():
            store.pause(
                state,
                status="paused_error",
                error_code="workflow_error",
                message=str(exc),
                resume_stage=state["status"],
                pending_decisions=list(state.get("pending_decisions", [])),
            )
    except Exception:
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="将各种文案转换成需要人工确认的可朗读逐字稿")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--root", type=Path, required=True)
    inputs = prepare_parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--input-file", type=Path)
    inputs.add_argument("--text")
    prepare_parser.add_argument("--output-root", type=Path)
    prepare_parser.add_argument("--workspace-dir", type=Path)
    prepare_parser.add_argument("--workspace-run-id")
    for command in ("apply-decisions", "approve", "resume", "verify", "status"):
        child = subparsers.add_parser(command)
        child.add_argument("--root", type=Path, required=True)
        child.add_argument("--run-id", required=True)
        if command == "apply-decisions":
            child.add_argument("--decisions-file", type=Path)
        elif command == "approve":
            child.add_argument("--confirmed-by", required=True)
            child.add_argument("--preview-sha256", required=True)
        elif command == "verify":
            child.add_argument("--tts-manifest", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = args.root.resolve()
    try:
        if args.command == "prepare":
            result = prepare(
                root=root,
                input_file=args.input_file,
                text=args.text,
                output_root=args.output_root,
                workspace_dir=args.workspace_dir,
                workspace_run_id=args.workspace_run_id,
            )
        elif args.command == "apply-decisions":
            result = apply_decisions(root=root, run_id=args.run_id, decisions_file=args.decisions_file)
        elif args.command == "approve":
            result = approve(
                root=root,
                run_id=args.run_id,
                confirmed_by=args.confirmed_by,
                preview_sha256=args.preview_sha256,
            )
        elif args.command == "resume":
            result = resume(root=root, run_id=args.run_id)
        elif args.command == "verify":
            result = verify(root=root, run_id=args.run_id, tts_manifest=args.tts_manifest)
        else:
            result = status(root=root, run_id=args.run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (TranscriptWorkflowError, WorkflowStateError, ValueError, OSError, json.JSONDecodeError) as exc:
        run_id = getattr(args, "run_id", "")
        if run_id and args.command not in {"verify", "status"} and not isinstance(exc, CommandStateError):
            _pause_after_error(root, run_id, exc)
        print(json.dumps(structured_error_receipt(exc, failure_run_id=run_id), ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
