"""Run deterministic speech-to-text jobs with durable approval checkpoints."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'utils/scripts/timestamp.py').is_file())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.scripts.artifact_manifest import ArtifactManifestStore
from utils.scripts.caption_timeline import CaptionTimelineError, build_caption_events
from utils.scripts.ffmpeg_plan import find_ffmpeg
from utils.scripts.media_probe import probe
from utils.scripts.structured_io import read_json, structured_error_receipt
from utils.scripts.run_state import write_json
from utils.scripts.subprocess_runner import run_command
from utils.scripts.timestamp import iso_timestamp, unique_filename_timestamp
from utils.scripts.project_env import project_env
from utils.scripts.file_transaction import project_lock
from utils.scripts.workflow_state import WorkflowDefinition, WorkflowStateStore
from utils.scripts.speech_hotwords import merge_hotwords
from utils.scripts.speech_handoff import publish_handoff
from utils.scripts.volcengine_asr import (
    normalize_asr_response,
    parse_hotwords,
    query_recognition,
    read_env_value,
    submit_recognition,
)
from utils.scripts.volcengine_streaming_asr import recognize_file

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

SKILL_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = SKILL_DIR / "config.yaml"
REQUEST_SCHEMA_PATH = SKILL_DIR / "references" / "request.schema.json"
OUTPUT_SCHEMA_PATH = SKILL_DIR / "references" / "output.schema.json"
RUNS_RELATIVE = Path("outputs") / "run-speech-to-text" / "runs"
SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".mp3", ".wav", ".m4a", ".flac"}


class SpeechToTextError(ValueError):
    """Raised when a speech-to-text job cannot proceed safely."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_path(root: Path, raw: str) -> Path:
    candidate = Path(raw)
    return (candidate if candidate.is_absolute() else root / candidate).resolve()


def load_config() -> dict[str, Any]:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if config.get("schema_version") != 1:
        raise SpeechToTextError("run-speech-to-text config schema_version 必须为 1")
    asr = config.get("asr")
    if not isinstance(asr, dict):
        raise SpeechToTextError("run-speech-to-text config 缺少 asr")
    required = {"provider", "implementation", "default_hotwords_path", "credential_env", "max_duration_ms", "max_audio_bytes", "audio", "implementations"}
    missing = sorted(required - set(asr))
    if missing:
        raise SpeechToTextError(f"ASR 配置缺少字段：{', '.join(missing)}")
    if asr["provider"] != "volcengine":
        raise SpeechToTextError("当前只支持 volcengine")
    if asr["implementation"] not in {"streaming", "recording_file"}:
        raise SpeechToTextError("ASR implementation 必须为 streaming 或 recording_file")
    implementations = asr["implementations"]
    if not isinstance(implementations, dict) or not all(name in implementations for name in ("streaming", "recording_file")):
        raise SpeechToTextError("ASR 配置必须同时包含 streaming 和 recording_file")
    return config


def validate_request(request: Any) -> dict[str, Any]:
    errors = sorted(
        Draft202012Validator(read_json(REQUEST_SCHEMA_PATH)).iter_errors(request),
        key=lambda item: list(item.path),
    )
    if errors:
        raise SpeechToTextError("请求不符合 schema：" + "；".join(error.message for error in errors))
    return request


def _paths(run_dir: Path) -> dict[str, Path]:
    project = next(parent.parent for parent in run_dir.parents if parent.name == "outputs")
    log_dir = project / "logs/run-speech-to-text/runs" / run_dir.name
    request_path = log_dir / "inputs" / "request.json"
    saved_request = read_json(request_path) if request_path.is_file() else {}
    config_path = log_dir / "inputs/skill-config.yaml"
    frozen_config = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.is_file() else load_config()
    implementation = str(saved_request.get("implementation") or frozen_config["asr"]["implementation"])
    audio_name = "asr-audio.pcm" if implementation == "streaming" else "asr-audio.mp3"
    return {
        "request": log_dir / "inputs" / "request.json",
        "config": log_dir / "inputs" / "skill-config.yaml",
        "source": next(log_dir.glob("inputs/source.*"), log_dir / "inputs/source.bin"),
        "metadata": log_dir / "inputs/input-metadata.json",
        "hotwords": log_dir / "inputs/hotwords.txt",
        "audio": log_dir / "generated" / audio_name,
        "raw_response": log_dir / "raw" / "response.json",
        "stream_events": log_dir / "raw" / "stream-events.jsonl",
        "task": log_dir / "raw" / "task.json",
        "request_metadata": log_dir / "attempts" / "api-request-metadata.json",
        "transcript": run_dir / "generated" / "transcript.txt",
        "timestamps": run_dir / "generated" / "full.timestamps.json",
        "correction_template": log_dir / "generated" / "correction-template.json",
        "corrections": log_dir / "approved" / "corrections.json",
        "approved_transcript": run_dir / "approved" / "transcript.txt",
        "approved_timestamps": run_dir / "approved" / "full.timestamps.json",
        "approval": run_dir / "approved" / "transcript-approval.json",
        "audio_command": log_dir / "attempts" / "asr-audio-command.json",
        "audio_log": log_dir / "attempts" / "asr-audio.log",
        "verification": log_dir / "qa" / "verification.json",
        "manifest": run_dir / "manifest.json",
        "state": log_dir / "state.json",
    }


ASR_STAGES = (
    "initialized", "input_validated", "audio_extracted", "asr_submitting", "asr_submitted",
    "asr_polling", "asr_connecting", "asr_initialized", "asr_streaming", "asr_receiving",
    "asr_stream_completed", "asr_responded", "transcript_normalized", "awaiting_transcript_approval",
    "transcript_approved", "verifying", "completed")
_transitions = {
    "initialized": {"input_validated"}, "input_validated": {"audio_extracted"},
    "audio_extracted": {"asr_submitting", "asr_connecting", "asr_responded", "asr_polling"},
    "asr_submitting": {"asr_submitted", "asr_polling"}, "asr_submitted": {"asr_polling"},
    "asr_polling": {"asr_responded"}, "asr_connecting": {"asr_initialized", "asr_connecting"},
    "asr_initialized": {"asr_streaming", "asr_connecting"}, "asr_streaming": {"asr_receiving", "asr_connecting"},
    "asr_receiving": {"asr_stream_completed", "asr_connecting"}, "asr_stream_completed": {"asr_responded"},
    "asr_responded": {"transcript_normalized"}, "transcript_normalized": {"awaiting_transcript_approval"},
    "awaiting_transcript_approval": {"transcript_approved"}, "transcript_approved": {"verifying"},
    "verifying": {"completed"}, "completed": set(), "paused_error": set(ASR_STAGES[:-1]),
}
for _stage in ASR_STAGES[:-1]:
    _transitions[_stage].add("paused_error")
ASR_DEFINITION = WorkflowDefinition.build(name="run-speech-to-text", transitions=_transitions)


def _state_store(state: dict) -> WorkflowStateStore:
    root = Path(state["project_root"])
    return WorkflowStateStore(root=root, workflow="run-speech-to-text", run_id=state["run_id"],
                              definition=ASR_DEFINITION,
                              schema_path=ROOT / "utils/references/workflow-state-v1.schema.json",
                              events_dir=root / "logs/run-speech-to-text/runs" / state["run_id"] / "events")


def _save_state(path: Path, state: dict[str, Any]) -> None:
    store = _state_store(state)
    if not path.exists():
        store.create(state)
    else:
        store.checkpoint(state, event="checkpoint")


def _checkpoint_advance(state_path: Path, state: dict[str, Any], stage: str, step: str | None = None) -> None:
    if step and step in state["completed_steps"] and state["status"] != stage:
        return
    store = _state_store(state)
    if state["status"] == stage:
        store.checkpoint(state, event="stage_checkpoint", completed_step=step)
    else:
        store.transition(state, stage, stage=stage, completed_step=step)


def _new_run(root: Path, output_root: Path | None) -> tuple[str, Path]:
    base = (output_root or (root / RUNS_RELATIVE)).resolve()
    try:
        base.relative_to(root.resolve())
    except ValueError as exc:
        raise SpeechToTextError("output_root 必须位于项目根目录内") from exc
    if base != (root / RUNS_RELATIVE).resolve():
        raise SpeechToTextError("output_root 必须是 outputs/run-speech-to-text/runs")
    base.mkdir(parents=True, exist_ok=True)
    log_root = root / "logs/run-speech-to-text/runs"
    with project_lock(root / "logs/run-speech-to-text/initialize.lock", "asr:initialize"):
        existing = [item.name.removeprefix("speech_to_text_")
                    for parent in (base, log_root) for item in parent.glob("speech_to_text_*")]
        run_id = f"speech_to_text_{unique_filename_timestamp(existing)}"
        run_dir = base / run_id
        for name in ("generated", "approved"):
            (run_dir / name).mkdir(parents=True, exist_ok=False)
        for name in ("inputs", "generated", "raw", "approved", "attempts", "qa"):
            (log_root / run_id / name).mkdir(parents=True, exist_ok=False)
    return run_id, run_dir


def _run_dir(root: Path, run_id: str | None, run_dir_arg: str | None) -> Path:
    if run_dir_arg:
        path = resolve_path(root, run_dir_arg)
        if path.parent != (root / RUNS_RELATIVE).resolve():
            raise SpeechToTextError("run_dir 必须位于本项目 outputs/run-speech-to-text/runs")
        if not _paths(path)["state"].is_file():
            raise SpeechToTextError(f"运行不存在：{path}")
        return path
    if not run_id or not run_id.startswith("speech_to_text_"):
        raise SpeechToTextError("必须提供合法的 --run-id 或 --run-dir")
    path = (root / RUNS_RELATIVE / run_id).resolve()
    if path.parent != (root / RUNS_RELATIVE).resolve() or not path.is_dir():
        raise SpeechToTextError(f"运行不存在：{run_id}")
    return path


def _validate_input(root: Path, request: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    config = yaml.safe_load(_paths(run_dir)["config"].read_text(encoding="utf-8"))
    input_path = resolve_path(root, request["input_path"])
    if input_path.suffix.lower() not in SUPPORTED_EXTENSIONS or not input_path.is_file():
        raise SpeechToTextError("输入必须是存在且扩展名受支持的音视频文件")
    paths = _paths(run_dir)
    if paths["metadata"].is_file() and file_sha256(input_path) != read_json(paths["metadata"])["source_sha256"]:
        raise SpeechToTextError("输入音视频在运行期间发生变化；请新建运行")
    if paths["manifest"].is_file():
        manifest_store = ArtifactManifestStore(root=root, path=paths["manifest"])
        manifest_store.verify_files(manifest_store.load())
    info = probe(input_path, log_path=paths["audio_log"].parent / "ffprobe-input.log")
    duration_ms = info.get("duration_ms")
    if not isinstance(duration_ms, int) or duration_ms <= 0:
        raise SpeechToTextError("无法确定输入媒体时长")
    asr = config["asr"]
    if duration_ms > int(asr["max_duration_ms"]):
        raise SpeechToTextError("输入媒体超过 5 小时限制")
    audio_count = sum(item.get("type") == "audio" for item in info.get("streams", []))
    if audio_count < 1:
        raise SpeechToTextError("输入媒体不包含音轨")
    credential = str(asr["credential_env"])
    if not project_env(root, credential):
        raise SpeechToTextError(f"项目根目录 .env 缺少 {credential}")
    hotword_source = resolve_path(
        root,
        str(request.get("hotwords_path") or asr["default_hotwords_path"]),
    )
    snapshot = _paths(run_dir)["hotwords"]
    if snapshot.is_file():
        values = parse_hotwords(snapshot)
        sources = read_json(snapshot.with_suffix(".sources.json"))
    else:
        values, sources = merge_hotwords(root / asr["default_hotwords_path"], hotword_source)
    return {
        "input_path": input_path,
        "duration_ms": duration_ms,
        "audio_count": audio_count,
        "hotword_source": hotword_source,
        "hotwords": values, "hotword_sources": sources,
        "info": info,
    }


def _extract_audio(root: Path, input_path: Path, run_dir: Path) -> Path:
    paths = _paths(run_dir)
    if paths["audio"].is_file() and paths["audio"].stat().st_size > 0:
        return paths["audio"]
    frozen = yaml.safe_load(paths["config"].read_text(encoding="utf-8"))["asr"]
    config = frozen["audio"]
    request = read_json(paths["request"])
    implementation = str(request.get("implementation") or frozen["implementation"])
    command = [
        find_ffmpeg(), "-v", "error", "-y", "-i", input_path, "-vn",
        "-ac", str(config["channels"]), "-ar", str(config["sample_rate"]),
    ]
    if implementation == "streaming":
        command.extend(["-c:a", "pcm_s16le", "-f", "s16le", paths["audio"]])
    else:
        command.extend(["-c:a", str(config["codec"]), "-b:a", str(config["bitrate"]), paths["audio"]])
    write_json(paths["audio_command"], {"args": [str(item) for item in command]})
    run_command(command, timeout_seconds=3600, log_path=paths["audio_log"])
    if not paths["audio"].is_file() or paths["audio"].stat().st_size == 0:
        raise SpeechToTextError("未生成有效的 ASR 音频")
    if paths["audio"].stat().st_size > int(frozen["max_audio_bytes"]):
        raise SpeechToTextError("提取后的音频超过 512 MB 限制")
    return paths["audio"]


def _write_normalized(response: dict[str, Any], run_dir: Path) -> None:
    paths = _paths(run_dir)
    transcript, timestamps = normalize_asr_response(response)
    paths["transcript"].write_text(transcript, encoding="utf-8", newline="\n")
    write_json(paths["timestamps"], timestamps)
    write_json(paths["correction_template"], {
        "schema_version": "1.0",
        "source_sha256": file_sha256(paths["timestamps"]),
        "corrections": [],
    })


def _record(store: ArtifactManifestStore, root: Path, path: Path, role: str, schema: str | int | None = None) -> None:
    from utils.scripts.artifact_manifest import artifact_entry
    entry = artifact_entry(root, path, role=role, schema_version=schema)
    manifest = store.load()
    store.upsert(manifest, entry)


def _run_asr(root: Path, request: dict[str, Any], run_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    paths = _paths(run_dir)
    store = ArtifactManifestStore(root=root, path=paths["manifest"])
    state_path = paths["state"]
    asr_config = yaml.safe_load(paths["config"].read_text(encoding="utf-8"))["asr"]
    implementation = str(request.get("implementation") or asr_config["implementation"])
    config = asr_config["implementations"][implementation]
    validation = _validate_input(root, request, run_dir)
    input_path = validation["input_path"]
    shutil.copyfile(input_path, paths["source"])
    paths["metadata"].write_text(
        json.dumps({
            "source_path": str(input_path),
            "source_sha256": file_sha256(input_path),
            "duration_ms": validation["duration_ms"],
            "audio_count": validation["audio_count"],
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    if not paths["hotwords"].is_file():
        paths["hotwords"].write_text("".join(f"{item.word}|{item.weight}\n" for item in validation["hotwords"]), encoding="utf-8", newline="\n")
        write_json(paths["hotwords"].with_suffix(".sources.json"), validation["hotword_sources"])
    for path, role in ((paths["hotwords"], "speech_hotwords"),
                       (paths["hotwords"].with_suffix(".sources.json"), "speech_hotword_sources"), (paths["source"], "speech_source"),
                       (paths["metadata"], "speech_input_metadata")):
        _record(store, root, path, role)
    _checkpoint_advance(state_path, state, "input_validated", "input_validated")
    audio = _extract_audio(root, input_path, run_dir)
    _record(store, root, audio, "speech_audio")
    _checkpoint_advance(state_path, state, "audio_extracted", "audio_extracted")
    if paths["raw_response"].is_file():
        response = read_json(paths["raw_response"])
        # The response can reach disk before its state transition on a crash.
        if implementation == "streaming" and state["status"] == "asr_receiving":
            _checkpoint_advance(state_path, state, "asr_stream_completed", "asr_stream_completed")
    elif implementation == "streaming":
        api_key: str = project_env(root, str(asr_config["credential_env"]))
        if not api_key:
            raise SpeechToTextError(f"{asr_config['credential_env']} 不能为空")
        _checkpoint_advance(state_path, state, "asr_connecting")
        _checkpoint_advance(state_path, state, "asr_initialized", "asr_initialized")
        _checkpoint_advance(state_path, state, "asr_streaming", "asr_streaming")
        response, events = recognize_file(
            audio,
            api_key=api_key,
            endpoint=str(config["endpoint"]),
            resource_id=str(config["resource_id"]),
            timeout_seconds=int(config["request_timeout_seconds"]),
            chunk_duration_ms=int(config["chunk_duration_ms"]),
            send_interval_seconds=float(config["send_interval_seconds"]),
            hotwords=validation["hotwords"],
            language=str(request.get("language", "zh-CN")),
            enable_nonstream=bool(config["enable_nonstream"]),
            result_type=str(config["result_type"]),
        )
        _checkpoint_advance(state_path, state, "asr_receiving", "asr_receiving")
        paths["stream_events"].write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n", encoding="utf-8", newline="\n")
        write_json(paths["raw_response"], response)
        write_json(paths["request_metadata"], {"implementation": implementation, "endpoint": config["endpoint"], "resource_id": config["resource_id"], "event_count": len(events), "recovery": "restart_from_beginning"})
        _checkpoint_advance(state_path, state, "asr_stream_completed", "asr_stream_completed")
    else:
        api_key: str = project_env(root, str(asr_config["credential_env"]))
        assert api_key
        if paths["task"].is_file():
            task = read_json(paths["task"])
            task_id = str(task["task_id"])
            submit_metadata = task["submit_metadata"]
        else:
            _checkpoint_advance(state_path, state, "asr_submitting")
            request_id = str(uuid.uuid4())
            write_json(paths["task"], {"task_id": request_id, "submit_metadata": {"submission_uncertain": True}})
            task_id, submit_metadata = submit_recognition(
                audio,
                api_key=api_key,
                endpoint=str(config["submit_endpoint"]),
                resource_id=str(config["resource_id"]),
                timeout_seconds=int(config["request_timeout_seconds"]),
                max_attempts=int(config["max_attempts"]),
                hotwords=validation["hotwords"],
                language=str(request.get("language", "zh-CN")),
                request_id=request_id,
            )
            write_json(paths["task"], {"task_id": task_id, "submit_metadata": submit_metadata})
            _checkpoint_advance(state_path, state, "asr_submitted", "asr_submitted")
        _checkpoint_advance(state_path, state, "asr_polling")
        response, query_metadata = query_recognition(
            task_id,
            api_key=api_key,
            endpoint=str(config["query_endpoint"]),
            resource_id=str(config["resource_id"]),
            request_timeout_seconds=int(config["request_timeout_seconds"]),
            poll_interval_seconds=float(config["poll_interval_seconds"]),
            poll_timeout_seconds=int(config["poll_timeout_seconds"]),
            max_attempts=int(config["max_attempts"]),
        )
        write_json(paths["raw_response"], response)
        write_json(paths["request_metadata"], {"submit": submit_metadata, "query": query_metadata})
    _checkpoint_advance(state_path, state, "asr_responded", "asr_responded")
    _write_normalized(response, run_dir)
    _checkpoint_advance(state_path, state, "transcript_normalized", "transcript_normalized")
    for path, role, schema in (
        (paths["source"], "speech_source", None),
        (paths["metadata"], "speech_input_metadata", "1.0"),
        (paths["hotwords"], "speech_hotwords", None),
        (audio, "speech_audio", None),
        (paths["raw_response"], "speech_raw_response", None),
        (paths["task"], "speech_task", "1.0"),
        (paths["stream_events"], "speech_stream_events", "1.0"),
        (paths["request_metadata"], "speech_request_metadata", "1.0"),
        (paths["transcript"], "speech_transcript", None),
        (paths["timestamps"], "speech_timestamps", "1.1"),
        (paths["correction_template"], "speech_correction_template", "1.0"),
    ):
        if path.is_file():
            _record(store, root, path, role, schema)
    receipt = {
        "type": "speech_to_text_transcript_approval",
        "transcript_path": str(paths["transcript"]),
        "timestamps_path": str(paths["timestamps"]),
        "transcript_sha256": file_sha256(paths["transcript"]),
        "timestamps_sha256": file_sha256(paths["timestamps"]),
        "correction_template": str(paths["correction_template"]),
    }
    state["pending_decisions"] = [receipt]
    _checkpoint_advance(state_path, state, "awaiting_transcript_approval")
    store.set_status(store.load(), "awaiting_transcript_approval")
    return {"status": state["status"], "run_id": state["run_id"], "run_dir": str(run_dir), **receipt}


def _apply_corrections(root: Path, run_dir: Path, submitted: dict[str, Any]) -> dict[str, Any]:
    paths = _paths(run_dir)
    schema = read_json(root / "utils/references/asr-corrections-v1.schema.json")
    errors = sorted(Draft202012Validator(schema).iter_errors(submitted), key=lambda item: list(item.path))
    if errors:
        raise SpeechToTextError("纠错文件不符合 schema：" + "；".join(error.message for error in errors))
    source = read_json(paths["timestamps"])
    if submitted["source_sha256"] != file_sha256(paths["timestamps"]):
        raise SpeechToTextError("纠错文件来源哈希不一致")
    items = deepcopy(source["items"])
    previous_end = 0
    for patch in sorted(submitted["corrections"], key=lambda item: (item["item_start"], item["item_end"])):
        start, end = int(patch["item_start"]), int(patch["item_end"])
        if start < previous_end or end <= start or end > len(items):
            raise SpeechToTextError("纠错 item 范围重叠、为空或越界")
        if len({int(item["sentence_index"]) for item in items[start:end]}) != 1:
            raise SpeechToTextError("单条纠错不能跨越句子边界")
        replacement = str(patch["replacement_text"]).strip()
        if not replacement or "\n" in replacement or "\r" in replacement:
            raise SpeechToTextError("replacement_text 必须是非空单行文本")
        previous_end = end
    for patch in reversed(sorted(submitted["corrections"], key=lambda item: (item["item_start"], item["item_end"]))):
        start, end = int(patch["item_start"]), int(patch["item_end"])
        items[start:end] = [{
            "text": str(patch["replacement_text"]).strip(),
            "start_ms": int(items[start]["start_ms"]),
            "end_ms": int(items[end - 1]["end_ms"]),
            "sentence_index": int(items[start]["sentence_index"]),
        }]
    sentences = []
    cursor = 0
    for sentence_id in sorted({int(item["sentence_index"]) for item in items}):
        group = [item for item in items if int(item["sentence_index"]) == sentence_id]
        sentences.append({
            "text": "".join(str(item["text"]) for item in group),
            "start_ms": int(group[0]["start_ms"]),
            "end_ms": int(group[-1]["end_ms"]),
            "item_range": {"start": cursor, "end": cursor + len(group)},
        })
        cursor += len(group)
    corrected = {**source, "items": items, "sentences": sentences, "corrections_applied": len(submitted["corrections"])}
    paths["transcript"].write_text("\n".join(item["text"] for item in sentences), encoding="utf-8", newline="\n")
    write_json(paths["timestamps"], corrected)
    write_json(paths["corrections"], submitted)
    store = ArtifactManifestStore(root=root, path=paths["manifest"])
    _record(store, root, paths["transcript"], "speech_transcript")
    _record(store, root, paths["timestamps"], "speech_timestamps", "1.1")
    _record(store, root, paths["corrections"], "speech_corrections", "1.0")
    return {"corrections_applied": len(submitted["corrections"])}


def run_request(root: Path, request: dict[str, Any], *, on_initialized=None) -> dict[str, Any]:
    validate_request(request)
    output_root = resolve_path(root, request["output_root"]) if request.get("output_root") else None
    run_id, run_dir = _new_run(root, output_root)
    paths = _paths(run_dir)
    state = {
        "schema_version": "1.0", "workflow": "run-speech-to-text", "event_sequence": 0, "current_batch_id": None, "run_id": run_id, "run_dir": str(run_dir),
        "project_root": str(root.resolve()), "status": "initialized",
        "current_stage": "initialized", "resume_stage": "initialized",
        "current_object_id": None, "completed_steps": [], "pending_decisions": [],
        "error": None, "created_at": iso_timestamp(), "updated_at": iso_timestamp(),
        "last_heartbeat_at": iso_timestamp(),
    }
    write_json(paths["request"], request)
    frozen_config = deepcopy(load_config())
    frozen_config["asr"]["implementation"] = request.get("implementation") or frozen_config["asr"]["implementation"]
    paths["config"].write_text(yaml.safe_dump(frozen_config, allow_unicode=True, sort_keys=False), encoding="utf-8", newline="\n")
    _save_state(paths["state"], state)
    store = ArtifactManifestStore(root=root, path=paths["manifest"])
    store.create(workflow="run-speech-to-text", run_id=run_id, status="initialized")
    _record(store, root, paths["request"], "speech_request", "1.0")
    _record(store, root, paths["config"], "speech_skill_config", 1)
    if on_initialized:
        on_initialized(run_id, run_dir)
    try:
        result = _run_asr(root, request, run_dir, state)
        _save_state(paths["state"], state)
        return result
    except Exception as exc:
        _state_store(state).pause(state, status="paused_error", error_code="asr_error", message=str(exc), resume_stage=state["current_stage"])
        raise


def submit_corrections(root: Path, run_dir_arg: str, corrections_file: Path) -> dict[str, Any]:
    run_dir = _run_dir(root, None, run_dir_arg)
    paths = _paths(run_dir)
    state = read_json(paths["state"])
    if state.get("status") != "awaiting_transcript_approval":
        raise SpeechToTextError("当前运行不在 awaiting_transcript_approval")
    verify_run(root, str(run_dir))
    result = _apply_corrections(root, run_dir, read_json(corrections_file.resolve()))
    receipt = {
        "type": "speech_to_text_transcript_approval",
        "transcript_path": str(paths["transcript"]),
        "timestamps_path": str(paths["timestamps"]),
        "transcript_sha256": file_sha256(paths["transcript"]),
        "timestamps_sha256": file_sha256(paths["timestamps"]),
        **result,
    }
    state["pending_decisions"] = [receipt]
    _save_state(paths["state"], state)
    return {"status": state["status"], "run_id": state["run_id"], "run_dir": str(run_dir), **receipt}


def approve_transcript(root: Path, run_dir_arg: str, confirmed_by: str, transcript_sha256: str, timestamps_sha256: str) -> dict[str, Any]:
    run_dir = _run_dir(root, None, run_dir_arg)
    paths = _paths(run_dir)
    state = read_json(paths["state"])
    if state.get("status") != "awaiting_transcript_approval":
        raise SpeechToTextError("当前运行不在 awaiting_transcript_approval")
    if not confirmed_by.strip():
        raise SpeechToTextError("confirmed_by 不能为空")
    verify_run(root, str(run_dir))
    actual_transcript = file_sha256(paths["transcript"])
    actual_timestamps = file_sha256(paths["timestamps"])
    if (transcript_sha256, timestamps_sha256) != (actual_transcript, actual_timestamps):
        raise SpeechToTextError("逐字稿或时间戳 SHA-256 与提交值不一致")
    shutil.copyfile(paths["transcript"], paths["approved_transcript"])
    shutil.copyfile(paths["timestamps"], paths["approved_timestamps"])
    receipt = {
        "schema_version": "1.0", "confirmed_by": confirmed_by, "confirmed_at": iso_timestamp(),
        "transcript_sha256": actual_transcript, "timestamps_sha256": actual_timestamps,
        "transcript_path": str(paths["approved_transcript"]),
        "timestamps_path": str(paths["approved_timestamps"]),
    }
    write_json(paths["approval"], receipt)
    store = ArtifactManifestStore(root=root, path=paths["manifest"])
    _record(store, root, paths["corrections"], "speech_corrections", "1.0") if paths["corrections"].is_file() else None
    _record(store, root, paths["approved_transcript"], "approved_transcript")
    _record(store, root, paths["approved_timestamps"], "approved_timestamps", "1.1")
    _record(store, root, paths["approval"], "transcript_approval", "1.0")
    state["pending_decisions"] = []
    _checkpoint_advance(paths["state"], state, "transcript_approved", "transcript_approved")
    _checkpoint_advance(paths["state"], state, "verifying")
    verification = verify_run(root, run_dir_arg)
    write_json(paths["verification"], verification)
    _record(store, root, paths["verification"], "speech_verification", "1.0")
    store.set_status(store.load(), "completed")
    store.verify_files(store.load())
    _checkpoint_advance(paths["state"], state, "completed", "verified")
    publish_handoff(root, "run-speech-to-text", state["run_id"], run_dir / "handoff.json")
    return {"status": "completed", "run_id": state["run_id"], "run_dir": str(run_dir), **receipt}


def resume_run(root: Path, run_dir_arg: str) -> dict[str, Any]:
    run_dir = _run_dir(root, None, run_dir_arg)
    state = read_json(_paths(run_dir)["state"])
    if state.get("status") == "completed":
        verify_run(root, run_dir_arg)
        return {"status": "completed", "run_id": state["run_id"], "run_dir": str(run_dir)}
    if state.get("status") == "awaiting_transcript_approval":
        return {"status": state["status"], "run_id": state["run_id"], "run_dir": str(run_dir), "pending_decisions": state["pending_decisions"]}
    request = read_json(_paths(run_dir)["request"])
    if state["status"] == "paused_error":
        _state_store(state).resume(state)
    if state["status"] in {"transcript_approved", "verifying"}:
        if state["status"] == "transcript_approved":
            _checkpoint_advance(_paths(run_dir)["state"], state, "verifying")
        verify_run(root, run_dir_arg)
        manifest_store = ArtifactManifestStore(root=root, path=_paths(run_dir)["manifest"])
        manifest_store.set_status(manifest_store.load(), "completed")
        _checkpoint_advance(_paths(run_dir)["state"], state, "completed", "verified")
        publish_handoff(root, "run-speech-to-text", state["run_id"], run_dir / "handoff.json")
        return {"status": "completed", "run_id": state["run_id"], "run_dir": str(run_dir)}
    try:
        result = _run_asr(root, request, run_dir, state)
        _save_state(_paths(run_dir)["state"], state)
        return result
    except Exception as exc:
        _state_store(state).pause(state, status="paused_error", error_code="asr_error", message=str(exc), resume_stage=state["current_stage"])
        raise


def verify_run(root: Path, run_dir_arg: str) -> dict[str, Any]:
    run_dir = _run_dir(root, None, run_dir_arg)
    paths = _paths(run_dir)
    state = read_json(paths["state"])
    if paths["manifest"].is_file():
        manifest_store = ArtifactManifestStore(root=root, path=paths["manifest"])
        manifest_store.verify_files(manifest_store.load())
    approved = state.get("status") in {"verifying", "completed"}
    if approved and not paths["approval"].is_file():
        raise SpeechToTextError("completed 运行缺少逐字稿批准回执")
    if approved:
        approval = read_json(paths["approval"])
        if (approval.get("transcript_sha256") != file_sha256(paths["approved_transcript"])
                or approval.get("timestamps_sha256") != file_sha256(paths["approved_timestamps"])):
            raise SpeechToTextError("批准稿或时间戳与确认回执哈希不一致")
    if approved and (not paths["approved_transcript"].is_file() or not paths["approved_timestamps"].is_file()):
        raise SpeechToTextError("completed 运行缺少 approved 逐字稿或时间戳")
    transcript_path = paths["approved_transcript"] if approved else paths["transcript"]
    timestamps_path = paths["approved_timestamps"] if approved else paths["timestamps"]
    if not transcript_path.is_file() or not timestamps_path.is_file():
        raise SpeechToTextError("缺少标准逐字稿或时间戳")
    transcript = transcript_path.read_text(encoding="utf-8")
    timestamps = read_json(timestamps_path)
    Draft202012Validator(read_json(OUTPUT_SCHEMA_PATH)).validate(timestamps)
    if not timestamps.get("items") or not timestamps.get("sentences"):
        raise SpeechToTextError("时间戳 items 和 sentences 不能为空")
    try:
        build_caption_events(transcript, timestamps, max_chars=10**9)
    except CaptionTimelineError as exc:
        raise SpeechToTextError(f"逐字稿与词级时间戳校验失败：{exc}") from exc
    if paths["manifest"].is_file():
        store = ArtifactManifestStore(root=root, path=paths["manifest"])
        store.verify_files(store.load())
    return {
        "status": "verified",
        "run_id": state["run_id"],
        "transcript_sha256": file_sha256(transcript_path),
        "timestamps_sha256": file_sha256(timestamps_path),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="将音频或视频转换为带时间戳的逐字稿")
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-request")
    init.add_argument("--file", required=True)
    run = commands.add_parser("run")
    run.add_argument("--root", required=True)
    run.add_argument("--input")
    run.add_argument("--request")
    run.add_argument("--output-root")
    run.add_argument("--hotwords-file")
    run.add_argument("--implementation", choices=("streaming", "recording_file"))
    for name in ("resume", "status", "verify", "submit-corrections", "approve-transcript"):
        child = commands.add_parser(name)
        child.add_argument("--root", required=True)
        child.add_argument("--run-dir", required=True)
    commands.choices["submit-corrections"].add_argument("--corrections", required=True)
    approve = commands.choices["approve-transcript"]
    approve.add_argument("--confirmed-by", required=True)
    approve.add_argument("--transcript-sha256", required=True)
    approve.add_argument("--timestamps-sha256", required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "init-request":
            target = Path(args.file).resolve()
            if target.exists():
                raise SpeechToTextError(f"请求文件已存在：{target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text((SKILL_DIR / "assets" / "request.template.json").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
            receipt = {"status": "created", "request_path": str(target)}
        else:
            root = Path(args.root).resolve()
            if args.command == "run":
                if args.request:
                    request = validate_request(read_json(Path(args.request).resolve()))
                else:
                    request = {"schema_version": "1.0", "input_path": args.input}
                    if args.output_root:
                        request["output_root"] = args.output_root
                    if args.hotwords_file:
                        request["hotwords_path"] = args.hotwords_file
                    if args.implementation:
                        request["implementation"] = args.implementation
                    receipt = run_request(root, request)
                if args.request:
                    receipt = run_request(root, request)
            elif args.command == "resume":
                receipt = resume_run(root, args.run_dir)
            elif args.command == "status":
                receipt = read_json(_paths(_run_dir(root, None, args.run_dir))["state"])
            elif args.command == "verify":
                receipt = verify_run(root, args.run_dir)
            elif args.command == "submit-corrections":
                receipt = submit_corrections(root, args.run_dir, Path(args.corrections))
            else:
                receipt = approve_transcript(root, args.run_dir, args.confirmed_by, args.transcript_sha256, args.timestamps_sha256)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        error = structured_error_receipt(exc)
        if getattr(exc, "retryable", False):
            error["error_code"] = "runtime_error"
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
