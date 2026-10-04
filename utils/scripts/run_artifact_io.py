"""Shared helpers for archiving external JSON inputs inside a run directory."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_text_atomic(destination: Path, text: str) -> None:
    """Publish a checkpoint without exposing a partially written file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".checkpoint-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def archive_json_input(source: Path, destination: Path, payload: Any) -> Path:
    """Write a normalized input snapshot below the current run directory."""
    write_text_atomic(destination, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return destination
