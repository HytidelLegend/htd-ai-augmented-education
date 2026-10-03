from __future__ import annotations

import importlib.util
import json
import shutil
from hashlib import sha256
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts/run_speech_to_text.py"
spec = importlib.util.spec_from_file_location("run_speech_to_text", RUNNER_PATH)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
from utils.scripts.artifact_manifest import ArtifactManifestStore
from utils.scripts.volcengine_asr import (
    Hotword,
    VolcengineAsrError,
    normalize_asr_response,
    parse_hotwords,
    query_recognition,
    submit_recognition,
)
from utils.scripts.volcengine_streaming_asr import build_audio_request, build_full_request, parse_response


def test_request_schema_and_parser_interfaces() -> None:
    request = runner.validate_request({"schema_version": "1.0", "input_path": "a.mp4"})
    assert request["input_path"] == "a.mp4"
    parser = runner.parser()
    assert parser.parse_args(["run", "--root", ".", "--input", "a.mp4"]).command == "run"
    assert parser.parse_args(["approve-transcript", "--root", ".", "--run-dir", "x", "--confirmed-by", "u", "--transcript-sha256", "a" * 64, "--timestamps-sha256", "b" * 64]).command == "approve-transcript"


def test_normalized_asr_output_uses_shared_timestamp_contract() -> None:
    response = {
        "audio_info": {"duration": 1600},
        "result": {
            "utterances": [{
                "text": "你好，世界。",
                "words": [
                    {"text": "你好", "start_time": 100, "end_time": 600},
                    {"text": "世界", "start_time": 800, "end_time": 1500},
                ],
            }]
        },
    }
    transcript, timestamps = runner.normalize_asr_response(response)
    assert transcript == "你好，世界。"
    assert timestamps["schema_version"] == "1.1"
    assert timestamps["items"] == [
        {"text": "你好，", "start_ms": 100, "end_ms": 600, "sentence_index": 1},
        {"text": "世界。", "start_ms": 800, "end_ms": 1500, "sentence_index": 1},
    ]


def test_hotwords_are_utf8_validated_deduplicated_and_weighted(tmp_path: Path) -> None:
    path = tmp_path / "hotwords.txt"
    path.write_text("火山语音|8\nAskUserQuestion\n火山语音|8\n", encoding="utf-8", newline="\n")
    assert parse_hotwords(path) == [Hotword("火山语音", 8), Hotword("AskUserQuestion", 4)]


def test_async_submit_sends_corpus_hotwords_and_redacts_credentials(tmp_path: Path) -> None:
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake mp3")
    captured: dict[str, object] = {}

    def transport(url: str, body: bytes, headers: dict[str, str], timeout: int):
        captured.update(url=url, body=json.loads(body), headers=headers, timeout=timeout)
        return (
            {"audio_info": {"duration": 1000}, "result": {"text": "测试", "utterances": []}},
            {"x-api-status-code": "20000000", "x-api-message": "OK", "x-tt-logid": "log-1"},
        )

    task_id, metadata = submit_recognition(
        audio,
        api_key="TEST_SECRET_KEY",
        endpoint="https://example.test/submit",
        resource_id="volc.seedasr.auc",
        timeout_seconds=10,
        max_attempts=1,
        hotwords=[Hotword("火山语音", 8)],
        transport=transport,
    )
    assert json.loads(captured["body"]["request"]["corpus"]["context"]) == {"hotwords": [{"word": "火山语音"}]}
    assert captured["headers"]["X-Api-Key"] == "TEST_SECRET_KEY"
    assert "TEST_SECRET_KEY" not in json.dumps(metadata)
    assert task_id == metadata["request_id"]


def test_async_submit_does_not_retry_non_retryable_provider_error(tmp_path: Path) -> None:
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake mp3")
    attempts = 0

    def transport(url: str, body: bytes, headers: dict[str, str], timeout: int):
        nonlocal attempts
        attempts += 1
        raise VolcengineAsrError("resource not granted")

    with pytest.raises(VolcengineAsrError, match="resource not granted"):
        submit_recognition(
            audio,
            api_key="TEST_SECRET_KEY",
            endpoint="https://example.test/submit",
            resource_id="volc.seedasr.auc",
            timeout_seconds=10,
            max_attempts=3,
            transport=transport,
        )
    assert attempts == 1


def test_async_query_polls_queued_and_processing_until_complete() -> None:
    statuses = iter(
        [
            ({}, {"x-api-status-code": "20000002", "x-api-message": "queued"}),
            ({}, {"x-api-status-code": "20000001", "x-api-message": "processing"}),
            (
                {"audio_info": {"duration": 1000}, "result": {"text": "测试", "utterances": []}},
                {"x-api-status-code": "20000000", "x-api-message": "OK", "x-tt-logid": "log-2"},
            ),
        ]
    )

    def transport(url: str, body: bytes, headers: dict[str, str], timeout: int):
        return next(statuses)

    response, metadata = query_recognition(
        "task-1",
        api_key="TEST_SECRET_KEY",
        endpoint="https://example.test/query",
        resource_id="volc.seedasr.auc",
        request_timeout_seconds=10,
        poll_interval_seconds=1,
        poll_timeout_seconds=30,
        max_attempts=1,
        transport=transport,
        sleep=lambda seconds: None,
    )
    assert response["result"]["text"] == "测试"
    assert metadata["polls"] == 3
    assert metadata["log_id"] == "log-2"


def test_streaming_protocol_builds_and_parses_frames() -> None:
    frame = build_full_request({"model_name": "bigmodel"})
    assert frame[:4] == bytes([0x11, 0x10, 0x11, 0x00])
    audio = build_audio_request(b"pcm", is_last=True)
    assert audio[:4] == bytes([0x11, 0x22, 0x01, 0x00])

    import gzip
    import struct

    payload = gzip.compress(json.dumps({"is_last_package": True, "result": {"utterances": []}}).encode())
    response = bytes([0x11, 0x90, 0x11, 0x00]) + struct.pack(">I", len(payload)) + payload
    parsed = parse_response(response)
    assert parsed["type"] == "result"
    assert parsed["data"]["is_last_package"] is True


def test_approval_copies_outputs_and_completes(tmp_path: Path) -> None:
    schema_dir = tmp_path / "utils/references"
    schema_dir.mkdir(parents=True)
    shutil.copyfile(runner.ROOT / "utils/references/artifact-manifest-v1.schema.json", schema_dir / "artifact-manifest-v1.schema.json")
    run_id, run_dir = runner._new_run(tmp_path, None)
    paths = runner._paths(run_dir)
    transcript = run_dir / "generated/transcript.txt"
    timestamps = run_dir / "generated/full.timestamps.json"
    transcript.write_text("测试。", encoding="utf-8", newline="\n")
    timestamps.write_text(json.dumps({
        "schema_version": "1.1",
        "time_unit": "ms",
        "interval_semantics": "[start_ms, end_ms)",
        "duration_ms": 1000,
        "items": [{"text": "测试。", "start_ms": 0, "end_ms": 800, "sentence_index": 1}],
        "sentences": [{"text": "测试。", "start_ms": 0, "end_ms": 800, "item_range": {"start": 0, "end": 1}}],
    }), encoding="utf-8")
    state = {
        "schema_version": "1.0", "workflow": "run-speech-to-text", "run_id": run_id,
        "project_root": str(tmp_path), "run_dir": str(run_dir), "status": "awaiting_transcript_approval",
        "current_stage": "awaiting_transcript_approval", "resume_stage": None,
        "current_object_id": None, "current_batch_id": None, "completed_steps": [],
        "pending_decisions": [], "error": None, "event_sequence": 0,
        "created_at": runner.iso_timestamp(), "updated_at": runner.iso_timestamp(),
        "last_heartbeat_at": runner.iso_timestamp(),
    }
    runner._save_state(paths["state"], state)
    ArtifactManifestStore(root=tmp_path, path=run_dir / "manifest.json").create(
        workflow="run-speech-to-text",
        run_id=run_id,
        status="awaiting_transcript_approval",
    )
    result = runner.approve_transcript(
        tmp_path,
        str(run_dir),
        "tester",
        sha256(transcript.read_bytes()).hexdigest(),
        sha256(timestamps.read_bytes()).hexdigest(),
    )
    assert result["status"] == "completed"
    assert (run_dir / "approved/transcript.txt").read_text(encoding="utf-8") == "测试。"
    assert (run_dir / "approved/full.timestamps.json").is_file()
