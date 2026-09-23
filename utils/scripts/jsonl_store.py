"""Append-only JSONL event storage with deterministic UTF-8 output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping


def append_jsonl(path: Path, rows: Iterable[Mapping[str, object]], *, key_fields: tuple[str, ...] = ()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[tuple[object, ...]] = set()
    if path.is_file() and key_fields:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                existing.add(tuple(item.get(key) for key in key_fields))
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            key = tuple(row.get(field) for field in key_fields)
            if key_fields and key in existing:
                continue
            stream.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
            existing.add(key)
