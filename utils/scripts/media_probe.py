"""Deterministic FFprobe wrapper returning normalized stream metadata."""
from __future__ import annotations

import json
import shutil
import os
from fractions import Fraction
from pathlib import Path
from typing import Any

from .subprocess_runner import run_command


class MediaProbeError(ValueError):
    pass


def find_ffprobe(explicit: Path | None = None) -> Path:
    candidate = str(explicit) if explicit else os.environ.get("FFPROBE_PATH") or shutil.which("ffprobe")
    if not candidate or not Path(candidate).is_file():
        raise MediaProbeError("找不到 FFprobe；请安装 FFmpeg 或通过 --ffprobe 指定路径")
    return Path(candidate).resolve()


def _duration_ms(value: Any) -> int | None:
    try:
        return round(float(value) * 1000)
    except (TypeError, ValueError):
        return None


def _rate(value: str | None) -> dict[str, int] | None:
    if not value or value == "0/0":
        return None
    rate = Fraction(value)
    return {"numerator": rate.numerator, "denominator": rate.denominator}


def probe(path: Path, *, ffprobe: Path | None = None, log_path: Path | None = None) -> dict[str, Any]:
    media = path.resolve()
    if not media.is_file() or media.stat().st_size == 0:
        raise MediaProbeError(f"媒体文件不存在或为空：{media}")
    binary = find_ffprobe(ffprobe)
    result = run_command([binary, "-v", "error", "-show_format", "-show_streams", "-of", "json", media], log_path=log_path)
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaProbeError("FFprobe 未返回有效 JSON") from exc
    streams = []
    for item in raw.get("streams", []):
        stream = {"index": item.get("index"), "type": item.get("codec_type"), "codec": item.get("codec_name"), "duration_ms": _duration_ms(item.get("duration"))}
        if item.get("codec_type") == "video":
            stream.update({"width": item.get("width"), "height": item.get("height"), "frame_rate": _rate(item.get("avg_frame_rate") or item.get("r_frame_rate"))})
        if item.get("codec_type") == "audio":
            stream.update(
                {
                    "sample_rate": int(item["sample_rate"]) if item.get("sample_rate") else None,
                    "channels": item.get("channels"),
                    "default": bool(item.get("disposition", {}).get("default", 0)),
                }
            )
        streams.append(stream)
    return {"path": str(media), "size_bytes": media.stat().st_size, "duration_ms": _duration_ms(raw.get("format", {}).get("duration")), "format": raw.get("format", {}).get("format_name"), "streams": streams}
