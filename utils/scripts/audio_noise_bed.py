"""Deterministically generate and loop a subtle white-noise bed under audio."""
from __future__ import annotations

import argparse
from array import array
import hashlib
import json
import math
import os
import random
import shutil
import struct
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any


class AudioNoiseBedError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generate_white_noise_asset(
    output: Path,
    *,
    duration_seconds: int = 30,
    sample_rate: int = 24000,
    rms_dbfs: float = -20.0,
    seed: int = 20260825,
) -> dict[str, Any]:
    """Create mono 16-bit PCM white noise with deterministic samples and RMS."""
    if duration_seconds <= 0 or sample_rate <= 0 or not -90.0 <= rms_dbfs <= -3.0:
        raise AudioNoiseBedError("白噪音素材参数无效")
    rng = random.Random(seed)
    frame_count = duration_seconds * sample_rate
    samples = [rng.uniform(-1.0, 1.0) for _ in range(frame_count)]
    source_rms = math.sqrt(sum(value * value for value in samples) / frame_count)
    target_rms = 10.0 ** (rms_dbfs / 20.0)
    scale = target_rms / source_rms
    pcm = bytearray()
    for value in samples:
        integer = max(-32768, min(32767, round(value * scale * 32767)))
        pcm.extend(struct.pack("<h", integer))
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = output.with_name(f".{output.name}.staged")
    with wave.open(str(staged), "wb") as destination:
        destination.setnchannels(1)
        destination.setsampwidth(2)
        destination.setframerate(sample_rate)
        destination.writeframes(bytes(pcm))
    os.replace(staged, output)
    return {
        "path": str(output.resolve()),
        "sha256": file_sha256(output),
        "duration_seconds": duration_seconds,
        "sample_rate": sample_rate,
        "channels": 1,
        "sample_width_bytes": 2,
        "rms_dbfs": rms_dbfs,
        "seed": seed,
    }


def wav_rms_dbfs(path: Path) -> float:
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise AudioNoiseBedError("白噪音素材必须为单声道 16-bit PCM WAV")
        frames = source.readframes(source.getnframes())
    values = struct.unpack(f"<{len(frames) // 2}h", frames)
    if not values:
        raise AudioNoiseBedError("白噪音素材为空")
    rms = math.sqrt(sum(value * value for value in values) / len(values)) / 32768.0
    if rms <= 0:
        raise AudioNoiseBedError("白噪音素材 RMS 为零")
    return 20.0 * math.log10(rms)


def _ffmpeg() -> str:
    executable = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")
    if not executable:
        raise AudioNoiseBedError("未找到 ffmpeg，无法解码或编码音频")
    return executable


def _run_ffmpeg(command: list[str], label: str) -> None:
    completed = subprocess.run(
        command, check=False, capture_output=True, text=True, encoding="utf-8",
        errors="replace", shell=False,
    )
    if completed.returncode != 0:
        raise AudioNoiseBedError(f"ffmpeg {label}失败：{completed.stderr.strip()}")


def _decode_to_wav(
    clean_audio: Path, destination: Path, *, sample_rate: int, audio_format: str
) -> None:
    command = [_ffmpeg(), "-v", "error", "-y"]
    if audio_format == "pcm":
        command.extend(["-f", "s16le", "-ar", str(sample_rate), "-ac", "1"])
    command.extend([
        "-i", str(clean_audio), "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_s16le", str(destination),
    ])
    _run_ffmpeg(command, "解码")


def detect_silence_intervals_wav(
    path: Path,
    *,
    threshold_dbfs: float = -60.0,
    minimum_silence_ms: int = 120,
    window_ms: int = 10,
) -> list[dict[str, int]]:
    """Detect sustained low-RMS regions in mono 16-bit PCM WAV."""
    if not -96.0 <= threshold_dbfs <= -20.0:
        raise AudioNoiseBedError("静音阈值必须位于 [-96, -20] dBFS")
    if minimum_silence_ms < window_ms or window_ms <= 0:
        raise AudioNoiseBedError("静音最短时长不得小于检测窗口")
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise AudioNoiseBedError("静音检测要求单声道 16-bit PCM WAV")
        sample_rate = source.getframerate()
        total_frames = source.getnframes()
        window_frames = max(1, round(sample_rate * window_ms / 1000))
        threshold = 32768.0 * 10.0 ** (threshold_dbfs / 20.0)
        threshold_square = threshold * threshold
        intervals: list[dict[str, int]] = []
        silent_start: int | None = None
        cursor = 0
        while cursor < total_frames:
            raw = source.readframes(min(window_frames, total_frames - cursor))
            values = array("h")
            values.frombytes(raw)
            mean_square = sum(value * value for value in values) / max(1, len(values))
            silent = mean_square <= threshold_square
            if silent and silent_start is None:
                silent_start = cursor
            if not silent and silent_start is not None:
                end = cursor
                if (end - silent_start) * 1000 >= minimum_silence_ms * sample_rate:
                    intervals.append({"start_frame": silent_start, "end_frame": end})
                silent_start = None
            cursor += len(values)
        if silent_start is not None:
            if (total_frames - silent_start) * 1000 >= minimum_silence_ms * sample_rate:
                intervals.append({"start_frame": silent_start, "end_frame": total_frames})
    return [
        {
            **item,
            "start_ms": round(item["start_frame"] * 1000 / sample_rate),
            "end_ms": round(item["end_frame"] * 1000 / sample_rate),
            "duration_ms": round((item["end_frame"] - item["start_frame"]) * 1000 / sample_rate),
        }
        for item in intervals
    ]


def _copy_frames(source: wave.Wave_read, destination: wave.Wave_write, count: int) -> None:
    remaining = count
    while remaining > 0:
        chunk = source.readframes(min(65536, remaining))
        if not chunk:
            raise AudioNoiseBedError("clean WAV 在预期结束前中断")
        destination.writeframesraw(chunk)
        remaining -= len(chunk) // 2


def _mix_silence_only_wav(
    clean_wav: Path,
    noise_asset: Path,
    output_wav: Path,
    intervals: list[dict[str, int]],
    *,
    target_rms_dbfs: float,
    fade_ms: int,
) -> None:
    with wave.open(str(noise_asset), "rb") as noise:
        if noise.getnchannels() != 1 or noise.getsampwidth() != 2:
            raise AudioNoiseBedError("白噪音素材必须为单声道 16-bit PCM WAV")
        noise_rate = noise.getframerate()
        noise_values = array("h")
        noise_values.frombytes(noise.readframes(noise.getnframes()))
    source_dbfs = wav_rms_dbfs(noise_asset)
    gain = 10.0 ** ((target_rms_dbfs - source_dbfs) / 20.0)
    with wave.open(str(clean_wav), "rb") as clean:
        params = (clean.getnchannels(), clean.getsampwidth(), clean.getframerate())
        if params[0:2] != (1, 2) or params[2] != noise_rate:
            raise AudioNoiseBedError("clean 与白噪音必须为同采样率单声道 16-bit PCM WAV")
        fade_frames = max(0, round(params[2] * fade_ms / 1000))
        with wave.open(str(output_wav), "wb") as destination:
            destination.setnchannels(1)
            destination.setsampwidth(2)
            destination.setframerate(params[2])
            cursor = 0
            noise_cursor = 0
            for interval in intervals:
                start, end = int(interval["start_frame"]), int(interval["end_frame"])
                if start < cursor or end < start:
                    raise AudioNoiseBedError("静音区间重叠或无效")
                _copy_frames(clean, destination, start - cursor)
                active_cursor = start
                interval_frames = end - start
                while active_cursor < end:
                    count = min(65536, end - active_cursor)
                    raw = clean.readframes(count)
                    clean_values = array("h")
                    clean_values.frombytes(raw)
                    mixed = array("h")
                    for offset, clean_value in enumerate(clean_values):
                        position = active_cursor - start + offset
                        envelope = 1.0
                        if fade_frames:
                            envelope = min(
                                1.0,
                                position / fade_frames,
                                (interval_frames - 1 - position) / fade_frames,
                            )
                            envelope = max(0.0, envelope)
                        noise_value = noise_values[noise_cursor % len(noise_values)]
                        noise_cursor += 1
                        value = clean_value + round(noise_value * gain * envelope)
                        mixed.append(max(-32768, min(32767, value)))
                    destination.writeframesraw(mixed.tobytes())
                    active_cursor += len(clean_values)
                cursor = end
            _copy_frames(clean, destination, clean.getnframes() - cursor)


def _encode_wav(
    source_wav: Path,
    output: Path,
    *,
    sample_rate: int,
    audio_format: str,
    mp3_bitrate_kbps: int,
) -> None:
    if audio_format == "wav":
        shutil.copy2(source_wav, output)
        return
    command = [_ffmpeg(), "-v", "error", "-y", "-i", str(source_wav), "-ar", str(sample_rate)]
    if audio_format == "mp3":
        command.extend(["-c:a", "libmp3lame", "-b:a", f"{mp3_bitrate_kbps}k"])
    elif audio_format == "ogg_opus":
        command.extend(["-c:a", "libopus"])
    elif audio_format == "pcm":
        command.extend(["-f", "s16le", "-ac", "1", "-c:a", "pcm_s16le"])
    command.append(str(output))
    _run_ffmpeg(command, "编码")


def add_white_noise_bed(
    clean_audio: Path,
    noise_asset: Path,
    output: Path,
    *,
    target_rms_dbfs: float,
    sample_rate: int,
    audio_format: str,
    silence_threshold_dbfs: float = -60.0,
    minimum_silence_ms: int = 120,
    detection_window_ms: int = 10,
    fade_ms: int = 15,
    mp3_bitrate_kbps: int = 96,
) -> dict[str, Any]:
    clean_audio, noise_asset, output = clean_audio.resolve(), noise_asset.resolve(), output.resolve()
    if not clean_audio.is_file() or not noise_asset.is_file():
        raise AudioNoiseBedError("clean 音频或白噪音素材不存在")
    if not -90.0 <= target_rms_dbfs <= -3.0:
        raise AudioNoiseBedError("白噪音音量必须位于 [-90, -3] dBFS")
    if not 32 <= mp3_bitrate_kbps <= 320:
        raise AudioNoiseBedError("MP3 码率必须位于 [32, 320] kbps")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="silence_noise_bed_", dir=output.parent) as temporary:
        temp_dir = Path(temporary)
        clean_wav = clean_audio if audio_format == "wav" else temp_dir / "clean-decoded.wav"
        if audio_format != "wav":
            _decode_to_wav(
                clean_audio, clean_wav, sample_rate=sample_rate, audio_format=audio_format
            )
        intervals = detect_silence_intervals_wav(
            clean_wav,
            threshold_dbfs=silence_threshold_dbfs,
            minimum_silence_ms=minimum_silence_ms,
            window_ms=detection_window_ms,
        )
        staged = temp_dir / output.name
        if intervals:
            mixed_wav = temp_dir / "silence-only-mixed.wav"
            _mix_silence_only_wav(
                clean_wav,
                noise_asset,
                mixed_wav,
                intervals,
                target_rms_dbfs=target_rms_dbfs,
                fade_ms=fade_ms,
            )
            _encode_wav(
                mixed_wav,
                staged,
                sample_rate=sample_rate,
                audio_format=audio_format,
                mp3_bitrate_kbps=mp3_bitrate_kbps,
            )
        else:
            shutil.copy2(clean_audio, staged)
        os.replace(staged, output)
    if not output.is_file() or output.stat().st_size == 0:
        raise AudioNoiseBedError("白噪音混音没有生成有效输出")
    rendered_intervals = [
        {key: item[key] for key in ("start_ms", "end_ms", "duration_ms")}
        for item in intervals
    ]
    interval_sha256 = hashlib.sha256(
        json.dumps(rendered_intervals, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "path": str(output),
        "sha256": file_sha256(output),
        "clean_audio_sha256": file_sha256(clean_audio),
        "asset_path": str(noise_asset),
        "asset_sha256": file_sha256(noise_asset),
        "asset_rms_dbfs": round(wav_rms_dbfs(noise_asset), 3),
        "volume_dbfs": target_rms_dbfs,
        "mode": "silence_only",
        "silence_threshold_dbfs": silence_threshold_dbfs,
        "minimum_silence_ms": minimum_silence_ms,
        "detection_window_ms": detection_window_ms,
        "fade_ms": fade_ms,
        "mp3_bitrate_kbps": mp3_bitrate_kbps if audio_format == "mp3" else None,
        "silence_interval_count": len(rendered_intervals),
        "silence_duration_ms": sum(item["duration_ms"] for item in rendered_intervals),
        "silence_intervals_sha256": interval_sha256,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成白噪音素材或仅为完整音频的静音区间铺底")
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate-asset")
    generate.add_argument("--output", required=True)
    generate.add_argument("--duration-seconds", type=int, default=30)
    generate.add_argument("--sample-rate", type=int, default=24000)
    generate.add_argument("--rms-dbfs", type=float, default=-20.0)
    generate.add_argument("--seed", type=int, default=20260825)
    mix = subparsers.add_parser("mix")
    mix.add_argument("--clean-audio", required=True)
    mix.add_argument("--noise-asset", required=True)
    mix.add_argument("--output", required=True)
    mix.add_argument("--volume-dbfs", type=float, default=-70.0)
    mix.add_argument("--sample-rate", type=int, default=24000)
    mix.add_argument("--format", choices=("mp3", "wav", "ogg_opus", "pcm"), required=True)
    mix.add_argument("--silence-threshold-dbfs", type=float, default=-60.0)
    mix.add_argument("--minimum-silence-ms", type=int, default=120)
    mix.add_argument("--detection-window-ms", type=int, default=10)
    mix.add_argument("--fade-ms", type=int, default=15)
    mix.add_argument("--mp3-bitrate-kbps", type=int, default=96)
    args = parser.parse_args(argv)
    if args.command == "generate-asset":
        result = generate_white_noise_asset(
            Path(args.output), duration_seconds=args.duration_seconds,
            sample_rate=args.sample_rate, rms_dbfs=args.rms_dbfs, seed=args.seed,
        )
    else:
        result = add_white_noise_bed(
            Path(args.clean_audio), Path(args.noise_asset), Path(args.output),
            target_rms_dbfs=args.volume_dbfs, sample_rate=args.sample_rate,
            audio_format=args.format, silence_threshold_dbfs=args.silence_threshold_dbfs,
            minimum_silence_ms=args.minimum_silence_ms,
            detection_window_ms=args.detection_window_ms, fade_ms=args.fade_ms,
            mp3_bitrate_kbps=args.mp3_bitrate_kbps,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
