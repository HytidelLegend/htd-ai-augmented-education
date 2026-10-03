from __future__ import annotations

import base64
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import wave

import pytest

from utils.scripts.file_transaction import _process_is_alive
from utils.scripts.tts_handoff import validate_tts_handoff


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_tts.py"
SPEC = importlib.util.spec_from_file_location("run_text_to_speech", SCRIPT)
assert SPEC and SPEC.loader
tts = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tts
SPEC.loader.exec_module(tts)

CONVERT_SCRIPT = tts.DEFAULT_PROJECT_DIR / "skills/convert-copy-to-transcript/scripts/convert_copy_to_transcript.py"
CONVERT_SPEC = importlib.util.spec_from_file_location("convert_copy_for_tts_tests", CONVERT_SCRIPT)
assert CONVERT_SPEC and CONVERT_SPEC.loader
converter = importlib.util.module_from_spec(CONVERT_SPEC)
sys.path.insert(0, str(CONVERT_SCRIPT.parent))
CONVERT_SPEC.loader.exec_module(converter)


@pytest.fixture(autouse=True)
def project_schemas(tmp_path: Path, monkeypatch):
    references = tmp_path / "utils" / "references"
    references.mkdir(parents=True, exist_ok=True)
    source = tts.DEFAULT_PROJECT_DIR / "utils" / "references"
    for name in ("workflow-state-v1.schema.json", "artifact-manifest-v1.schema.json"):
        shutil.copy2(source / name, references / name)
    assets = tmp_path / "utils" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    shutil.copy2(tts.DEFAULT_PROJECT_DIR / "utils" / "assets" / "white-noise.wav", assets)
    real_load_config = tts.load_config

    def load_without_preview(path=None):
        config = copy.deepcopy(real_load_config(path))
        config["backend"] = "volcengine"
        config["preview"]["enabled"] = False
        return config

    monkeypatch.setattr(tts, "load_config", load_without_preview)
    return real_load_config


def approve_transcript(root: Path, run_id: str) -> None:
    tts_state = root / "logs" / tts.WORKFLOW / "runs" / run_id / "state.json"
    if tts_state.is_file():
        workspace = tts.open_workspace(Path(tts.read_json(tts_state)["workspace_dir"]))
        run_id = tts.read_json(workspace.pipeline_manifest)["components"]["transcript"]["producer_run_id"]
    current = converter.status(root=root, run_id=run_id)
    if current["status"] == "awaiting_semantic_decisions":
        current = converter.apply_decisions(root=root, run_id=run_id)
    if current["status"] == "awaiting_transcript_approval":
        converter.approve(
            root=root,
            run_id=run_id,
            confirmed_by="pytest-user-confirmation",
            preview_sha256=current["preview_sha256"],
        )
    converter.verify(root=root, run_id=run_id)


def completed_transcript_workspace(root: Path, text: str) -> Path:
    receipt = converter.prepare(root=root, input_file=None, text=text)
    approve_transcript(root, receipt["run_id"])
    return Path(receipt["workspace_dir"])


def run_after_transcript_approval(argv: list[str], capsys) -> int:
    exit_code = tts.main(argv)
    first = json.loads(capsys.readouterr().out)
    if first.get("status") != "paused_transcript_not_ready":
        print(json.dumps(first, ensure_ascii=False))
        return exit_code
    root = Path(argv[argv.index("--root") + 1])
    approve_transcript(root, first["run_id"])
    return tts.main(["resume", "--root", str(root), "--run-id", first["run_id"]])


def make_wav(duration_ms: int = 250, sample_rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * round(sample_rate * duration_ms / 1000))
    return buffer.getvalue()


def test_changed_noise_asset_after_preflight_blocks_publication(tmp_path, monkeypatch, capsys):
    original = tts.validate_input_checkpoint
    def changed_asset(state):
        result = original(state)
        if state["status"] == "adding_white_noise":
            (tmp_path / "utils/assets/white-noise.wav").write_bytes(b"TEST changed asset")
        return result
    monkeypatch.setattr(tts, "validate_input_checkpoint", changed_asset)
    assert run_after_transcript_approval(["run", "--root", str(tmp_path), "--text", "素材完整性测试。", "--mock"], capsys) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "paused_verification"
    manifest = tts.read_json(Path(receipt["output_dir"]) / "manifest.json")
    assert manifest["status"] != "completed"


def test_white_noise_is_mixed_only_into_sustained_silence(tmp_path: Path) -> None:
    sample_rate = 24000
    voiced_frames = round(sample_rate * 0.2)
    silent_frames = round(sample_rate * 0.3)
    clean = tmp_path / "clean.wav"
    mixed = tmp_path / "mixed.wav"
    with wave.open(str(clean), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\xe8\x03" * voiced_frames)
        output.writeframes(b"\x00\x00" * silent_frames)
        output.writeframes(b"\xe8\x03" * voiced_frames)
    result = tts.add_white_noise_bed(
        clean,
        tts.DEFAULT_PROJECT_DIR / "utils" / "assets" / "white-noise.wav",
        mixed,
        target_rms_dbfs=-70.0,
        sample_rate=sample_rate,
        audio_format="wav",
        silence_threshold_dbfs=-60.0,
        minimum_silence_ms=120,
        detection_window_ms=10,
        fade_ms=15,
        mp3_bitrate_kbps=96,
    )
    with wave.open(str(clean), "rb") as source:
        clean_pcm = source.readframes(source.getnframes())
    with wave.open(str(mixed), "rb") as source:
        mixed_pcm = source.readframes(source.getnframes())
    first_end = voiced_frames * 2
    silence_end = (voiced_frames + silent_frames) * 2
    assert mixed_pcm[:first_end] == clean_pcm[:first_end]
    assert mixed_pcm[silence_end:] == clean_pcm[silence_end:]
    assert mixed_pcm[first_end:silence_end] != clean_pcm[first_end:silence_end]
    assert result["mode"] == "silence_only"
    assert result["silence_interval_count"] == 1


def test_default_speed_and_effective_batch_range() -> None:
    config = tts.load_config()
    assert config["audio"]["speech_rate"] == 10
    assert tts.effective_batch_range(config, 10) == (88, 132)
    assert tts.effective_batch_range(config, 0) == (80, 120)


def test_cloned_voice_uses_icl_resource_by_default() -> None:
    assert tts.resolve_voice("S_example", None, tts.load_config()) == (
        "S_example",
        "S_example",
        "seed-icl-2.0",
    )


def test_default_voice_is_hytidel_normal_speech() -> None:
    assert tts.resolve_voice(None, None, tts.load_config()) == (
        "Hytidel（正常说话）",
        "S_IBxS5KRZ1",
        "seed-icl-2.0",
    )


def test_output_only_uses_approved_transcript_and_default_voice(tmp_path: Path, capsys) -> None:
    workspace = completed_transcript_workspace(tmp_path, "默认音色测试。")
    assert tts.main(
        ["run", "--root", str(tmp_path), "--output-root", str(workspace), "--mock"]
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    manifest = tts.read_json(Path(receipt["output_dir"]) / "manifest.json")
    assert manifest["speaker"] == "Hytidel（正常说话）"
    assert manifest["speaker_id"] == "S_IBxS5KRZ1"
    assert manifest["resource_id"] == "seed-icl-2.0"
    assert manifest["input"]["copied_path"].endswith("transcript\\approved\\transcript.txt")


def test_slides_video_workspace_reads_transcript_and_writes_tts(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "outputs/convert-copy-to-transcript/runs/20260825T120000"
    workspace.mkdir(parents=True)
    transcript = converter.prepare(
        root=tmp_path,
        input_file=None,
        text="统一 pipeline 工作区。",
        workspace_dir=workspace,
        workspace_run_id="20260825T120000",
    )
    approve_transcript(tmp_path, transcript["run_id"])
    assert tts.main(
        ["run", "--root", str(tmp_path), "--workspace-dir", str(workspace), "--mock"]
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "completed"
    assert Path(receipt["output_dir"]).parent != workspace
    assert (Path(receipt["output_dir"]) / "manifest.json").is_file()
    assert not (workspace / "speech" / "manifest.json").exists()


def test_input_mismatch_pauses_before_api_and_can_resume_with_correction(
    tmp_path: Path, capsys
) -> None:
    source = "一致的原始文案。"
    workspace = completed_transcript_workspace(tmp_path, source)
    assert tts.main(
        [
            "run",
            "--root",
            str(tmp_path),
            "--output-root",
            str(workspace),
            "--text",
            "不一致的文案。",
            "--mock",
        ]
    ) == 0
    paused = json.loads(capsys.readouterr().out)
    assert paused["status"] == "paused_input_mismatch"
    assert not (workspace / "speech" / "full_clean.wav").exists()
    assert tts.main(
        [
            "resume",
            "--root",
            str(tmp_path),
            "--run-id",
            paused["run_id"],
            "--text",
            source,
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"


def test_input_and_output_path_cannot_both_be_missing(tmp_path: Path, capsys) -> None:
    assert tts.main(["run", "--root", str(tmp_path), "--mock"]) == 1
    receipt = json.loads(capsys.readouterr().out)
    assert "至少必须提供一个" in receipt["error"]
    assert not (tmp_path / "tmp").exists()


def test_input_only_creates_workspace_and_waits_for_transcript(tmp_path: Path, capsys) -> None:
    assert tts.main(
        ["run", "--root", str(tmp_path), "--text", "等待逐字稿确认。", "--mock"]
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "paused_transcript_not_ready"
    workspace = Path(receipt["output_dir"]).parent
    assert workspace.name == receipt["run_id"]
    assert (tts.open_workspace(workspace).inputs_dir / "input.txt").is_file()
    assert not (workspace / "speech" / "full_clean.wav").exists()


def test_preview_pauses_requires_explicit_approval_and_reuses_batches(
    tmp_path: Path, capsys, monkeypatch, project_schemas
) -> None:
    def load_with_preview(path=None):
        config = copy.deepcopy(project_schemas(path))
        config["preview"] = {"enabled": True, "duration_seconds": 0.1}
        return config

    monkeypatch.setattr(tts, "load_config", load_with_preview)
    workspace = completed_transcript_workspace(
        tmp_path,
        "第一句用于试听。第二句继续合成。第三句完成测试。",
    )
    assert tts.main(
        ["run", "--root", str(tmp_path), "--workspace-dir", str(workspace), "--mock"]
    ) == 0
    paused = json.loads(capsys.readouterr().out)
    assert paused["status"] == "paused_preview_approval"
    assert paused["next_action"] == "approve-preview-or-retry-preview"
    assert Path(paused["preview"]["audio_path"]).is_file()
    state = tts.state_store(tmp_path, paused["run_id"]).load()
    preview_attempts = sum(int(batch["attempts"]) for batch in state["batches"])
    assert preview_attempts >= 1

    assert tts.main(["resume", "--root", str(tmp_path), "--run-id", paused["run_id"]]) == 1
    assert "approve-preview" in json.loads(capsys.readouterr().out)["error"]

    assert tts.main(
        [
            "approve-preview",
            "--root",
            str(tmp_path),
            "--run-id",
            paused["run_id"],
            "--preview-sha256",
            paused["preview"]["audio_sha256"],
            "--confirmed-by",
            "pytest-user",
        ]
    ) == 0
    completed = json.loads(capsys.readouterr().out)
    assert completed["status"] == "completed"
    final_state = tts.state_store(tmp_path, paused["run_id"]).load()
    assert sum(int(batch["attempts"]) for batch in final_state["batches"]) >= preview_attempts
    assert final_state["preview"]["approval"]["status"] == "approved"


def test_preview_retry_creates_versioned_output_in_same_run(
    tmp_path: Path, capsys, monkeypatch, project_schemas
) -> None:
    def load_with_preview(path=None):
        config = copy.deepcopy(project_schemas(path))
        config["preview"] = {"enabled": True, "duration_seconds": 0.1}
        return config

    monkeypatch.setattr(tts, "load_config", load_with_preview)
    workspace = completed_transcript_workspace(tmp_path, "第一版试听。第二句内容。")
    assert tts.main(
        ["run", "--root", str(tmp_path), "--workspace-dir", str(workspace), "--mock"]
    ) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["preview"]["version"] == 1
    assert tts.main(
        [
            "retry-preview",
            "--root",
            str(tmp_path),
            "--run-id",
            first["run_id"],
            "--reason",
            "调整语速",
            "--speech-rate",
            "20",
        ]
    ) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["status"] == "paused_preview_approval"
    assert second["run_id"] == first["run_id"]
    assert second["preview"]["version"] == 2
    assert Path(second["preview"]["audio_path"]).parent.name == "v002"


def test_normalize_items_supports_icl_camel_case_seconds() -> None:
    items = tts.normalize_items(
        [{"words": [{"word": "测", "startTime": 0.125, "endTime": 0.375}]}]
    )
    assert items == [{"text": "测", "start_ms": 125, "end_ms": 375}]


def test_split_batches_uses_natural_pauses_and_preserves_text() -> None:
    text = "第一段内容，" + "甲" * 30 + "。第二段内容，" + "乙" * 30 + "。"
    batches = tts.split_batches(
        text,
        minimum=20,
        maximum=38,
        priority=["paragraph", "sentence_end", "question", "exclamation", "semicolon", "colon", "comma"],
    )
    assert "".join(batch["text"] for batch in batches) == text
    assert len(batches) >= 2
    assert all(batch["text"][-1] in "，。" for batch in batches[:-1])


def test_real_newlines_are_preserved_as_paragraph_boundaries() -> None:
    text = "第一段。\r\n\r\n第二段。"
    normalized = tts.validate_inline_text_transport(text)
    assert normalized == "第一段。\n\n第二段。"
    assert (6, "paragraph") in tts.find_boundaries(normalized)


def test_synthesis_text_removes_layout_newlines_and_adds_missing_stop() -> None:
    text = (
        "上大学千万不要好好学习，一定要极致地功利。\n\n"
        "今天分享 X 个大学的认知、潜规则和信息差\n\n"
        "前两个小节讲背景。\n"
    )
    assert tts.normalize_synthesis_text(text) == (
        "上大学千万不要好好学习，一定要极致地功利。"
        "今天分享 X 个大学的认知、潜规则和信息差。"
        "前两个小节讲背景。"
    )


def test_synthesis_map_tracks_paragraphs_and_batches() -> None:
    source = "第一段没有句号\n\n第二段有句号。"
    synthesis, mapping = tts.build_synthesis_text_and_map(source)
    assert synthesis == "第一段没有句号。第二段有句号。"
    assert mapping["paragraphs"][0]["inserted_suffix"] == "。"
    batches = [
        {"index": 1, "batch_id": "batch_001", "text": synthesis[:10]},
        {"index": 2, "batch_id": "batch_002", "text": synthesis[10:]},
    ]
    tts.annotate_synthesis_map(mapping, batches, synthesis)
    assert mapping["paragraphs"][0]["batch_ids"] == ["batch_001"]
    assert mapping["paragraphs"][1]["batch_ids"] == ["batch_001", "batch_002"]
    assert batches[0]["paragraph_ids"] == ["paragraph_001", "paragraph_002"]
    assert tts.validate_synthesis_map(mapping, source, synthesis, batches) == []


def test_synthesis_map_removes_pause_markers_and_preserves_anchors() -> None:
    source = "{{pause:100ms}}甲{{pause:200ms}}{{pause:300ms}}乙\n{{pause:400ms}}\n丙。{{pause:500ms}}"
    synthesis, mapping = tts.build_synthesis_text_and_map(source)
    assert synthesis == "甲乙。丙。"
    assert [item["duration_ms"] for item in mapping["pauses"]] == [100, 200, 300, 400, 500]
    assert [item["synthesis_offset"] for item in mapping["pauses"]] == [0, 1, 1, 3, 5]
    batches = tts.split_batches_with_pauses(
        synthesis,
        mapping["pauses"],
        minimum=1,
        maximum=20,
        priority=["sentence_end", "comma"],
    )
    for index, batch in enumerate(batches, 1):
        batch["batch_id"] = f"batch_{index:03d}"
    tts.annotate_synthesis_map(mapping, batches, synthesis)
    assert "{{pause:" not in "".join(item["text"] for item in batches)
    assert tts.validate_synthesis_map(mapping, source, synthesis, batches) == []


def test_timing_diagnostics_reports_long_unconfigured_gap() -> None:
    diagnostics = tts.build_timing_diagnostics(
        [
            {"text": "甲", "start_ms": 0, "end_ms": 100},
            {"text": "乙", "start_ms": 900, "end_ms": 1000},
        ],
        duration_ms=1200,
        long_gap_warning_ms=700,
    )
    assert diagnostics["item_duration_ms"] == 200
    assert diagnostics["positive_gap_ms"] == 800
    assert diagnostics["measured_silence_ms"] == 1000
    assert diagnostics["long_gaps"][0]["duration_ms"] == 800
    assert diagnostics["warnings"]


def test_timing_diagnostics_excludes_explicit_pause_from_long_gap_warning() -> None:
    diagnostics = tts.build_timing_diagnostics(
        [
            {"text": "甲", "start_ms": 0, "end_ms": 100},
            {"text": "乙", "start_ms": 1100, "end_ms": 1200},
        ],
        duration_ms=1200,
        long_gap_warning_ms=700,
        explicit_pauses=[{"start_ms": 100, "end_ms": 1100, "duration_ms": 1000}],
    )
    assert diagnostics["explicit_pause_ms"] == 1000
    assert diagnostics["long_gaps"] == []
    assert diagnostics["warnings"] == []


@pytest.mark.parametrize("sequence", ["`n`n", "`r`n", "\\n\\n", "\\r\\n"])
def test_inline_text_rejects_literal_shell_newline_escapes(
    tmp_path: Path, capsys, sequence: str
) -> None:
    exit_code = run_after_transcript_approval(
        [
            "run",
            "--root",
            str(tmp_path),
            "--speaker",
            "mock-speaker",
            "--text",
            f"第一段。{sequence}第二段。",
            "--mock",
        ],
        capsys,
    )
    assert exit_code == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "error"
    assert "改用 --input-file" in receipt["error"]
    assert not (tmp_path / "tmp").exists()


def test_payload_exposes_speed_and_timestamp_interfaces() -> None:
    payload = tts.build_payload(
        "测试文本。",
        "speaker-id",
        {
            "format": "mp3",
            "sample_rate": 24000,
            "speech_rate": 20,
            "loudness_rate": 0,
            "emotion": None,
            "emotion_scale": 4,
        },
    )
    audio = payload["req_params"]["audio_params"]
    assert audio["speech_rate"] == 20
    assert audio["enable_timestamp"] is True
    assert audio["enable_subtitle"] is True


def test_api_client_works_with_mock_transport() -> None:
    wav = make_wav()
    events = [{"items": [{"text": "测", "start_ms": 0, "end_ms": 250}]}]

    def transport(url, headers, payload, timeout):
        assert url.startswith("https://")
        assert headers["X-Api-Key"] == "mock-key"
        assert payload["req_params"]["audio_params"]["speech_rate"] == 10
        assert timeout > 0
        yield json.dumps(
            {"code": 0, "data": base64.b64encode(wav).decode("ascii"), "sentence": events[0]}
        ).encode("utf-8")
        yield json.dumps({"code": 20000000}).encode("utf-8")

    client = tts.VolcengineClient("mock-key", tts.load_config(), transport=transport)
    audio, raw_events = client.synthesize(
        "测", "speaker-id", "seed-tts-2.0", tts.load_config()["audio"]
    )
    assert audio == wav
    assert raw_events == events


def test_mock_cli_text_input_end_to_end_without_network(tmp_path: Path, monkeypatch, capsys) -> None:
    def network_must_not_run(*args, **kwargs):
        raise AssertionError("mock 测试不得访问网络")

    monkeypatch.setattr(tts, "urllib_transport", network_must_not_run)
    monkeypatch.setattr(tts, "read_env_value", network_must_not_run)
    text = ("这是第一句话，用于测试批次切分和字词时间戳。" * 8) + "这是最后一句。"
    exit_code = run_after_transcript_approval(
        [
            "run",
            "--root",
            str(tmp_path),
            "--speaker",
            "mock-speaker",
            "--text",
            text,
            "--mock",
        ],
        capsys,
    )
    assert exit_code == 0
    receipt = json.loads(capsys.readouterr().out)
    output_dir = Path(receipt["output_dir"])
    assert (tts.open_workspace(output_dir.parent).inputs_dir / "input.txt").read_text(encoding="utf-8") == text
    synthesis_path = tts.open_workspace(output_dir.parent).logs_dir / "generated" / "synthesis.txt"
    assert synthesis_path.read_text(encoding="utf-8") == text
    assert (output_dir / "full_clean.wav").stat().st_size > 0
    assert (output_dir / "full_with_white_noise.wav").stat().st_size > 0
    with wave.open(str(output_dir / "full_with_white_noise.wav"), "rb") as mixed:
        assert any(mixed.readframes(mixed.getnframes()))
    timeline = json.loads((output_dir / "full.timestamps.json").read_text(encoding="utf-8"))
    assert timeline["audio_file"] == "full_with_white_noise.wav"
    assert timeline["clean_audio_file"] == "full_clean.wav"
    assert timeline["items"]
    assert timeline["sentences"]
    assert len(timeline["batches"]) >= 2
    assert timeline["diagnostics"]["item_duration_ms"] > 0
    assert isinstance(timeline["diagnostics"]["warnings"], list)
    assert receipt["verification"]["status"] == "passed"
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 4
    assert manifest["full_audio"].endswith("full_with_white_noise.wav")
    assert manifest["clean_audio"]["path"].endswith("full_clean.wav")
    assert manifest["white_noise"]["status"] == "completed"
    assert manifest["white_noise"]["volume_dbfs"] == -70.0
    assert manifest["white_noise"]["mode"] == "silence_only"
    assert manifest["white_noise"]["silence_interval_count"] >= 1
    handoff = validate_tts_handoff(
        output_dir,
        transcript_sha256=manifest["input"]["file_sha256"],
    )
    assert handoff["paths"]["audio"].endswith("full_with_white_noise.wav")
    assert manifest["mock"] is True
    assert manifest["audio"]["format"] == "wav"
    assert manifest["audio"]["speech_rate"] == 10
    assert manifest["input"]["synthesis_path"] == str(synthesis_path)
    assert manifest["diagnostics"] == timeline["diagnostics"]
    state = tts.state_store(tmp_path, receipt["run_id"]).load()
    assert state["status"] == "completed"
    assert state["current_stage"] == "completed"
    assert state["current_object_id"] is None
    assert state["current_batch_id"] is None
    assert state["last_heartbeat_at"]
    assert all(batch["status"] == "verified" for batch in state["batches"])
    assert "verification_passed" in state["completed_steps"]


def test_white_noise_can_be_disabled(tmp_path: Path, monkeypatch, capsys) -> None:
    config = tts.load_config()
    config["white_noise"]["enabled"] = False
    monkeypatch.setattr(tts, "load_config", lambda path=None: config)
    assert run_after_transcript_approval(
        ["run", "--root", str(tmp_path), "--text", "禁用白噪音。", "--mock"],
        capsys,
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    output = Path(receipt["output_dir"])
    manifest = tts.read_json(output / "manifest.json")
    assert (output / "full_clean.wav").is_file()
    assert not (output / "full_with_white_noise.wav").exists()
    assert manifest["full_audio"] == str(output / "full_clean.wav")
    assert manifest["white_noise"]["status"] == "skipped"


def test_mock_end_to_end_inserts_explicit_pauses_without_speaking_markers(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    def network_must_not_run(*args, **kwargs):
        raise AssertionError("mock 测试不得访问网络")

    monkeypatch.setattr(tts, "urllib_transport", network_must_not_run)
    monkeypatch.setattr(tts, "read_env_value", network_must_not_run)
    source = "{{pause:100ms}}甲。{{pause:250ms}}{{pause:350ms}}乙。{{pause:400ms}}"
    exit_code = run_after_transcript_approval(
        [
            "run",
            "--root",
            str(tmp_path),
            "--speaker",
            "mock-speaker",
            "--text",
            source,
            "--mock",
        ],
        capsys,
    )
    assert exit_code == 0
    receipt = json.loads(capsys.readouterr().out)
    output = Path(receipt["output_dir"])
    assert (tts.open_workspace(output.parent).logs_dir / "generated" / "synthesis.txt").read_text(encoding="utf-8") == "甲。乙。"
    manifest = tts.read_json(output / "manifest.json")
    assert all("{{pause:" not in batch["text"] for batch in manifest["batches"])
    timeline = tts.read_json(output / "full.timestamps.json")
    assert timeline["schema_version"] == "1.1"
    assert [item["duration_ms"] for item in timeline["pauses"]] == [100, 250, 350, 400]
    assert timeline["pauses"][0]["start_ms"] == 0
    assert timeline["pauses"][-1]["end_ms"] == timeline["duration_ms"]
    assert timeline["diagnostics"]["explicit_pause_ms"] == 1100
    assert receipt["verification"]["counts"]["pauses"] == 4


def test_file_input_is_copied_byte_for_byte(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.md"
    original = "第一行。\r\n第二行。\r\n".encode("utf-8")
    source.write_bytes(original)
    original_mtime = source.stat().st_mtime_ns
    exit_code = run_after_transcript_approval(
        [
            "run",
            "--root",
            str(tmp_path),
            "--speaker",
            "mock-speaker",
            "--input-file",
            str(source),
            "--mock",
        ],
        capsys,
    )
    assert exit_code == 0
    receipt = json.loads(capsys.readouterr().out)
    copied = tts.open_workspace(Path(receipt["output_dir"]).parent).inputs_dir / source.name
    assert source.read_bytes() == original
    assert source.stat().st_mtime_ns == original_mtime
    assert copied.read_bytes() == original
    assert (tts.open_workspace(Path(receipt["output_dir"]).parent).logs_dir / "generated" / "synthesis.txt").read_text(
        encoding="utf-8"
    ) == "第一行。第二行。"


def test_file_input_preserves_intentional_literal_escape_text(tmp_path: Path, capsys) -> None:
    source = tmp_path / "technical.txt"
    original = "Python 字符串中的 \\n 表示换行。\n"
    source.write_text(original, encoding="utf-8", newline="\n")
    assert run_after_transcript_approval(
        [
            "run",
            "--root",
            str(tmp_path),
            "--speaker",
            "mock-speaker",
            "--input-file",
            str(source),
            "--mock",
        ],
        capsys,
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    output_dir = Path(receipt["output_dir"])
    assert receipt["verification"]["status"] == "passed"
    assert (tts.open_workspace(output_dir.parent).inputs_dir / source.name).read_text(encoding="utf-8") == original
    assert (tts.open_workspace(output_dir.parent).logs_dir / "generated" / "synthesis.txt").read_text(
        encoding="utf-8"
    ) == original.rstrip("\n")


def test_verify_rejects_literal_shell_escape_in_text_manifest(tmp_path: Path, capsys) -> None:
    assert run_after_transcript_approval(
        ["run", "--root", str(tmp_path), "--speaker", "mock", "--text", "测试。", "--mock"],
        capsys,
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    output_dir = Path(receipt["output_dir"])
    manifest_path = output_dir / "manifest.json"
    manifest = tts.read_json(manifest_path)
    manifest["batches"][0]["text"] = "第一段。`n`n第二段。"
    tts.write_json(manifest_path, manifest)
    verification = tts.verify_output(output_dir)
    assert verification["status"] == "failed"
    assert any("shell 换行转义" in error for error in verification["errors"])


def test_verify_rejects_missing_timing_diagnostics(tmp_path: Path, capsys) -> None:
    assert run_after_transcript_approval(
        ["run", "--root", str(tmp_path), "--speaker", "mock", "--text", "测试。", "--mock"],
        capsys,
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    timestamps_path = Path(receipt["output_dir"]) / "full.timestamps.json"
    timestamps = tts.read_json(timestamps_path)
    timestamps.pop("diagnostics")
    tts.write_json(timestamps_path, timestamps)
    verification = tts.verify_output(Path(receipt["output_dir"]))
    assert verification["status"] == "failed"
    assert "时间戳缺少 diagnostics" in verification["errors"]


def test_verify_and_resume_interfaces(tmp_path: Path, capsys) -> None:
    assert run_after_transcript_approval(
        [
            "run",
            "--root",
            str(tmp_path),
            "--speaker",
            "mock-speaker",
            "--text",
            "测试恢复接口。这是第二句。",
            "--mock",
        ],
        capsys,
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert tts.main(
        ["verify", "--root", str(tmp_path), "--output-dir", receipt["output_dir"]]
    ) == 0
    verification = json.loads(capsys.readouterr().out)
    assert verification["status"] == "passed"
    assert tts.main(
        ["resume", "--root", str(tmp_path), "--run-id", receipt["run_id"]]
    ) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["status"] == "completed"


def test_locate_and_revision_reuses_unmodified_batches(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    source = ("前置内容用于形成稳定批次。" * 12) + "但大家也只听我说的。" + ("后续内容继续展开。" * 16)
    assert run_after_transcript_approval(
        ["run", "--root", str(tmp_path), "--speaker", "mock", "--text", source, "--mock"],
        capsys,
    ) == 0
    base_receipt = json.loads(capsys.readouterr().out)
    base_output = Path(base_receipt["output_dir"])
    base_manifest_path = base_output / "manifest.json"
    base_manifest_hash = tts.file_sha256(base_manifest_path)

    query_file = tmp_path / "query.txt"
    query_file.write_text("但大家也只听我说的", encoding="utf-8", newline="\n")
    assert tts.main(
        [
            "locate",
            "--root",
            str(tmp_path),
            "--run-id",
            base_receipt["run_id"],
            "--query-file",
            str(query_file),
        ]
    ) == 0
    located = json.loads(capsys.readouterr().out)
    assert located["candidate_count"] == 1
    candidate = located["candidates"][0]
    assert candidate["revisable"] is True

    replacement_file = tmp_path / "replacement.txt"
    replacement_file.write_text("但大家也别只听我说的", encoding="utf-8", newline="\n")
    calls: list[str] = []
    original_synthesize = tts.MockClient.synthesize

    def recording_synthesize(self, text, speaker_id, resource_id, params):
        calls.append(text)
        return original_synthesize(self, text, speaker_id, resource_id, params)

    monkeypatch.setattr(tts.MockClient, "synthesize", recording_synthesize)
    assert tts.main(
        [
            "revise",
            "--root",
            str(tmp_path),
            "--base-run-id",
            base_receipt["run_id"],
            "--replace",
            f"{candidate['match_id']}={replacement_file}",
        ]
    ) == 0
    waiting = json.loads(capsys.readouterr().out)
    assert waiting["status"] == "paused_transcript_not_ready"
    approve_transcript(tmp_path, waiting["run_id"])
    assert tts.main(
        ["resume", "--root", str(tmp_path), "--run-id", waiting["run_id"]]
    ) == 0
    revised = json.loads(capsys.readouterr().out)
    revised_output = Path(revised["output_dir"])
    assert revised_output != base_output
    assert revised["verification"]["status"] == "passed"
    assert len(calls) == 1
    assert "但大家也别只听我说的" in calls[0]
    assert tts.file_sha256(base_manifest_path) == base_manifest_hash

    manifest = tts.read_json(revised_output / "manifest.json")
    assert manifest["mode"] == "revision"
    assert len(manifest["revision"]["affected_batch_ids"]) == 1
    assert len(manifest["revision"]["reused_batch_ids"]) == len(manifest["batches"]) - 1
    assert all(batch["attempts"] == 0 for batch in manifest["batches"] if batch["origin"] == "reused")
    assert (tts.open_workspace(revised_output.parent).inputs_dir / "input.txt").read_text(encoding="utf-8").find(
        "但大家也别只听我说的"
    ) >= 0
    assert (tts.open_workspace(revised_output.parent).logs_dir / "generated" / "synthesis-map.json").is_file()
    assert tts.main(
        ["verify", "--root", str(tmp_path), "--output-dir", str(revised_output)]
    ) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed"


def test_pause_retime_reuses_all_batches(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    source = "开头。{{pause:1000ms}}中间内容。{{pause:1500ms}}结尾。"
    assert run_after_transcript_approval(
        ["run", "--root", str(tmp_path), "--text", source, "--mock"],
        capsys,
    ) == 0
    base = json.loads(capsys.readouterr().out)
    revised_source = tmp_path / "pause-v2.txt"
    revised_source.write_text(
        "开头。{{pause:500ms}}中间内容。{{pause:1000ms}}结尾。",
        encoding="utf-8",
        newline="\n",
    )
    calls: list[str] = []
    original_synthesize = tts.MockClient.synthesize

    def recording_synthesize(self, text, speaker_id, resource_id, params):
        calls.append(text)
        return original_synthesize(self, text, speaker_id, resource_id, params)

    monkeypatch.setattr(tts.MockClient, "synthesize", recording_synthesize)
    assert tts.main(
        [
            "retime-pauses",
            "--root",
            str(tmp_path),
            "--base-run-id",
            base["run_id"],
            "--input-file",
            str(revised_source),
        ]
    ) == 0
    waiting = json.loads(capsys.readouterr().out)
    assert waiting["status"] == "paused_transcript_not_ready"
    approve_transcript(tmp_path, waiting["run_id"])
    assert tts.main(
        ["resume", "--root", str(tmp_path), "--run-id", waiting["run_id"]]
    ) == 0
    revised = json.loads(capsys.readouterr().out)
    assert revised["status"] == "completed"
    assert calls == []
    manifest = tts.read_json(Path(revised["output_dir"]) / "manifest.json")
    assert manifest["mode"] == "revision"
    assert manifest["revision"]["kind"] == "pause_retime"
    assert manifest["revision"]["affected_batch_ids"] == []
    assert len(manifest["revision"]["reused_batch_ids"]) == len(manifest["batches"])
    assert all(batch["origin"] == "reused" for batch in manifest["batches"])
    assert [item["duration_ms"] for item in manifest["pauses"]] == [500, 1000]


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="需要系统 ffmpeg 和 ffprobe",
)
def test_non_wav_merge_interface_with_local_silence(tmp_path: Path) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    first.write_bytes(make_wav(200))
    second.write_bytes(make_wav(200))
    output = tmp_path / "merged.mp3"
    tts.merge_with_ffmpeg([first, second], [50, 0], output, 24000, "mp3")
    assert output.stat().st_size > 0
    duration = tts.ffprobe_duration_ms(output)
    assert 350 <= duration <= 700


def test_pcm_merge_and_duration_interfaces(tmp_path: Path) -> None:
    sample_rate = 24000
    first = tmp_path / "first.pcm"
    second = tmp_path / "second.pcm"
    first.write_bytes(b"\x00\x00" * 2400)
    second.write_bytes(b"\x00\x00" * 2400)
    output = tmp_path / "full.pcm"
    tts.merge_pcm([first, second], [50, 0], output, sample_rate)
    assert 240 <= tts.batch_audio_duration_ms(output, "pcm", sample_rate) <= 260


def test_illegal_transition_is_rejected(tmp_path: Path) -> None:
    config = tts.load_config()
    parser = tts.build_parser()
    args = parser.parse_args(
        ["start", "--root", str(tmp_path), "--speaker", "mock", "--text", "测试。", "--mock"]
    )
    state, store = tts.initialize_run(args, config)
    with store.lock():
        with pytest.raises(tts.WorkflowStateError, match="非法状态迁移"):
            store.transition(state, "completed", stage="completed")


def test_retryable_pause_then_resume_same_run(tmp_path: Path, monkeypatch, capsys) -> None:
    original = tts.MockClient.synthesize

    def fail_once(self, text, speaker_id, resource_id, params):
        raise tts.TTSRetryableError("mock timeout")

    monkeypatch.setattr(tts.MockClient, "synthesize", fail_once)
    assert run_after_transcript_approval(
        ["start", "--root", str(tmp_path), "--speaker", "mock", "--text", "第一句。第二句。", "--mock"]
        , capsys
    ) == 0
    paused = json.loads(capsys.readouterr().out)
    assert paused["status"] == "paused_retryable"
    assert paused["current_stage"] == "synthesizing"
    assert paused["next_action"] == "resume"
    run_id = paused["run_id"]
    monkeypatch.setattr(tts.MockClient, "synthesize", original)
    assert tts.main(["resume", "--root", str(tmp_path), "--run-id", run_id]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["status"] == "completed"
    state = tts.state_store(tmp_path, run_id).load()
    assert state["batches"][0]["attempts"] == 2


def test_configuration_pause_never_reads_network(tmp_path: Path, monkeypatch, capsys) -> None:
    def missing_configuration(state):
        raise tts.TTSConfigurationError("mock missing voice")

    monkeypatch.setattr(tts, "create_client", missing_configuration)
    assert run_after_transcript_approval(
        ["start", "--root", str(tmp_path), "--speaker", "mock", "--text", "配置测试。", "--mock"]
        , capsys
    ) == 0
    paused = json.loads(capsys.readouterr().out)
    assert paused["status"] == "paused_configuration"
    assert paused["error"]["code"] == "configuration_error"


def test_batch_fingerprint_change_pauses_verification_on_resume(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    original_merge = tts.merge_audio

    def interrupt_merge(*args, **kwargs):
        raise tts.TTSRetryableError("mock merge interruption")

    monkeypatch.setattr(tts, "merge_audio", interrupt_merge)
    assert run_after_transcript_approval(
        ["start", "--root", str(tmp_path), "--speaker", "mock", "--text", "指纹测试。第二句。", "--mock"]
        , capsys
    ) == 0
    paused = json.loads(capsys.readouterr().out)
    assert paused["status"] == "paused_retryable"
    store = tts.state_store(tmp_path, paused["run_id"])
    state = store.load()
    first_audio = Path(state["batches"][0]["audio"]["path"])
    first_audio.write_bytes(first_audio.read_bytes() + b"changed")
    monkeypatch.setattr(tts, "merge_audio", original_merge)
    assert tts.main(["resume", "--root", str(tmp_path), "--run-id", paused["run_id"]]) == 0
    verification_pause = json.loads(capsys.readouterr().out)
    assert verification_pause["status"] == "paused_verification"
    assert verification_pause["error"]["code"] == "verification_error"


def test_process_liveness_probe_is_safe_for_current_process() -> None:
    assert _process_is_alive(os.getpid()) is True


def test_run_lock_blocks_concurrent_same_run(tmp_path: Path) -> None:
    args = tts.build_parser().parse_args(
        ["start", "--root", str(tmp_path), "--speaker", "mock", "--text", "锁测试。", "--mock"]
    )
    state, store = tts.initialize_run(args, tts.load_config())
    assert state["status"] == "initialized"
    holder_script = tmp_path / "hold_lock.py"
    holder_script.write_text(
        """from pathlib import Path
import sys
import time

sys.path.insert(0, sys.argv[1])
from utils.scripts.file_transaction import project_lock

lock_path = Path(sys.argv[2])
ready_path = Path(sys.argv[3])
release_path = Path(sys.argv[4])
with project_lock(lock_path, "test-child"):
    ready_path.write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 15
    while not release_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
""",
        encoding="utf-8",
        newline="\n",
    )
    ready_path = tmp_path / "holder.ready"
    release_path = tmp_path / "holder.release"
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    process = subprocess.Popen(
        [
            sys.executable,
            str(holder_script),
            str(tts.DEFAULT_PROJECT_DIR),
            str(store.lock_path),
            str(ready_path),
            str(release_path),
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        shell=False,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready_path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_path.exists(), process.communicate(timeout=1)
        with pytest.raises(ValueError, match="已有事务正在执行"):
            with store.lock():
                pass
    finally:
        release_path.touch()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, (stdout, stderr)


def test_status_supersede_and_terminal_resume_interfaces(tmp_path: Path, monkeypatch, capsys) -> None:
    def pause_client(state):
        raise tts.TTSConfigurationError("wait for voice")

    monkeypatch.setattr(tts, "create_client", pause_client)
    assert run_after_transcript_approval(
        ["start", "--root", str(tmp_path), "--speaker", "mock", "--text", "废弃测试。", "--mock"]
        , capsys
    ) == 0
    paused = json.loads(capsys.readouterr().out)
    run_id = paused["run_id"]
    assert tts.main(["status", "--root", str(tmp_path), "--run-id", run_id]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "paused_configuration"
    assert tts.main(
        ["supersede", "--root", str(tmp_path), "--run-id", run_id, "--reason", "测试取代"]
    ) == 0
    superseded = json.loads(capsys.readouterr().out)
    assert superseded["status"] == "superseded"
    assert tts.main(["resume", "--root", str(tmp_path), "--run-id", run_id]) == 1
    error = json.loads(capsys.readouterr().out)
    assert error["status"] == "error"


def test_daily_event_log_has_unique_monotonic_sequences(tmp_path: Path, capsys) -> None:
    assert run_after_transcript_approval(
        ["run", "--root", str(tmp_path), "--speaker", "mock", "--text", "事件一。事件二。", "--mock"],
        capsys,
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    event_files = list((tmp_path / "logs" / tts.WORKFLOW / "runs" / receipt["run_id"] / "events").glob("*.jsonl"))
    assert len(event_files) == 1
    events = [json.loads(line) for line in event_files[0].read_text(encoding="utf-8").splitlines()]
    own = [event for event in events if event["run_id"] == receipt["run_id"]]
    sequences = [event["sequence"] for event in own]
    assert sequences == list(range(1, len(sequences) + 1))


@pytest.mark.parametrize("text", ["你好，很高兴见到你", "你好。{{pause:250ms}}很高兴见到你。"])
def test_short_audio_completes_without_preview(tmp_path, monkeypatch, capsys, text):
    config = tts.load_config()
    config["preview"] = {"enabled": True, "duration_seconds": 30}
    monkeypatch.setattr(tts, "load_config", lambda: config)
    assert run_after_transcript_approval(["start", "--root", str(tmp_path), "--text", text, "--backend", "volcengine", "--speaker", "哆啦A梦", "--mock"], capsys) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "completed"
    state = tts.state_store(tmp_path, receipt["run_id"]).load()
    assert state["backend"] == "volcengine"
    assert state["speaker_id"] == "S_QBxS5KRZ1"
    assert state["preview"] == {"current_version": 0, "versions": [], "approval": None}
    assert "preview_skipped_short_audio" in state["completed_steps"]
    assert not (Path(receipt["output_dir"]) / "preview").exists()
    assert all(batch["attempts"] == 1 for batch in state["batches"])
    assert tts.main(["verify", "--root", str(tmp_path), "--run-id", receipt["run_id"]]) == 0
    capsys.readouterr()
    state["config_snapshot"]["preview"]["duration_seconds"] = 0.001
    state["config_sha256"] = tts.json_sha256(state["config_snapshot"])
    tts.write_json(tts.state_store(tmp_path, receipt["run_id"]).path, state)
    assert tts.verify_output(Path(receipt["output_dir"]))["status"] == "failed"


def test_backend_override_keeps_config_and_resume_snapshot(tmp_path, monkeypatch, capsys):
    config = tts.load_config()
    config["backend"] = "edge-tts"
    original = copy.deepcopy(config)
    monkeypatch.setattr(tts, "load_config", lambda: config)
    assert tts.main(["start", "--root", str(tmp_path), "--text", "你好。", "--backend", "volcengine", "--speaker", "哆啦A梦", "--mock"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["next_action"] == "approve"
    assert config == original
    assert tts.state_store(tmp_path, receipt["run_id"]).load()["backend"] == "volcengine"
    approve_transcript(tmp_path, receipt["run_id"])
    assert tts.main(["resume", "--root", str(tmp_path), "--run-id", receipt["run_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert tts.state_store(tmp_path, receipt["run_id"]).load()["backend"] == "volcengine"


def test_short_audio_threshold_includes_boundary_and_explicit_pauses(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "_pauses_by_synthesis_offset", lambda state: {0: [{"duration_ms": 100}]})
    config = tts.load_config()
    config["preview"]["duration_seconds"] = 1
    config["boundary_pause_ms"]["comma"] = 100
    state = {"config_snapshot": config, "preview": {"current_version": 0}, "batches": [{"status": "verified", "duration_ms": 400, "boundary": "comma"}, {"status": "verified", "duration_ms": 399, "boundary": "sentence_end"}]}
    assert tts.short_audio_without_preview(state)
    state["batches"][-1]["duration_ms"] = 400
    assert not tts.short_audio_without_preview(state)
    state["batches"][-1]["status"] = "pending"
    assert not tts.short_audio_without_preview(state)


@pytest.mark.parametrize("text,expected", [("你好。", "completed"), ("你好啊。", "paused_preview_approval"), ("你好朋友。", "paused_preview_approval")])
def test_actual_audio_at_preview_threshold(tmp_path, monkeypatch, capsys, text, expected):
    config = tts.load_config()
    config["preview"] = {"enabled": True, "duration_seconds": 1}
    monkeypatch.setattr(tts, "load_config", lambda: config)
    assert run_after_transcript_approval(["start", "--root", str(tmp_path), "--text", text, "--speech-rate", "0", "--mock"], capsys) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == expected


def test_retry_becomes_short_preserves_history_without_new_preview(tmp_path, monkeypatch, capsys):
    config = tts.load_config()
    config["preview"] = {"enabled": True, "duration_seconds": 1}
    monkeypatch.setattr(tts, "load_config", lambda: config)
    assert run_after_transcript_approval(["start", "--root", str(tmp_path), "--text", "你好。", "--speech-rate", "-50", "--mock"], capsys) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "paused_preview_approval"
    old_audio = Path(preview["preview"]["audio_path"])
    assert tts.main(["retry-preview", "--root", str(tmp_path), "--run-id", preview["run_id"], "--reason", "TEST faster voice", "--speech-rate", "100"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed"
    assert "preview" not in result
    state = tts.state_store(tmp_path, result["run_id"]).load()
    assert state["preview"]["current_version"] == 0
    assert state["preview"]["approval"] is None
    assert len(state["preview"]["versions"]) == 1
    assert state["preview"]["versions"][0]["status"] == "rejected"
    assert old_audio.is_file()
    assert not (old_audio.parent.parent / "v002").exists()
    assert tts.verify_output(Path(result["output_dir"]))["status"] == "passed"


def test_independent_preview_gate_requires_state_and_config_fingerprint(tmp_path, capsys):
    assert run_after_transcript_approval(["start", "--root", str(tmp_path), "--text", "你好。", "--mock"], capsys) == 0
    result = json.loads(capsys.readouterr().out)
    output = Path(result["output_dir"])
    store = tts.state_store(tmp_path, result["run_id"])
    original = store.path.read_bytes()
    store.path.unlink()
    assert "缺少运行状态，无法验证试听门禁" in tts.verify_output(output)["errors"]
    store.path.write_bytes(original)
    state = store.load()
    state["config_snapshot"]["preview"]["enabled"] = not state["config_snapshot"]["preview"]["enabled"]
    tts.write_json(store.path, state)
    assert "运行配置快照指纹不一致" in tts.verify_output(output)["errors"]
    store.path.write_bytes(original)
    manifest_path = output / "manifest.json"
    manifest = tts.read_json(manifest_path)
    manifest["preview"] = {}
    tts.write_json(manifest_path, manifest)
    assert "manifest 试听记录与运行状态不一致" in tts.verify_output(output)["errors"]
