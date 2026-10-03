"""Project contracts: hotword snapshots, recovery, cross-run approval and CLI gates."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import wave
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "skills/run-speech-to-text/scripts"))
import run_speech_to_text as asr
from utils.scripts.speech_hotwords import merge_hotwords
from utils.scripts.speech_handoff import build_handoff, verify_handoff


def fixture_root(root):
    shutil.copytree(ROOT / "utils/references", root / "utils/references")
    (root / "utils/references/术语表.txt").write_text("Codex\n科学立法\n", encoding="utf-8")
    (root / ".env").write_text("VOLCENGINE_API_KEY=TEST_ASR_KEY\n", encoding="utf-8")
    source = root / "source.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 16000)
    return source


def response():
    return {"audio_info": {"duration": 1000}, "result": {"utterances": [{"text": "测试。",
        "words": [{"text": "测试", "start_time": 0, "end_time": 800}]}]}}


def test_hotwords_merge_does_not_mutate_shared_source(tmp_path):
    shared = tmp_path / "terms.txt"
    shared.write_text("Codex\n科学立法\n", encoding="utf-8")
    before = shared.read_bytes()
    extra = tmp_path / "extra.txt"
    extra.write_text("codex|8\n火山语音|6\n", encoding="utf-8")
    terms, sources = merge_hotwords(shared, extra)
    assert [(item.word, item.weight) for item in terms] == [("codex", 8), ("科学立法", 4), ("火山语音", 6)]
    assert shared.read_bytes() == before and len(sources) == 2


def test_uncertain_submit_resumes_query_without_resubmit(tmp_path, monkeypatch):
    source = fixture_root(tmp_path)
    calls = []
    def submit(audio, **kwargs):
        calls.append("submit")
        assert kwargs["request_id"]
        raise TimeoutError("TEST ambiguous submission")
    def query(task_id, **kwargs):
        calls.append("query")
        assert task_id
        return response(), {"mock": True}
    monkeypatch.setattr(asr, "submit_recognition", submit)
    monkeypatch.setattr(asr, "query_recognition", query)
    request = {"schema_version": "1.0", "input_path": str(source), "implementation": "recording_file"}
    with pytest.raises(TimeoutError):
        asr.run_request(tmp_path, request)
    run_dir = next((tmp_path / "outputs/run-speech-to-text/runs").iterdir())
    # Editing global terms must not change the frozen per-run list.
    (tmp_path / "utils/references/术语表.txt").write_text("新增词\n", encoding="utf-8")
    receipt = asr.resume_run(tmp_path, str(run_dir))
    assert receipt["status"] == "awaiting_transcript_approval"
    assert calls == ["submit", "query"]
    assert "Codex" in asr._paths(run_dir)["hotwords"].read_text(encoding="utf-8")


def test_corrections_and_handoff_reject_tampered_approval(tmp_path, monkeypatch):
    source = fixture_root(tmp_path)
    monkeypatch.setattr(asr, "recognize_file", lambda *a, **k: (response(), [{"type": "mock"}]))
    receipt = asr.run_request(tmp_path, {"schema_version": "1.0", "input_path": str(source)})
    run_dir = Path(receipt["run_dir"])
    paths = asr._paths(run_dir)
    correction = tmp_path / "corrections.json"
    correction.write_text(json.dumps({"schema_version": "1.0", "source_sha256": receipt["timestamps_sha256"],
        "corrections": [{"item_start": 0, "item_end": 1, "replacement_text": "校正。"}]}), encoding="utf-8")
    corrected = asr.submit_corrections(tmp_path, str(run_dir), correction)
    asr.approve_transcript(tmp_path, str(run_dir), "TEST_USER", corrected["transcript_sha256"], corrected["timestamps_sha256"])
    handoff = build_handoff(tmp_path, "run-speech-to-text", receipt["run_id"])
    assert verify_handoff(tmp_path, handoff) == handoff
    receipt_data = json.loads(paths["approval"].read_text(encoding="utf-8"))
    receipt_data["transcript_sha256"] = "0" * 64
    paths["approval"].write_text(json.dumps(receipt_data), encoding="utf-8")
    with pytest.raises(ValueError):
        verify_handoff(tmp_path, handoff)


def test_cli_waiting_status_and_verification_exit_codes(tmp_path):
    fixture_root(tmp_path)
    cli = ROOT / "skills/convert-copy-to-transcript/scripts/cli.py"
    def call(*arguments):
        return subprocess.run([sys.executable, str(cli), *arguments, "--root", str(tmp_path)],
            capture_output=True, text=True, encoding="utf-8")
    start = call("start", "--text", "接口测试。")
    assert start.returncode == 3
    run_id = json.loads(start.stdout)["run_id"]
    assert call("status", "--run-id", run_id).returncode == 0
    assert call("verify", "--run-id", run_id).returncode == 4
    preview = call("apply-decisions", "--run-id", run_id)
    assert preview.returncode == 3
    digest = json.loads(preview.stdout)["preview_sha256"]
    assert call("deliver", "--run-id", run_id).returncode == 3
    assert call("approve", "--run-id", run_id, "--confirmed-by", "TEST_USER", "--preview-sha256", digest).returncode == 0
    assert call("verify", "--run-id", run_id).returncode == 0
    assert call("deliver", "--run-id", run_id).returncode == 0


def test_batch_persists_children_and_waits_for_all_approvals(tmp_path, monkeypatch, capsys):
    fixture_root(tmp_path)
    source_dir = tmp_path / "media"
    source_dir.mkdir()
    (source_dir / "input.mp3").write_bytes(b"TEST_FAKE_MEDIA")
    import batch_transcripts as batch
    calls = []
    def recognize(root, request, *, on_initialized):
        run_id, run_dir = asr._new_run(root, None)
        on_initialized(run_id, run_dir)
        paths = asr._paths(run_dir)
        paths["state"].write_text(json.dumps({"status": "awaiting_transcript_approval"}), encoding="utf-8")
        paths["transcript"].write_text("TEST transcript", encoding="utf-8")
        paths["approved_transcript"].write_text("TEST transcript", encoding="utf-8")
        calls.append("asr")
        return {"run_id": run_id, "run_dir": str(run_dir)}
    monkeypatch.setattr(batch, "run_request", recognize)
    monkeypatch.setattr(batch, "resume_run", lambda *a: {})
    monkeypatch.setattr(batch, "verify_run", lambda *a: {"status": "verified"})
    monkeypatch.setattr(batch.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, '{"output":"TEST.md"}', ''))
    assert batch.main(["start", "--root", str(tmp_path), "--input-dir", str(source_dir)]) == 3
    receipt = json.loads(capsys.readouterr().out)
    state = batch.store_for(tmp_path, receipt["run_id"]).load()
    assert len(state["children"]) == 1
    saved_state = batch.store_for(tmp_path, receipt["run_id"]).path.read_bytes()
    assert batch.main(["verify", "--root", str(tmp_path), "--run-id", receipt["run_id"]]) == 3
    capsys.readouterr()
    assert batch.store_for(tmp_path, receipt["run_id"]).path.read_bytes() == saved_state
    def failed_verification(*a):
        raise ValueError("TEST child verification failure")
    monkeypatch.setattr(batch, "verify_run", failed_verification)
    assert batch.main(["verify", "--root", str(tmp_path), "--run-id", receipt["run_id"]]) == 4
    capsys.readouterr()
    assert batch.store_for(tmp_path, receipt["run_id"]).path.read_bytes() == saved_state
    monkeypatch.setattr(batch, "verify_run", lambda *a: {"status": "verified"})
    child = Path(next(iter(state["children"].values())))
    asr._paths(child)["state"].write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    assert batch.main(["resume", "--root", str(tmp_path), "--run-id", receipt["run_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert calls == ["asr"]


@pytest.mark.parametrize("preview_seconds", [0.1, 30])
def test_approved_asr_to_tts_preview_and_delivery(tmp_path, monkeypatch, capsys, preview_seconds):
    source = fixture_root(tmp_path)
    shutil.copytree(ROOT / "utils/assets", tmp_path / "utils/assets")
    monkeypatch.setattr(asr, "recognize_file", lambda *a, **k: (response(), [{"type": "mock"}]))
    receipt = asr.run_request(tmp_path, {"schema_version": "1.0", "input_path": str(source)})
    asr.approve_transcript(tmp_path, receipt["run_dir"], "TEST_USER", receipt["transcript_sha256"], receipt["timestamps_sha256"])
    paths = asr._paths(Path(receipt["run_dir"]))
    before = paths["manifest"].read_bytes()
    sys.path.insert(0, str(ROOT / "skills/run-text-to-speech/scripts"))
    import run_tts as tts
    from utils.scripts.tts_handoff import resolve_tts_speech_dir
    config = tts.load_config()
    config["preview"]["duration_seconds"] = preview_seconds
    monkeypatch.setattr(tts, "load_config", lambda: config)
    assert tts.main(["start", "--root", str(tmp_path), "--producer-skill", "run-speech-to-text",
                     "--transcript-run-id", receipt["run_id"], "--mock"]) == 0
    preview = json.loads(capsys.readouterr().out)
    if preview_seconds == 0.1:
        assert preview["status"] == "paused_preview_approval"
        assert tts.main(["approve-preview", "--root", str(tmp_path), "--run-id", preview["run_id"],
                         "--preview-sha256", preview["preview"]["audio_sha256"], "--confirmed-by", "TEST_USER"]) == 0
        delivered = json.loads(capsys.readouterr().out)
        assert delivered["status"] == "completed"
    else:
        assert preview["status"] == "completed"
        assert "preview" not in preview
    run_dir = tmp_path / "outputs/run-text-to-speech/runs" / preview["run_id"]
    assert resolve_tts_speech_dir(run_dir) == run_dir / "speech"
    assert paths["manifest"].read_bytes() == before
    paths["approval"].write_text("{}", encoding="utf-8")
    for target in (["--run-id", preview["run_id"]], ["--output-dir", str(run_dir / "speech")]):
        assert tts.main(["verify", "--root", str(tmp_path), *target]) == 1
        capsys.readouterr()


def test_asr_delivery_and_export_are_approval_gated_and_do_not_overwrite(tmp_path, monkeypatch):
    source = fixture_root(tmp_path)
    monkeypatch.setattr(asr, "recognize_file", lambda *a, **k: (response(), [{"type": "mock"}]))
    receipt = asr.run_request(tmp_path, {"schema_version": "1.0", "input_path": str(source)})
    run_dir = Path(receipt["run_dir"])
    cli = ROOT / "skills/run-speech-to-text/scripts/cli.py"
    command = [sys.executable, str(cli), "deliver", "--root", str(tmp_path), "--run-id", receipt["run_id"]]
    assert subprocess.run(command, capture_output=True).returncode == 3
    export_script = cli.with_name("export_transcript_markdown.py")
    export = [sys.executable, str(export_script), "--root", str(tmp_path), "--run-dir", str(run_dir),
              "--output-dir", str(run_dir / "generated")]
    result = subprocess.run(export, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    output = Path(json.loads(result.stdout)["output"])
    output.write_text("TEST user-edited export", encoding="utf-8")
    assert subprocess.run(export, capture_output=True).returncode != 0
    assert output.read_text(encoding="utf-8") == "TEST user-edited export"
    asr.approve_transcript(tmp_path, str(run_dir), "TEST_USER", receipt["transcript_sha256"], receipt["timestamps_sha256"])
    assert subprocess.run(command, capture_output=True).returncode == 0


@pytest.mark.parametrize("artifact", ["hotwords", "timestamps"])
def test_asr_rejects_snapshot_tampering_before_correction(tmp_path, monkeypatch, artifact):
    source = fixture_root(tmp_path)
    monkeypatch.setattr(asr, "recognize_file", lambda *a, **k: (response(), [{"type": "mock"}]))
    receipt = asr.run_request(tmp_path, {"schema_version": "1.0", "input_path": str(source)})
    paths = asr._paths(Path(receipt["run_dir"]))
    if artifact == "hotwords":
        paths["hotwords"].with_suffix(".sources.json").write_text("[]", encoding="utf-8")
    else:
        paths["timestamps"].write_text("{}", encoding="utf-8")
    correction = tmp_path / "correction.json"
    correction.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        asr.submit_corrections(tmp_path, receipt["run_dir"], correction)


def test_asr_parallel_initialization_allocates_distinct_runs(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    def initialize(_):
        try:
            return asr._new_run(tmp_path, None)[0]
        except ValueError as exc:
            assert "已有事务正在执行" in str(exc)
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = [value for value in pool.map(initialize, range(2)) if value]
    if len(runs) == 1:
        runs.append(asr._new_run(tmp_path, None)[0])
    assert len(set(runs)) == 2


def test_workspace_collision_includes_output_runs_without_logs(tmp_path, monkeypatch):
    import utils.scripts.tts_workspace as workspace
    parent = tmp_path / "outputs/convert-copy-to-transcript/runs"
    existing_id = "20261003T000000"
    (parent / existing_id).mkdir(parents=True)
    def frozen_timestamp(values):
        return existing_id + "_1" if existing_id in set(values) else existing_id
    monkeypatch.setattr(workspace, "unique_filename_timestamp", frozen_timestamp)
    created = workspace.create_workspace(tmp_path)
    assert created.run_id == existing_id + "_1"
    assert not (parent / existing_id / "pipeline-manifest.json").exists()


@pytest.mark.parametrize("error_code, expected", [("runtime_error", 5), ("validation_error", 2)])
def test_cli_uses_structured_error_codes_for_runtime_failures(monkeypatch, capsys, error_code, expected):
    from utils.scripts import speech_cli
    receipt = {"status": "error", "error_code": error_code, "error": "TEST 运行失败"}
    monkeypatch.setattr(speech_cli.subprocess, "run", lambda *a, **k:
        subprocess.CompletedProcess([], 1, "", json.dumps(receipt)))
    skill = ROOT / "skills/run-speech-to-text"
    assert speech_cli.invoke(skill, "run_speech_to_text.py", ["start"], start_command="run") == expected
    capsys.readouterr()


def test_durable_stream_response_recovers_without_another_cloud_call(tmp_path, monkeypatch):
    source = fixture_root(tmp_path)
    calls = []
    def recognize(*a, **k):
        calls.append("recognize")
        return response(), [{"type": "mock"}]
    monkeypatch.setattr(asr, "recognize_file", recognize)
    original_write = asr.write_json
    def interrupted_write(path, value):
        if path.name == "api-request-metadata.json":
            raise RuntimeError("TEST interruption after durable response")
        return original_write(path, value)
    monkeypatch.setattr(asr, "write_json", interrupted_write)
    with pytest.raises(RuntimeError):
        asr.run_request(tmp_path, {"schema_version": "1.0", "input_path": str(source)})
    monkeypatch.setattr(asr, "write_json", original_write)
    run_dir = next((tmp_path / "outputs/run-speech-to-text/runs").iterdir())
    assert asr.resume_run(tmp_path, str(run_dir))["status"] == "awaiting_transcript_approval"
    assert calls == ["recognize"]
