"""Deterministically clip audio files without shell invocation."""

from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import wave


class AudioClipError(RuntimeError):
    """Raised when an audio clip cannot be created safely."""


def _clip_wav(source: Path, output: Path, duration_ms: int) -> None:
    with wave.open(str(source), "rb") as reader:
        parameters = reader.getparams()
        frame_count = min(reader.getnframes(), round(reader.getframerate() * duration_ms / 1000))
        frames = reader.readframes(frame_count)
    with wave.open(str(output), "wb") as writer:
        writer.setparams(parameters)
        writer.writeframes(frames)


def _clip_pcm(source: Path, output: Path, duration_ms: int, sample_rate: int) -> None:
    frame_count = round(sample_rate * duration_ms / 1000)
    output.write_bytes(source.read_bytes()[: frame_count * 2])


def _clip_with_ffmpeg(source: Path, output: Path, duration_ms: int, audio_format: str) -> None:
    executable = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")
    if not executable:
        raise AudioClipError("未找到 ffmpeg，无法裁切非 WAV 音频")
    codecs = {"mp3": ["-c:a", "libmp3lame", "-b:a", "96k"], "ogg_opus": ["-c:a", "libopus"]}
    command = [
        executable,
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-t",
        f"{duration_ms / 1000:.3f}",
        *codecs.get(audio_format, []),
        str(output),
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    if completed.returncode != 0:
        raise AudioClipError(f"ffmpeg 裁切失败：{completed.stderr.strip()}")


def clip_audio(
    source: Path,
    output: Path,
    *,
    duration_ms: int,
    audio_format: str,
    sample_rate: int,
) -> Path:
    """Create a clip containing the half-open interval ``[0, duration_ms)``."""
    if duration_ms <= 0:
        raise AudioClipError("duration_ms 必须是正整数")
    source = source.resolve()
    output = output.resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise AudioClipError(f"源音频不存在或为空：{source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if audio_format == "wav":
        _clip_wav(source, output, duration_ms)
    elif audio_format == "pcm":
        _clip_pcm(source, output, duration_ms, sample_rate)
    else:
        _clip_with_ffmpeg(source, output, duration_ms, audio_format)
    if not output.is_file() or output.stat().st_size == 0:
        raise AudioClipError(f"裁切产物不存在或为空：{output}")
    return output
