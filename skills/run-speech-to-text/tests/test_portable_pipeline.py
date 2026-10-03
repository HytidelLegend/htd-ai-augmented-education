"""Exercise both ASR branches with real media tools and a fake cloud transport."""
import importlib.util
import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("portable_pipeline_runner", PACKAGE / "scripts/run_speech_to_text.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.mark.parametrize("implementation", ["streaming", "recording_file"])
def test_asr_pipeline_to_approved_markdown(tmp_path, monkeypatch, implementation):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.fail("完整 ASR 测试需要 FFmpeg 与 FFprobe")
    root = tmp_path / "workspace"
    root.mkdir()
    shutil.copytree(runner.ROOT / "utils/references", root / "utils/references")
    (root / "utils/references/术语表.txt").write_text("Codex\n", encoding="utf-8")
    (root / ".env").write_text("VOLCENGINE_API_KEY=TEST_PIPELINE_KEY\n", encoding="utf-8", newline="\n")
    source_wav = root / "source.wav"
    with wave.open(str(source_wav), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000)
    source = root / "source.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source_wav), str(source)], check=True, capture_output=True)
    response = {"audio_info": {"duration": 1000}, "result": {"utterances": [{
        "text": "测试。", "words": [{"text": "测试", "start_time": 0, "end_time": 800}]}]}}
    calls = []

    def streaming(audio, **kwargs):
        assert audio.is_file() and audio.stat().st_size > 0
        assert kwargs["api_key"] == "TEST_PIPELINE_KEY"
        assert "Codex" in [item.word for item in kwargs["hotwords"]]
        calls.append("streaming")
        return response, [{"type": "mock_completed"}]

    def submit(audio, **kwargs):
        assert audio.is_file() and audio.stat().st_size > 0
        assert "Codex" in [item.word for item in kwargs["hotwords"]]
        calls.append("submit")
        return "TEST_TASK_ID", {"mock": True}

    def query(task_id, **kwargs):
        assert task_id == "TEST_TASK_ID"
        calls.append("query")
        return response, {"mock": True}

    monkeypatch.setattr(runner, "recognize_file", streaming)
    monkeypatch.setattr(runner, "submit_recognition", submit)
    monkeypatch.setattr(runner, "query_recognition", query)
    receipt = runner.run_request(root, {"schema_version": "1.0", "input_path": str(source), "implementation": implementation})
    assert receipt["status"] == "awaiting_transcript_approval"
    run_dir = Path(receipt["run_dir"])
    assert runner._paths(run_dir)["hotwords"].read_text(encoding="utf-8") == "Codex|4\n"
    assert runner.resume_run(root, str(run_dir))["status"] == "awaiting_transcript_approval"
    assert calls == (["streaming"] if implementation == "streaming" else ["submit", "query"])
    assert runner.verify_run(root, str(run_dir))["status"] == "verified"
    approved = runner.approve_transcript(root, str(run_dir), "mock-test-confirmation", receipt["transcript_sha256"], receipt["timestamps_sha256"])
    assert approved["status"] == "completed"
    result = subprocess.run([sys.executable, str(PACKAGE / "scripts/export_transcript_markdown.py"),
        "--root", str(root), "--run-dir", str(run_dir), "--output-dir", str(run_dir / "approved")],
        check=True, capture_output=True, text=True, encoding="utf-8")
    output = Path(json.loads(result.stdout)["output"])
    assert "已确认" in output.read_text(encoding="utf-8")
