"""Shared helpers for archiving external JSON inputs inside a run directory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def archive_json_input(source: Path, destination: Path, payload: Any) -> Path:
    """Write a normalized input snapshot below the current run directory."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return destination
