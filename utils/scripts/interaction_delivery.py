"""Shared helpers for deterministic, schema-backed conversation deliveries."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def source_hash(value: Any) -> str:
    """Return a stable hash for the machine result source used by a delivery."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def render_preview_delivery(*, prompt: str, question: str, run_id: str, source: Any) -> dict[str, Any]:
    """Build the one-code-block preview contract used by conversation adapters."""
    if not prompt.strip():
        raise ValueError("预览提示词不能为空")
    if not question.strip():
        raise ValueError("确认问题不能为空")
    prompt_code_block = f"```text\n{prompt.rstrip()}\n```"
    message = f"{prompt_code_block}\n\n{question.strip()}"
    return {
        "schema_version": "1.0",
        "mode": "preview",
        "run_id": run_id,
        "source_result_sha256": source_hash(source),
        "message_markdown": message,
        "prompt_code_block": prompt_code_block,
        "confirmation_question": question.strip(),
        "next_state": "paused_imagen_confirmation",
    }


def validate_preview_delivery(delivery: dict[str, Any], source: Any) -> None:
    """Validate ordering and provenance before a delivery is shown to a user."""
    required = ("message_markdown", "prompt_code_block", "confirmation_question", "source_result_sha256")
    missing = [key for key in required if not isinstance(delivery.get(key), str) or not delivery[key].strip()]
    if missing:
        raise ValueError(f"预览交付载荷缺少字段：{', '.join(missing)}")
    if delivery["source_result_sha256"] != source_hash(source):
        raise ValueError("预览交付载荷与当前 result.json 不一致")
    message = delivery["message_markdown"]
    code = delivery["prompt_code_block"]
    question = delivery["confirmation_question"]
    if message.count("```text") != 1 or code not in message:
        raise ValueError("预览交付必须包含且只能包含一个 text 代码块")
    if message.index(code) > message.index(question):
        raise ValueError("提示词代码块必须位于确认问题之前")

