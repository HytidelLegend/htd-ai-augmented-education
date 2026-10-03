"""Validate TTS schema 2 handoff and produce a compact navigation model."""
from __future__ import annotations
import json
import os
import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Any

class TtsHandoffError(ValueError):
    pass


def resolve_tts_speech_dir(run_dir: Path) -> Path:
    """Resolve a project run (or legacy workspace) or its speech directory."""
    run_dir = run_dir.resolve()
    if (run_dir / "manifest.json").is_file():
        return run_dir
    pipeline_path = run_dir / "pipeline-manifest.json"
    if not pipeline_path.is_file():
        raise TtsHandoffError("TTS 运行目录缺少 pipeline-manifest.json 或 speech/manifest.json")
    pipeline = _read(pipeline_path)
    speech = pipeline.get("components", {}).get("speech", {})
    if pipeline.get("schema_version") not in {"1.0", "2.0"} or speech.get("status") != "completed":
        raise TtsHandoffError("TTS pipeline 的 speech 组件必须为 completed")
    manifest_path = Path(str(speech.get("manifest_path", ""))).resolve()
    try:
        manifest_path.relative_to(run_dir)
    except ValueError as exc:
        raise TtsHandoffError("TTS speech manifest 越出完整运行目录") from exc
    if manifest_path != (run_dir / "speech" / "manifest.json").resolve() or not manifest_path.is_file():
        raise TtsHandoffError("TTS pipeline 的 speech manifest 路径无效")
    return manifest_path.parent

def resolve_speech_audio_file(tts_dir: Path, audio_file: str) -> Path:
    """Resolve one direct child audio file from a completed TTS speech directory."""
    speech_dir = resolve_tts_speech_dir(tts_dir)
    name = str(audio_file).strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name or Path(name).is_absolute():
        raise TtsHandoffError("speech_audio_file 必须是 speech/ 下的直属文件名")
    audio = (speech_dir / name).resolve()
    if audio.parent != speech_dir or not audio.is_file():
        raise TtsHandoffError(f"指定的 speech 音频不存在：{name}")
    return audio

def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TtsHandoffError(f"无法读取 JSON：{path}") from exc

def _hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _audio_duration_ms(audio: Path) -> int:
    command = [os.environ.get("FFPROBE_PATH") or "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(audio)]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", timeout=30)
        return round(float(result.stdout.strip()) * 1000)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise TtsHandoffError(f"FFprobe 无法确认音频时长：{audio}") from exc

def validate_tts_handoff(tts_dir: Path, *, transcript_sha256: str, duration_tolerance_ms: int = 80, audio_file: str | None = None) -> dict[str, Any]:
    tts_dir = resolve_tts_speech_dir(tts_dir)
    manifest_path = tts_dir / "manifest.json"
    timestamps_path = tts_dir / "full.timestamps.json"
    manifest = _read(manifest_path)
    declared_map_path = manifest.get("input", {}).get("synthesis_map_path")
    map_path = Path(declared_map_path).resolve() if declared_map_path else tts_dir / "inputs" / "generated" / "synthesis-map.json"
    mapping, timing = _read(map_path), _read(timestamps_path)
    if manifest.get("schema_version") not in {2, 3, 4} or manifest.get("status") != "completed":
        raise TtsHandoffError("TTS manifest 必须为兼容 schema 且状态 completed")
    if mapping.get("schema_version") not in {"1.0", "1.1"} or timing.get("schema_version") not in {"1.0", "1.1"}:
        raise TtsHandoffError("synthesis map 与 full timestamps 必须为兼容的 schema 1.x")
    if manifest.get("input", {}).get("file_sha256") != transcript_sha256:
        raise TtsHandoffError("TTS 输入哈希与已确认逐字稿不一致")
    if manifest.get("input", {}).get("synthesis_map_file_sha256") != _hash(map_path):
        raise TtsHandoffError("synthesis-map.json 哈希不一致")
    canonical_audio = tts_dir / timing.get("audio_file", "")
    if not canonical_audio.is_file():
        raise TtsHandoffError("完整音频不存在")
    if manifest.get("schema_version") in {3, 4}:
        clean_info = manifest.get("clean_audio") or {}
        clean_audio = Path(str(clean_info.get("path", "")))
        if not clean_audio.is_file() or _hash(clean_audio) != clean_info.get("sha256"):
            raise TtsHandoffError("TTS clean 音频缺失或指纹不一致")
        noise = manifest.get("white_noise") or {}
        if noise.get("enabled"):
            asset = Path(str(noise.get("asset_path", "")))
            if noise.get("status") != "completed" or not asset.is_file():
                raise TtsHandoffError("TTS 白噪音素材或处理状态无效")
            if _hash(asset) != noise.get("asset_sha256") or _hash(canonical_audio) != noise.get("output_sha256"):
                raise TtsHandoffError("TTS 白噪音素材或铺底音频指纹不一致")
        elif noise.get("status") != "skipped" or canonical_audio.resolve() != clean_audio.resolve():
            raise TtsHandoffError("禁用白噪音时主音频必须指向 clean 音频")
    measured, duration = _audio_duration_ms(canonical_audio), int(timing.get("duration_ms", -1))
    if duration <= 0 or abs(measured - duration) > duration_tolerance_ms:
        raise TtsHandoffError(f"完整音频时长不一致：timestamps={duration}ms，FFprobe={measured}ms")
    selected_audio = resolve_speech_audio_file(tts_dir, audio_file) if audio_file is not None else canonical_audio.resolve()
    selected_measured = measured if selected_audio == canonical_audio.resolve() else _audio_duration_ms(selected_audio)
    if abs(selected_measured - duration) > duration_tolerance_ms:
        raise TtsHandoffError(f"指定的 speech 音频时长不一致：timestamps={duration}ms，FFprobe={selected_measured}ms")
    paragraphs, items = mapping.get("paragraphs", []), timing.get("items", [])
    if not paragraphs or not items:
        raise TtsHandoffError("TTS 段落或 item 为空")
    previous, synthesis_cursor, normalized_items = 0, 0, []
    for index, item in enumerate(items, 1):
        start, end = int(item["start_ms"]), int(item["end_ms"])
        if start < previous or end <= start or end > duration:
            raise TtsHandoffError("TTS item 时间必须单调、为正且不越界")
        text = str(item["text"])
        normalized_items.append({"timed_span_id": f"timed-span-{index:04d}", "text": text, "start_ms": start, "end_ms": end, "batch_index": item.get("batch_index"), "synthesis_span": {"start": synthesis_cursor, "end": synthesis_cursor + len(text)}})
        synthesis_cursor += len(text)
        previous = end
    origins = {str(item.get("batch_id")): item.get("origin") for item in timing.get("batches", [])}
    if any(value not in {"generated", "reused"} for value in origins.values()):
        raise TtsHandoffError("batch origin 必须为 generated 或 reused")
    revision = manifest.get("revision")
    if manifest.get("mode") == "revision" and (not revision or not revision.get("base_run_id")):
        raise TtsHandoffError("修订运行缺少 revision lineage")
    return {"schema_version": "1.0", "tts_run_id": manifest["run_id"], "transcript_sha256": transcript_sha256, "duration_ms": duration, "measured_audio_duration_ms": selected_measured, "selected_audio_file": selected_audio.name, "paths": {"manifest": str(manifest_path), "synthesis_map": str(map_path), "timestamps": str(timestamps_path), "audio": str(selected_audio), "canonical_audio": str(canonical_audio.resolve())}, "revision": revision, "batch_origins": origins, "paragraphs": paragraphs, "timed_spans": normalized_items, "pauses": timing.get("pauses", []), "diagnostics": timing.get("diagnostics", {})}
