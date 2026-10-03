"""Shared UTF-8 text input helpers for project CLIs."""
from __future__ import annotations

from pathlib import Path


class TextInputError(ValueError):
    pass


def read_text_file(path: Path) -> str:
    resolved = path.expanduser().resolve()
    if resolved.suffix.lower() not in {".txt", ".md"}:
        raise TextInputError("文本文件只支持 .txt 或 .md")
    try:
        return resolved.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise TextInputError(f"文本文件不存在：{resolved}") from exc
    except (OSError, UnicodeError) as exc:
        raise TextInputError(f"无法读取文本文件：{resolved}：{exc}") from exc


def normalize_input_text(raw: str) -> str:
    return raw.replace("\r\n", "\n").replace("\r", "\n").strip()


def resolve_text(text: str | None, text_file: Path | None) -> str:
    raw = read_text_file(text_file) if text_file else str(text or "")
    normalized = normalize_input_text(raw)
    if not normalized:
        raise TextInputError("文本不能为空")
    return normalized
