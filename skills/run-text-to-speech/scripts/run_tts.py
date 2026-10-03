"""Batch Edge / Volcengine TTS runner with resumable state and merged timestamps."""

from __future__ import annotations

import argparse
import copy
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import wave
import unicodedata

import yaml

sys.stdout.reconfigure(encoding="utf-8")

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PROJECT_DIR = next(p for p in Path(__file__).resolve().parents if (p / 'utils/scripts/timestamp.py').is_file())
if str(DEFAULT_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(DEFAULT_PROJECT_DIR))

from utils.scripts.file_transaction import file_sha256, project_lock
from utils.scripts.audio_clip import AudioClipError, clip_audio
from utils.scripts.audio_noise_bed import AudioNoiseBedError, add_white_noise_bed
from utils.scripts.structured_io import read_json, structured_error_receipt
from utils.scripts.run_state import write_json
from utils.scripts.speech_pause_markers import (
    MARKER_VERSION,
    marker_summary,
    parse_pause_markers,
    remove_pause_markers,
)
from utils.scripts.text_spans import apply_text_edits, find_literal_spans, spans_overlap
from utils.scripts.timestamp import iso_timestamp
from utils.scripts.tts_backend import BACKENDS, ADAPTER_VERSION, check_backend, dotenv_credential, validate_backend_parameters, validate_voice_mapping, validate_backend_configuration
from utils.scripts.speech_cli import transcript_next_action
from utils.scripts.ffmpeg_plan import find_ffmpeg
from utils.scripts.media_probe import find_ffprobe
from utils.scripts.speech_handoff import build_handoff, verify_handoff, producer_paths
from utils.scripts.tts_workspace import (
    TtsWorkspace,
    create_workspace,
    input_metadata,
    inputs_match,
    open_workspace,
    update_component,
)
from utils.scripts.workflow_state import (
    WorkflowDefinition,
    WorkflowStateError,
    WorkflowStateStore,
)


BOUNDARY_CHARS = {
    "sentence_end": set("。."),
    "question": set("？?"),
    "exclamation": set("！!"),
    "semicolon": set("；;"),
    "colon": set("：:"),
    "comma": set("，,、"),
}
SENTENCE_END_CHARS = set("。.!！?？")
SYNTHESIS_CLOSING_CHARS = "”’\"'）)]】』」》〉"
SUPPORTED_INPUT_SUFFIXES = {".txt", ".md"}
SUPPORTED_FORMATS = {"mp3", "wav", "ogg_opus", "pcm"}
FORMAT_EXTENSIONS = {"mp3": "mp3", "wav": "wav", "ogg_opus": "ogg", "pcm": "pcm"}
SUSPICIOUS_INLINE_ESCAPE_SEQUENCES = ("`r`n", "`n", "`r", "\\r\\n", "\\n", "\\r")
WORKFLOW = "run-text-to-speech"
ACTIVE_STAGES = (
    "initialized",
    "workspace_resolved",
    "input_validated",
    "backend_resolved",
    "checking_backend_environment",
    "awaiting_transcript",
    "transcript_validated",
    "input_staged",
    "batches_planned",
    "preview_synthesizing",
    "preview_rendering",
    "preview_ready",
    "synthesizing",
    "batches_ready",
    "merging_audio",
    "timestamps_merged",
    "adding_white_noise",
    "final_audio_ready",
    "verifying",
)
PAUSED_STATES = (
    "paused_input_mismatch",
    "paused_transcript_not_ready",
    "paused_preview_approval",
    "paused_retryable",
    "paused_configuration",
    "paused_verification",
)


def _tts_transitions() -> dict[str, set[str]]:
    next_stage = {
        "initialized": "workspace_resolved",
        "workspace_resolved": "input_validated",
        "input_validated": "backend_resolved",
        "backend_resolved": "checking_backend_environment",
        "checking_backend_environment": "awaiting_transcript",
        "awaiting_transcript": "transcript_validated",
        "transcript_validated": "input_staged",
        "input_staged": "batches_planned",
        "batches_planned": "preview_synthesizing",
        "preview_synthesizing": "preview_rendering",
        "preview_rendering": "preview_ready",
        "preview_ready": "paused_preview_approval",
        "paused_preview_approval": "synthesizing",
        "synthesizing": "batches_ready",
        "batches_ready": "merging_audio",
        "merging_audio": "timestamps_merged",
        "timestamps_merged": "adding_white_noise",
        "adding_white_noise": "final_audio_ready",
        "final_audio_ready": "verifying",
        "verifying": "completed",
    }
    transitions: dict[str, set[str]] = {}
    for stage in ACTIVE_STAGES:
        targets = {"superseded", "failed_terminal", *PAUSED_STATES}
        if stage in next_stage:
            targets.add(next_stage[stage])
        transitions[stage] = targets
    transitions["input_validated"].add("awaiting_transcript")  # legacy checkpoints
    transitions["batches_planned"].add("synthesizing")
    transitions["preview_synthesizing"].add("batches_ready")
    for paused in PAUSED_STATES:
        transitions[paused] = {"superseded", "failed_terminal", *ACTIVE_STAGES}
    transitions["completed"] = set()
    transitions["superseded"] = set()
    transitions["failed_terminal"] = set()
    return transitions


TTS_STATE_MACHINE = WorkflowDefinition.build(
    name=WORKFLOW,
    transitions=_tts_transitions(),
)


class TTSWorkflowError(RuntimeError):
    """Raised for user-correctable workflow failures."""


class TTSRetryableError(TTSWorkflowError):
    """Raised for transient failures that can resume at the same stage."""


class TTSConfigurationError(TTSWorkflowError):
    """Raised when credentials, voices, or required local tools are unavailable."""


class TTSVerificationError(TTSWorkflowError):
    """Raised when generated artifacts fail deterministic verification."""


class TTSTerminalError(TTSWorkflowError):
    """Raised when safe recovery cannot be inferred from persisted state."""


def load_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or SKILL_DIR / "config.yaml"
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise TTSWorkflowError(f"配置文件无效：{config_path}")
    try:
        validate_backend_configuration(data)
    except ValueError as exc:
        raise TTSWorkflowError(str(exc)) from exc
    preview = data.get("preview")
    if not isinstance(preview, dict) or not isinstance(preview.get("enabled"), bool):
        raise TTSWorkflowError("preview.enabled 必须为布尔值")
    duration_seconds = preview.get("duration_seconds")
    if not isinstance(duration_seconds, (int, float)) or not 1 <= float(duration_seconds) <= 600:
        raise TTSWorkflowError("preview.duration_seconds 必须位于 [1, 600]")
    noise = data.get("white_noise")
    if not isinstance(noise, dict) or not isinstance(noise.get("enabled"), bool):
        raise TTSWorkflowError("white_noise.enabled 必须为布尔值")
    volume_dbfs = noise.get("volume_dbfs")
    if not isinstance(volume_dbfs, (int, float)) or not -90.0 <= float(volume_dbfs) <= -3.0:
        raise TTSWorkflowError("white_noise.volume_dbfs 必须位于 [-90, -3]")
    if not str(noise.get("asset_path", "")).strip():
        raise TTSWorkflowError("white_noise.asset_path 不能为空")
    if not -96.0 <= float(noise.get("silence_threshold_dbfs", -60.0)) <= -20.0:
        raise TTSWorkflowError("white_noise.silence_threshold_dbfs 必须位于 [-96, -20]")
    detection_window_ms = int(noise.get("detection_window_ms", 0))
    minimum_silence_ms = int(noise.get("minimum_silence_ms", 0))
    if detection_window_ms <= 0:
        raise TTSWorkflowError("white_noise.detection_window_ms 必须是正整数")
    if minimum_silence_ms < detection_window_ms:
        raise TTSWorkflowError("white_noise.minimum_silence_ms 不得小于 detection_window_ms")
    if int(noise.get("fade_ms", -1)) < 0:
        raise TTSWorkflowError("white_noise.fade_ms 必须是非负整数")
    if not 32 <= int(noise.get("mp3_bitrate_kbps", 0)) <= 320:
        raise TTSWorkflowError("white_noise.mp3_bitrate_kbps 必须位于 [32, 320]")
    return data


def read_env_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return None


def normalize_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise TTSWorkflowError("逐字稿不能为空")
    return normalized


def suspicious_inline_escape_sequences(text: str) -> list[str]:
    return [sequence for sequence in SUSPICIOUS_INLINE_ESCAPE_SEQUENCES if sequence in text]


def validate_inline_text_transport(text: str) -> str:
    normalized = normalize_text(text)
    suspicious = suspicious_inline_escape_sequences(normalized)
    if suspicious:
        rendered = "、".join(json.dumps(item, ensure_ascii=False) for item in suspicious)
        raise TTSWorkflowError(
            f"--text 包含疑似 shell 换行转义字面量：{rendered}；"
            "请将多行逐字稿保存为 UTF-8 文本文件并改用 --input-file"
        )
    return normalized


def has_sentence_ending(text: str) -> bool:
    candidate = text.rstrip().rstrip(SYNTHESIS_CLOSING_CHARS)
    return bool(candidate) and candidate[-1] in SENTENCE_END_CHARS


def build_synthesis_text_and_map(
    text: str, missing_sentence_ending: str = "。"
) -> tuple[str, dict[str, Any]]:
    if not missing_sentence_ending or "\n" in missing_sentence_ending or "\r" in missing_sentence_ending:
        raise TTSWorkflowError("合成文本补全标点配置无效")
    normalized_source = normalize_text(text)
    pause_markers = parse_pause_markers(normalized_source)
    line_records: list[dict[str, Any]] = []
    for match in re.finditer(r"[^\n]+", normalized_source):
        raw = match.group(0)
        stripped = raw.strip()
        if not stripped:
            continue
        leading = len(raw) - len(raw.lstrip())
        source_start = match.start() + leading
        source_end = source_start + len(stripped)
        line_markers = [
            item
            for item in pause_markers
            if source_start <= int(item["source_span"]["start"])
            and int(item["source_span"]["end"]) <= source_end
        ]
        relative_markers = [
            {
                **item,
                "source_span": {
                    "start": int(item["source_span"]["start"]) - source_start,
                    "end": int(item["source_span"]["end"]) - source_start,
                },
            }
            for item in line_markers
        ]
        spoken_raw = remove_pause_markers(stripped, relative_markers)
        spoken_leading = len(spoken_raw) - len(spoken_raw.lstrip())
        spoken = spoken_raw.strip()
        local_pauses = []
        removed = 0
        for item in relative_markers:
            marker_start = int(item["source_span"]["start"])
            raw_offset = marker_start - removed
            local_pauses.append(
                {
                    **line_markers[len(local_pauses)],
                    "line_spoken_offset": max(0, min(len(spoken), raw_offset - spoken_leading)),
                }
            )
            removed += len(str(item["directive"]))
        line_records.append(
            {
                "source_start": source_start,
                "source_end": source_end,
                "source_text": stripped,
                "spoken_text": spoken,
                "pauses": local_pauses,
            }
        )
    spoken_record_indexes = [
        index for index, record in enumerate(line_records) if record["spoken_text"]
    ]
    if not spoken_record_indexes:
        raise TTSWorkflowError("逐字稿没有可合成内容")
    last_spoken_record = spoken_record_indexes[-1]
    paragraphs: list[dict[str, Any]] = []
    synthesis_parts: list[str] = []
    synthesis_offset = 0
    mapped_pauses: list[dict[str, Any]] = []
    for record_index, record in enumerate(line_records):
        spoken = str(record["spoken_text"])
        suffix = ""
        if spoken and record_index < last_spoken_record and not has_sentence_ending(spoken):
            suffix = missing_sentence_ending
        rendered = spoken + suffix
        for item in record["pauses"]:
            local_offset = int(item["line_spoken_offset"])
            if spoken and local_offset == len(spoken):
                local_offset = len(rendered)
            mapped_pauses.append(
                {
                    **{key: value for key, value in item.items() if key != "line_spoken_offset"},
                    "synthesis_offset": synthesis_offset + local_offset,
                }
            )
        if rendered:
            paragraph = {
                "paragraph_id": f"paragraph_{len(paragraphs) + 1:03d}",
                "source_span": {
                    "start": int(record["source_start"]),
                    "end": int(record["source_end"]),
                },
                "source_text": record["source_text"],
                "synthesis_span": {
                    "start": synthesis_offset,
                    "end": synthesis_offset + len(rendered),
                },
                "synthesis_text": rendered,
                "inserted_suffix": suffix,
                "batch_ids": [],
            }
            paragraphs.append(paragraph)
            synthesis_parts.append(rendered)
            synthesis_offset += len(rendered)
    synthesis_text = "".join(synthesis_parts)
    mapping = {
        "schema_version": "1.1",
        "source_coordinate_system": "UTF-8 decoded text with CRLF/CR normalized to LF; character offsets",
        "synthesis_coordinate_system": "character offsets in inputs/generated/synthesis.txt",
        "normalized_source_text_sha256": text_sha256(normalized_source),
        "synthesis_text_sha256": text_sha256(synthesis_text),
        "pause_marker_version": MARKER_VERSION,
        "pause_summary": marker_summary(mapped_pauses),
        "pauses": mapped_pauses,
        "paragraphs": paragraphs,
        "batches": [],
    }
    return synthesis_text, mapping


def normalize_synthesis_text(text: str, missing_sentence_ending: str = "。") -> str:
    return build_synthesis_text_and_map(text, missing_sentence_ending)[0]


def annotate_synthesis_map(
    mapping: dict[str, Any], batches: list[dict[str, Any]], synthesis_text: str
) -> dict[str, Any]:
    offset = 0
    mapped_batches: list[dict[str, Any]] = []
    for batch in batches:
        start = offset
        end = start + len(str(batch["text"]))
        segments: list[dict[str, Any]] = []
        paragraph_ids: list[str] = []
        for paragraph in mapping["paragraphs"]:
            paragraph_span = paragraph["synthesis_span"]
            overlap_start = max(start, int(paragraph_span["start"]))
            overlap_end = min(end, int(paragraph_span["end"]))
            if overlap_start >= overlap_end:
                continue
            paragraph_id = str(paragraph["paragraph_id"])
            paragraph_ids.append(paragraph_id)
            if batch["batch_id"] not in paragraph["batch_ids"]:
                paragraph["batch_ids"].append(batch["batch_id"])
            segments.append(
                {
                    "paragraph_id": paragraph_id,
                    "synthesis_span": {"start": overlap_start, "end": overlap_end},
                    "batch_local_span": {
                        "start": overlap_start - start,
                        "end": overlap_end - start,
                    },
                    "text": synthesis_text[overlap_start:overlap_end],
                }
            )
        metadata = {
            "batch_id": batch["batch_id"],
            "batch_index": int(batch["index"]),
            "synthesis_span": {"start": start, "end": end},
            "paragraph_ids": paragraph_ids,
            "paragraph_segments": segments,
            "text_sha256": text_sha256(str(batch["text"])),
        }
        batch.update(
            synthesis_span=metadata["synthesis_span"],
            paragraph_ids=paragraph_ids,
            paragraph_segments=segments,
        )
        mapped_batches.append(metadata)
        offset = end
    if offset != len(synthesis_text) or "".join(str(batch["text"]) for batch in batches) != synthesis_text:
        raise TTSVerificationError("batch 文本无法完整覆盖合成文本")
    mapping["batches"] = mapped_batches
    for pause in mapping.get("pauses", []):
        position = int(pause["synthesis_offset"])
        previous = next(
            (item for item in reversed(mapped_batches) if int(item["synthesis_span"]["end"]) <= position),
            None,
        )
        following = next(
            (item for item in mapped_batches if int(item["synthesis_span"]["start"]) >= position),
            None,
        )
        pause["after_batch_id"] = previous["batch_id"] if previous else None
        pause["before_batch_id"] = following["batch_id"] if following else None
    return mapping


def validate_synthesis_map(
    mapping: dict[str, Any], normalized_source: str, synthesis_text: str, batches: list[dict[str, Any]]
) -> list[str]:
    errors: list[str] = []
    paragraphs = mapping.get("paragraphs")
    if not isinstance(paragraphs, list) or not paragraphs:
        return ["合成映射缺少 paragraphs"]
    if mapping.get("normalized_source_text_sha256") != text_sha256(normalized_source):
        errors.append("合成映射的原文指纹不一致")
    if mapping.get("synthesis_text_sha256") != text_sha256(synthesis_text):
        errors.append("合成映射的合成文本指纹不一致")
    try:
        expected_text, expected_mapping = build_synthesis_text_and_map(normalized_source)
        if expected_text != synthesis_text:
            errors.append("停顿标记移除后的合成文本不一致")
        comparable = ("pause_id", "directive", "duration_ms", "source_span", "synthesis_offset")
        actual_pauses = [
            {key: item.get(key) for key in comparable} for item in mapping.get("pauses", [])
        ]
        expected_pauses = [
            {key: item.get(key) for key in comparable} for item in expected_mapping.get("pauses", [])
        ]
        if actual_pauses != expected_pauses:
            errors.append("停顿事件与逐字稿指令不一致")
    except TTSWorkflowError as exc:
        errors.append(f"停顿事件无法重建：{exc}")
    rebuilt = "".join(str(item.get("synthesis_text", "")) for item in paragraphs)
    if rebuilt != synthesis_text:
        errors.append("段落映射无法重建合成文本")
    previous_source_end = -1
    previous_synthesis_end = 0
    for paragraph in paragraphs:
        source_span = paragraph.get("source_span") or {}
        synthesis_span = paragraph.get("synthesis_span") or {}
        source_start = int(source_span.get("start", -1))
        source_end = int(source_span.get("end", -1))
        synthesis_start = int(synthesis_span.get("start", -1))
        synthesis_end = int(synthesis_span.get("end", -1))
        if source_start < previous_source_end or source_end <= source_start:
            errors.append("原文段落 span 非法或重叠")
        if synthesis_start != previous_synthesis_end or synthesis_end <= synthesis_start:
            errors.append("合成段落 span 不连续")
        if normalized_source[source_start:source_end] != paragraph.get("source_text"):
            errors.append(f"{paragraph.get('paragraph_id')} 原文 span 与文本不一致")
        if synthesis_text[synthesis_start:synthesis_end] != paragraph.get("synthesis_text"):
            errors.append(f"{paragraph.get('paragraph_id')} 合成 span 与文本不一致")
        previous_source_end = source_end
        previous_synthesis_end = synthesis_end
    if batches:
        try:
            expected = json.loads(json.dumps(mapping, ensure_ascii=False))
            for paragraph in expected["paragraphs"]:
                paragraph["batch_ids"] = []
            annotate_synthesis_map(expected, [dict(batch) for batch in batches], synthesis_text)
            if expected.get("batches") != mapping.get("batches"):
                errors.append("合成映射中的 batch 映射不一致")
            expected_ids = {
                paragraph["paragraph_id"]: paragraph["batch_ids"] for paragraph in expected["paragraphs"]
            }
            actual_ids = {
                paragraph["paragraph_id"]: paragraph.get("batch_ids", []) for paragraph in paragraphs
            }
            if expected_ids != actual_ids:
                errors.append("段落到 batch 的反向映射不一致")
        except (KeyError, TypeError, ValueError, TTSWorkflowError) as exc:
            errors.append(f"无法验证 batch 映射：{exc}")
    return errors


def counted_chars(text: str, count_whitespace: bool = False) -> int:
    if count_whitespace:
        return len(text)
    return sum(1 for char in text if not char.isspace())


def speech_rate_multiplier(speech_rate: int) -> float:
    if not -50 <= speech_rate <= 100:
        raise TTSWorkflowError("speech_rate 必须位于 [-50, 100]")
    return 1.0 + speech_rate / 100.0


def effective_batch_range(config: dict[str, Any], speech_rate: int) -> tuple[int, int]:
    batching = config["batching"]
    multiplier = speech_rate_multiplier(speech_rate)
    minimum = max(1, round(int(batching["base_min_chars"]) * multiplier))
    maximum = max(minimum, round(int(batching["base_max_chars"]) * multiplier))
    return minimum, maximum


def find_boundaries(text: str) -> list[tuple[int, str]]:
    boundaries: list[tuple[int, str]] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\n":
            end = index + 1
            while end < len(text) and text[end] == "\n":
                end += 1
            boundaries.append((end, "paragraph"))
            index = end
            continue
        for kind, chars in BOUNDARY_CHARS.items():
            if char in chars:
                boundaries.append((index + 1, kind))
                break
        index += 1
    if not boundaries or boundaries[-1][0] != len(text):
        boundaries.append((len(text), "end_of_text"))
    return boundaries


def split_batches(
    text: str,
    minimum: int,
    maximum: int,
    priority: list[str],
) -> list[dict[str, Any]]:
    if minimum < 1 or maximum < minimum:
        raise TTSWorkflowError("batch 字数范围无效")
    rank = {kind: index for index, kind in enumerate(priority)}
    boundaries = find_boundaries(text)
    batches: list[dict[str, Any]] = []
    start = 0
    while start < len(text):
        remaining = text[start:]
        if counted_chars(remaining) <= maximum:
            end, kind = len(text), "end_of_text"
        else:
            candidates = [
                (position, kind, counted_chars(text[start:position]))
                for position, kind in boundaries
                if position > start
            ]
            in_range = [item for item in candidates if minimum <= item[2] <= maximum]
            under_max = [item for item in candidates if item[2] <= maximum]
            if in_range:
                position, kind, _ = min(
                    in_range,
                    key=lambda item: (rank.get(item[1], len(rank)), -item[0]),
                )
                end = position
            elif under_max:
                position, kind, _ = max(under_max, key=lambda item: item[0])
                end = position
            else:
                # No natural pause before the limit: preserve the clause and accept oversize.
                position, kind, _ = candidates[0]
                end = position
        chunk = text[start:end]
        if chunk:
            batches.append(
                {
                    "index": len(batches) + 1,
                    "text": chunk,
                    "char_count": counted_chars(chunk),
                    "boundary": kind,
                }
            )
        start = end
    if not batches:
        raise TTSWorkflowError("无法从逐字稿生成 batch")
    return batches


def split_batches_with_pauses(
    text: str,
    pauses: list[dict[str, Any]],
    minimum: int,
    maximum: int,
    priority: list[str],
) -> list[dict[str, Any]]:
    """Split at every explicit pause before applying ordinary natural-boundary batching."""
    positions = sorted({int(item["synthesis_offset"]) for item in pauses})
    if any(position < 0 or position > len(text) for position in positions):
        raise TTSWorkflowError("停顿事件超出合成文本范围")
    batches: list[dict[str, Any]] = []
    cursor = 0
    for end in positions + [len(text)]:
        segment = text[cursor:end]
        if segment:
            segment_batches = split_batches(segment, minimum, maximum, priority)
            for batch in segment_batches:
                batches.append({**batch, "index": len(batches) + 1})
            if end in positions:
                batches[-1]["boundary"] = "explicit_pause"
        cursor = end
    if not batches:
        raise TTSWorkflowError("停顿指令之外没有可合成文本")
    if "".join(str(item["text"]) for item in batches) != text:
        raise TTSVerificationError("显式停顿分段无法重建合成文本")
    return batches


def resolve_voice(
    speaker: str | None, requested_resource_id: str | None, config: dict[str, Any]
) -> tuple[str, str, str]:
    voice_path = SKILL_DIR / "references" / "voices.yaml"
    with voice_path.open("r", encoding="utf-8") as handle:
        voices_data = yaml.safe_load(handle) or {}
    try:
        validate_voice_mapping(voices_data, config.get("backend", "volcengine"))
    except ValueError as exc:
        raise TTSConfigurationError(str(exc)) from exc
    if config.get("backend", "volcengine") == "edge-tts":
        if requested_resource_id:
            raise TTSConfigurationError("edge-tts 不支持 resource-id")
        selected = speaker or voices_data["edge_tts"]["default_voice"]
        for voice in voices_data["edge_tts"]["voices"]:
            if selected in {voice["name"], voice["speaker_id"]}:
                return voice["name"], voice["speaker_id"], ""
        if not re.fullmatch(r"[a-z]{2,}-[A-Z]{2,}-.+Neural", str(selected)):
            raise TTSConfigurationError("无效的 Edge 音色名称或 ID")
        return str(selected), str(selected), ""
    selected = speaker or voices_data.get("default_voice")
    if not selected:
        raise TTSConfigurationError("未指定音色，且音色列表没有 default_voice")
    for voice in voices_data.get("voices", []):
        if selected in {voice.get("name"), voice.get("speaker_id")}:
            resource_id = requested_resource_id or voice.get("resource_id")
            speaker_id = str(voice["speaker_id"])
            if not resource_id and speaker_id.startswith("S_"):
                resource_id = config["api"].get("default_icl_resource_id", "seed-icl-2.0")
            return str(voice["name"]), speaker_id, str(
                resource_id or config["api"]["default_resource_id"]
            )
    if requested_resource_id:
        return str(selected), str(selected), str(requested_resource_id)
    if str(selected).startswith("S_"):
        return str(selected), str(selected), str(
            config["api"].get("default_icl_resource_id", "seed-icl-2.0")
        )
    return str(selected), str(selected), str(config["api"]["default_resource_id"])


def build_payload(text: str, speaker_id: str, params: dict[str, Any]) -> dict[str, Any]:
    audio_params: dict[str, Any] = {
        "format": params["format"],
        "sample_rate": params["sample_rate"],
        "speech_rate": params["speech_rate"],
        "loudness_rate": params["loudness_rate"],
        "enable_timestamp": True,
        "enable_subtitle": True,
    }
    if params.get("emotion"):
        audio_params["emotion"] = params["emotion"]
        audio_params["emotion_scale"] = params["emotion_scale"]
    return {
        "user": {"uid": "content_factory_tts"},
        "req_params": {
            "speaker": speaker_id,
            "audio_params": audio_params,
            "text": text,
            "ssml": "",
            "additions": json.dumps(
                {"disable_markdown_filter": True}, ensure_ascii=False
            ),
        },
    }


Transport = Callable[[str, dict[str, str], dict[str, Any], int], Iterable[bytes]]


def urllib_transport(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: int,
) -> Iterable[bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=body, headers=headers, method="POST")
    with urlopen(request, timeout=timeout_seconds) as response:
        for line in response:
            yield line.rstrip(b"\r\n")


def parse_stream(lines: Iterable[bytes]) -> tuple[bytes, list[dict[str, Any]]]:
    audio = bytearray()
    timestamp_events: list[dict[str, Any]] = []
    for line in lines:
        if not line:
            continue
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            audio.extend(line)
            continue
        code = event.get("code")
        if code == 0:
            if event.get("data"):
                audio.extend(base64.b64decode(event["data"]))
            if isinstance(event.get("sentence"), dict):
                timestamp_events.append(event["sentence"])
        elif code == 20000000:
            continue
        else:
            if code in {45000000, 45000001, 45000002}:
                raise TTSConfigurationError(f"API 鉴权或音色配置错误：{event}")
            if code in {55000000, 55000001} or "quota" in str(event).casefold():
                raise TTSRetryableError(f"API 临时错误：{event}")
            raise TTSWorkflowError(f"API 返回错误：{event}")
    if not audio:
        raise TTSVerificationError("API 未返回音频数据")
    if not timestamp_events:
        raise TTSVerificationError("API 未返回时间戳数据")
    return bytes(audio), timestamp_events


class VolcengineClient:
    def __init__(
        self,
        api_key: str,
        config: dict[str, Any],
        transport: Transport = urllib_transport,
    ) -> None:
        self.api_key = api_key
        self.config = config
        self.transport = transport

    def synthesize(
        self,
        text: str,
        speaker_id: str,
        resource_id: str,
        params: dict[str, Any],
    ) -> tuple[bytes, list[dict[str, Any]]]:
        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": resource_id,
            "Content-Type": "application/json",
        }
        payload = build_payload(text, speaker_id, params)
        api = self.config["api"]
        attempts = int(api["max_retries"])
        for attempt in range(1, attempts + 1):
            try:
                lines = self.transport(
                    str(api["url"]), headers, payload, int(api["timeout_seconds"])
                )
                return parse_stream(lines)
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                if attempt >= attempts:
                    raise TTSRetryableError(f"API 请求失败：{exc}") from exc
                time.sleep(float(api["retry_backoff_seconds"]) * attempt)
        raise AssertionError("unreachable")


def create_mock_wav(text: str, sample_rate: int, speech_rate: int) -> tuple[bytes, list[dict[str, Any]]]:
    multiplier = speech_rate_multiplier(speech_rate)
    item_chars = [char for char in text if not char.isspace()]
    duration_ms = max(400, round(len(item_chars) / (4.0 * multiplier) * 1000))
    frame_count = max(1, round(sample_rate * duration_ms / 1000))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * frame_count)
    items: list[dict[str, Any]] = []
    for index, char in enumerate(item_chars):
        start_ms = round(index * duration_ms / max(1, len(item_chars)))
        end_ms = round((index + 1) * duration_ms / max(1, len(item_chars)))
        items.append({"text": char, "start_ms": start_ms, "end_ms": end_ms})
    return buffer.getvalue(), [{"items": items}]


class MockClient:
    def synthesize(
        self,
        text: str,
        speaker_id: str,
        resource_id: str,
        params: dict[str, Any],
    ) -> tuple[bytes, list[dict[str, Any]]]:
        del speaker_id, resource_id
        return create_mock_wav(text, int(params["sample_rate"]), int(params["speech_rate"]))


def extract_time(item: dict[str, Any], names: tuple[str, ...]) -> int | None:
    for name in names:
        if name in item and item[name] is not None:
            value = float(item[name])
            if name in {"startTime", "endTime"}:
                value *= 1000
            return round(value)
    return None


def normalize_items(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for event in events:
        collections = []
        for key in ("items", "words", "tokens"):
            value = event.get(key)
            if isinstance(value, list):
                collections.extend(value)
        for item in collections:
            if not isinstance(item, dict):
                continue
            text = item.get("text", item.get("word", item.get("token", "")))
            start = extract_time(item, ("start_ms", "start_time", "start", "startTime"))
            end = extract_time(item, ("end_ms", "end_time", "end", "endTime"))
            if text != "" and start is not None and end is not None and end >= start:
                normalized.append(
                    {"text": str(text), "start_ms": start, "end_ms": end}
                )
    normalized.sort(key=lambda item: (item["start_ms"], item["end_ms"]))
    if not normalized:
        raise TTSVerificationError("无法从 API 时间戳中解析字／词级 items")
    return normalized


def sentence_chunks(text: str) -> list[str]:
    chunks: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char in SENTENCE_END_CHARS or char == "\n":
            end = index + 1
            chunk = text[start:end]
            if chunk.strip():
                chunks.append(chunk)
            start = end
        index += 1
    tail = text[start:]
    if tail.strip():
        chunks.append(tail)
    return chunks or [text]


def derive_sentences(text: str, items: list[dict[str, Any]], *, word_boundaries: bool = False) -> list[dict[str, Any]]:
    count = (lambda value: sum(not c.isspace() and not unicodedata.category(c).startswith("P") for c in value)) if word_boundaries else counted_chars
    chunks = sentence_chunks(text)
    sentences: list[dict[str, Any]] = []
    item_index = 0
    for chunk in chunks:
        target_chars = count(chunk)
        if target_chars == 0:
            continue
        first = item_index
        consumed = 0
        while item_index < len(items) and consumed < target_chars:
            consumed += count(str(items[item_index]["text"]))
            item_index += 1
        if item_index == first:
            continue
        selected = items[first:item_index]
        sentences.append(
            {
                "text": chunk,
                "start_ms": selected[0]["start_ms"],
                "end_ms": selected[-1]["end_ms"],
            }
        )
    if not sentences:
        raise TTSVerificationError("无法生成句子级时间戳")
    return sentences


def wav_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as audio:
        return round(audio.getnframes() / audio.getframerate() * 1000)


def ffprobe_duration_ms(path: Path) -> int:
    executable = os.environ.get("FFPROBE_PATH") or shutil.which("ffprobe")
    if not executable:
        raise TTSConfigurationError("未找到 ffprobe，无法验证非 WAV 音频")
    completed = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise TTSVerificationError(f"ffprobe 失败：{completed.stderr.strip()}")
    return round(float(completed.stdout.strip()) * 1000)


def audio_duration_ms(path: Path, audio_format: str) -> int:
    if audio_format == "wav":
        return wav_duration_ms(path)
    if audio_format == "pcm":
        raise TTSConfigurationError("PCM 时长探测需要提供采样率")
    return ffprobe_duration_ms(path)


def batch_audio_duration_ms(path: Path, audio_format: str, sample_rate: int) -> int:
    if audio_format == "pcm":
        # 火山引擎 PCM 输出按单声道、16-bit little-endian 处理。
        return round(path.stat().st_size / (sample_rate * 2) * 1000)
    return audio_duration_ms(path, audio_format)


def write_silence_wav(path: Path, duration_ms: int, sample_rate: int) -> None:
    frames = max(0, round(sample_rate * duration_ms / 1000))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * frames)


def merge_wav(
    paths: list[Path], pauses_ms: list[int], output: Path, leading_pause_ms: int = 0
) -> None:
    parameters: tuple[int, int, int] | None = None
    with wave.open(str(output), "wb") as destination:
        for index, path in enumerate(paths):
            with wave.open(str(path), "rb") as source:
                current = (source.getnchannels(), source.getsampwidth(), source.getframerate())
                if parameters is None:
                    parameters = current
                    destination.setnchannels(current[0])
                    destination.setsampwidth(current[1])
                    destination.setframerate(current[2])
                    if leading_pause_ms:
                        frames = round(current[2] * leading_pause_ms / 1000)
                        destination.writeframes(b"\x00" * frames * current[0] * current[1])
                elif current != parameters:
                    raise TTSWorkflowError("WAV batch 的声道、位宽或采样率不一致")
                destination.writeframes(source.readframes(source.getnframes()))
            pause = pauses_ms[index] if index < len(pauses_ms) else 0
            if pause and parameters:
                frames = round(parameters[2] * pause / 1000)
                destination.writeframes(b"\x00" * frames * parameters[0] * parameters[1])


def merge_pcm(
    paths: list[Path],
    pauses_ms: list[int],
    output: Path,
    sample_rate: int,
    leading_pause_ms: int = 0,
) -> None:
    with output.open("wb") as destination:
        if leading_pause_ms:
            frames = round(sample_rate * leading_pause_ms / 1000)
            destination.write(b"\x00\x00" * frames)
        for index, path in enumerate(paths):
            destination.write(path.read_bytes())
            pause = pauses_ms[index] if index < len(pauses_ms) else 0
            if pause:
                frames = round(sample_rate * pause / 1000)
                destination.write(b"\x00\x00" * frames)


def merge_with_ffmpeg(
    paths: list[Path],
    pauses_ms: list[int],
    output: Path,
    sample_rate: int,
    audio_format: str,
    leading_pause_ms: int = 0,
) -> None:
    executable = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")
    if not executable:
        raise TTSConfigurationError("未找到 ffmpeg，无法拼接非 WAV 音频")
    with tempfile.TemporaryDirectory(prefix="tts_merge_") as temporary:
        temp_dir = Path(temporary)
        inputs: list[Path] = []
        if leading_pause_ms:
            leading = temp_dir / "silence_leading.wav"
            write_silence_wav(leading, leading_pause_ms, sample_rate)
            inputs.append(leading)
        for index, path in enumerate(paths):
            inputs.append(path)
            pause = pauses_ms[index] if index < len(pauses_ms) else 0
            if pause:
                silence = temp_dir / f"silence_{index:03d}.wav"
                write_silence_wav(silence, pause, sample_rate)
                inputs.append(silence)
        def concatenate(group: list[Path], destination: Path, *, lossless: bool) -> None:
            command = [executable, "-v", "error", "-y"]
            for item in group:
                command.extend(["-i", str(item)])
            labels = "".join(f"[{index}:a]" for index in range(len(group)))
            command.extend(
                [
                    "-filter_complex",
                    f"{labels}concat=n={len(group)}:v=0:a=1[out]",
                    "-map",
                    "[out]",
                ]
            )
            if lossless:
                command.extend(["-c:a", "pcm_s16le"])
            elif audio_format == "ogg_opus":
                command.extend(["-c:a", "libopus"])
            command.append(str(destination))
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if completed.returncode != 0:
                raise TTSVerificationError(f"ffmpeg 拼接失败：{completed.stderr.strip()}")

        # Windows limits the complete process command line to roughly 32 KiB, and
        # common shells impose an even lower limit. Large pause-heavy transcripts
        # can create hundreds of input files, so first concatenate small groups to
        # lossless PCM WAV files and only encode the final container once.
        chunk_size = 24
        if len(inputs) <= chunk_size:
            concatenate(inputs, output, lossless=False)
        else:
            intermediates: list[Path] = []
            for start in range(0, len(inputs), chunk_size):
                intermediate = temp_dir / f"merge_chunk_{len(intermediates):03d}.wav"
                concatenate(inputs[start:start + chunk_size], intermediate, lossless=True)
                intermediates.append(intermediate)
            concatenate(intermediates, output, lossless=False)


def merge_audio(
    paths: list[Path],
    pauses_ms: list[int],
    output: Path,
    audio_format: str,
    sample_rate: int,
    leading_pause_ms: int = 0,
) -> None:
    if audio_format == "wav":
        merge_wav(paths, pauses_ms, output, leading_pause_ms)
    elif audio_format == "pcm":
        merge_pcm(paths, pauses_ms, output, sample_rate, leading_pause_ms)
    else:
        merge_with_ffmpeg(paths, pauses_ms, output, sample_rate, audio_format, leading_pause_ms)


def validate_utf8_input(path: Path) -> str:
    if path.suffix.lower() not in SUPPORTED_INPUT_SUFFIXES:
        raise TTSWorkflowError("输入文件只支持 .txt 和 .md")
    try:
        return normalize_text(path.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise TTSWorkflowError("输入文件必须是 UTF-8 编码") from exc


def state_store(root: Path, run_id: str) -> WorkflowStateStore:
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise TTSWorkflowError("run ID 无效")
    return WorkflowStateStore(
        root=root,
        workflow=WORKFLOW,
        run_id=run_id,
        definition=TTS_STATE_MACHINE,
        schema_path=DEFAULT_PROJECT_DIR / "utils" / "references" / "workflow-state-v1.schema.json",
        events_dir=root / "logs" / WORKFLOW / "runs" / run_id / "events",
    )


def _read_short_text_file(path_value: str, *, label: str) -> str:
    path = Path(path_value).resolve()
    if not path.is_file():
        raise TTSWorkflowError(f"{label}文件不存在：{path}")
    try:
        value = normalize_text(path.read_text(encoding="utf-8-sig")).rstrip("\n")
    except UnicodeDecodeError as exc:
        raise TTSWorkflowError(f"{label}文件必须是 UTF-8 编码") from exc
    if not value:
        raise TTSWorkflowError(f"{label}不能为空")
    return value


def _match_id(text: str, start: int, end: int) -> str:
    digest = text_sha256(text[start:end])[:10]
    return f"m{start:08d}_{end:08d}_{digest}"


def _parse_match_id(match_id: str, text: str) -> tuple[int, int]:
    matched = re.fullmatch(r"m(\d{8})_(\d{8})_([0-9a-f]{10})", match_id)
    if not matched:
        raise TTSWorkflowError(f"match_id 格式无效：{match_id}")
    start, end = int(matched.group(1)), int(matched.group(2))
    if start < 0 or end <= start or end > len(text):
        raise TTSWorkflowError(f"match_id 超出原文范围：{match_id}")
    if text_sha256(text[start:end])[:10] != matched.group(3):
        raise TTSWorkflowError(f"match_id 与原文指纹不一致：{match_id}")
    return start, end


def _load_completed_run(root: Path, run_id: str) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    state = state_store(root, run_id).load()
    if state.get("status") != "completed":
        raise TTSWorkflowError(f"基准运行尚未完成：{run_id}（{state.get('status')}）")
    output_dir = Path(state["output_dir"])
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.is_file():
        raise TTSVerificationError(f"基准运行缺少 manifest.json：{run_id}")
    return state, manifest_path, read_json(manifest_path)


def _run_text_and_mapping(state: dict[str, Any]) -> tuple[str, str, dict[str, Any], list[dict[str, Any]]]:
    input_info = state.get("input") or {}
    copied_path = Path(str(input_info.get("copied_path", "")))
    normalized_source = validate_utf8_input(copied_path)
    policy = dict(input_info.get("synthesis_policy") or state["config_snapshot"].get("synthesis_text", {}))
    synthesis_text, mapping = build_synthesis_text_and_map(
        normalized_source, str(policy.get("missing_sentence_ending", "。"))
    )
    batches = [dict(batch) for batch in state.get("batches", [])]
    if not batches or "".join(str(batch.get("text", "")) for batch in batches) != synthesis_text:
        raise TTSVerificationError("基准运行的 batch 文本与合成文本不一致")
    annotate_synthesis_map(mapping, batches, synthesis_text)
    return normalized_source, synthesis_text, mapping, batches


def locate_text(root: Path, run_id: str, query_file: str) -> dict[str, Any]:
    state, _, _ = _load_completed_run(root, run_id)
    source_text, _, mapping, batches = _run_text_and_mapping(state)
    query = _read_short_text_file(query_file, label="查询文本")
    candidates: list[dict[str, Any]] = []
    for start, end in find_literal_spans(source_text, query):
        paragraph = next(
            (
                item
                for item in mapping["paragraphs"]
                if start >= int(item["source_span"]["start"])
                and end <= int(item["source_span"]["end"])
            ),
            None,
        )
        if paragraph is None:
            batch_ids: list[str] = []
            synthesis_span = None
        else:
            synthesis_start = int(paragraph["synthesis_span"]["start"]) + start - int(
                paragraph["source_span"]["start"]
            )
            synthesis_span = {"start": synthesis_start, "end": synthesis_start + len(query)}
            batch_ids = [
                str(batch["batch_id"])
                for batch in batches
                if spans_overlap(
                    synthesis_span["start"],
                    synthesis_span["end"],
                    int(batch["synthesis_span"]["start"]),
                    int(batch["synthesis_span"]["end"]),
                )
            ]
        candidates.append(
            {
                "match_id": _match_id(source_text, start, end),
                "text": query,
                "source_span": {"start": start, "end": end},
                "paragraph_id": paragraph.get("paragraph_id") if paragraph else None,
                "synthesis_span": synthesis_span,
                "batch_ids": batch_ids,
                "revisable": paragraph is not None and len(batch_ids) == 1,
                "context_before": source_text[max(0, start - 30):start],
                "context_after": source_text[end:min(len(source_text), end + 30)],
            }
        )
    return {
        "status": "matched" if candidates else "not_found",
        "run_id": run_id,
        "query": query,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def initialize_revision(
    args: argparse.Namespace, config: dict[str, Any]
) -> tuple[dict[str, Any], WorkflowStateStore]:
    root = Path(args.root).resolve()
    base_state, base_manifest_path, _ = _load_completed_run(root, args.base_run_id)
    source_text, _, old_mapping, base_batches = _run_text_and_mapping(base_state)
    edits: list[dict[str, Any]] = []
    for specification in args.replace:
        if "=" not in specification:
            raise TTSWorkflowError("--replace 必须使用 <match_id>=<replacement_file>")
        match_id, replacement_path = specification.split("=", 1)
        start, end = _parse_match_id(match_id, source_text)
        replacement = _read_short_text_file(replacement_path, label="替换文本")
        if "\n" in replacement or "\r" in replacement:
            raise TTSWorkflowError("第一版局部修订不支持在替换文本中新增段落或换行")
        paragraph = next(
            (
                item
                for item in old_mapping["paragraphs"]
                if start >= int(item["source_span"]["start"])
                and end <= int(item["source_span"]["end"])
            ),
            None,
        )
        if paragraph is None:
            raise TTSWorkflowError(f"修改跨越原文段落边界：{match_id}")
        synthesis_start = int(paragraph["synthesis_span"]["start"]) + start - int(
            paragraph["source_span"]["start"]
        )
        synthesis_end = synthesis_start + (end - start)
        containing = [
            batch
            for batch in base_batches
            if synthesis_start >= int(batch["synthesis_span"]["start"])
            and synthesis_end <= int(batch["synthesis_span"]["end"])
        ]
        if len(containing) != 1:
            raise TTSWorkflowError(f"第一版局部修订不支持跨 batch 修改：{match_id}")
        batch = containing[0]
        edits.append(
            {
                "match_id": match_id,
                "start": start,
                "end": end,
                "old_text": source_text[start:end],
                "replacement": replacement,
                "paragraph_id": paragraph["paragraph_id"],
                "batch_id": batch["batch_id"],
                "batch_local_start": synthesis_start - int(batch["synthesis_span"]["start"]),
                "batch_local_end": synthesis_end - int(batch["synthesis_span"]["start"]),
            }
        )
    try:
        revised_text = apply_text_edits(source_text, edits)
    except ValueError as exc:
        raise TTSWorkflowError(str(exc)) from exc
    policy = dict((base_state.get("input") or {}).get("synthesis_policy") or {})
    revised_synthesis, _ = build_synthesis_text_and_map(
        revised_text, str(policy.get("missing_sentence_ending", "。"))
    )
    batch_edits: dict[str, list[dict[str, Any]]] = {}
    for edit in edits:
        batch_edits.setdefault(str(edit["batch_id"]), []).append(
            {
                "start": edit["batch_local_start"],
                "end": edit["batch_local_end"],
                "replacement": edit["replacement"],
            }
        )
    revision_batches: list[dict[str, Any]] = []
    for base_batch in base_batches:
        batch_id = str(base_batch["batch_id"])
        changed = batch_id in batch_edits
        try:
            revised_batch_text = (
                apply_text_edits(str(base_batch["text"]), batch_edits[batch_id])
                if changed
                else str(base_batch["text"])
            )
        except ValueError as exc:
            raise TTSWorkflowError(str(exc)) from exc
        revision_batches.append(
            {
                "index": int(base_batch["index"]),
                "batch_id": batch_id,
                "text": revised_batch_text,
                "char_count": counted_chars(revised_batch_text),
                "boundary": base_batch["boundary"],
                "origin": "generated" if changed else "reused",
                "base_audio_path": (base_batch.get("audio") or {}).get("path"),
                "base_timestamp_path": (base_batch.get("timestamps") or {}).get("path"),
                "base_audio_sha256": (base_batch.get("audio") or {}).get("sha256"),
                "base_timestamp_sha256": (base_batch.get("timestamps") or {}).get("sha256"),
            }
        )
    if "".join(batch["text"] for batch in revision_batches) != revised_synthesis:
        raise TTSWorkflowError(
            "batch_layout_changed：修改影响了原 batch 边界；第一版不支持重新分 batch"
        )
    output_root = (
        Path(args.output_root).resolve()
        if args.output_root
        else root / config["output"]["default_root"]
    )
    transcript_args = argparse.Namespace(input_file=None, text=revised_text)
    workspace = _prepare_transcript_workspace(root, transcript_args, output_root)
    init_lock = root / "logs" / WORKFLOW / ".init.lock"
    with project_lock(init_lock, f"{WORKFLOW}:initialize-revision"):
        run_id = workspace.run_id
        output_dir = workspace.speech_dir
        store = state_store(root, run_id)
        (store.run_dir / "staging").mkdir(parents=True)
        revision = {
            "base_run_id": args.base_run_id,
            "base_output_dir": str(base_state["output_dir"]),
            "base_manifest_sha256": file_sha256(base_manifest_path),
            "affected_batch_ids": [batch["batch_id"] for batch in revision_batches if batch["origin"] == "generated"],
            "reused_batch_ids": [batch["batch_id"] for batch in revision_batches if batch["origin"] == "reused"],
            "edits": edits,
        }
        request = {
            "schema_version": "1.0",
            "mode": "revision",
            "base_run_id": args.base_run_id,
            "base_input_path": str((base_state.get("input") or {}).get("copied_path")),
            "revised_text": revised_text,
            "output_root": str(output_root),
            "workspace_dir": str(workspace.root),
            "revision_plan": {"batches": revision_batches},
            "revision": revision,
        }
        request_path = store.run_dir / "request.json"
        write_json(request_path, request)
        now = iso_timestamp()
        state = {
            "schema_version": "1.0",
            "workflow": WORKFLOW,
            "run_id": run_id,
            "status": "initialized",
            "current_stage": "initialized",
            "resume_stage": None,
            "current_object_id": None,
            "current_batch_id": None,
            "completed_steps": [],
            "pending_decisions": [],
            "error": None,
            "created_at": now,
            "updated_at": now,
            "last_heartbeat_at": now,
            "event_sequence": 0,
            "root": str(root),
            "output_dir": str(output_dir),
            "workspace_dir": str(workspace.root),
            "request_path": str(request_path),
            "input": None,
            "mode": "revision",
            "backend": base_state.get("backend", "volcengine"),
            "backend_version": base_state.get("backend_version", "mock" if base_state["mock"] else "api-v3"),
            "adapter_version": base_state.get("adapter_version", ADAPTER_VERSION),
            "revision": revision,
            "speaker": base_state["speaker"],
            "speaker_id": base_state["speaker_id"],
            "resource_id": base_state["resource_id"],
            "audio": dict(base_state["audio"]),
            "config_snapshot": dict(base_state["config_snapshot"]),
            "config_sha256": base_state["config_sha256"],
            "batch_range": dict(base_state["batch_range"]),
            "batches": [],
            "preview": {"current_version": 0, "versions": [], "approval": None},
            "artifacts": {},
            "mock": bool(base_state["mock"]),
        }
        if "backend" not in base_state:
            state.pop("backend", None)
            state.pop("adapter_version", None)
        store.create(state)
    return state, store


def json_sha256(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _audio_config(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    if config.get("backend", "volcengine") == "edge-tts" and args.emotion_scale is not None:
        raise TTSConfigurationError("edge-tts 不支持 emotion-scale 参数")
    audio_config = dict(config["audio"])
    for argument, key in (
        (args.audio_format, "format"),
        (args.sample_rate, "sample_rate"),
        (args.speech_rate, "speech_rate"),
        (args.loudness_rate, "loudness_rate"),
        (args.emotion, "emotion"),
        (args.emotion_scale, "emotion_scale"),
    ):
        if argument is not None:
            audio_config[key] = argument
    if args.mock:
        audio_config["format"] = "wav"
    if audio_config["format"] not in SUPPORTED_FORMATS:
        raise TTSWorkflowError(f"不支持的音频格式：{audio_config['format']}")
    speech_rate_multiplier(int(audio_config["speech_rate"]))
    return audio_config


def _candidate_text(args: argparse.Namespace) -> str | None:
    if getattr(args, "input_file", None):
        source = Path(args.input_file).resolve()
        if not source.is_file():
            raise TTSWorkflowError(f"输入文件不存在：{source}")
        return validate_utf8_input(source)
    if getattr(args, "text", None) is not None:
        return validate_inline_text_transport(args.text)
    return None


def _prepare_transcript_workspace(
    root: Path, args: argparse.Namespace, output_root: Path
) -> TtsWorkspace:
    script = DEFAULT_PROJECT_DIR / "skills/convert-copy-to-transcript/scripts/convert_copy_to_transcript.py"
    command = [
        sys.executable,
        str(script),
        "prepare",
        "--root",
        str(root),
        "--output-root",
        str(root / "outputs/convert-copy-to-transcript/runs"),
    ]
    if args.input_file:
        command.extend(["--input-file", str(Path(args.input_file).resolve())])
    else:
        command.extend(["--text", str(args.text)])
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        shell=False,
    )
    if completed.returncode != 0:
        raise TTSWorkflowError(f"启动逐字稿转换失败：{completed.stderr.strip() or completed.stdout.strip()}")
    receipt = json.loads(completed.stdout)
    return _link_transcript_workspace(root, Path(receipt["workspace_dir"]), output_root)


def _link_transcript_workspace(root: Path, producer_dir: Path, output_root: Path | None = None) -> TtsWorkspace:
    """Allocate an independent consumer run; upstream remains read-only."""
    producer_skill = producer_dir.parents[1].name
    if producer_skill == "run-speech-to-text":
        handoff = build_handoff(root, producer_skill, producer_dir.name)
        verify_handoff(root, handoff)
        workspace = create_workspace(root, output_root, workflow=WORKFLOW)
        copied = workspace.inputs_dir / "input.txt"
        shutil.copy2(root / handoff["approved_transcript_path"], copied)
        metadata = input_metadata(copied)
        metadata["kind"] = "approved_asr"
        write_json(workspace.inputs_dir / "input-metadata.json", metadata)
        update_component(workspace, "transcript", status="referenced", details={
            "producer_skill": producer_skill, "producer_run_id": producer_dir.name,
            "producer_dir": str(producer_dir)})
        return open_workspace(workspace.root)
    producer = open_workspace(producer_dir)
    if producer.layout_profile != "education":
        raise TTSWorkflowError("本项目使用 outputs/<skill>/runs/<run-id>；请重新准备逐字稿")
    workspace = create_workspace(root, output_root, workflow=WORKFLOW)
    for path in producer.inputs_dir.iterdir():
        if path.is_file():
            shutil.copy2(path, workspace.inputs_dir / path.name)
    metadata_path = workspace.inputs_dir / "input-metadata.json"
    metadata = read_json(metadata_path)
    metadata["path"] = str(workspace.inputs_dir / Path(metadata["path"]).name)
    write_json(metadata_path, metadata)
    update_component(workspace, "transcript", status="referenced", details={
        "producer_skill": "convert-copy-to-transcript", "producer_run_id": producer.run_id,
        "producer_dir": str(producer.root)})
    return open_workspace(workspace.root)


def initialize_run(args: argparse.Namespace, config: dict[str, Any]) -> tuple[dict[str, Any], WorkflowStateStore]:
    config = copy.deepcopy(config)
    if getattr(args, "backend", None) is not None:
        config["backend"] = args.backend
    validate_backend_configuration(config)
    root = Path(args.root).resolve()
    candidate_text = _candidate_text(args)
    if getattr(args, "transcript_run_id", None):
        skill = getattr(args, "producer_skill", "convert-copy-to-transcript")
        args.workspace_dir = str(root / "outputs" / skill / "runs" / args.transcript_run_id)
    if candidate_text is None and not args.output_root and not args.workspace_dir:
        raise TTSWorkflowError("输入文案和输出路径至少必须提供一个")
    audio_config = _audio_config(args, config)
    speaker_name, speaker_id, resource_id = resolve_voice(args.speaker, args.resource_id, config)
    requested_output = Path(args.output_root).resolve() if args.output_root else None
    if args.workspace_dir:
        if requested_output is not None:
            raise TTSWorkflowError("--workspace-dir 与 --output-root 不能同时使用")
        workspace = _link_transcript_workspace(root, Path(args.workspace_dir))
    elif requested_output and (requested_output / "pipeline-manifest.json").is_file():
        workspace = _link_transcript_workspace(root, requested_output)
    else:
        if candidate_text is None:
            raise TTSWorkflowError("只提供输出路径时必须指向具体的 tts_<时间戳> 目录")
        output_parent = requested_output or root / config["output"]["default_root"]
        workspace = _prepare_transcript_workspace(root, args, output_parent)
    run_id = workspace.run_id
    output_dir = workspace.speech_dir
    init_lock = root / "logs" / WORKFLOW / ".init.lock"
    with project_lock(init_lock, f"{WORKFLOW}:initialize"):
        store = state_store(root, run_id)
        if store.path.exists():
            raise TTSWorkflowError(f"TTS 运行已存在，请使用 resume：{run_id}")
        (store.run_dir / "staging").mkdir(parents=True)
        request = {
            "schema_version": "1.0",
            "speaker": speaker_name,
            "resource_id": args.resource_id,
            "input_file": str(Path(args.input_file).resolve()) if args.input_file else None,
            "text": args.text if not args.input_file and args.text is not None else None,
            "workspace_dir": str(workspace.root),
            "audio": audio_config,
            "mock": bool(args.mock),
        }
        request_path = store.run_dir / "request.json"
        write_json(request_path, request)
        now = iso_timestamp()
        state = {
            "schema_version": "1.0",
            "workflow": WORKFLOW,
            "run_id": run_id,
            "status": "initialized",
            "current_stage": "initialized",
            "resume_stage": None,
            "current_object_id": None,
            "current_batch_id": None,
            "completed_steps": [],
            "pending_decisions": [],
            "error": None,
            "created_at": now,
            "updated_at": now,
            "last_heartbeat_at": now,
            "event_sequence": 0,
            "root": str(root),
            "output_dir": str(output_dir),
            "workspace_dir": str(workspace.root),
            "request_path": str(request_path),
            "input": None,
            "backend": config.get("backend", "volcengine"),
            "adapter_version": ADAPTER_VERSION,
            "speaker": speaker_name,
            "speaker_id": speaker_id,
            "resource_id": resource_id,
            "audio": audio_config,
            "config_snapshot": config,
            "config_sha256": json_sha256(config),
            "batch_range": None,
            "batches": [],
            "preview": {"current_version": 0, "versions": [], "approval": None},
            "artifacts": {},
            "mock": bool(args.mock),
        }
        store.create(state)
    return state, store


def initialize_pause_retime(
    args: argparse.Namespace, config: dict[str, Any]
) -> tuple[dict[str, Any], WorkflowStateStore]:
    if not getattr(args, "input_file", None):
        raise TTSWorkflowError("停顿重定时必须提供 --input-file")
    root = Path(args.root).resolve()
    base_state, base_manifest_path, _ = _load_completed_run(root, args.base_run_id)
    state, store = initialize_run(args, dict(base_state["config_snapshot"]))
    request_path = Path(state["request_path"])
    request = read_json(request_path)
    request.update(
        {
            "mode": "pause_retime",
            "base_run_id": args.base_run_id,
            "base_input_path": str((base_state.get("input") or {}).get("copied_path", "")),
        }
    )
    write_json(request_path, request)
    revision = {
        "kind": "pause_retime",
        "base_run_id": args.base_run_id,
        "base_output_dir": str(base_state["output_dir"]),
        "base_manifest_sha256": file_sha256(base_manifest_path),
        "affected_batch_ids": [],
        "reused_batch_ids": [],
        "edits": [],
    }
    state.update(
        {
            "mode": "revision",
            "revision": revision,
            "speaker": base_state["speaker"],
            "speaker_id": base_state["speaker_id"],
            "resource_id": base_state["resource_id"],
            "audio": dict(base_state["audio"]),
            "config_snapshot": dict(base_state["config_snapshot"]),
            "config_sha256": base_state["config_sha256"],
            "batch_range": dict(base_state["batch_range"]),
            "mock": bool(base_state["mock"]),
        }
    )
    if "backend" not in base_state:
        state.pop("backend", None)
        state.pop("adapter_version", None)
    else:
        state["backend_version"] = base_state.get("backend_version")
        state["adapter_version"] = base_state.get("adapter_version", ADAPTER_VERSION)
    store.checkpoint(
        state,
        event="pause_retime_initialized",
        details={"base_run_id": args.base_run_id},
    )
    return state, store


def resolve_workspace_checkpoint(
    state: dict[str, Any], store: WorkflowStateStore
) -> dict[str, Any]:
    workspace = open_workspace(Path(state["workspace_dir"]))
    if workspace.run_id != state["run_id"] or workspace.speech_dir != Path(state["output_dir"]):
        raise TTSVerificationError("共享输出目录与 TTS 运行状态不一致")
    return store.transition(
        state,
        "workspace_resolved",
        stage="workspace_resolved",
        completed_step="workspace_resolved",
    )


def validate_workspace_input(
    state: dict[str, Any], store: WorkflowStateStore
) -> dict[str, Any]:
    if not state["mock"]:
        try:
            find_ffmpeg()
            find_ffprobe()
        except ValueError as exc:
            raise TTSConfigurationError(str(exc)) from exc
    noise = state["config_snapshot"].get("white_noise", {})
    if noise.get("enabled"):
        asset = Path(str(noise["asset_path"]))
        asset = asset if asset.is_absolute() else Path(state["root"]) / asset
        if not asset.is_file():
            raise TTSConfigurationError("共享白噪音素材缺失")
        asset_hash = file_sha256(asset)
        if state.get("noise_asset_sha256") and state["noise_asset_sha256"] != asset_hash:
            raise TTSVerificationError("运行期间共享白噪音素材发生变化")
        state["noise_asset_sha256"] = asset_hash
    workspace = open_workspace(Path(state["workspace_dir"]))
    metadata_path = workspace.inputs_dir / "input-metadata.json"
    if not metadata_path.is_file():
        raise TTSVerificationError("共享输出目录缺少 inputs/input-metadata.json")
    metadata = read_json(metadata_path)
    stored_path = Path(str(metadata.get("path", "")))
    if not stored_path.is_file() or stored_path.parent != workspace.inputs_dir:
        raise TTSVerificationError("共享输入路径无效")
    current = input_metadata(stored_path)
    if current["file_sha256"] != metadata.get("file_sha256"):
        raise TTSVerificationError("共享输入副本的字节指纹已变化")
    request = read_json(Path(state["request_path"]))
    candidate: str | None = None
    if request.get("input_file"):
        candidate = validate_utf8_input(Path(request["input_file"]))
    elif request.get("text") is not None:
        candidate = validate_inline_text_transport(str(request["text"]))
    if candidate is not None:
        matched, comparison = inputs_match(candidate, stored_path)
        if not matched:
            return store.pause(
                state,
                status="paused_input_mismatch",
                error_code="input_mismatch",
                message="输入文案与共享输出目录中的原始文案不一致",
                resume_stage="workspace_resolved",
                details=comparison,
                pending_decisions=[
                    {
                        "type": "input_mismatch",
                        "question": "请提供与共享输入一致的文案，或改用正确的 tts_<时间戳> 目录。",
                    }
                ],
            )
    return store.transition(
        state,
        "input_validated",
        stage="input_validated",
        completed_step="input_validated",
        updates={"workspace_input": metadata},
    )


def validate_transcript_checkpoint(
    state: dict[str, Any], store: WorkflowStateStore
) -> dict[str, Any]:
    if state["status"] in {"input_validated", "checking_backend_environment"}:
        state = store.transition(state, "awaiting_transcript", stage="awaiting_transcript")
    workspace = open_workspace(Path(state["workspace_dir"]))
    pipeline = read_json(workspace.pipeline_manifest)
    component = (pipeline.get("components") or {}).get("transcript") or {}
    if workspace.layout_profile == "education":
        producer_skill = component["producer_skill"]
        producer_run_id = component["producer_run_id"]
        producer_state_path = producer_paths(Path(state["root"]), producer_skill, producer_run_id)["state"]
        component = {**component, "status": read_json(producer_state_path)["status"]}
    if component.get("status") != "completed":
        return store.pause(
            state,
            status="paused_transcript_not_ready",
            error_code="transcript_not_ready",
            message=f"逐字稿转换尚未完成：{component.get('status', 'unknown')}",
            resume_stage="awaiting_transcript",
            details={"transcript_status": component.get("status")},
            pending_decisions=[
                {
                    "type": "transcript_approval",
                    "question": f"上游 {producer_skill} 运行 {producer_run_id} 当前为 {component.get('status')}；下一步执行 {transcript_next_action(component.get('status', 'unknown'))}。完成并验证上游后再恢复 TTS。",
                }
            ],
        )
    if workspace.layout_profile == "education":
        handoff_path = workspace.logs_dir / "upstream-handoff.json"
        handoff = read_json(handoff_path) if handoff_path.is_file() else build_handoff(Path(state["root"]), producer_skill, producer_run_id)
        verify_handoff(Path(state["root"]), handoff)
        write_json(handoff_path, handoff)
        state["upstream_handoff"] = handoff
    manifest_path = workspace.transcript_dir / "manifest.json"
    approved_path = workspace.transcript_dir / "approved" / "transcript.txt"
    approval_path = workspace.transcript_dir / "approved" / "approval-receipt.json"
    if workspace.layout_profile == "education":
        upstream = producer_paths(Path(state["root"]), producer_skill, producer_run_id)
        manifest_path = upstream["manifest"]
        approved_path = upstream["approved_transcript"]
        approval_path = upstream["approval_receipt"]
    if not manifest_path.is_file() or not approved_path.is_file() or not approval_path.is_file():
        raise TTSVerificationError("已完成的逐字稿运行缺少 manifest、批准稿或确认回执")
    manifest = read_json(manifest_path)
    approval = read_json(approval_path)
    approved_hash = file_sha256(approved_path)
    if manifest.get("status") != "completed":
        raise TTSVerificationError("逐字稿 manifest 状态不是 completed")
    if approval.get("approved_transcript_sha256", approval.get("transcript_sha256")) != approved_hash:
        raise TTSVerificationError("批准逐字稿与确认回执哈希不一致")
    return store.transition(
        state,
        "transcript_validated",
        stage="transcript_validated",
        completed_step="transcript_validated",
        updates={
            "approved_transcript_path": str(approved_path),
            "approved_transcript_sha256": approved_hash,
            "transcript_manifest_path": str(manifest_path),
            "approval_receipt_path": str(approval_path),
        },
    )


def stage_input(state: dict[str, Any], store: WorkflowStateStore) -> dict[str, Any]:
    request = read_json(Path(state["request_path"]))
    output_dir = Path(state["output_dir"])
    generated_dir = store.run_dir / "generated"
    if request.get("mode") == "revision":
        text = normalize_text(str(request["revised_text"]))
        copied = Path(state["approved_transcript_path"])
        if normalize_text(validate_utf8_input(copied)) != text:
            raise TTSVerificationError("修订文本未经过当前 workspace 的逐字稿确认")
        input_info = {
            "kind": "revision",
            "copied_path": str(copied),
            "base_run_id": request["base_run_id"],
            "base_input_path": request["base_input_path"],
        }
    else:
        copied = Path(state["approved_transcript_path"])
        text = validate_utf8_input(copied)
        input_info = {
            "kind": "approved_transcript",
            "source_kind": (state.get("workspace_input") or {}).get("kind"),
            "copied_path": str(copied),
            "transcript_manifest_path": state["transcript_manifest_path"],
            "approval_receipt_path": state["approval_receipt_path"],
        }
    copied_text = validate_utf8_input(copied)
    synthesis_policy = dict(state["config_snapshot"].get("synthesis_text", {}))
    synthesis_text, synthesis_map = build_synthesis_text_and_map(
        copied_text, str(synthesis_policy.get("missing_sentence_ending", "。"))
    )
    synthesis_path = generated_dir / "synthesis.txt"
    synthesis_map_path = generated_dir / "synthesis-map.json"
    if not synthesis_path.exists():
        synthesis_path.parent.mkdir(parents=True, exist_ok=True)
        synthesis_path.write_text(synthesis_text, encoding="utf-8", newline="\n")
    if not synthesis_map_path.exists():
        write_json(synthesis_map_path, synthesis_map)
    input_info.update(
        {
            "file_sha256": file_sha256(copied),
            "normalized_text_sha256": text_sha256(copied_text),
            "synthesis_path": str(synthesis_path),
            "synthesis_file_sha256": file_sha256(synthesis_path),
            "synthesis_text_sha256": text_sha256(synthesis_text),
            "synthesis_map_path": str(synthesis_map_path),
            "synthesis_map_file_sha256": file_sha256(synthesis_map_path),
            "synthesis_policy": synthesis_policy,
        }
    )
    return store.transition(
        state,
        "input_staged",
        stage="input_staged",
        completed_step="input_staged",
        updates={"input": input_info},
    )


def validate_input_checkpoint(state: dict[str, Any]) -> str:
    info = state.get("input") or {}
    copied = Path(str(info.get("copied_path", "")))
    if not copied.is_file() or file_sha256(copied) != info.get("file_sha256"):
        raise TTSVerificationError("运行输入副本缺失或指纹已变化")
    text = validate_utf8_input(copied)
    if text_sha256(text) != info.get("normalized_text_sha256"):
        raise TTSVerificationError("标准化逐字稿指纹已变化")
    synthesis_path = Path(str(info.get("synthesis_path", "")))
    if not synthesis_path.is_file() or file_sha256(synthesis_path) != info.get(
        "synthesis_file_sha256"
    ):
        raise TTSVerificationError("合成文本缺失或指纹已变化")
    synthesis_text = validate_utf8_input(synthesis_path)
    if text_sha256(synthesis_text) != info.get("synthesis_text_sha256"):
        raise TTSVerificationError("合成文本内容指纹已变化")
    synthesis_policy = dict(state["config_snapshot"].get("synthesis_text", {}))
    if synthesis_text != normalize_synthesis_text(
        text, str(synthesis_policy.get("missing_sentence_ending", "。"))
    ):
        raise TTSVerificationError("合成文本与逐字稿归一化结果不一致")
    synthesis_map_path = Path(str(info.get("synthesis_map_path", "")))
    if not synthesis_map_path.is_file() or file_sha256(synthesis_map_path) != info.get(
        "synthesis_map_file_sha256"
    ):
        raise TTSVerificationError("合成映射缺失或指纹已变化")
    mapping_errors = validate_synthesis_map(read_json(synthesis_map_path), text, synthesis_text, [])
    if mapping_errors:
        raise TTSVerificationError("合成映射无效：" + "；".join(mapping_errors))
    if json_sha256(state["config_snapshot"]) != state.get("config_sha256"):
        raise TTSTerminalError("状态中的配置快照指纹不一致")
    return synthesis_text


def plan_batches(state: dict[str, Any], store: WorkflowStateStore) -> dict[str, Any]:
    text = validate_input_checkpoint(state)
    config = state["config_snapshot"]
    minimum, maximum = effective_batch_range(config, int(state["audio"]["speech_rate"]))
    request = read_json(Path(state["request_path"]))
    mapping_path = Path(str((state.get("input") or {}).get("synthesis_map_path", "")))
    mapping = read_json(mapping_path)
    pause_events = list(mapping.get("pauses") or [])
    if request.get("mode") == "pause_retime":
        root = Path(state["root"])
        base_state, _, _ = _load_completed_run(root, str(request["base_run_id"]))
        base_text = validate_input_checkpoint(base_state)
        if text != base_text:
            raise TTSWorkflowError("pause_retime_text_changed：停顿之外的合成文本发生变化")
        base_mapping_path = Path(
            str((base_state.get("input") or {}).get("synthesis_map_path", ""))
        )
        base_mapping = read_json(base_mapping_path)
        base_pause_offsets = [
            int(item["synthesis_offset"]) for item in base_mapping.get("pauses") or []
        ]
        pause_offsets = [int(item["synthesis_offset"]) for item in pause_events]
        if pause_offsets != base_pause_offsets:
            raise TTSWorkflowError(
                "pause_retime_layout_changed：停顿数量或位置发生变化，不能复用全部 batch"
            )
        newly_planned = split_batches_with_pauses(
            text,
            pause_events,
            minimum,
            maximum,
            list(config["batching"]["boundary_priority"]),
        )
        base_batches = list(base_state.get("batches") or [])
        if len(newly_planned) != len(base_batches):
            raise TTSWorkflowError("pause_retime_layout_changed：batch 数量发生变化")
        planned = []
        for new_batch, base_batch in zip(newly_planned, base_batches):
            if (
                str(new_batch["text"]) != str(base_batch["text"])
                or str(new_batch["boundary"]) != str(base_batch["boundary"])
            ):
                raise TTSWorkflowError(
                    f"pause_retime_layout_changed：{base_batch['batch_id']} 的文本或边界发生变化"
                )
            planned.append(
                {
                    **new_batch,
                    "batch_id": str(base_batch["batch_id"]),
                    "origin": "reused",
                    "base_audio_path": (base_batch.get("audio") or {}).get("path"),
                    "base_timestamp_path": (base_batch.get("timestamps") or {}).get("path"),
                    "base_audio_sha256": (base_batch.get("audio") or {}).get("sha256"),
                    "base_timestamp_sha256": (base_batch.get("timestamps") or {}).get("sha256"),
                }
            )
        revision = state.get("revision") or {}
        revision["reused_batch_ids"] = [str(item["batch_id"]) for item in planned]
        state["revision"] = revision
        request["revision_plan"] = {"batches": planned}
        request["revision"] = revision
        write_json(Path(state["request_path"]), request)
    elif state.get("mode") == "revision":
        planned = list((request.get("revision_plan") or {}).get("batches") or [])
        if not planned:
            raise TTSWorkflowError("修订请求缺少 batch 计划")
        planned_boundaries = []
        planned_offset = 0
        for batch in planned:
            planned_offset += len(str(batch["text"]))
            planned_boundaries.append(planned_offset)
        unsupported = sorted(
            {
                int(item["synthesis_offset"])
                for item in pause_events
                if int(item["synthesis_offset"]) not in {0, len(text), *planned_boundaries}
            }
        )
        if unsupported:
            raise TTSWorkflowError(
                "batch_layout_changed：局部修订新增了 batch 内停顿；请作为新 TTS 运行处理"
            )
    else:
        planned = split_batches_with_pauses(
            text,
            pause_events,
            minimum,
            maximum,
            list(config["batching"]["boundary_priority"]),
        )
    batches = []
    for batch in planned:
        batch_id = str(batch.get("batch_id") or f"batch_{int(batch['index']):03d}")
        origin = str(batch.get("origin") or "generated")
        batches.append(
            {
                **batch,
                "batch_id": batch_id,
                "text_sha256": text_sha256(batch["text"]),
                "origin": origin,
                "status": "reuse_pending" if origin == "reused" else "pending",
                "attempts": 0,
                "started_at": None,
                "completed_at": None,
                "error": None,
                "audio": None,
                "timestamps": None,
                "duration_ms": None,
            }
        )
    for paragraph in mapping["paragraphs"]:
        paragraph["batch_ids"] = []
    annotate_synthesis_map(mapping, batches, text)
    write_json(mapping_path, mapping)
    state["input"]["synthesis_map_file_sha256"] = file_sha256(mapping_path)
    return store.transition(
        state,
        "batches_planned",
        stage="batches_planned",
        completed_step="batches_planned",
        updates={"batch_range": {"minimum": minimum, "maximum": maximum}, "batches": batches},
        details={"batch_count": len(batches)},
    )


def check_backend_checkpoint(state: dict[str, Any]) -> dict:
    try:
        if not state["mock"]:
            find_ffmpeg()
            find_ffprobe()
        metadata = check_backend(Path(state["root"]), state.get("backend", "volcengine"), mock=bool(state["mock"]))
    except ValueError as exc:
        raise TTSConfigurationError(str(exc)) from exc
    try:
        validate_backend_parameters(state.get("backend", "volcengine"), state["audio"], state.get("resource_id", ""))
    except ValueError as exc:
        raise TTSConfigurationError(str(exc)) from exc
    if state.get("adapter_version", ADAPTER_VERSION) != ADAPTER_VERSION:
        raise TTSConfigurationError("适配器版本发生变化，请创建新运行")
    if state.get("backend_version") and state["backend_version"] != metadata["backend_version"]:
        raise TTSConfigurationError("后端版本发生变化，请恢复锁定版本或创建新运行")
    state.update(metadata)
    return metadata


def synthesis_fingerprint(state: dict[str, Any], text: str, *, adapter_version: str | None = None) -> str:
    return json_sha256({"text": text, "backend": state.get("backend", "volcengine"),
                        "backend_version": state.get("backend_version", "api-v3"),
                        "adapter_version": adapter_version or state.get("adapter_version", ADAPTER_VERSION), "speaker_id": state["speaker_id"],
                        "resource_id": state["resource_id"], "audio": state["audio"], "mock": state["mock"]})


def create_client(state: dict[str, Any]) -> Any:
    if state["mock"]:
        return MockClient()
    check_backend_checkpoint(state)
    if state.get("backend", "volcengine") == "edge-tts":
        import importlib.util
        spec = importlib.util.spec_from_file_location("edge_tts_client", SKILL_DIR / "scripts/edge_tts_client.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        EdgeTTSClient = module.EdgeTTSClient
        config = {**state["config_snapshot"], "_staging_dir": str(state_store(Path(state["root"]), state["run_id"]).run_dir / "staging")}
        return EdgeTTSClient(config, (TTSConfigurationError, TTSRetryableError, TTSVerificationError))
    try:
        api_key: str = dotenv_credential(Path(state["root"]), "VOLCENGINE_API_KEY")
    except ValueError as exc:
        raise TTSConfigurationError(str(exc)) from exc
    if not api_key:
        raise TTSConfigurationError("项目根目录 .env 缺少 VOLCENGINE_API_KEY")
    return VolcengineClient(api_key, state["config_snapshot"])


def batch_artifact_paths(state: dict[str, Any], batch: dict[str, Any]) -> tuple[Path, Path]:
    output_dir = Path(state["output_dir"])
    extension = FORMAT_EXTENSIONS[str(state["audio"]["format"])]
    stem = str(batch["batch_id"])
    return (
        output_dir / "batches" / f"{stem}.{extension}",
        Path(state["root"]) / "logs" / WORKFLOW / "runs" / state["run_id"] / "batches" / f"{stem}.timestamps.json",
    )


def inspect_batch_artifacts(state: dict[str, Any], batch: dict[str, Any]) -> dict[str, Any]:
    if text_sha256(str(batch["text"])) != batch.get("text_sha256"):
        raise TTSTerminalError(f"{batch['batch_id']} 文本指纹与状态不一致")
    audio_path, timestamp_path = batch_artifact_paths(state, batch)
    if not audio_path.is_file() or not timestamp_path.is_file():
        raise TTSVerificationError(f"{batch['batch_id']} 的音频或时间戳文件缺失")
    raw = read_json(timestamp_path)
    if not isinstance(raw, dict) or not isinstance(raw.get("events"), list):
        raise TTSVerificationError(f"{batch['batch_id']} 原始时间戳格式无效")
    if "backend" in state and not raw.get("synthesis_fingerprint"):
        raise TTSVerificationError("batch 缺少合成指纹")
    if raw.get("synthesis_fingerprint") and raw["synthesis_fingerprint"] != synthesis_fingerprint(state, str(batch["text"])):
        raise TTSVerificationError("batch 后端或音频参数指纹不一致")
    items = normalize_items(raw["events"])
    sentences = derive_sentences(str(batch["text"]), items, word_boundaries=state.get("backend") == "edge-tts" and not state["mock"])
    duration = batch_audio_duration_ms(
        audio_path,
        str(state["audio"]["format"]),
        int(state["audio"]["sample_rate"]),
    )
    if duration <= 0:
        raise TTSVerificationError(f"{batch['batch_id']} 音频时长无效")
    audio_hash = file_sha256(audio_path)
    timestamp_hash = file_sha256(timestamp_path)
    stored_audio = batch.get("audio") or {}
    stored_timestamps = batch.get("timestamps") or {}
    if batch.get("status") == "verified":
        if stored_audio.get("sha256") != audio_hash or stored_timestamps.get("sha256") != timestamp_hash:
            raise TTSVerificationError(f"{batch['batch_id']} 已验证产物指纹发生变化")
    return {
        "audio_path": audio_path,
        "timestamp_path": timestamp_path,
        "audio_sha256": audio_hash,
        "timestamp_sha256": timestamp_hash,
        "duration_ms": duration,
        "events": raw["events"],
        "items": items,
        "sentences": sentences,
    }


def _sentence_is_complete(text: str) -> bool:
    rendered = text.rstrip()
    while rendered and rendered[-1] in SYNTHESIS_CLOSING_CHARS:
        rendered = rendered[:-1].rstrip()
    return bool(rendered and rendered[-1] in SENTENCE_END_CHARS)


def _pauses_by_synthesis_offset(state: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    mapping = read_json(Path(state["input"]["synthesis_map_path"]))
    grouped: dict[int, list[dict[str, Any]]] = {}
    for event in mapping.get("pauses") or []:
        grouped.setdefault(int(event["synthesis_offset"]), []).append(event)
    return grouped


def preview_selection(state: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first complete sentence ending at or after the configured target."""
    target_ms = round(float(state["config_snapshot"]["preview"]["duration_seconds"]) * 1000)
    pauses_by_offset = _pauses_by_synthesis_offset(state)
    offset = sum(int(item["duration_ms"]) for item in pauses_by_offset.get(0, []))
    verified_ids: list[str] = []
    last_complete: dict[str, Any] | None = None
    for batch in state["batches"]:
        if batch.get("status") != "verified":
            break
        result = inspect_batch_artifacts(state, batch)
        verified_ids.append(str(batch["batch_id"]))
        for sentence in result["sentences"]:
            if not _sentence_is_complete(str(sentence["text"])):
                continue
            last_complete = {
                "target_duration_ms": target_ms,
                "cutoff_ms": offset + int(sentence["end_ms"]),
                "sentence_text": sentence["text"],
                "last_batch_id": batch["batch_id"],
                "batch_ids": list(verified_ids),
                "reached_target": offset + int(sentence["end_ms"]) >= target_ms,
            }
            if last_complete["reached_target"]:
                return last_complete
        offset += int(result["duration_ms"])
        offset += pause_after_batch(state["config_snapshot"], str(batch["boundary"]))
        offset += sum(
            int(item["duration_ms"])
            for item in pauses_by_offset.get(int(batch["synthesis_span"]["end"]), [])
        )
    if len(verified_ids) == len(state["batches"]):
        return last_complete
    return None


def short_audio_without_preview(state: dict[str, Any], duration_ms: int | None = None) -> bool:
    """Decide from verified audio durations, including all explicit and boundary pauses."""
    batches = state.get("batches") or []
    if not batches or any(batch.get("status") != "verified" for batch in batches):
        return False
    duration = sum(int(batch["duration_ms"]) for batch in batches)
    duration += sum(int(event["duration_ms"]) for events in _pauses_by_synthesis_offset(state).values() for event in events)
    duration += sum(pause_after_batch(state["config_snapshot"], batch["boundary"]) for batch in batches[:-1])
    return (duration if duration_ms is None else duration_ms) < round(float(state["config_snapshot"]["preview"]["duration_seconds"]) * 1000)


def measure_complete_audio_duration(state: dict[str, Any], store: WorkflowStateStore) -> int:
    """Measure the final merge, including container padding, without creating a preview."""
    config = state["config_snapshot"]
    batches = state["batches"]
    grouped = _pauses_by_synthesis_offset(state)
    leading = sum(int(item["duration_ms"]) for item in grouped.get(0, []))
    pauses = [
        (pause_after_batch(config, batch["boundary"]) if index < len(batches) - 1 else 0)
        + sum(int(item["duration_ms"]) for item in grouped.get(int(batch["synthesis_span"]["end"]), []))
        for index, batch in enumerate(batches)
    ]
    audio_format = state["audio"]["format"]
    temporary = store.run_dir / "staging" / f"duration-check.{FORMAT_EXTENSIONS[audio_format]}"
    try:
        merge_audio([inspect_batch_artifacts(state, batch)["audio_path"] for batch in batches], pauses,
                    temporary, audio_format, int(state["audio"]["sample_rate"]), leading)
        return batch_audio_duration_ms(temporary, audio_format, int(state["audio"]["sample_rate"]))
    finally:
        temporary.unlink(missing_ok=True)


def checkpoint_batch(
    store: WorkflowStateStore,
    state: dict[str, Any],
    batch: dict[str, Any],
    *,
    event: str,
    completed_step: str | None = None,
) -> None:
    state["current_batch_id"] = batch["batch_id"]
    store.checkpoint(
        state,
        event=event,
        completed_step=completed_step,
        details={"batch_id": batch["batch_id"], "batch_status": batch["status"]},
    )


def synthesize_batches(
    state: dict[str, Any], store: WorkflowStateStore, *, preview_only: bool = False
) -> dict[str, Any]:
    validate_input_checkpoint(state)
    if state.get("upstream_handoff"):
        verify_handoff(Path(state["root"]), state["upstream_handoff"])
    if preview_only and state["status"] == "batches_planned":
        store.transition(state, "preview_synthesizing", stage="preview_synthesizing")
    elif not preview_only and state["status"] == "batches_planned":
        store.transition(state, "synthesizing", stage="synthesizing")
    client = None
    staging = store.run_dir / "staging"
    audio_format = str(state["audio"]["format"])
    extension = FORMAT_EXTENSIONS[audio_format]
    for batch in state["batches"]:
        state["current_batch_id"] = batch["batch_id"]
        if batch.get("status") == "verified":
            inspect_batch_artifacts(state, batch)
            if preview_only and preview_selection(state) is not None:
                break
            continue
        audio_path, timestamp_path = batch_artifact_paths(state, batch)
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp_path.parent.mkdir(parents=True, exist_ok=True)
        if batch.get("status") == "reuse_pending":
            base_audio = Path(str(batch.get("base_audio_path", "")))
            base_timestamps = Path(str(batch.get("base_timestamp_path", "")))
            if not base_audio.is_file() or not base_timestamps.is_file():
                raise TTSVerificationError(f"{batch['batch_id']} 的基准 batch 产物缺失")
            if file_sha256(base_audio) != batch.get("base_audio_sha256") or file_sha256(
                base_timestamps
            ) != batch.get("base_timestamp_sha256"):
                raise TTSVerificationError(f"{batch['batch_id']} 的基准 batch 指纹不一致")
            shutil.copy2(base_audio, audio_path)
            shutil.copy2(base_timestamps, timestamp_path)
            reused = inspect_batch_artifacts(state, batch)
            batch.update(
                status="verified",
                completed_at=iso_timestamp(),
                error=None,
                duration_ms=reused["duration_ms"],
                audio={"path": str(audio_path), "sha256": reused["audio_sha256"]},
                timestamps={"path": str(timestamp_path), "sha256": reused["timestamp_sha256"]},
                reused_from={
                    "run_id": (state.get("revision") or {}).get("base_run_id"),
                    "batch_id": batch["batch_id"],
                    "audio_sha256": reused["audio_sha256"],
                    "timestamp_sha256": reused["timestamp_sha256"],
                },
            )
            checkpoint_batch(
                store,
                state,
                batch,
                event="batch_reused",
                completed_step=f"{batch['batch_id']}_verified",
            )
            continue
        if audio_path.is_file() and timestamp_path.is_file():
            recovered = inspect_batch_artifacts(state, batch)
            batch.update(
                status="verified",
                completed_at=iso_timestamp(),
                error=None,
                duration_ms=recovered["duration_ms"],
                audio={"path": str(audio_path), "sha256": recovered["audio_sha256"]},
                timestamps={"path": str(timestamp_path), "sha256": recovered["timestamp_sha256"]},
            )
            checkpoint_batch(
                store,
                state,
                batch,
                event="batch_recovered",
                completed_step=f"{batch['batch_id']}_verified",
            )
            continue
        if audio_path.exists() != timestamp_path.exists():
            orphan_dir = staging / "orphaned"
            orphan_dir.mkdir(parents=True, exist_ok=True)
            orphan = audio_path if audio_path.exists() else timestamp_path
            recovered_name = f"{batch['batch_id']}.{int(batch.get('attempts', 0)):03d}.{orphan.name}"
            os.replace(orphan, orphan_dir / recovered_name)
            batch.update(status="pending", audio=None, timestamps=None, duration_ms=None)
            checkpoint_batch(store, state, batch, event="incomplete_batch_quarantined")
        batch.update(
            status="synthesizing",
            attempts=int(batch.get("attempts", 0)) + 1,
            started_at=batch.get("started_at") or iso_timestamp(),
            error=None,
        )
        checkpoint_batch(store, state, batch, event="batch_synthesis_started")
        try:
            if client is None:
                client = create_client(state)
            audio_bytes, events = client.synthesize(
                str(batch["text"]), state["speaker_id"], state["resource_id"], state["audio"]
            )
            staged_audio = staging / f"{batch['batch_id']}.staged.{extension}"
            staged_timestamps = staging / f"{batch['batch_id']}.timestamps.staged.json"
            staged_audio.write_bytes(audio_bytes)
            duration = batch_audio_duration_ms(
                staged_audio, audio_format, int(state["audio"]["sample_rate"])
            )
            write_json(
                staged_timestamps,
                {"batch_index": batch["index"], "text": batch["text"], "events": events,
                 "synthesis_fingerprint": synthesis_fingerprint(state, str(batch["text"])),
                 "adapter_version": state.get("adapter_version", ADAPTER_VERSION)},
            )
            normalize_items(events)
            os.replace(staged_audio, audio_path)
            batch.update(
                status="audio_ready",
                duration_ms=duration,
                audio={"path": str(audio_path), "sha256": file_sha256(audio_path)},
            )
            checkpoint_batch(store, state, batch, event="batch_audio_ready")
            os.replace(staged_timestamps, timestamp_path)
            batch.update(
                status="timestamps_ready",
                timestamps={"path": str(timestamp_path), "sha256": file_sha256(timestamp_path)},
            )
            checkpoint_batch(store, state, batch, event="batch_timestamps_ready")
            verified = inspect_batch_artifacts(state, batch)
            batch.update(
                status="verified",
                completed_at=iso_timestamp(),
                duration_ms=verified["duration_ms"],
                error=None,
            )
            checkpoint_batch(
                store,
                state,
                batch,
                event="batch_verified",
                completed_step=f"{batch['batch_id']}_verified",
            )
            if preview_only and preview_selection(state) is not None:
                break
        except Exception as exc:
            batch["error"] = {"message": str(exc), "at": iso_timestamp()}
            checkpoint_batch(store, state, batch, event="batch_synthesis_error")
            raise
    state["current_batch_id"] = None
    if preview_only:
        measured_duration = None
        if all(batch.get("status") == "verified" for batch in state["batches"]):
            measured_duration = measure_complete_audio_duration(state, store)
        if measured_duration is not None and short_audio_without_preview(state, measured_duration):
            preview = state.get("preview") or {}
            previous_version = int(preview.get("current_version", 0))
            preview.update(current_version=0, approval=None)
            return store.transition(
                state, "batches_ready", stage="batches_ready",
                completed_step="preview_skipped_short_audio",
                updates={"preview": preview},
                details={"reason": "total_audio_below_preview_target", "previous_version": previous_version},
            )
        selection = preview_selection(state)
        if selection is None:
            raise TTSVerificationError("无法在已合成内容中定位完整句末试听截止点")
        return store.transition(
            state,
            "preview_rendering",
            stage="preview_rendering",
            completed_step=f"preview_v{int((state.get('preview') or {}).get('current_version', 0)) + 1:03d}_batches_ready",
            updates={"preview_selection": selection, "current_batch_id": None},
        )
    return store.transition(
        state,
        "batches_ready",
        stage="batches_ready",
        completed_step="all_batches_verified",
    )


def render_preview(state: dict[str, Any], store: WorkflowStateStore) -> dict[str, Any]:
    selection = dict(state.get("preview_selection") or {})
    selected_ids = list(selection.get("batch_ids") or [])
    if not selected_ids or int(selection.get("cutoff_ms", 0)) <= 0:
        raise TTSVerificationError("试听选择信息缺失")
    selected = [batch for batch in state["batches"] if batch["batch_id"] in selected_ids]
    if [batch["batch_id"] for batch in selected] != selected_ids:
        raise TTSVerificationError("试听 batch 顺序与状态不一致")
    results = [inspect_batch_artifacts(state, batch) for batch in selected]
    config = state["config_snapshot"]
    pauses_by_offset = _pauses_by_synthesis_offset(state)
    leading_pause_ms = sum(int(item["duration_ms"]) for item in pauses_by_offset.get(0, []))
    pauses_after: list[int] = []
    for index, batch in enumerate(selected):
        if index == len(selected) - 1:
            pauses_after.append(0)
            continue
        pauses_after.append(
            pause_after_batch(config, str(batch["boundary"]))
            + sum(
                int(item["duration_ms"])
                for item in pauses_by_offset.get(int(batch["synthesis_span"]["end"]), [])
            )
        )
    preview_state = state.setdefault("preview", {"current_version": 0, "versions": [], "approval": None})
    version = int(preview_state.get("current_version", 0)) + 1
    version_id = f"v{version:03d}"
    output_dir = Path(state["output_dir"])
    version_dir = output_dir / "preview" / version_id
    version_dir.mkdir(parents=True, exist_ok=False)
    staging = store.run_dir / "staging"
    audio_format = str(state["audio"]["format"])
    sample_rate = int(state["audio"]["sample_rate"])
    extension = FORMAT_EXTENSIONS[audio_format]
    merged = staging / f"preview_{version_id}_merged.{extension}"
    clean_audio = version_dir / f"preview_clean.{extension}"
    merge_audio(
        [result["audio_path"] for result in results],
        pauses_after,
        merged,
        audio_format,
        sample_rate,
        leading_pause_ms,
    )
    try:
        clip_audio(
            merged,
            clean_audio,
            duration_ms=int(selection["cutoff_ms"]),
            audio_format=audio_format,
            sample_rate=sample_rate,
        )
    except AudioClipError as exc:
        raise TTSConfigurationError(str(exc)) from exc
    merged.unlink(missing_ok=True)

    noise_config = dict(config.get("white_noise") or {"enabled": False})
    if bool(noise_config.get("enabled", False)):
        final_audio = version_dir / f"preview_with_white_noise.{extension}"
        raw_asset = Path(str(noise_config["asset_path"]))
        asset = raw_asset if raw_asset.is_absolute() else Path(state["root"]) / raw_asset
        if state.get("noise_asset_sha256") and file_sha256(asset) != state["noise_asset_sha256"]:
            raise TTSVerificationError("白噪音素材与预检时的指纹不一致")
        try:
            noise_result = add_white_noise_bed(
                clean_audio,
                asset,
                final_audio,
                target_rms_dbfs=float(noise_config["volume_dbfs"]),
                sample_rate=sample_rate,
                audio_format=audio_format,
                silence_threshold_dbfs=float(noise_config["silence_threshold_dbfs"]),
                minimum_silence_ms=int(noise_config["minimum_silence_ms"]),
                detection_window_ms=int(noise_config["detection_window_ms"]),
                fade_ms=int(noise_config["fade_ms"]),
                mp3_bitrate_kbps=int(noise_config["mp3_bitrate_kbps"]),
            )
        except AudioNoiseBedError as exc:
            raise TTSConfigurationError(str(exc)) from exc
    else:
        final_audio = clean_audio
        noise_result = {"status": "skipped", "path": str(clean_audio), "sha256": file_sha256(clean_audio)}

    actual_duration = batch_audio_duration_ms(final_audio, audio_format, sample_rate)
    timestamps = {
        "schema_version": "1.0",
        "duration_ms": actual_duration,
        "target_duration_ms": int(selection["target_duration_ms"]),
        "cutoff_ms": int(selection["cutoff_ms"]),
        "ended_at_sentence": selection["sentence_text"],
        "batch_ids": selected_ids,
    }
    timestamps_path = version_dir / "preview.timestamps.json"
    write_json(timestamps_path, timestamps)
    manifest = {
        "schema_version": "1.0",
        "workflow": WORKFLOW,
        "run_id": state["run_id"],
        "version": version,
        "version_id": version_id,
        "status": "awaiting_approval",
        "created_at": iso_timestamp(),
        "approved_transcript_sha256": state["approved_transcript_sha256"],
        "config_sha256": state["config_sha256"],
        "audio_config_sha256": json_sha256(state["audio"]),
        "speaker": state["speaker"],
        "speaker_id": state["speaker_id"],
        "resource_id": state["resource_id"],
        "selection": selection,
        "batch_ids": selected_ids,
        "clean_audio": {"path": str(clean_audio), "sha256": file_sha256(clean_audio)},
        "preview_audio": {"path": str(final_audio), "sha256": file_sha256(final_audio)},
        "timestamps": {"path": str(timestamps_path), "sha256": file_sha256(timestamps_path)},
        "white_noise": noise_result,
    }
    manifest_path = version_dir / "manifest.json"
    write_json(manifest_path, manifest)
    version_record = {
        "version": version,
        "version_id": version_id,
        "status": "awaiting_approval",
        "manifest_path": str(manifest_path),
        "manifest_sha256": file_sha256(manifest_path),
        "audio_path": str(final_audio),
        "audio_sha256": file_sha256(final_audio),
        "created_at": manifest["created_at"],
    }
    preview_state["current_version"] = version
    preview_state.setdefault("versions", []).append(version_record)
    preview_state["approval"] = None
    state = store.transition(
        state,
        "preview_ready",
        stage="preview_ready",
        completed_step=f"preview_{version_id}_ready",
        updates={"preview": preview_state},
        details={"version": version, "preview_audio_sha256": version_record["audio_sha256"]},
    )
    update_component(
        open_workspace(Path(state["workspace_dir"])),
        "speech",
        status="awaiting_preview_approval",
        manifest_path=manifest_path,
        details={"preview_version": version, "preview_audio_path": str(final_audio)},
    )
    return store.pause(
        state,
        status="paused_preview_approval",
        error_code="preview_approval_required",
        message=f"试听 {version_id} 已生成，等待人工确认",
        resume_stage="synthesizing",
        details={"version": version, "audio_path": str(final_audio), "audio_sha256": version_record["audio_sha256"]},
        pending_decisions=[
            {
                "type": "preview_approval",
                "question": "请试听后批准，或提交参数调整并生成新版本试听。",
                "version": version,
                "audio_path": str(final_audio),
                "audio_sha256": version_record["audio_sha256"],
            }
        ],
    )


def pause_after_batch(config: dict[str, Any], boundary: str) -> int:
    return max(0, int(config.get("boundary_pause_ms", {}).get(boundary, 0)))


def build_timing_diagnostics(
    items: list[dict[str, Any]],
    duration_ms: int,
    long_gap_warning_ms: int,
    explicit_pauses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    pause_intervals = list(explicit_pauses or [])
    positive_gaps: list[dict[str, Any]] = []
    item_duration_ms = 0
    for index, item in enumerate(items):
        item_duration_ms += max(0, int(item["end_ms"]) - int(item["start_ms"]))
        if index == 0:
            continue
        previous = items[index - 1]
        gap_ms = int(item["start_ms"]) - int(previous["end_ms"])
        if gap_ms > 0:
            gap_start = int(previous["end_ms"])
            gap_end = int(item["start_ms"])
            explicit_overlap_ms = sum(
                max(
                    0,
                    min(gap_end, int(pause["end_ms"]))
                    - max(gap_start, int(pause["start_ms"])),
                )
                for pause in pause_intervals
            )
            positive_gaps.append(
                {
                    "after": str(previous.get("text", "")),
                    "before": str(item.get("text", "")),
                    "start_ms": int(previous["end_ms"]),
                    "end_ms": int(item["start_ms"]),
                    "duration_ms": gap_ms,
                    "explicit_pause_ms": explicit_overlap_ms,
                    "non_explicit_duration_ms": max(0, gap_ms - explicit_overlap_ms),
                }
            )
    leading_silence_ms = int(items[0]["start_ms"]) if items else duration_ms
    trailing_silence_ms = max(0, duration_ms - int(items[-1]["end_ms"])) if items else 0
    positive_gap_ms = sum(gap["duration_ms"] for gap in positive_gaps)
    measured_silence_ms = leading_silence_ms + positive_gap_ms + trailing_silence_ms
    long_gaps = [
        gap for gap in positive_gaps if gap["non_explicit_duration_ms"] > long_gap_warning_ms
    ]
    warnings = [
        f"检测到 {len(long_gaps)} 处超过 {long_gap_warning_ms} ms 的非显式停顿"
    ] if long_gaps else []
    return {
        "item_duration_ms": item_duration_ms,
        "positive_gap_ms": positive_gap_ms,
        "leading_silence_ms": leading_silence_ms,
        "trailing_silence_ms": trailing_silence_ms,
        "measured_silence_ms": measured_silence_ms,
        "explicit_pause_ms": sum(int(item["duration_ms"]) for item in pause_intervals),
        "measured_silence_ratio": round(measured_silence_ms / duration_ms, 6)
        if duration_ms > 0
        else 0,
        "long_gap_warning_ms": long_gap_warning_ms,
        "long_gaps": long_gaps,
        "warnings": warnings,
    }


def merge_and_build_timestamps(state: dict[str, Any], store: WorkflowStateStore) -> dict[str, Any]:
    if state["status"] == "batches_ready":
        store.transition(state, "merging_audio", stage="merging_audio")
    validate_input_checkpoint(state)
    results = [inspect_batch_artifacts(state, batch) for batch in state["batches"]]
    config = state["config_snapshot"]
    mapping = read_json(Path(state["input"]["synthesis_map_path"]))
    pause_events = list(mapping.get("pauses") or [])
    pauses_by_offset: dict[int, list[dict[str, Any]]] = {}
    for event in pause_events:
        pauses_by_offset.setdefault(int(event["synthesis_offset"]), []).append(event)
    leading_events = pauses_by_offset.get(0, [])
    leading_pause_ms = sum(int(item["duration_ms"]) for item in leading_events)
    boundary_pauses: list[int] = []
    explicit_pauses_after: list[int] = []
    for index, batch in enumerate(state["batches"]):
        boundary_pause = pause_after_batch(config, batch["boundary"])
        if index == len(state["batches"]) - 1:
            boundary_pause = 0
        explicit_pause = sum(
            int(item["duration_ms"])
            for item in pauses_by_offset.get(int(batch["synthesis_span"]["end"]), [])
        )
        boundary_pauses.append(boundary_pause)
        explicit_pauses_after.append(explicit_pause)
    mapped_offsets = {0, *(int(batch["synthesis_span"]["end"]) for batch in state["batches"])}
    unmapped_offsets = sorted(set(pauses_by_offset) - mapped_offsets)
    if unmapped_offsets:
        raise TTSVerificationError(f"停顿事件未落在 batch 边界：{unmapped_offsets}")
    merge_pauses = [
        boundary + explicit
        for boundary, explicit in zip(boundary_pauses, explicit_pauses_after)
    ]
    output_dir = Path(state["output_dir"])
    staging = store.run_dir / "staging"
    audio_format = str(state["audio"]["format"])
    extension = FORMAT_EXTENSIONS[audio_format]
    noise_config = dict(config.get("white_noise") or {"enabled": False})
    noise_enabled = bool(noise_config.get("enabled", False))
    clean_stem = str(config["output"].get("clean_audio_stem") or config["output"]["full_audio_stem"])
    final_stem = str(config["output"]["full_audio_stem"]) if noise_enabled else clean_stem
    clean_audio = output_dir / f"{clean_stem}.{extension}"
    full_audio = output_dir / f"{final_stem}.{extension}"
    staged_audio = staging / f"full_clean.staged.{extension}"
    merge_audio(
        [result["audio_path"] for result in results],
        merge_pauses,
        staged_audio,
        audio_format,
        int(state["audio"]["sample_rate"]),
        leading_pause_ms,
    )
    full_duration = batch_audio_duration_ms(
        staged_audio, audio_format, int(state["audio"]["sample_rate"])
    )
    global_items: list[dict[str, Any]] = []
    global_sentences: list[dict[str, Any]] = []
    global_batches: list[dict[str, Any]] = []
    global_pauses: list[dict[str, Any]] = []
    offset = 0
    for event in leading_events:
        duration = int(event["duration_ms"])
        global_pauses.append({**event, "start_ms": offset, "end_ms": offset + duration})
        offset += duration
    for batch, result, boundary_pause, explicit_pause in zip(
        state["batches"], results, boundary_pauses, explicit_pauses_after
    ):
        index = int(batch["index"])
        global_items.extend(
            {
                **item,
                "start_ms": item["start_ms"] + offset,
                "end_ms": item["end_ms"] + offset,
                "batch_index": index,
            }
            for item in result["items"]
        )
        global_sentences.extend(
            {
                **sentence,
                "start_ms": sentence["start_ms"] + offset,
                "end_ms": sentence["end_ms"] + offset,
                "batch_index": index,
            }
            for sentence in result["sentences"]
        )
        global_batches.append(
            {
                "batch_index": index,
                "batch_id": batch["batch_id"],
                "text": batch["text"],
                "origin": batch.get("origin", "generated"),
                "synthesis_span": batch.get("synthesis_span"),
                "paragraph_ids": batch.get("paragraph_ids", []),
                "paragraph_segments": batch.get("paragraph_segments", []),
                "start_ms": offset,
                "end_ms": offset + int(result["duration_ms"]),
                "pause_after_ms": boundary_pause + explicit_pause,
                "boundary_pause_after_ms": boundary_pause,
                "explicit_pause_after_ms": explicit_pause,
                "audio_file": str(result["audio_path"].relative_to(output_dir)).replace("\\", "/"),
                "timestamp_file": Path(os.path.relpath(result["timestamp_path"], output_dir)).as_posix(),
            }
        )
        offset += int(result["duration_ms"])
        for event in pauses_by_offset.get(int(batch["synthesis_span"]["end"]), []):
            duration = int(event["duration_ms"])
            global_pauses.append({**event, "start_ms": offset, "end_ms": offset + duration})
            offset += duration
        offset += boundary_pause
    diagnostics = build_timing_diagnostics(
        global_items,
        full_duration,
        int(config.get("diagnostics", {}).get("long_gap_warning_ms", 700)),
        global_pauses,
    )
    staged_timestamps = staging / "full.timestamps.staged.json"
    write_json(
        staged_timestamps,
        {
            "schema_version": str(config["output"]["timestamp_schema_version"]),
            "audio_file": full_audio.name,
            "clean_audio_file": clean_audio.name,
            "duration_ms": full_duration,
            "time_unit": "ms",
            "interval_semantics": "[start_ms, end_ms)",
            "items": global_items,
            "sentences": global_sentences,
            "batches": global_batches,
            "pauses": global_pauses,
            "diagnostics": diagnostics,
        },
    )
    timestamps_path = output_dir / "full.timestamps.json"
    os.replace(staged_audio, clean_audio)
    os.replace(staged_timestamps, timestamps_path)
    manifest = {
        "schema_version": 4 if "backend" in state else 3,
        "backend": state.get("backend", "volcengine"),
        "backend_version": state.get("backend_version", "api-v3"),
        "timestamp_granularity": state.get("timestamp_granularity", "word"),
        "run_id": state["run_id"],
        "workspace_dir": state["workspace_dir"],
        "mode": state.get("mode", "original"),
        "revision": state.get("revision"),
        "status": "verifying",
        "created_at": state["created_at"],
        "input": state["input"],
        "speaker": state["speaker"],
        "speaker_id": state["speaker_id"],
        "resource_id": state["resource_id"],
        "audio": state["audio"],
        "preview": state.get("preview"),
        "batch_range": state["batch_range"],
        "mock": state["mock"],
        "full_audio": str(full_audio),
        "clean_audio": {
            "path": str(clean_audio),
            "sha256": file_sha256(clean_audio),
        },
        "white_noise": {
            "enabled": noise_enabled,
            "status": "pending" if noise_enabled else "skipped",
            "volume_dbfs": float(noise_config.get("volume_dbfs", -70.0)),
            "mode": "silence_only",
            "silence_threshold_dbfs": float(noise_config.get("silence_threshold_dbfs", -60.0)),
            "minimum_silence_ms": int(noise_config.get("minimum_silence_ms", 120)),
            "detection_window_ms": int(noise_config.get("detection_window_ms", 10)),
            "fade_ms": int(noise_config.get("fade_ms", 15)),
            "mp3_bitrate_kbps": int(noise_config.get("mp3_bitrate_kbps", 96)),
            "asset_path": str(noise_config.get("asset_path", "")),
            "asset_sha256": None,
            "output_path": str(full_audio) if noise_enabled else None,
            "output_sha256": None,
        },
        "full_timestamps": str(timestamps_path),
        "duration_ms": full_duration,
        "diagnostics": diagnostics,
        "pauses": global_pauses,
        "warnings": diagnostics["warnings"],
        "batches": [
            {
                "batch_id": batch["batch_id"],
                "index": batch["index"],
                "text": batch["text"],
                "char_count": batch["char_count"],
                "boundary": batch["boundary"],
                "origin": batch.get("origin", "generated"),
                "reused_from": batch.get("reused_from"),
                "attempts": batch.get("attempts", 0),
                "synthesis_span": batch.get("synthesis_span"),
                "paragraph_ids": batch.get("paragraph_ids", []),
                "paragraph_segments": batch.get("paragraph_segments", []),
                "audio_path": batch["audio"]["path"],
                "timestamp_path": batch["timestamps"]["path"],
                "duration_ms": batch["duration_ms"],
            }
            for batch in state["batches"]
        ],
    }
    manifest_path = output_dir / "manifest.json"
    write_json(manifest_path, manifest)
    state["artifacts"] = {
        "clean_audio": {"path": str(clean_audio), "sha256": file_sha256(clean_audio)},
        "full_timestamps": {"path": str(timestamps_path), "sha256": file_sha256(timestamps_path)},
        "manifest": {"path": str(manifest_path), "sha256": file_sha256(manifest_path)},
    }
    return store.transition(
        state,
        "timestamps_merged",
        stage="timestamps_merged",
        completed_step="timestamps_merged",
    )


def add_white_noise_checkpoint(
    state: dict[str, Any], store: WorkflowStateStore
) -> dict[str, Any]:
    if state["status"] == "timestamps_merged":
        store.transition(state, "adding_white_noise", stage="adding_white_noise")
    validate_input_checkpoint(state)
    output_dir = Path(state["output_dir"])
    manifest_path = output_dir / "manifest.json"
    timestamps_path = output_dir / "full.timestamps.json"
    manifest = read_json(manifest_path)
    clean_info = manifest.get("clean_audio") or {}
    clean_audio = Path(str(clean_info.get("path", "")))
    if not clean_audio.is_file() or file_sha256(clean_audio) != clean_info.get("sha256"):
        raise TTSVerificationError("clean 完整音频缺失或指纹不一致")
    noise_info = dict(manifest.get("white_noise") or {})
    if bool(noise_info.get("enabled")):
        raw_asset = Path(str(noise_info.get("asset_path", "")))
        asset = raw_asset if raw_asset.is_absolute() else Path(state["root"]) / raw_asset
        full_audio = Path(str(manifest["full_audio"]))
        if state.get("noise_asset_sha256") and file_sha256(asset) != state["noise_asset_sha256"]:
            raise TTSVerificationError("白噪音素材与预检时的指纹不一致")
        try:
            mixed = add_white_noise_bed(
                clean_audio,
                asset,
                full_audio,
                target_rms_dbfs=float(noise_info["volume_dbfs"]),
                sample_rate=int(state["audio"]["sample_rate"]),
                audio_format=str(state["audio"]["format"]),
                silence_threshold_dbfs=float(noise_info["silence_threshold_dbfs"]),
                minimum_silence_ms=int(noise_info["minimum_silence_ms"]),
                detection_window_ms=int(noise_info["detection_window_ms"]),
                fade_ms=int(noise_info["fade_ms"]),
                mp3_bitrate_kbps=int(noise_info["mp3_bitrate_kbps"]),
            )
        except AudioNoiseBedError as exc:
            raise TTSConfigurationError(str(exc)) from exc
        clean_duration = batch_audio_duration_ms(
            clean_audio, str(state["audio"]["format"]), int(state["audio"]["sample_rate"])
        )
        mixed_duration = batch_audio_duration_ms(
            full_audio, str(state["audio"]["format"]), int(state["audio"]["sample_rate"])
        )
        if abs(clean_duration - mixed_duration) > 80:
            raise TTSVerificationError(
                f"白噪音铺底前后时长不一致：clean={clean_duration}ms，mixed={mixed_duration}ms"
            )
        noise_info.update(
            status="completed",
            asset_path=mixed["asset_path"],
            asset_sha256=mixed["asset_sha256"],
            asset_rms_dbfs=mixed["asset_rms_dbfs"],
            output_path=mixed["path"],
            output_sha256=mixed["sha256"],
            silence_interval_count=mixed["silence_interval_count"],
            silence_duration_ms=mixed["silence_duration_ms"],
            silence_intervals_sha256=mixed["silence_intervals_sha256"],
        )
    else:
        full_audio = clean_audio
        manifest["full_audio"] = str(clean_audio)
        noise_info.update(status="skipped", output_path=None, output_sha256=None)
    manifest["white_noise"] = noise_info
    manifest["status"] = "verifying"
    write_json(manifest_path, manifest)
    state["artifacts"]["full_audio"] = {
        "path": str(full_audio),
        "sha256": file_sha256(full_audio),
    }
    state["artifacts"]["manifest"]["sha256"] = file_sha256(manifest_path)
    if not timestamps_path.is_file():
        raise TTSVerificationError("完整时间戳缺失")
    return store.transition(
        state,
        "final_audio_ready",
        stage="final_audio_ready",
        completed_step="white_noise_added" if noise_info["status"] == "completed" else "white_noise_skipped",
    )


def finalize_verification(state: dict[str, Any], store: WorkflowStateStore) -> dict[str, Any]:
    if state["status"] == "final_audio_ready":
        store.transition(state, "verifying", stage="verifying")
    validate_input_checkpoint(state)
    if state.get("upstream_handoff"):
        verify_handoff(Path(state["root"]), state["upstream_handoff"])
    full_duration = int(read_json(Path(state["output_dir"]) / "full.timestamps.json")["duration_ms"])
    if bool(state["config_snapshot"]["preview"]["enabled"]) and not short_audio_without_preview(state, full_duration):
        approval = (state.get("preview") or {}).get("approval") or {}
        if approval.get("status") != "approved":
            raise TTSVerificationError("完整合成缺少有效试听确认回执")
        receipt_path = Path(
            str((state.get("preview") or {}).get("versions", [])[-1].get("approval_receipt_path", ""))
        )
        if not receipt_path.is_file() or file_sha256(receipt_path) != (state.get("preview") or {}).get(
            "versions", []
        )[-1].get("approval_receipt_sha256"):
            raise TTSVerificationError("试听确认回执缺失或指纹发生变化")
    for batch in state["batches"]:
        inspect_batch_artifacts(state, batch)
    if state.get("mode") == "revision":
        revision = state.get("revision") or {}
        base_manifest = Path(str(revision.get("base_output_dir", ""))) / "manifest.json"
        if not base_manifest.is_file() or file_sha256(base_manifest) != revision.get(
            "base_manifest_sha256"
        ):
            raise TTSVerificationError("基准运行 manifest 在修订期间发生变化")
    for artifact in state.get("artifacts", {}).values():
        path = Path(artifact["path"])
        if not path.is_file() or file_sha256(path) != artifact["sha256"]:
            raise TTSVerificationError(f"完整产物缺失或指纹变化：{path}")
    output_dir = Path(state["output_dir"])
    verification = verify_output(output_dir, accepted_manifest_statuses={"verifying", "completed"})
    if verification["status"] != "passed":
        raise TTSVerificationError("输出验证失败：" + "；".join(verification["errors"]))
    manifest_path = output_dir / "manifest.json"
    manifest = read_json(manifest_path)
    manifest["status"] = "completed"
    manifest["completed_at"] = iso_timestamp()
    write_json(manifest_path, manifest)
    state["artifacts"]["manifest"]["sha256"] = file_sha256(manifest_path)
    final_verification = verify_output(output_dir)
    if final_verification["status"] != "passed":
        raise TTSVerificationError("最终验证失败：" + "；".join(final_verification["errors"]))
    write_json(store.run_dir / "verification.json", final_verification)
    state = store.transition(
        state,
        "completed",
        stage="completed",
        completed_step="verification_passed",
        updates={"current_batch_id": None, "verification": final_verification},
    )
    update_component(
        open_workspace(Path(state["workspace_dir"])),
        "speech",
        status="completed",
        manifest_path=manifest_path,
    )
    return state


def compact_receipt(state: dict[str, Any]) -> dict[str, Any]:
    next_action = None
    if state["status"] == "paused_preview_approval":
        next_action = "approve-preview-or-retry-preview"
    elif state["status"] == "paused_transcript_not_ready":
        component = read_json(open_workspace(Path(state["workspace_dir"])).pipeline_manifest)["components"]["transcript"]
        upstream = read_json(producer_paths(Path(state["root"]), component["producer_skill"], component["producer_run_id"])["state"])
        next_action = transcript_next_action(upstream["status"], consumer=True)
    elif str(state["status"]).startswith("paused_"):
        next_action = "resume"
    elif state["status"] not in {"completed", "superseded", "failed_terminal"}:
        next_action = "resume"
    receipt = {
        "status": state["status"],
        "run_id": state["run_id"],
        "current_stage": state["current_stage"],
        "current_batch_id": state.get("current_batch_id"),
        "output_dir": state.get("output_dir"),
        "error": state.get("error"),
        "next_action": next_action,
    }
    if state.get("verification") is not None:
        receipt["verification"] = state["verification"]
    preview = state.get("preview") or {}
    if preview.get("current_version"):
        current = preview.get("versions", [])[-1]
        receipt["preview"] = {
            "version": current.get("version"),
            "status": current.get("status"),
            "audio_path": current.get("audio_path"),
            "audio_sha256": current.get("audio_sha256"),
            "manifest_path": current.get("manifest_path"),
        }
    return receipt


def approve_preview(
    state: dict[str, Any],
    store: WorkflowStateStore,
    *,
    preview_sha256: str,
    confirmed_by: str,
) -> dict[str, Any]:
    if state.get("status") != "paused_preview_approval":
        raise TTSWorkflowError("当前运行不在试听确认状态")
    preview = state.get("preview") or {}
    versions = preview.get("versions") or []
    if not versions:
        raise TTSVerificationError("状态缺少试听版本")
    current = versions[-1]
    if preview_sha256 != current.get("audio_sha256"):
        raise TTSVerificationError("提交的试听音频哈希与当前版本不一致")
    audio_path = Path(str(current.get("audio_path", "")))
    manifest_path = Path(str(current.get("manifest_path", "")))
    if not audio_path.is_file() or file_sha256(audio_path) != preview_sha256:
        raise TTSVerificationError("当前试听音频缺失或指纹发生变化")
    manifest = read_json(manifest_path)
    if manifest.get("status") != "awaiting_approval":
        raise TTSVerificationError("试听 manifest 状态不允许确认")
    if manifest.get("approved_transcript_sha256") != state.get("approved_transcript_sha256"):
        raise TTSVerificationError("试听对应的逐字稿已变化")
    if manifest.get("audio_config_sha256") != json_sha256(state["audio"]):
        raise TTSVerificationError("试听对应的音频参数已变化")
    receipt = {
        "schema_version": "1.0",
        "run_id": state["run_id"],
        "preview_version": int(current["version"]),
        "status": "approved",
        "preview_audio_path": str(audio_path),
        "preview_audio_sha256": preview_sha256,
        "approved_transcript_sha256": state["approved_transcript_sha256"],
        "audio_config_sha256": json_sha256(state["audio"]),
        "confirmed_by": confirmed_by.strip(),
        "confirmed_at": iso_timestamp(),
    }
    if not receipt["confirmed_by"]:
        raise TTSWorkflowError("confirmed_by 不能为空")
    receipt_path = manifest_path.parent / "approval-receipt.json"
    write_json(receipt_path, receipt)
    manifest["status"] = "approved"
    manifest["approval_receipt"] = {"path": str(receipt_path), "sha256": file_sha256(receipt_path)}
    write_json(manifest_path, manifest)
    current.update(
        status="approved",
        manifest_sha256=file_sha256(manifest_path),
        approval_receipt_path=str(receipt_path),
        approval_receipt_sha256=file_sha256(receipt_path),
    )
    preview["approval"] = receipt
    store.checkpoint(
        state,
        event="preview_approved",
        completed_step=f"preview_v{int(current['version']):03d}_approved",
        updates={"preview": preview},
        details={"version": current["version"], "confirmed_by": receipt["confirmed_by"]},
    )
    return store.transition(state, "synthesizing", stage="synthesizing")


def retry_preview(
    state: dict[str, Any], store: WorkflowStateStore, args: argparse.Namespace
) -> dict[str, Any]:
    if state.get("status") != "paused_preview_approval":
        raise TTSWorkflowError("当前运行不在试听确认状态")
    if not str(args.reason).strip():
        raise TTSWorkflowError("重新生成试听必须提供 reason")
    if state.get("backend", "volcengine") == "edge-tts" and args.emotion_scale is not None:
        raise TTSConfigurationError("edge-tts 不支持 emotion-scale 参数")
    audio = dict(state["audio"])
    for key, argument_name in (
        ("format", "audio_format"),
        ("sample_rate", "sample_rate"),
        ("speech_rate", "speech_rate"),
        ("loudness_rate", "loudness_rate"),
        ("emotion", "emotion"),
        ("emotion_scale", "emotion_scale"),
    ):
        value = getattr(args, argument_name, None)
        if value is not None:
            audio[key] = value
    if state.get("mock"):
        audio["format"] = "wav"
    if str(audio["format"]) not in SUPPORTED_FORMATS:
        raise TTSWorkflowError(f"不支持的音频格式：{audio['format']}")
    speech_rate_multiplier(int(audio["speech_rate"]))
    candidate = {**state, "audio": audio}
    if args.speaker:
        speaker, speaker_id, resource_id = resolve_voice(
            args.speaker, args.resource_id, state["config_snapshot"]
        )
        candidate.update(speaker=speaker, speaker_id=speaker_id, resource_id=resource_id)
    elif args.resource_id:
        candidate["resource_id"] = args.resource_id
    check_backend_checkpoint(candidate)
    signature_changed = synthesis_fingerprint(state, "") != synthesis_fingerprint(candidate, "")
    state.update(audio=audio, speaker=candidate["speaker"], speaker_id=candidate["speaker_id"],
                 resource_id=candidate["resource_id"])
    if signature_changed:
        for batch in state["batches"]:
            batch["origin"] = "generated"
            batch.pop("reused_from", None)
        if state.get("revision"):
            state["revision"]["affected_batch_ids"] = [batch["batch_id"] for batch in state["batches"]]
            state["revision"]["reused_batch_ids"] = []
    for batch in state["batches"]:
        audio_path, timestamps_path = batch_artifact_paths(state, batch)
        audio_path.unlink(missing_ok=True)
        timestamps_path.unlink(missing_ok=True)
        batch.update(
            status="reuse_pending" if batch.get("origin") == "reused" else "pending",
            attempts=0,
            started_at=None,
            completed_at=None,
            error=None,
            audio=None,
            timestamps=None,
            duration_ms=None,
        )
    preview = state.get("preview") or {}
    if preview.get("versions"):
        rejected = preview["versions"][-1]
        rejected_at = iso_timestamp()
        rejected["rejected_at"] = rejected_at
        rejected["rejection_reason"] = str(args.reason).strip()
        rejected["status"] = "rejected"
        rejected_manifest_path = Path(str(rejected["manifest_path"]))
        rejected_manifest = read_json(rejected_manifest_path)
        rejected_manifest.update(
            status="rejected",
            rejected_at=rejected_at,
            rejection_reason=str(args.reason).strip(),
        )
        write_json(rejected_manifest_path, rejected_manifest)
        rejected["manifest_sha256"] = file_sha256(rejected_manifest_path)
    preview["approval"] = None
    state["audio"] = audio
    request_path = Path(state["request_path"])
    request = read_json(request_path)
    request["speaker"] = state["speaker"]
    request["resource_id"] = state["resource_id"]
    request["audio"] = audio
    request.setdefault("preview_retries", []).append(
        {
            "rejected_version": int(preview.get("current_version", 0)),
            "reason": str(args.reason).strip(),
            "requested_at": iso_timestamp(),
            "audio": audio,
        }
    )
    write_json(request_path, request)
    store.checkpoint(
        state,
        event="preview_retry_requested",
        updates={"preview": preview, "preview_selection": None, "current_batch_id": None},
        details={"reason": str(args.reason).strip(), "next_version": int(preview.get("current_version", 0)) + 1},
    )
    return store.transition(state, "preview_synthesizing", stage="preview_synthesizing")


def pause_for_exception(
    state: dict[str, Any], store: WorkflowStateStore, exc: Exception
) -> dict[str, Any]:
    resume_stage = str(state["current_stage"])
    if isinstance(exc, TTSRetryableError) or isinstance(exc, OSError):
        return store.pause(
            state,
            status="paused_retryable",
            error_code="retryable_error",
            message=str(exc),
            resume_stage=resume_stage,
        )
    if isinstance(exc, TTSConfigurationError) or isinstance(exc, TTSWorkflowError) and not isinstance(
        exc, (TTSVerificationError, TTSTerminalError)
    ):
        return store.pause(
            state,
            status="paused_configuration",
            error_code="configuration_error",
            message=str(exc),
            resume_stage=resume_stage,
        )
    if isinstance(exc, TTSVerificationError):
        return store.pause(
            state,
            status="paused_verification",
            error_code="verification_error",
            message=str(exc),
            resume_stage=resume_stage,
        )
    return store.transition(
        state,
        "failed_terminal",
        stage=resume_stage,
        updates={"error": {"code": "terminal_error", "message": str(exc), "details": None}},
    )


def advance_state_machine(state: dict[str, Any], store: WorkflowStateStore) -> dict[str, Any]:
    try:
        if "backend" in state and state["status"] not in {"initialized", "workspace_resolved", "input_validated", "backend_resolved", "completed", "superseded", "failed_terminal"} and not str(state["status"]).startswith("paused_"):
            check_backend_checkpoint(state)
        while True:
            status = str(state["status"])
            if status == "initialized":
                state = resolve_workspace_checkpoint(state, store)
            elif status == "workspace_resolved":
                state = validate_workspace_input(state, store)
            elif status == "input_validated":
                if "backend" not in state:  # preserve old run semantics
                    state = validate_transcript_checkpoint(state, store)
                else:
                    state = store.transition(state, "backend_resolved", stage="backend_resolved")
            elif status == "backend_resolved":
                state = store.transition(state, "checking_backend_environment", stage="checking_backend_environment")
            elif status in {"checking_backend_environment", "awaiting_transcript"}:
                if "backend" in state:
                    check_backend_checkpoint(state)
                state = validate_transcript_checkpoint(state, store)
            elif status == "transcript_validated":
                state = stage_input(state, store)
            elif status == "input_staged":
                state = plan_batches(state, store)
            elif status == "batches_planned":
                if bool(state["config_snapshot"]["preview"]["enabled"]):
                    state = synthesize_batches(state, store, preview_only=True)
                else:
                    state = synthesize_batches(state, store)
            elif status == "preview_synthesizing":
                state = synthesize_batches(state, store, preview_only=True)
            elif status == "preview_rendering":
                state = render_preview(state, store)
            elif status == "preview_ready":
                raise TTSTerminalError("试听已生成但尚未进入确认暂停状态")
            elif status == "synthesizing":
                state = synthesize_batches(state, store)
            elif status in {"batches_ready", "merging_audio"}:
                state = merge_and_build_timestamps(state, store)
            elif status in {"timestamps_merged", "adding_white_noise"}:
                state = add_white_noise_checkpoint(state, store)
            elif status in {"final_audio_ready", "verifying"}:
                state = finalize_verification(state, store)
            elif status in {"completed", "superseded", "failed_terminal"} or status.startswith("paused_"):
                return state
            else:
                raise TTSTerminalError(f"无法处理状态：{status}")
    except Exception as exc:
        if state["status"] in {"completed", "superseded", "failed_terminal"}:
            raise
        return pause_for_exception(state, store, exc)


def validate_timeline(items: list[dict[str, Any]], duration_ms: int, label: str) -> list[str]:
    errors: list[str] = []
    previous_start = -1
    for index, item in enumerate(items):
        start = item.get("start_ms")
        end = item.get("end_ms")
        if not isinstance(start, int) or not isinstance(end, int):
            errors.append(f"{label}[{index}] 缺少整数时间")
            continue
        if start < previous_start or end < start:
            errors.append(f"{label}[{index}] 时间不单调")
        if end > duration_ms + 1000:
            errors.append(f"{label}[{index}] 超出完整音频时长")
        previous_start = start
    return errors


def verify_output(
    output_dir: Path, *, accepted_manifest_statuses: set[str] | None = None
) -> dict[str, Any]:
    errors: list[str] = []
    manifest_path = output_dir / "manifest.json"
    timestamp_path = output_dir / "full.timestamps.json"
    if not manifest_path.exists():
        return {"status": "failed", "errors": ["缺少 manifest.json"]}
    if not timestamp_path.exists():
        return {"status": "failed", "errors": ["缺少 full.timestamps.json"]}
    manifest = read_json(manifest_path)
    timestamps = read_json(timestamp_path)
    accepted = accepted_manifest_statuses or {"completed"}
    if manifest.get("status") not in accepted:
        errors.append("manifest 状态不在允许集合：" + "、".join(sorted(accepted)))
    schema_version = int(manifest.get("schema_version", 0))
    if schema_version not in {2, 3, 4}:
        errors.append("manifest schema_version 必须为 2、3 或 4")
    audio_path = Path(manifest.get("full_audio", ""))
    if not audio_path.is_file() or audio_path.stat().st_size == 0:
        errors.append("完整音频不存在或为空")
    if schema_version == 4:
        if manifest.get("backend") not in BACKENDS or not manifest.get("backend_version") or manifest.get("timestamp_granularity") != "word":
            errors.append("manifest 后端元数据无效")
    duration_ms = int(timestamps.get("duration_ms", 0))
    if duration_ms <= 0:
        errors.append("完整音频时长无效")
    elif audio_path.is_file():
        try:
            measured = batch_audio_duration_ms(
                audio_path,
                str((manifest.get("audio") or {}).get("format", "")),
                int((manifest.get("audio") or {}).get("sample_rate", 0)),
            )
            if abs(measured - duration_ms) > 80:
                errors.append(f"完整音频与时间戳时长不一致：audio={measured}ms，timestamps={duration_ms}ms")
        except (OSError, ValueError, TTSWorkflowError) as exc:
            errors.append(f"无法探测完整音频时长：{exc}")
    if schema_version >= 3:
        clean_info = manifest.get("clean_audio") or {}
        clean_path = Path(str(clean_info.get("path", "")))
        if not clean_path.is_file() or clean_path.stat().st_size == 0:
            errors.append("clean 完整音频不存在或为空")
        elif file_sha256(clean_path) != clean_info.get("sha256"):
            errors.append("clean 完整音频指纹不一致")
        noise_info = manifest.get("white_noise") or {}
        if not isinstance(noise_info.get("enabled"), bool):
            errors.append("white_noise.enabled 必须为布尔值")
        elif noise_info["enabled"]:
            asset_path = Path(str(noise_info.get("asset_path", "")))
            if noise_info.get("status") != "completed":
                errors.append("启用白噪音时 white_noise.status 必须为 completed")
            if not asset_path.is_file() or file_sha256(asset_path) != noise_info.get("asset_sha256"):
                errors.append("白噪音素材缺失或指纹不一致")
            if str(audio_path) != str(noise_info.get("output_path", "")):
                errors.append("full_audio 与白噪音输出路径不一致")
            elif audio_path.is_file() and file_sha256(audio_path) != noise_info.get("output_sha256"):
                errors.append("白噪音铺底音频指纹不一致")
            if noise_info.get("mode") != "silence_only":
                errors.append("白噪音处理模式必须为 silence_only")
            for key in ("silence_interval_count", "silence_duration_ms"):
                if not isinstance(noise_info.get(key), int) or int(noise_info[key]) < 0:
                    errors.append(f"white_noise.{key} 必须是非负整数")
            if not isinstance(noise_info.get("silence_intervals_sha256"), str):
                errors.append("white_noise.silence_intervals_sha256 缺失")
        else:
            if noise_info.get("status") != "skipped" or audio_path != clean_path:
                errors.append("禁用白噪音时 full_audio 必须指向 clean 音频")
        if timestamps.get("audio_file") != audio_path.name:
            errors.append("full.timestamps.json 的 audio_file 与主音频不一致")
        if timestamps.get("clean_audio_file") != clean_path.name:
            errors.append("full.timestamps.json 的 clean_audio_file 与 clean 音频不一致")
    for key in ("items", "sentences", "batches"):
        collection = timestamps.get(key)
        if not isinstance(collection, list) or not collection:
            errors.append(f"时间戳缺少 {key}")
        else:
            errors.extend(validate_timeline(collection, duration_ms, key))
    pauses = timestamps.get("pauses")
    if not isinstance(pauses, list):
        errors.append("时间戳缺少 pauses")
        pauses = []
    else:
        errors.extend(validate_timeline(pauses, duration_ms, "pauses"))
        for index, pause in enumerate(pauses):
            if int(pause.get("duration_ms", -1)) != int(pause.get("end_ms", 0)) - int(
                pause.get("start_ms", 0)
            ):
                errors.append(f"pauses[{index}] 时长与区间不一致")
    diagnostics = timestamps.get("diagnostics")
    if not isinstance(diagnostics, dict):
        errors.append("时间戳缺少 diagnostics")
        diagnostics = {}
    else:
        for key in (
            "item_duration_ms",
            "positive_gap_ms",
            "leading_silence_ms",
            "trailing_silence_ms",
            "measured_silence_ms",
            "explicit_pause_ms",
            "long_gap_warning_ms",
        ):
            if not isinstance(diagnostics.get(key), int) or int(diagnostics[key]) < 0:
                errors.append(f"diagnostics.{key} 必须是非负整数")
        if not isinstance(diagnostics.get("long_gaps"), list):
            errors.append("diagnostics.long_gaps 必须是数组")
        if not isinstance(diagnostics.get("warnings"), list):
            errors.append("diagnostics.warnings 必须是数组")
    if manifest.get("diagnostics") != diagnostics:
        errors.append("manifest 与时间戳的 diagnostics 不一致")
    if manifest.get("pauses") != pauses:
        errors.append("manifest 与时间戳的 pauses 不一致")
    if list(manifest.get("warnings") or []) != list(diagnostics.get("warnings") or []):
        errors.append("manifest 与 diagnostics 的 warnings 不一致")
    for batch in manifest.get("batches", []):
        for key in ("audio_path", "timestamp_path"):
            path = Path(batch.get(key, ""))
            if not path.is_file() or path.stat().st_size == 0:
                errors.append(f"batch 文件缺失：{path}")
        timestamp_file = Path(str(batch.get("timestamp_path", "")))
        if timestamp_file.is_file():
            raw_timestamps = read_json(timestamp_file)
            if raw_timestamps.get("text") != batch.get("text"):
                errors.append(f"{batch.get('batch_id')} 原始时间戳文本与 batch 文本不一致")
            if schema_version == 4 and not raw_timestamps.get("synthesis_fingerprint"):
                errors.append(f"{batch.get('batch_id')} 缺少合成指纹")
            if schema_version == 4 and raw_timestamps.get("synthesis_fingerprint"):
                if raw_timestamps["synthesis_fingerprint"] != synthesis_fingerprint(manifest, str(batch["text"]), adapter_version=raw_timestamps.get("adapter_version", "1")):
                    errors.append(f"{batch.get('batch_id')} 合成后端或参数指纹不一致")
    input_info = manifest.get("input") or {}
    copied_path = Path(str(input_info.get("copied_path", "")))
    synthesis_path = Path(str(input_info.get("synthesis_path", "")))
    for path, hash_key in (
        (copied_path, "file_sha256"),
        (synthesis_path, "synthesis_file_sha256"),
    ):
        if not path.is_file() or path.stat().st_size == 0:
            errors.append(f"输入产物缺失：{path}")
        elif file_sha256(path) != input_info.get(hash_key):
            errors.append(f"输入产物指纹不一致：{path}")
    if copied_path.is_file() and synthesis_path.is_file():
        try:
            copied_text = validate_utf8_input(copied_path)
            synthesis_text = validate_utf8_input(synthesis_path)
            synthesis_policy = dict(input_info.get("synthesis_policy") or {})
            if synthesis_text != normalize_synthesis_text(
                copied_text, str(synthesis_policy.get("missing_sentence_ending", "。"))
            ):
                errors.append("合成文本与逐字稿归一化结果不一致")
            if "".join(str(batch.get("text", "")) for batch in manifest.get("batches", [])) != synthesis_text:
                errors.append("batch 文本与合成文本不一致")
            if int(manifest.get("schema_version", 1)) >= 2:
                synthesis_map_path = Path(str(input_info.get("synthesis_map_path", "")))
                if not synthesis_map_path.is_file():
                    errors.append("缺少 inputs/generated/synthesis-map.json")
                elif file_sha256(synthesis_map_path) != input_info.get("synthesis_map_file_sha256"):
                    errors.append("合成映射指纹不一致")
                else:
                    synthesis_map = read_json(synthesis_map_path)
                    errors.extend(
                        validate_synthesis_map(
                            synthesis_map,
                            copied_text,
                            synthesis_text,
                            [dict(batch) for batch in manifest.get("batches", [])],
                        )
                    )
                    map_pauses = synthesis_map.get("pauses") or []
                    comparable = ("pause_id", "directive", "duration_ms", "source_span", "synthesis_offset")
                    if [
                        {key: item.get(key) for key in comparable} for item in pauses
                    ] != [
                        {key: item.get(key) for key in comparable} for item in map_pauses
                    ]:
                        errors.append("完整时间戳的 pauses 与合成映射不一致")
        except TTSWorkflowError as exc:
            errors.append(f"输入产物无效：{exc}")
    if manifest.get("mode") == "revision":
        revision = manifest.get("revision") or {}
        affected = {
            str(batch.get("batch_id"))
            for batch in manifest.get("batches", [])
            if batch.get("origin") == "generated"
        }
        reused = {
            str(batch.get("batch_id"))
            for batch in manifest.get("batches", [])
            if batch.get("origin") == "reused"
        }
        if affected != set(revision.get("affected_batch_ids") or []):
            errors.append("revision.affected_batch_ids 与 batch 来源不一致")
        if reused != set(revision.get("reused_batch_ids") or []):
            errors.append("revision.reused_batch_ids 与 batch 来源不一致")
        for batch in manifest.get("batches", []):
            if batch.get("origin") == "reused":
                provenance = batch.get("reused_from") or {}
                if int(batch.get("attempts", -1)) != 0:
                    errors.append(f"{batch.get('batch_id')} 复用 batch 不应产生合成尝试")
                if provenance.get("audio_sha256") != file_sha256(Path(batch["audio_path"])):
                    errors.append(f"{batch.get('batch_id')} 复用音频指纹不一致")
                if provenance.get("timestamp_sha256") != file_sha256(Path(batch["timestamp_path"])):
                    errors.append(f"{batch.get('batch_id')} 复用时间戳指纹不一致")
            elif int(batch.get("attempts", 0)) < 1:
                errors.append(f"{batch.get('batch_id')} 修改 batch 缺少合成尝试")
        base_manifest = Path(str(revision.get("base_output_dir", ""))) / "manifest.json"
        if not base_manifest.is_file() or file_sha256(base_manifest) != revision.get(
            "base_manifest_sha256"
        ):
            errors.append("基准运行 manifest 指纹不一致")
    preview = manifest.get("preview") or {}
    workspace = open_workspace(Path(str(manifest.get("workspace_dir") or output_dir.parent)))
    saved_state_path = workspace.logs_dir / "state.json"
    if schema_version == 4 and workspace.layout_profile == "education" and not saved_state_path.is_file():
        errors.append("缺少运行状态，无法验证试听门禁")
    if saved_state_path.is_file():
        saved_state = read_json(saved_state_path)
        if saved_state.get("config_sha256") != json_sha256(saved_state.get("config_snapshot") or {}):
            errors.append("运行配置快照指纹不一致")
        if saved_state.get("preview") != manifest.get("preview"):
            errors.append("manifest 试听记录与运行状态不一致")
        preview_config = (saved_state.get("config_snapshot") or {}).get("preview") or {}
        if preview_config.get("enabled") and not preview.get("current_version", 0):
            if not short_audio_without_preview(saved_state, duration_ms):
                errors.append("达到试听目标的完整音频缺少试听确认")
    if int(preview.get("current_version", 0)) > 0:
        versions = preview.get("versions") or []
        approval = preview.get("approval") or {}
        if not versions or versions[-1].get("status") != "approved":
            errors.append("当前试听版本尚未批准")
        elif approval.get("status") != "approved":
            errors.append("缺少有效试听确认信息")
        else:
            current_preview = versions[-1]
            for path_key, hash_key in (
                ("audio_path", "audio_sha256"),
                ("manifest_path", "manifest_sha256"),
                ("approval_receipt_path", "approval_receipt_sha256"),
            ):
                preview_path = Path(str(current_preview.get(path_key, "")))
                if not preview_path.is_file() or file_sha256(preview_path) != current_preview.get(hash_key):
                    errors.append(f"试听产物缺失或指纹不一致：{preview_path}")
    if (manifest.get("input") or {}).get("source_kind") == "text":
        suspicious = sorted(
            {
                sequence
                for batch in manifest.get("batches", [])
                for sequence in suspicious_inline_escape_sequences(str(batch.get("text", "")))
            }
        )
        if suspicious:
            rendered = "、".join(json.dumps(item, ensure_ascii=False) for item in suspicious)
            errors.append(f"文本输入包含疑似 shell 换行转义字面量：{rendered}")
    workspace_dir = Path(str(manifest.get("workspace_dir") or output_dir.parent))
    inputs_dir = open_workspace(workspace_dir).inputs_dir
    if not inputs_dir.is_dir() or not any(inputs_dir.iterdir()):
        errors.append("inputs 目录为空")
    return {
        "status": "passed" if not errors else "failed",
        "checked_at": iso_timestamp(),
        "errors": errors,
        "warnings": list(diagnostics.get("warnings") or []),
        "counts": {
            "items": len(timestamps.get("items", [])),
            "sentences": len(timestamps.get("sentences", [])),
            "batches": len(timestamps.get("batches", [])),
            "pauses": len(timestamps.get("pauses", [])),
        },
    }


def add_common_audio_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--resource-id")
    parser.add_argument("--format", dest="audio_format", choices=sorted(SUPPORTED_FORMATS))
    parser.add_argument("--sample-rate", type=int)
    parser.add_argument("--speech-rate", type=int)
    parser.add_argument("--loudness-rate", type=int)
    parser.add_argument("--emotion")
    parser.add_argument("--emotion-scale", type=int, choices=range(1, 6))


def configure_start_parser(parser: argparse.ArgumentParser, *, allow_backend: bool = True) -> None:
    parser.add_argument("--root", default=str(DEFAULT_PROJECT_DIR))
    parser.add_argument("--speaker")
    if allow_backend:
        parser.add_argument("--backend", choices=BACKENDS, help="本次后端覆盖；恢复使用运行快照")
    source = parser.add_mutually_exclusive_group(required=False)
    source.add_argument("--text")
    source.add_argument("--input-file")
    parser.add_argument("--output-root")
    parser.add_argument("--workspace-dir")
    parser.add_argument("--transcript-run-id")
    parser.add_argument("--producer-skill", choices=("convert-copy-to-transcript", "run-speech-to-text"), default="convert-copy-to-transcript")
    parser.add_argument("--mock", action="store_true")
    add_common_audio_arguments(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="分批运行 Edge / 火山引擎文字转语音")
    subparsers = parser.add_subparsers(dest="command", required=True)
    configure_start_parser(subparsers.add_parser("start", help="创建状态机运行并自动推进"))
    configure_start_parser(subparsers.add_parser("run", help="start 的兼容别名"))

    resume_parser = subparsers.add_parser("resume", help="从运行状态恢复")
    resume_parser.add_argument("--root", default=str(DEFAULT_PROJECT_DIR))
    resume_parser.add_argument("--run-id", required=True)
    resume_source = resume_parser.add_mutually_exclusive_group(required=False)
    resume_source.add_argument("--text")
    resume_source.add_argument("--input-file")

    approve_preview_parser = subparsers.add_parser(
        "approve-preview", help="确认当前试听版本并继续全量合成"
    )
    approve_preview_parser.add_argument("--root", default=str(DEFAULT_PROJECT_DIR))
    approve_preview_parser.add_argument("--run-id", required=True)
    approve_preview_parser.add_argument("--preview-sha256", required=True)
    approve_preview_parser.add_argument("--confirmed-by", required=True)

    retry_preview_parser = subparsers.add_parser(
        "retry-preview", help="在同一运行中调整参数并生成下一版试听"
    )
    retry_preview_parser.add_argument("--root", default=str(DEFAULT_PROJECT_DIR))
    retry_preview_parser.add_argument("--run-id", required=True)
    retry_preview_parser.add_argument("--reason", required=True)
    retry_preview_parser.add_argument("--speaker")
    add_common_audio_arguments(retry_preview_parser)

    status_parser = subparsers.add_parser("status", help="只读查看运行状态")
    status_parser.add_argument("--root", default=str(DEFAULT_PROJECT_DIR))
    status_parser.add_argument("--run-id", required=True)

    verify_parser = subparsers.add_parser("verify", help="验证输出目录")
    verify_parser.add_argument("--root", default=str(DEFAULT_PROJECT_DIR))
    verify_target = verify_parser.add_mutually_exclusive_group(required=True)
    verify_target.add_argument("--output-dir")
    verify_target.add_argument("--run-id")

    supersede_parser = subparsers.add_parser("supersede", help="标记未完成运行已被取代")
    supersede_parser.add_argument("--root", default=str(DEFAULT_PROJECT_DIR))
    supersede_parser.add_argument("--run-id", required=True)
    supersede_parser.add_argument("--reason", required=True)

    locate_parser = subparsers.add_parser("locate", help="在已完成运行的原文中定位修订目标")
    locate_parser.add_argument("--root", default=str(DEFAULT_PROJECT_DIR))
    locate_parser.add_argument("--run-id", required=True)
    locate_parser.add_argument("--query-file", required=True)

    revise_parser = subparsers.add_parser("revise", help="创建局部修订运行并复用未修改 batch")
    revise_parser.add_argument("--root", default=str(DEFAULT_PROJECT_DIR))
    revise_parser.add_argument("--base-run-id", required=True)
    revise_parser.add_argument("--replace", action="append", required=True)
    revise_parser.add_argument("--output-root")

    retime_parser = subparsers.add_parser(
        "retime-pauses", help="仅修改停顿时长并复用基准运行的全部 batch"
    )
    configure_start_parser(retime_parser, allow_backend=False)
    retime_parser.add_argument("--base-run-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "locate":
            result = locate_text(Path(args.root).resolve(), args.run_id, args.query_file)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "matched" else 1
        if args.command == "verify":
            root = Path(args.root).resolve()
            if args.run_id:
                state = state_store(root, args.run_id).load()
                output_dir = Path(state["output_dir"])
            else:
                output_dir = Path(args.output_dir).resolve()
                state = state_store(root, output_dir.parent.name).load()
                if output_dir != Path(state["output_dir"]).resolve():
                    raise TTSVerificationError("输出目录与运行状态不一致")
            if state.get("upstream_handoff"):
                verify_handoff(root, state["upstream_handoff"])
            if state.get("status") != "completed":
                raise TTSVerificationError("运行尚未完成")
            result = verify_output(output_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "passed" else 1
        root = Path(args.root).resolve()
        if args.command in {"start", "run", "revise", "retime-pauses"}:
            if args.command == "revise":
                state, store = initialize_revision(args, load_config())
            elif args.command == "retime-pauses":
                state, store = initialize_pause_retime(args, load_config())
            else:
                state, store = initialize_run(args, load_config())
            with store.lock():
                state = advance_state_machine(state, store)
        else:
            store = state_store(root, args.run_id)
            if args.command == "status":
                print(json.dumps(compact_receipt(store.load()), ensure_ascii=False, indent=2))
                return 0
            with store.lock():
                state = store.load()
                if args.command == "supersede":
                    state = store.supersede(state, reason=args.reason)
                elif state["status"] == "completed":
                    pass
                elif state["status"] in {"superseded", "failed_terminal"}:
                    raise WorkflowStateError(f"终态运行不能恢复：{state['status']}")
                else:
                    if args.command == "approve-preview":
                        state = approve_preview(
                            state,
                            store,
                            preview_sha256=args.preview_sha256,
                            confirmed_by=args.confirmed_by,
                        )
                        state = advance_state_machine(state, store)
                        receipt = compact_receipt(state)
                        print(json.dumps(receipt, ensure_ascii=False, indent=2))
                        return 1 if state["status"] == "failed_terminal" else 0
                    if args.command == "retry-preview":
                        state = retry_preview(state, store, args)
                        state = advance_state_machine(state, store)
                        receipt = compact_receipt(state)
                        print(json.dumps(receipt, ensure_ascii=False, indent=2))
                        return 1 if state["status"] == "failed_terminal" else 0
                    if state["status"] == "paused_preview_approval":
                        raise WorkflowStateError(
                            "试听确认不能通过 resume 越过；请使用 approve-preview 或 retry-preview"
                        )
                    if str(state["status"]).startswith("paused_"):
                        if state["status"] == "paused_input_mismatch" and (
                            getattr(args, "text", None) is not None
                            or getattr(args, "input_file", None)
                        ):
                            request_path = Path(state["request_path"])
                            request = read_json(request_path)
                            request["input_file"] = (
                                str(Path(args.input_file).resolve()) if args.input_file else None
                            )
                            request["text"] = args.text if not args.input_file else None
                            _candidate_text(args)
                            write_json(request_path, request)
                            store.checkpoint(
                                state,
                                event="input_correction_received",
                                details={"input_kind": "file" if args.input_file else "text"},
                            )
                        store.resume(state)
                    state = advance_state_machine(state, store)
        receipt = compact_receipt(state)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 1 if state["status"] == "failed_terminal" else 0
    except Exception as exc:
        print(json.dumps(structured_error_receipt(exc), ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
