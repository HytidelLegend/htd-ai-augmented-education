"""Build FFmpeg argument arrays from structured render and audio plans."""
from __future__ import annotations

import shutil
import os
from pathlib import Path
from typing import Any


def find_ffmpeg(explicit: Path | None = None) -> Path:
    candidate = str(explicit) if explicit else os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")
    if not candidate or not Path(candidate).is_file():
        raise ValueError("找不到 FFmpeg；请安装 FFmpeg 或通过 --ffmpeg 指定路径")
    return Path(candidate).resolve()


def silent_audio_args(ffmpeg: Path, *, duration_ms: int, output: Path) -> list[str | Path]:
    return [ffmpeg, "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{duration_ms / 1000:.3f}", "-c:a", "pcm_s16le", output]


def audio_window_args(ffmpeg: Path, *, audio: Path, start_ms: int, end_ms: int, output: Path) -> list[str | Path]:
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("音频窗口范围无效")
    return [
        ffmpeg, "-y", "-ss", f"{start_ms / 1000:.3f}", "-i", audio,
        "-t", f"{(end_ms - start_ms) / 1000:.3f}", "-c:a", "pcm_s16le", output,
    ]


def audio_mix_args(ffmpeg: Path, *, narration: Path, plan: dict[str, Any], output: Path) -> list[str | Path]:
    pauses = plan.get("pauses", [])
    effects = plan.get("sound_effects", [])
    args: list[str | Path] = [ffmpeg, "-y", "-i", narration]
    for effect in effects:
        args.extend(["-i", Path(effect["path"])])
    parts, cursor, labels = [], 0, []
    for index, pause in enumerate(pauses):
        at = int(pause["at_original_ms"])
        if at > cursor:
            parts.append(f"[0:a]atrim=start={cursor/1000:.3f}:end={at/1000:.3f},asetpts=PTS-STARTPTS[n{index}]")
            labels.append(f"[n{index}]")
        parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={pause['duration_ms']/1000:.3f}[s{index}]")
        labels.append(f"[s{index}]")
        cursor = at
    if cursor < int(plan["original_duration_ms"]):
        parts.append(f"[0:a]atrim=start={cursor/1000:.3f},asetpts=PTS-STARTPTS[ntail]")
        labels.append("[ntail]")
    parts.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[narr]")
    mix_labels = ["[narr]"]
    for index, effect in enumerate(effects, 1):
        delay = max(0, int(effect["playback_at_ms"]))
        parts.append(f"[{index}:a]volume={effect['gain_db']}dB,adelay={delay}|{delay}[fx{index}]")
        mix_labels.append(f"[fx{index}]")
    if effects:
        parts.append("".join(mix_labels) + f"amix=inputs={len(mix_labels)}:duration=longest:normalize=0[mixed]")
    else:
        parts.append("[narr]anull[mixed]")
    parts.append(f"[mixed]atrim=duration={plan['playback_duration_ms']/1000:.3f},asetpts=PTS-STARTPTS[out]")
    args.extend(["-filter_complex", ";".join(parts), "-map", "[out]", "-c:a", "pcm_s16le", output])
    return args


def segment_video_args(
    ffmpeg: Path,
    *,
    frames_pattern: Path,
    fps: int,
    output: Path,
    threads: int | None = None,
) -> list[str | Path]:
    if threads is not None and (isinstance(threads, bool) or not isinstance(threads, int) or threads <= 0):
        raise ValueError("FFmpeg threads 必须是正整数")
    args: list[str | Path] = [ffmpeg, "-y", "-framerate", str(fps), "-i", frames_pattern, "-c:v", "libx264"]
    if threads is not None:
        args.extend(["-threads", str(threads)])
    args.extend(["-pix_fmt", "yuv420p", "-r", str(fps), "-an", output])
    return args


def mux_args(ffmpeg: Path, *, video: Path, audio: Path, output: Path) -> list[str | Path]:
    return [ffmpeg, "-y", "-i", video, "-i", audio, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", output]


def _filter_path(path: Path) -> str:
    """Escape an absolute path for an FFmpeg filter argument on Windows or POSIX."""
    value = path.resolve().as_posix()
    value = value.replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")
    value = value.replace("[", r"\[").replace("]", r"\]").replace(",", r"\,")
    return value


def ass_probe_frame_args(
    ffmpeg: Path,
    *,
    subtitle: Path,
    width: int,
    height: int,
    output: Path,
) -> list[str | Path]:
    """Render one RGB24 frame on a uniform green canvas for subtitle bounds QA."""
    if width <= 0 or height <= 0:
        raise ValueError("字幕探针画布宽高必须为正整数")
    return [
        ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
        f"color=c=0x00FF00:s={width}x{height}:r=1:d=1",
        "-vf", f"ass=filename='{_filter_path(subtitle)}'",
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", output,
    ]


def burned_text_video_args(
    ffmpeg: Path,
    *,
    video: Path,
    subtitle: Path,
    output: Path,
    codec: str,
    crf: int,
    preset: str,
    pixel_format: str,
    duration_ms: int | None = None,
) -> list[str | Path]:
    """Build a command that burns ASS text and preserves every input audio stream."""
    args: list[str | Path] = [
        ffmpeg,
        "-v",
        "error",
        "-n",
        "-i",
        video,
        "-vf",
        f"ass=filename='{_filter_path(subtitle)}'",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-c:v",
        codec,
        "-crf",
        str(crf),
        "-preset",
        preset,
        "-pix_fmt",
        pixel_format,
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
    ]
    if duration_ms is not None:
        if duration_ms <= 0:
            raise ValueError("字幕预览时长必须为正整数")
        args.extend(["-t", f"{duration_ms / 1000:.3f}"])
    args.append(output)
    return args


def burned_text_video_window_args(
    ffmpeg: Path,
    *,
    video: Path,
    subtitle: Path,
    output: Path,
    start_ms: int,
    end_ms: int,
    codec: str,
    crf: int,
    preset: str,
    pixel_format: str,
) -> list[str | Path]:
    """Burn shifted ASS text into a clamped source window and keep every audio track."""
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("视频预览窗口必须满足 0 ≤ start_ms < end_ms")
    return [
        ffmpeg, "-v", "error", "-n",
        "-ss", f"{start_ms / 1000:.3f}",
        "-t", f"{(end_ms - start_ms) / 1000:.3f}",
        "-i", video,
        "-vf", f"ass=filename='{_filter_path(subtitle)}'",
        "-map", "0:v:0", "-map", "0:a?",
        "-map_metadata", "0", "-c:v", codec, "-crf", str(crf),
        "-preset", preset, "-pix_fmt", pixel_format,
        "-c:a", "copy", "-movflags", "+faststart", output,
    ]


def burned_caption_args(
    ffmpeg: Path,
    *,
    video: Path,
    subtitle: Path,
    output: Path,
    codec: str,
    crf: int,
    preset: str,
    pixel_format: str,
    duration_ms: int | None = None,
) -> list[str | Path]:
    """Backward-compatible caption alias for the generic text video renderer."""
    return burned_text_video_args(
        ffmpeg,
        video=video,
        subtitle=subtitle,
        output=output,
        codec=codec,
        crf=crf,
        preset=preset,
        pixel_format=pixel_format,
        duration_ms=duration_ms,
    )


def burned_text_image_args(
    ffmpeg: Path,
    *,
    image: Path,
    subtitle: Path,
    output: Path,
) -> list[str | Path]:
    """Build a command that burns ASS text into one PNG or JPEG without overwrite."""
    suffix = output.suffix.casefold()
    if suffix not in {".png", ".jpg", ".jpeg"}:
        raise ValueError("文本图像输出只支持 .png、.jpg 或 .jpeg")
    args: list[str | Path] = [
        ffmpeg,
        "-v",
        "error",
        "-n",
        "-i",
        image,
        "-vf",
        f"ass=filename='{_filter_path(subtitle)}'",
        "-map_metadata",
        "0",
        "-frames:v",
        "1",
    ]
    if suffix in {".jpg", ".jpeg"}:
        args.extend(["-q:v", "2"])
    args.extend(["-update", "1", output])
    return args


def burned_caption_frame_args(
    ffmpeg: Path,
    *,
    video: Path,
    subtitle: Path,
    at_ms: int,
    output: Path,
) -> list[str | Path]:
    """Build a command that renders one captioned PNG at an exact playback time."""
    if at_ms < 0:
        raise ValueError("字幕单帧预览时间不得为负")
    return [
        ffmpeg,
        "-v",
        "error",
        "-y",
        "-i",
        video,
        "-ss",
        f"{at_ms / 1000:.3f}",
        "-vf",
        f"ass=filename='{_filter_path(subtitle)}'",
        "-frames:v",
        "1",
        output,
    ]


def timed_audio_overlay_args(
    ffmpeg: Path,
    *,
    video: Path,
    cues: list[dict[str, Any]],
    main_audio_stream_index: int | None,
    other_audio_stream_indices: list[int],
    video_duration_ms: int,
    output_duration_ms: int,
    width: int,
    height: int,
    frame_rate: dict[str, int],
    output: Path,
    render_duration_ms: int | None = None,
) -> list[str | Path]:
    """Build an FFmpeg command that mixes timed cues into the default audio track.

    Other source audio streams are mapped unchanged. When the requested output is
    longer than the source video, a black H.264 tail is appended deterministically.
    """
    if video_duration_ms <= 0 or output_duration_ms < video_duration_ms:
        raise ValueError("视频与输出时长必须为正，且输出时长不得短于视频")
    effective_duration_ms = output_duration_ms if render_duration_ms is None else render_duration_ms
    if effective_duration_ms <= 0 or effective_duration_ms > output_duration_ms:
        raise ValueError("实际渲染时长必须为正，且不得超过计划输出时长")
    numerator = int(frame_rate.get("numerator", 0))
    denominator = int(frame_rate.get("denominator", 0))
    if width <= 0 or height <= 0 or numerator <= 0 or denominator <= 0:
        raise ValueError("视频宽高和帧率必须为正")

    args: list[str | Path] = [ffmpeg, "-v", "error", "-n", "-i", video]
    for cue in cues:
        args.extend(["-i", Path(cue["path"])])

    duration_seconds = effective_duration_ms / 1000
    parts: list[str] = []
    if main_audio_stream_index is None:
        parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration_seconds:.3f}[base]")
    else:
        parts.append(
            f"[0:{main_audio_stream_index}]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"apad,atrim=duration={duration_seconds:.3f},asetpts=PTS-STARTPTS[base]"
        )

    mix_labels = ["[base]"]
    for input_index, cue in enumerate(cues, 1):
        delay = int(cue["playback_at_ms"])
        gain = float(cue.get("gain_db", 0))
        parts.append(
            f"[{input_index}:a:0]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"volume={gain:g}dB,adelay={delay}|{delay},apad,"
            f"atrim=duration={duration_seconds:.3f}[fx{input_index}]"
        )
        mix_labels.append(f"[fx{input_index}]")
    if cues:
        left_channels = "+".join(f"c{index * 2}" for index in range(len(mix_labels)))
        right_channels = "+".join(f"c{index * 2 + 1}" for index in range(len(mix_labels)))
        parts.append(
            "".join(mix_labels)
            + f"amerge=inputs={len(mix_labels)},pan=stereo|c0={left_channels}|c1={right_channels},"
            + f"atrim=duration={duration_seconds:.3f},asetpts=PTS-STARTPTS[mixed]"
        )
    else:
        parts.append("[base]anull[mixed]")

    extend_video = effective_duration_ms > video_duration_ms
    clip_video = effective_duration_ms < video_duration_ms
    rate = f"{numerator}/{denominator}"
    if extend_video:
        extension_seconds = (effective_duration_ms - video_duration_ms) / 1000
        parts.extend(
            [
                f"[0:v:0]fps={rate},format=yuv420p,setpts=PTS-STARTPTS[vmain]",
                f"color=c=black:s={width}x{height}:r={rate}:d={extension_seconds:.3f},format=yuv420p[vblack]",
                "[vmain][vblack]concat=n=2:v=1:a=0[vout]",
            ]
        )
    elif clip_video:
        parts.append(
            f"[0:v:0]trim=duration={duration_seconds:.3f},fps={rate},"
            "format=yuv420p,setpts=PTS-STARTPTS[vout]"
        )

    args.extend(["-filter_complex", ";".join(parts)])
    transform_video = extend_video or clip_video
    args.extend(["-map", "[vout]" if transform_video else "0:v:0", "-map", "[mixed]"])
    for stream_index in other_audio_stream_indices:
        args.extend(["-map", f"0:{stream_index}"])

    if transform_video:
        args.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p"])
    else:
        args.extend(["-c:v", "copy"])
    args.extend(["-c:a:0", "aac", "-b:a:0", "192k"])
    for output_audio_index in range(1, len(other_audio_stream_indices) + 1):
        args.extend([f"-c:a:{output_audio_index}", "copy"])
    args.extend(
        [
            "-map_metadata",
            "0",
            "-map_chapters",
            "0",
            "-t",
            f"{duration_seconds:.3f}",
            "-movflags",
            "+faststart",
            output,
        ]
    )
    return args
