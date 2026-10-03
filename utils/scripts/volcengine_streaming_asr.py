"""Volcengine bidirectional streaming ASR protocol helpers."""

from __future__ import annotations

import asyncio
import gzip
import json
import struct
import uuid
from pathlib import Path
from typing import Any

from utils.scripts.volcengine_asr import Hotword, VolcengineAsrError


def _header(message_type: int, flags: int, serialization: int, compression: int) -> bytes:
    return bytes([(1 << 4) | 1, (message_type << 4) | flags, (serialization << 4) | compression, 0])


def build_full_request(payload: dict[str, Any]) -> bytes:
    body = gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return _header(1, 0, 1, 1) + struct.pack(">I", len(body)) + body


def build_audio_request(audio: bytes, *, is_last: bool) -> bytes:
    body = gzip.compress(audio)
    flags = 2 if is_last else 0
    return _header(2, flags, 0, 1) + struct.pack(">I", len(body)) + body


def parse_response(raw: bytes) -> dict[str, Any]:
    if len(raw) < 8:
        raise VolcengineAsrError("流式 ASR 响应过短")
    header_size = (raw[0] & 0x0F) * 4
    message_type = raw[1] >> 4
    flags = raw[1] & 0x0F
    compression = raw[2] & 0x0F
    body = raw[header_size:]
    if message_type == 15:
        if len(body) < 8:
            raise VolcengineAsrError("流式 ASR 错误响应格式无效")
        code, size = struct.unpack(">II", body[:8])
        message = body[8 : 8 + size].decode("utf-8", errors="replace")
        raise VolcengineAsrError(f"流式 ASR 服务端错误：code={code}，message={message}")
    if message_type != 9:
        return {"type": "unknown", "message_type": message_type}
    sequence = None
    if flags in (1, 3):
        if len(body) < 4:
            raise VolcengineAsrError("流式 ASR 响应缺少序号")
        sequence = struct.unpack(">i", body[:4])[0]
        body = body[4:]
    if len(body) < 4:
        raise VolcengineAsrError("流式 ASR 响应缺少 payload 长度")
    size = struct.unpack(">I", body[:4])[0]
    payload = body[4 : 4 + size]
    if compression == 1:
        payload = gzip.decompress(payload)
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VolcengineAsrError("流式 ASR payload 不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise VolcengineAsrError("流式 ASR payload 必须是对象")
    return {"type": "result", "sequence": sequence, "data": data}


def _request_payload(*, uid: str, language: str, hotwords: list[Hotword], enable_nonstream: bool, result_type: str) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model_name": "bigmodel",
        "enable_itn": True,
        "enable_punc": True,
        "enable_ddc": True,
        "enable_nonstream": enable_nonstream,
        "show_utterances": True,
        "result_type": result_type,
    }
    if hotwords:
        request["corpus"] = {"context": json.dumps({"hotwords": [{"word": item.word} for item in hotwords]}, ensure_ascii=False, separators=(",", ":"))}
    return {
        "user": {"uid": uid},
        "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
        "request": request,
    }


async def _recognize_async(audio_path: Path, *, api_key: str, endpoint: str, resource_id: str, timeout_seconds: int, chunk_duration_ms: int, send_interval_seconds: float, hotwords: list[Hotword], language: str, enable_nonstream: bool, result_type: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        import websockets
    except ImportError as exc:
        raise VolcengineAsrError("缺少 websockets 依赖，请在目录包根目录安装 requirements.txt") from exc
    request_id = str(uuid.uuid4())
    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": request_id,
        "X-Api-Connect-Id": str(uuid.uuid4()),
        "X-Api-Sequence": "-1",
    }
    pcm = audio_path.read_bytes()
    # 16-bit PCM frames are two bytes wide.  Some ffmpeg conversions can
    # leave a single trailing byte; the ASR service rejects such a payload
    # instead of ignoring the incomplete frame.
    if len(pcm) % 2:
        pcm = pcm[:-1]
    chunk_size = max(1, int(16000 * 2 * chunk_duration_ms / 1000))
    chunks = [pcm[i : i + chunk_size] for i in range(0, len(pcm), chunk_size)]
    if not chunks:
        raise VolcengineAsrError("流式 ASR 音频为空")
    events: list[dict[str, Any]] = []
    utterances: list[dict[str, Any]] = []
    duration = 0
    seen: set[tuple[Any, ...]] = set()

    def collect(event: dict[str, Any]) -> None:
        nonlocal duration
        data = event.get("data", {})
        payload = data.get("payload_msg") if isinstance(data, dict) and isinstance(data.get("payload_msg"), dict) else data
        info = payload.get("audio_info", {}) if isinstance(payload, dict) else {}
        duration = max(duration, int(info.get("duration", 0) or 0))
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        raw_utterances = result.get("utterances", []) if isinstance(result, dict) else []
        for utterance in raw_utterances if isinstance(raw_utterances, list) else []:
            if not isinstance(utterance, dict) or utterance.get("definite") is False:
                continue
            key = (utterance.get("text"), utterance.get("start_time"), utterance.get("end_time"))
            if key not in seen:
                seen.add(key)
                utterances.append(utterance)
        # Server events can repeat the full transcript. Keep only diagnostics.
        events.append({"type": event.get("type"), "sequence": event.get("sequence"), "utterance_count": len(utterances)})
    try:
        async with websockets.connect(
            endpoint, additional_headers=headers, open_timeout=timeout_seconds,
            max_size=10 * 1024 * 1024, ping_interval=None,
        ) as ws:
            await ws.send(build_full_request(_request_payload(uid=request_id, language=language, hotwords=hotwords, enable_nonstream=enable_nonstream, result_type=result_type)))
            initial = parse_response(await asyncio.wait_for(ws.recv(), timeout=timeout_seconds))
            collect(initial)

            async def send_audio() -> None:
                for index, chunk in enumerate(chunks):
                    await ws.send(build_audio_request(chunk, is_last=index == len(chunks) - 1))
                    if index + 1 < len(chunks):
                        await asyncio.sleep(send_interval_seconds)

            async def receive_audio() -> None:
                while True:
                    try:
                        raw_event = await asyncio.wait_for(ws.recv(), timeout=timeout_seconds)
                    except Exception as exc:
                        # The service may close cleanly immediately after the
                        # last response.  Treat that as end-of-stream; any
                        # protocol or timeout error still propagates.
                        if exc.__class__.__name__ == "ConnectionClosedOK":
                            return
                        raise
                    event = parse_response(raw_event)
                    collect(event)
                    data = event.get("data", {})
                    if isinstance(data, dict) and data.get("is_last_package") is True:
                        return

            await asyncio.gather(send_audio(), receive_audio())
    except VolcengineAsrError:
        raise
    except Exception as exc:
        raise VolcengineAsrError(f"流式 ASR WebSocket 失败：{exc}", retryable=True) from exc
    if not utterances:
        raise VolcengineAsrError("流式 ASR 未返回最终 utterances")
    return {"audio_info": {"duration": duration}, "result": {"utterances": utterances}}, events


def recognize_file(audio_path: Path, **kwargs: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return asyncio.run(_recognize_async(audio_path, **kwargs))
