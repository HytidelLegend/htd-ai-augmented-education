"""Reusable Volcengine async file ASR, hotword, and timestamp helpers."""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class VolcengineAsrError(ValueError):
    """Raised when an ASR request or response cannot be used safely."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


Transport = Callable[[str, bytes, dict[str, str], int], tuple[dict[str, Any], dict[str, str]]]

PUNCTUATION_RE = re.compile(r"[^\w\u4e00-\u9fff\s]", re.UNICODE)


@dataclass(frozen=True)
class Hotword:
    word: str
    weight: int = 4


def read_env_value(path: Path, key: str) -> str | None:
    """Read one key from a UTF-8 dotenv file without mutating process state."""
    if not path.is_file():
        return None
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip().strip('"').strip("'") or None
    return None


def parse_hotwords(path: Path) -> list[Hotword]:
    """Parse a UTF-8 direct-context hotword list with optional table weights."""
    if path.suffix.lower() != ".txt" or not path.is_file():
        raise VolcengineAsrError("热词表必须是实际存在的 UTF-8 .txt 文件")
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise VolcengineAsrError("热词表必须使用 UTF-8 编码") from exc
    values: list[Hotword] = []
    seen: dict[str, int] = {}
    for line_number, raw in enumerate(lines, 1):
        value = raw.strip()
        if not value:
            continue
        parts = value.rsplit("|", 1)
        word = parts[0].strip()
        try:
            weight = int(parts[1]) if len(parts) == 2 else 4
        except ValueError as exc:
            raise VolcengineAsrError(f"热词第 {line_number} 行权重必须是 1～10 的整数") from exc
        if not word:
            raise VolcengineAsrError(f"热词第 {line_number} 行不能为空")
        if PUNCTUATION_RE.search(word):
            raise VolcengineAsrError(f"热词第 {line_number} 行包含不允许的标点")
        if not 1 <= weight <= 10:
            raise VolcengineAsrError(f"热词第 {line_number} 行权重必须位于 1～10")
        if word in seen:
            if seen[word] != weight:
                raise VolcengineAsrError(f"重复热词的权重不一致：{word}")
            continue
        seen[word] = weight
        values.append(Hotword(word, weight))
    if not values:
        raise VolcengineAsrError("热词表不能为空")
    if len(values) > 5000:
        raise VolcengineAsrError("单个热词表不能超过 5000 项")
    return values


def urllib_transport(
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout_seconds: int,
) -> tuple[dict[str, Any], dict[str, str]]:
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
            response_headers = {key.lower(): value for key, value in response.headers.items()}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise VolcengineAsrError(
            f"火山 ASR HTTP {exc.code}：{detail}",
            retryable=exc.code == 429 or exc.code >= 500,
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise VolcengineAsrError(f"火山 ASR 网络请求失败：{exc}", retryable=True) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VolcengineAsrError("火山 ASR 返回的不是合法 UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise VolcengineAsrError("火山 ASR 响应必须是 JSON 对象")
    return payload, response_headers


def build_submit_request(
    audio_path: Path,
    *,
    uid: str,
    hotwords: list[Hotword] | None = None,
    language: str = "zh-CN",
) -> dict[str, Any]:
    if not audio_path.is_file() or audio_path.stat().st_size == 0:
        raise VolcengineAsrError("ASR 音频不存在或为空")
    request: dict[str, Any] = {
        "model_name": "bigmodel",
        "enable_itn": True,
        "enable_punc": True,
        "show_utterances": True,
    }
    if hotwords:
        request["corpus"] = {
            "context": json.dumps(
                {"hotwords": [{"word": item.word} for item in hotwords]},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        }
    return {
        "user": {"uid": uid},
        "audio": {
            "data": base64.b64encode(audio_path.read_bytes()).decode("ascii"),
            "format": audio_path.suffix.lower().lstrip("."),
            "language": language,
            "rate": 16000,
            "channel": 1,
        },
        "request": request,
    }


def _request_headers(api_key: str, resource_id: str, task_id: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": task_id,
        "X-Api-Sequence": "-1",
    }


def _post_with_retries(
    *,
    endpoint: str,
    body: bytes,
    headers: dict[str, str],
    timeout_seconds: int,
    max_attempts: int,
    transport: Transport,
) -> tuple[dict[str, Any], dict[str, str], int]:
    last_error: VolcengineAsrError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response, response_headers = transport(endpoint, body, headers, timeout_seconds)
            return response, response_headers, attempt
        except VolcengineAsrError as exc:
            last_error = exc
            if not exc.retryable or attempt >= max_attempts:
                break
            time.sleep(min(attempt, 3))
    assert last_error is not None
    raise last_error


def _provider_error(response_headers: dict[str, str]) -> VolcengineAsrError:
    status = str(response_headers.get("x-api-status-code", ""))
    message = response_headers.get("x-api-message", "unknown error")
    return VolcengineAsrError(
        f"火山 ASR 失败：status={status or 'missing'}，message={message}",
        retryable=status.startswith("5"),
    )


def submit_recognition(
    audio_path: Path,
    *,
    api_key: str,
    endpoint: str,
    resource_id: str,
    timeout_seconds: int,
    max_attempts: int,
    hotwords: list[Hotword] | None = None,
    language: str = "zh-CN",
    transport: Transport = urllib_transport,
    request_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Submit one async recognition task and return its durable task ID."""
    if not api_key:
        raise VolcengineAsrError("VOLCENGINE_API_KEY 不能为空")
    request_id = request_id or str(uuid.uuid4())
    payload = build_submit_request(audio_path, uid=request_id, hotwords=hotwords, language=language)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    response, response_headers, attempt = _post_with_retries(
        endpoint=endpoint,
        body=body,
        headers=_request_headers(api_key, resource_id, request_id),
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        transport=transport,
    )
    status = str(response_headers.get("x-api-status-code", ""))
    if status != "20000000":
        raise _provider_error(response_headers)
    returned_task_id = response.get("task_id")
    task_id = returned_task_id if isinstance(returned_task_id, str) and returned_task_id else request_id
    return task_id, {
        "request_id": request_id,
        "task_id": task_id,
        "attempt": attempt,
        "endpoint": endpoint,
        "resource_id": resource_id,
        "status_code": status,
        "message": response_headers.get("x-api-message"),
        "log_id": response_headers.get("x-tt-logid"),
        "hotword_count": len(hotwords or []),
        "language": language,
    }


def query_recognition(
    task_id: str,
    *,
    api_key: str,
    endpoint: str,
    resource_id: str,
    request_timeout_seconds: int,
    poll_interval_seconds: float,
    poll_timeout_seconds: int,
    max_attempts: int,
    transport: Transport = urllib_transport,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Poll one submitted task until the provider returns the final result."""
    if not task_id:
        raise VolcengineAsrError("火山 ASR task_id 不能为空")
    deadline = time.monotonic() + poll_timeout_seconds
    polls = 0
    http_attempts = 0
    last_headers: dict[str, str] = {}
    while True:
        response, response_headers, attempts = _post_with_retries(
            endpoint=endpoint,
            body=b"{}",
            headers=_request_headers(api_key, resource_id, task_id),
            timeout_seconds=request_timeout_seconds,
            max_attempts=max_attempts,
            transport=transport,
        )
        polls += 1
        http_attempts += attempts
        last_headers = response_headers
        status = str(response_headers.get("x-api-status-code", ""))
        if status == "20000000":
            if not isinstance(response.get("result"), dict):
                raise VolcengineAsrError("火山 ASR 查询完成但响应缺少 result")
            return response, {
                "task_id": task_id,
                "polls": polls,
                "http_attempts": http_attempts,
                "endpoint": endpoint,
                "resource_id": resource_id,
                "status_code": status,
                "message": response_headers.get("x-api-message"),
                "log_id": response_headers.get("x-tt-logid"),
            }
        if status not in {"20000001", "20000002"}:
            raise _provider_error(response_headers)
        if time.monotonic() >= deadline:
            message = last_headers.get("x-api-message", "processing")
            raise VolcengineAsrError(f"火山 ASR 查询超时：task_id={task_id}，message={message}")
        sleep(poll_interval_seconds)


def _compact(value: str) -> str:
    return "".join(value.split())


def _valid_timed_word(value: Any) -> bool:
    if not isinstance(value, dict) or not _compact(str(value.get("text", ""))):
        return False
    try:
        start_ms = int(value.get("start_time", -1))
        end_ms = int(value.get("end_time", -1))
    except (TypeError, ValueError):
        return False
    return start_ms >= 0 and end_ms > start_ms


def _attach_sentence_text(
    sentence_text: str,
    words: list[dict[str, Any]],
    *,
    sentence_index: int,
) -> list[dict[str, Any]]:
    """Attach punctuation omitted from word entries without inventing timestamps."""
    compact = _compact(sentence_text)
    if not compact or not words:
        raise VolcengineAsrError(f"utterances[{sentence_index}] 缺少文本或词级时间戳")
    cursor = 0
    normalized: list[dict[str, Any]] = []
    for word_index, raw in enumerate(words):
        word = _compact(str(raw.get("text", "")))
        if not word:
            raise VolcengineAsrError(f"utterances[{sentence_index}].words[{word_index}] 文本为空")
        found = compact.find(word, cursor)
        if found < 0:
            raise VolcengineAsrError(f"utterances[{sentence_index}] 的句子与词级文本无法对齐")
        prefix = compact[cursor:found]
        if prefix:
            if normalized:
                normalized[-1]["text"] += prefix
            else:
                word = prefix + word
        try:
            start_ms, end_ms = int(raw["start_time"]), int(raw["end_time"])
        except (KeyError, TypeError, ValueError) as exc:
            raise VolcengineAsrError("火山 ASR 词级时间字段无效") from exc
        normalized.append(
            {
                "text": word,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "sentence_index": sentence_index + 1,
            }
        )
        cursor = found + len(_compact(str(raw.get("text", ""))))
    trailing = compact[cursor:]
    if trailing:
        normalized[-1]["text"] += trailing
    if "".join(item["text"] for item in normalized) != compact:
        raise VolcengineAsrError(f"utterances[{sentence_index}] 归一化后无法重建句子")
    return normalized


def normalize_asr_response(response: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Convert async ASR output to the caption pipeline's timestamp schema 1.1."""
    result = response.get("result")
    if not isinstance(result, dict):
        raise VolcengineAsrError("火山 ASR 响应缺少 result")
    utterances = result.get("utterances")
    if not isinstance(utterances, list) or not utterances:
        raise VolcengineAsrError("火山 ASR 未返回非空 utterances")
    all_items: list[dict[str, Any]] = []
    sentences: list[dict[str, Any]] = []
    previous_end = 0
    transcript_parts: list[str] = []
    for sentence_index, utterance in enumerate(utterances):
        if not isinstance(utterance, dict):
            raise VolcengineAsrError(f"utterances[{sentence_index}] 必须是对象")
        sentence_text = str(utterance.get("text", ""))
        words = utterance.get("words")
        if not isinstance(words, list):
            raise VolcengineAsrError(f"utterances[{sentence_index}] 缺少 words")
        words = [word for word in words if _valid_timed_word(word)]
        normalized = _attach_sentence_text(sentence_text, words, sentence_index=sentence_index)
        for item in normalized:
            if item["start_ms"] < previous_end or item["end_ms"] <= item["start_ms"]:
                raise VolcengineAsrError("火山 ASR 词级时间戳不单调、重叠或非正区间")
            previous_end = item["end_ms"]
        transcript_parts.append(sentence_text.strip())
        all_items.extend(normalized)
        sentences.append(
            {
                "text": "".join(item["text"] for item in normalized),
                "start_ms": normalized[0]["start_ms"],
                "end_ms": normalized[-1]["end_ms"],
                "item_range": {"start": len(all_items) - len(normalized), "end": len(all_items)},
            }
        )
    duration_value = response.get("audio_info", {}).get("duration")
    if duration_value is None:
        duration_value = result.get("additions", {}).get("duration")
    try:
        duration_ms = int(duration_value)
    except (TypeError, ValueError) as exc:
        raise VolcengineAsrError("火山 ASR 响应缺少合法 audio_info.duration") from exc
    if duration_ms < previous_end:
        raise VolcengineAsrError("火山 ASR 词级时间戳越过音频时长")
    transcript = "\n".join(part for part in transcript_parts if part)
    if _compact(transcript) != _compact("".join(item["text"] for item in all_items)):
        raise VolcengineAsrError("ASR 逐字稿与词级 items 无法精确对齐")
    timestamps = {
        "schema_version": "1.1",
        "time_unit": "ms",
        "interval_semantics": "[start_ms, end_ms)",
        "audio_file": "asr-audio.mp3",
        "duration_ms": duration_ms,
        "items": all_items,
        "sentences": sentences,
        "source": {"provider": "volcengine", "mode": "async", "model": "seedasr-2.0"},
    }
    return transcript, timestamps


# Backward-compatible import for old callers; new code should use normalize_asr_response.
normalize_flash_response = normalize_asr_response
