"""Shared UTF-8/LF readers and writers for structured project data."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator


def structured_error_receipt(exc: Exception, *, failure_run_id: str = "") -> dict[str, str]:
    """Return a stable machine-readable error receipt while preserving a human message."""
    if isinstance(exc, json.JSONDecodeError):
        error_code = "json_decode_error"
    elif isinstance(exc, UnicodeError):
        error_code = "unicode_error"
    elif isinstance(exc, OSError):
        error_code = "io_error"
    elif isinstance(exc, KeyError):
        error_code = "missing_key"
    elif isinstance(exc, ValueError):
        error_code = "validation_error"
    else:
        error_code = "runtime_error"
    return {
        "status": "error",
        "error_code": error_code,
        "error": str(exc),
        "failure_run_id": failure_run_id,
    }


def read_json(path: Path) -> Any:
    """Read JSON while accepting an optional UTF-8 BOM from imported files."""
    return json.loads(path.read_text(encoding="utf-8-sig"))


def validate_json_schema(value: Any, schema_path: Path) -> None:
    """Validate structured data and render all schema errors in one message."""
    schema = read_json(schema_path)
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda item: list(item.absolute_path))
    if errors:
        rendered = []
        for error in errors:
            location = "/".join(str(item) for item in error.absolute_path) or "$"
            rendered.append(f"{location}: {error.message}")
        raise ValueError("Schema 校验失败：" + "；".join(rendered))


def write_json(path: Path, value: Any) -> None:
    """Write deterministic human-readable JSON as UTF-8 without BOM and LF."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_text_atomic(path: Path, text: str) -> None:
    """Atomically replace one UTF-8/LF text file."""
    write_text_transaction({path: text})


def write_text_transaction(updates: Mapping[Path, str]) -> None:
    """Atomically replace multiple UTF-8/LF text files with rollback on failure."""
    entries = [(Path(path), str(value).replace("\r\n", "\n").replace("\r", "\n")) for path, value in updates.items()]
    if not entries:
        return
    if len({path.resolve() for path, _ in entries}) != len(entries):
        raise ValueError("文本事务目标路径不得重复")

    originals: dict[Path, bytes | None] = {}
    staged: dict[Path, Path] = {}
    replaced: list[Path] = []

    def stage_bytes(target: Path, content: bytes) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_temp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temp_path = Path(raw_temp)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        return temp_path

    try:
        for target, text in entries:
            originals[target] = target.read_bytes() if target.exists() else None
            staged[target] = stage_bytes(target, text.encode("utf-8"))
        for target, _ in entries:
            os.replace(staged[target], target)
            replaced.append(target)
    except Exception as write_error:
        rollback_errors: list[str] = []
        for target in reversed(replaced):
            try:
                original = originals[target]
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    os.replace(stage_bytes(target, original), target)
            except Exception as rollback_error:
                rollback_errors.append(f"{target}: {rollback_error}")
        if rollback_errors:
            raise RuntimeError(
                f"文本事务写入失败：{write_error}；回滚同时失败：{'；'.join(rollback_errors)}"
            ) from write_error
        raise
    finally:
        for temp_path in staged.values():
            temp_path.unlink(missing_ok=True)


def write_json_transaction(updates: Mapping[Path, Any]) -> None:
    """Replace multiple JSON files together and restore originals if any replace fails."""
    entries = [(Path(path), value) for path, value in updates.items()]
    if not entries:
        return
    if len({path.resolve() for path, _ in entries}) != len(entries):
        raise ValueError("JSON 事务目标路径不得重复")

    originals: dict[Path, bytes | None] = {}
    staged: dict[Path, Path] = {}
    replaced: list[Path] = []

    def stage_bytes(target: Path, content: bytes) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_temp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temp_path = Path(raw_temp)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        return temp_path

    try:
        for target, value in entries:
            originals[target] = target.read_bytes() if target.exists() else None
            rendered = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            staged[target] = stage_bytes(target, rendered)
        for target, _ in entries:
            os.replace(staged[target], target)
            replaced.append(target)
    except Exception as write_error:
        rollback_errors: list[str] = []
        for target in reversed(replaced):
            try:
                original = originals[target]
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    os.replace(stage_bytes(target, original), target)
            except Exception as rollback_error:
                rollback_errors.append(f"{target}: {rollback_error}")
        if rollback_errors:
            raise RuntimeError(
                f"JSON 事务写入失败：{write_error}；回滚同时失败：{'；'.join(rollback_errors)}"
            ) from write_error
        raise
    finally:
        for temp_path in staged.values():
            temp_path.unlink(missing_ok=True)
