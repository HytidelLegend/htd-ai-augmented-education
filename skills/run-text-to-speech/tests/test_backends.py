"""Backend dispatch, strict credentials, timestamp and resumable workflow contracts."""
import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from utils.scripts.tts_backend import check_backend, dotenv_credential, validate_backend_parameters, validate_backend_configuration

SCRIPT = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT))
import run_tts as tts
from edge_tts_client import EdgeTTSClient, normalize_boundary


def test_edge_defaults_and_known_voices():
    config = tts.load_config()
    assert config["backend"] == "edge-tts"
    assert config["audio"]["speech_rate"] == 10
    assert tts.resolve_voice(None, None, config) == ("晓艺", "zh-CN-XiaoyiNeural", "")
    for name, identifier in [("云扬", "zh-CN-YunyangNeural"), ("晓晓", "zh-CN-XiaoxiaoNeural"), ("云希", "zh-CN-YunxiNeural")]:
        assert tts.resolve_voice(name, None, config) == (name, identifier, "")
    with pytest.raises(tts.TTSConfigurationError):
        tts.resolve_voice("Hytidel", None, config)
    with pytest.raises(tts.TTSConfigurationError):
        tts.resolve_voice(None, "seed-tts-2.0", config)


def test_strict_dotenv_ignores_process_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VOLCENGINE_API_KEY", "SYNTHETIC_process_value")
    with pytest.raises(ValueError, match="VOLCENGINE_API_KEY"):
        check_backend(tmp_path, "volcengine")
    (tmp_path / ".env").write_text("VOLCENGINE_API_KEY=SYNTHETIC_file_value\n", encoding="utf-8")
    assert dotenv_credential(tmp_path, "VOLCENGINE_API_KEY") == "SYNTHETIC_file_value"
    assert check_backend(tmp_path, "volcengine")["backend_version"] == "api-v3"
    (tmp_path / ".env").write_text("VOLCENGINE_API_KEY=${VOLCENGINE_API_KEY}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="VOLCENGINE_API_KEY"):
        dotenv_credential(tmp_path, "VOLCENGINE_API_KEY")
    monkeypatch.setitem(sys.modules, "dotenv", None)
    with pytest.raises(ValueError, match="python-dotenv"):
        dotenv_credential(tmp_path, "VOLCENGINE_API_KEY")


def test_edge_preflight_missing_install_and_mock(tmp_path, monkeypatch):
    import edge_tts
    assert check_backend(tmp_path, "edge-tts")["backend_version"] == "7.2.8"
    with monkeypatch.context() as patch:
        patch.delattr(edge_tts, "Communicate")
        with pytest.raises(ValueError, match="接口"):
            check_backend(tmp_path, "edge-tts")
    monkeypatch.setitem(sys.modules, "edge_tts", None)
    with pytest.raises(ValueError, match="缺少 edge-tts"):
        check_backend(tmp_path, "edge-tts")
    assert check_backend(tmp_path, "edge-tts", mock=True)["backend_version"] == "mock"


def test_stream_uses_real_word_boundaries_and_rate(monkeypatch):
    import edge_tts
    arguments = {}
    class Communicate:
        def __init__(self, text, voice, **kwargs):
            arguments.update(kwargs)
        async def stream(self):
            yield {"type": "audio", "data": b"SYNTHETIC_audio"}
            yield {"type": "WordBoundary", "text": "测试", "offset": 1000000, "duration": 2000000}
    monkeypatch.setattr(edge_tts, "Communicate", Communicate)
    config = tts.load_config()
    audio, events = asyncio.run(EdgeTTSClient(config)._stream("测试。", "zh-CN-XiaoyiNeural", config["audio"]))
    assert audio == b"SYNTHETIC_audio"
    assert arguments["rate"] == "+10%"
    assert arguments["boundary"] == "WordBoundary"
    assert events[0]["items"] == [{"text": "测试", "start_ms": 100, "end_ms": 300}]


def test_sentence_aggregation_skips_unreported_punctuation():
    items = [{"text": "第一句", "start_ms": 0, "end_ms": 500},
             {"text": "第二句", "start_ms": 600, "end_ms": 1000}]
    sentences = tts.derive_sentences("第一句。第二句。", items, word_boundaries=True)
    assert [item["end_ms"] for item in sentences] == [500, 1000]


def test_fingerprint_changes_with_backend_voice_and_parameters():
    state = {"backend": "edge-tts", "backend_version": "7.2.8", "speaker_id": "晓艺",
             "resource_id": "", "audio": tts.load_config()["audio"], "mock": False}
    baseline = tts.synthesis_fingerprint(state, "测试")
    for key, value in [("backend", "volcengine"), ("backend_version", "other"), ("speaker_id", "云希")]:
        assert tts.synthesis_fingerprint({**state, key: value}, "测试") != baseline
    changed = copy.deepcopy(state)
    changed["audio"]["speech_rate"] = 0
    assert tts.synthesis_fingerprint(changed, "测试") != baseline


def test_client_dispatch_and_unsupported_emotion(tmp_path):
    state = {"root": str(tmp_path), "run_id": "SYNTHETIC", "backend": "edge-tts", "mock": False,
             "audio": tts.load_config()["audio"], "config_snapshot": tts.load_config()}
    assert type(tts.create_client(state)).__name__ == "EdgeTTSClient"
    state["audio"] = {**state["audio"], "emotion": "happy"}
    with pytest.raises(tts.TTSConfigurationError, match="emotion"):
        tts.create_client(state)
    state["mock"] = True
    assert isinstance(tts.create_client(state), tts.MockClient)


def test_edge_network_failure_is_retryable(tmp_path, monkeypatch):
    config = tts.load_config()
    config["api"]["max_retries"] = 1
    client = EdgeTTSClient(config, (tts.TTSConfigurationError, tts.TTSRetryableError, tts.TTSVerificationError))
    async def fail(*args):
        raise TimeoutError("SYNTHETIC timeout")
    monkeypatch.setattr(client, "_stream", fail)
    with pytest.raises(tts.TTSRetryableError, match="TimeoutError"):
        client.synthesize("测试", "zh-CN-XiaoyiNeural", "", config["audio"])


@pytest.mark.parametrize("audio_format", ["mp3", "wav", "pcm", "ogg_opus"])
def test_edge_output_formats(tmp_path, monkeypatch, audio_format):
    import subprocess
    from utils.scripts.ffmpeg_plan import find_ffmpeg
    source = tmp_path / "source.mp3"
    subprocess.run([str(find_ffmpeg()), "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-ar", "24000", str(source)], check=True)
    config = tts.load_config()
    config["_staging_dir"] = str(tmp_path)
    client = EdgeTTSClient(config)
    async def stream(*args):
        return source.read_bytes(), [{"items": [{"text": "测试", "start_ms": 0, "end_ms": 900}]}]
    monkeypatch.setattr(client, "_stream", stream)
    params = {**config["audio"], "format": audio_format, "sample_rate": 48000}
    audio, events = client.synthesize("测试", "zh-CN-XiaoyiNeural", "", params)
    path = tmp_path / f'converted.{tts.FORMAT_EXTENSIONS[audio_format]}'
    path.write_bytes(audio)
    assert abs(tts.batch_audio_duration_ms(path, audio_format, 48000) - 1000) < 150
    assert tts.normalize_items(events)[0]["end_ms"] == 900


def test_backend_configuration_pause_and_resume(tmp_path, monkeypatch, capsys):
    # Reuse the existing regression harness, including upstream approval fixtures.
    spec = importlib.util.spec_from_file_location("backend_harness", Path(__file__).with_name("test_run_tts.py"))
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    references = tmp_path / "utils/references"
    references.mkdir(parents=True)
    import shutil
    for name in ("workflow-state-v1.schema.json", "artifact-manifest-v1.schema.json"):
        shutil.copy2(tts.DEFAULT_PROJECT_DIR / "utils/references" / name, references / name)
    config = tts.load_config()
    config["white_noise"]["enabled"] = False
    config["preview"]["enabled"] = False
    monkeypatch.setattr(harness.tts, "load_config", lambda: config)
    original = harness.tts.check_backend
    def missing(*args, **kwargs):
        raise ValueError("SYNTHETIC missing edge-tts")
    monkeypatch.setattr(harness.tts, "check_backend", missing)
    assert harness.tts.main(["start", "--root", str(tmp_path), "--text", "接口测试。", "--mock"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    run_id = receipt["run_id"]
    state = harness.tts.state_store(tmp_path, run_id).load()
    assert state["status"] == "paused_configuration"
    assert state["resume_stage"] == "checking_backend_environment"
    monkeypatch.setattr(harness.tts, "check_backend", original)
    assert harness.tts.main(["resume", "--root", str(tmp_path), "--run-id", run_id]) == 0
    capsys.readouterr()
    harness.approve_transcript(tmp_path, run_id)
    assert harness.tts.main(["resume", "--root", str(tmp_path), "--run-id", run_id]) == 0
    completed = json.loads(capsys.readouterr().out)
    assert completed["status"] == "completed"
    manifest = harness.tts.read_json(Path(completed["output_dir"]) / "manifest.json")
    assert manifest["backend"] == "edge-tts"
    assert manifest["schema_version"] == 4
    assert harness.tts.main(["verify", "--root", str(tmp_path), "--run-id", run_id]) == 0


@pytest.fixture
def edge_workflow(tmp_path, monkeypatch):
    """Run the actual Edge adapter with deterministic streamed audio and metadata."""
    import subprocess
    import shutil
    from utils.scripts.ffmpeg_plan import find_ffmpeg
    spec = importlib.util.spec_from_file_location("edge_workflow_harness", Path(__file__).with_name("test_run_tts.py"))
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    runner = harness.tts
    references = tmp_path / "utils/references"
    references.mkdir(parents=True)
    for name in ("workflow-state-v1.schema.json", "artifact-manifest-v1.schema.json"):
        shutil.copy2(runner.DEFAULT_PROJECT_DIR / "utils/references" / name, references / name)
    source = tmp_path / "fixture.mp3"
    subprocess.run([str(find_ffmpeg()), "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-ar", "24000", str(source)], check=True)
    config = runner.load_config()
    config["white_noise"]["enabled"] = False
    config["audio"]["format"] = "wav"
    config["preview"]["duration_seconds"] = 1
    config["batching"].update(base_min_chars=10, base_max_chars=20)
    monkeypatch.setattr(runner, "load_config", lambda: copy.deepcopy(config))
    calls = []
    async def stream(self, text, voice, params):
        calls.append((text, voice, params["speech_rate"]))
        chunks = runner.sentence_chunks(text)
        items = [{"text": chunk.rstrip("。！？.!?"), "start_ms": round(i * 900 / len(chunks)),
                  "end_ms": round((i + 1) * 900 / len(chunks))} for i, chunk in enumerate(chunks)]
        return source.read_bytes(), [{"items": items}]
    monkeypatch.setattr(EdgeTTSClient, "_stream", stream)
    # Use the public adapter class, avoiding an independent importlib module identity.
    original_create = runner.create_client
    def create(state):
        if state.get("backend") == "edge-tts" and not state["mock"]:
            runner.check_backend_checkpoint(state)
            conf = {**state["config_snapshot"], "_staging_dir": str(runner.state_store(tmp_path, state["run_id"]).run_dir / "staging")}
            return EdgeTTSClient(conf, (runner.TTSConfigurationError, runner.TTSRetryableError, runner.TTSVerificationError))
        return original_create(state)
    monkeypatch.setattr(runner, "create_client", create)
    return harness, runner, config, calls


def approve_current_preview(runner, root, run_id, capsys):
    state = runner.state_store(root, run_id).load()
    current = state["preview"]["versions"][-1]
    assert runner.main(["approve-preview", "--root", str(root), "--run-id", run_id,
                        "--confirmed-by", "SYNTHETIC_test_confirmation", "--preview-sha256", current["audio_sha256"]]) == 0
    return json.loads(capsys.readouterr().out)


def test_edge_preview_revision_parameter_change_and_fingerprint_gate(edge_workflow, tmp_path, capsys):
    harness, runner, config, calls = edge_workflow
    text = "第一句接口测试。第二句接口测试。第三句接口测试。第四句接口测试。第五句接口测试。"
    assert harness.run_after_transcript_approval(["start", "--root", str(tmp_path), "--text", text], capsys) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "paused_preview_approval"
    run_id = preview["run_id"]
    state = runner.state_store(tmp_path, run_id).load()
    first_audio = Path(state["preview"]["versions"][-1]["audio_path"])
    before_hash = runner.file_sha256(first_audio)
    # Unsupported resource options fail before mutating the current preview or batches.
    assert runner.main(["retry-preview", "--root", str(tmp_path), "--run-id", run_id,
                        "--reason", "SYNTHETIC", "--resource-id", "seed-tts-2.0"]) == 1
    capsys.readouterr()
    assert runner.file_sha256(first_audio) == before_hash
    assert runner.state_store(tmp_path, run_id).load()["preview"]["versions"][-1]["status"] == "awaiting_approval"
    assert runner.main(["retry-preview", "--root", str(tmp_path), "--run-id", run_id,
                        "--reason", "SYNTHETIC", "--emotion-scale", "4"]) == 1
    capsys.readouterr()
    assert runner.file_sha256(first_audio) == before_hash
    completed = approve_current_preview(runner, tmp_path, run_id, capsys)
    assert completed["status"] == "completed"
    assert len(calls) == len(runner.state_store(tmp_path, run_id).load()["batches"])

    query = tmp_path / "query.txt"
    query.write_text("第一句", encoding="utf-8")
    assert runner.main(["locate", "--root", str(tmp_path), "--run-id", run_id, "--query-file", str(query)]) == 0
    match_id = json.loads(capsys.readouterr().out)["candidates"][0]["match_id"]
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("新首句", encoding="utf-8")
    assert runner.main(["revise", "--root", str(tmp_path), "--base-run-id", run_id,
                        "--replace", f"{match_id}={replacement}"]) == 0
    revised = json.loads(capsys.readouterr().out)
    revision_id = revised["run_id"]
    harness.approve_transcript(tmp_path, revision_id)
    assert runner.main(["resume", "--root", str(tmp_path), "--run-id", revision_id]) == 0
    capsys.readouterr()
    assert runner.main(["retry-preview", "--root", str(tmp_path), "--run-id", revision_id,
                        "--reason", "SYNTHETIC speed change", "--speech-rate", "0", "--speaker", "云希"]) == 0
    capsys.readouterr()
    revised_state = runner.state_store(tmp_path, revision_id).load()
    assert all(batch["origin"] == "generated" for batch in revised_state["batches"])
    completed = approve_current_preview(runner, tmp_path, revision_id, capsys)
    assert completed["status"] == "completed"
    manifest_path = Path(completed["output_dir"]) / "manifest.json"
    assert runner.verify_output(manifest_path.parent)["status"] == "passed"
    manifest = runner.read_json(manifest_path)
    raw_path = Path(manifest["batches"][0]["timestamp_path"])
    raw = runner.read_json(raw_path)
    raw.pop("synthesis_fingerprint")
    runner.write_json(raw_path, raw)
    assert any("缺少合成指纹" in error for error in runner.verify_output(manifest_path.parent)["errors"])


def test_environment_preflight_rejects_corrupt_edge_mapping(tmp_path):
    import yaml
    from utils.scripts.speech_cli import verify_installation
    skill = tmp_path / "skills/run-text-to-speech"
    (skill / "scripts").mkdir(parents=True)
    (skill / "references").mkdir()
    for filename in ("SKILL.md", "scripts/cli.py", "references/import-provenance.json"):
        (skill / filename).write_text("", encoding="utf-8")
    plugin = tmp_path / ".claude-plugin"
    plugin.mkdir()
    (plugin / "plugin.json").write_text(json.dumps({"skills": ["./skills/run-text-to-speech"]}), encoding="utf-8")
    config = tts.load_config()
    config["white_noise"]["enabled"] = False
    (skill / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (skill / "references/voices.yaml").write_text("edge_tts:\n  default_voice: 晓艺\n  voices:\n    - name: 晓艺\n", encoding="utf-8")
    result = verify_installation(skill, environment=True)
    assert result["status"] == "failed"
    assert any("speaker_id" in error for error in result["errors"])
    (skill / "config.yaml").write_text("[broken", encoding="utf-8")
    assert verify_installation(skill, environment=True)["status"] == "failed"


@pytest.mark.parametrize("parameter,value", [("emotion_scale", 3), ("sample_rate", 0), ("loudness_rate", -101), ("speech_rate", True)])
def test_edge_parameter_preflight_rejects_invalid_values(parameter, value):
    params = {**tts.load_config()["audio"], parameter: value}
    with pytest.raises(ValueError):
        validate_backend_parameters("edge-tts", params)


def test_invalid_backend_config_does_not_silently_select_volcengine():
    config = tts.load_config()
    config.pop("backend")
    with pytest.raises(ValueError, match="backend"):
        validate_backend_configuration(config)
    config["backend"] = "edge-tts"
    config["api"]["max_retries"] = 0
    with pytest.raises(ValueError, match="max_retries"):
        validate_backend_configuration(config)


def test_adapter_change_and_missing_audio_tool_pause_configuration(tmp_path, monkeypatch):
    state = {"root": str(tmp_path), "backend": "edge-tts", "mock": False,
             "audio": tts.load_config()["audio"], "resource_id": "", "adapter_version": "SYNTHETIC_other"}
    with pytest.raises(tts.TTSConfigurationError, match="适配器"):
        tts.check_backend_checkpoint(state)
    state["adapter_version"] = tts.ADAPTER_VERSION
    def missing():
        raise ValueError("SYNTHETIC missing FFmpeg")
    monkeypatch.setattr(tts, "find_ffmpeg", missing)
    with pytest.raises(tts.TTSConfigurationError, match="FFmpeg"):
        tts.check_backend_checkpoint(state)


def test_legacy_revision_stays_volcengine_without_requiring_new_fingerprints(edge_workflow, tmp_path, capsys):
    harness, runner, config, calls = edge_workflow
    config["backend"] = "volcengine"
    config["preview"]["enabled"] = False
    text = "第一句旧运行。第二句旧运行。第三句旧运行。"
    assert harness.run_after_transcript_approval(["start", "--root", str(tmp_path), "--text", text, "--mock"], capsys) == 0
    completed = json.loads(capsys.readouterr().out)
    run_id = completed["run_id"]
    store = runner.state_store(tmp_path, run_id)
    state = store.load()
    for key in ("backend", "backend_version", "timestamp_granularity", "adapter_version"):
        state.pop(key, None)
    state["config_snapshot"].pop("backend")
    state["config_snapshot"]["schema_version"] = 1
    state["config_sha256"] = runner.json_sha256(state["config_snapshot"])
    for batch in state["batches"]:
        raw_path = Path(batch["timestamps"]["path"])
        raw = runner.read_json(raw_path)
        raw.pop("synthesis_fingerprint")
        raw.pop("adapter_version", None)
        runner.write_json(raw_path, raw)
        batch["timestamps"]["sha256"] = runner.file_sha256(raw_path)
    manifest_path = Path(completed["output_dir"]) / "manifest.json"
    manifest = runner.read_json(manifest_path)
    manifest["schema_version"] = 3
    for key in ("backend", "backend_version", "timestamp_granularity"):
        manifest.pop(key)
    runner.write_json(manifest_path, manifest)
    state["artifacts"]["manifest"]["sha256"] = runner.file_sha256(manifest_path)
    store.save(state)
    assert runner.verify_output(manifest_path.parent)["status"] == "passed"
    # The current default changes to Edge, while the legacy base stays Volcengine.
    config["backend"] = "edge-tts"
    query = tmp_path / "legacy-query.txt"
    query.write_text("第一句", encoding="utf-8")
    assert runner.main(["locate", "--root", str(tmp_path), "--run-id", run_id, "--query-file", str(query)]) == 0
    match_id = json.loads(capsys.readouterr().out)["candidates"][0]["match_id"]
    replacement = tmp_path / "legacy-replacement.txt"
    replacement.write_text("新首句", encoding="utf-8")
    assert runner.main(["revise", "--root", str(tmp_path), "--base-run-id", run_id,
                        "--replace", f"{match_id}={replacement}"]) == 0
    revised = json.loads(capsys.readouterr().out)
    harness.approve_transcript(tmp_path, revised["run_id"])
    assert runner.main(["resume", "--root", str(tmp_path), "--run-id", revised["run_id"]]) == 0
    completed = json.loads(capsys.readouterr().out)
    assert completed["status"] == "completed"
    manifest_path = Path(completed["output_dir"]) / "manifest.json"
    assert runner.read_json(manifest_path)["schema_version"] == 3
    assert runner.verify_output(manifest_path.parent)["status"] == "passed"
