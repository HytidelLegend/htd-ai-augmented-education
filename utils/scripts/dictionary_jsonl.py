"""Atomic, letter-sharded dictionary entry storage shared by skill and app."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Callable

from utils.scripts.structured_io import write_text_atomic


def render_entry_line(entry: dict) -> str:
    raw = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    return re.sub(r'("confidence":)(-?\d+(?:\.\d+)?)',
                  lambda match: match.group(1) + f"{float(match.group(2)):.2f}", raw)


def bucket(lemma: str) -> str:
    normalized = unicodedata.normalize("NFKC", lemma).strip().casefold()
    match = re.search(r"[a-z]", normalized)
    if not match:
        raise ValueError(f"词条没有英文字母：{lemma}")
    return match.group(0)


def ensure_buckets(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for letter in "abcdefghijklmnopqrstuvwxyz":
        path = directory / f"{letter}.jsonl"
        if not path.exists():
            write_text_atomic(path, "")


def read_bucket(path: Path, validate: Callable[[dict], None] | None = None) -> dict[str, dict]:
    result: dict[str, dict] = {}
    if not path.exists():
        return result
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            raise ValueError(f"JSONL 空行：{path.name}:{number}")
        if any(not re.fullmatch(r"(?:0|1)\.\d{2}", value.strip()) for value in
               re.findall(r'"confidence"\s*:\s*([^,}]+)', line)):
            raise ValueError(f"置信度须保留两位小数：{path.name}:{number}")
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSONL 格式错误：{path.name}:{number}: {exc}") from exc
        if not isinstance(entry, dict) or not isinstance(entry.get("wordId"), str) or not isinstance(entry.get("lemma"), str):
            raise ValueError(f"词条字段无效：{path.name}:{number}")
        if bucket(entry["lemma"]) != path.stem:
            raise ValueError(f"词条字母分桶错误：{path.name}:{number}")
        if entry["wordId"] in result:
            raise ValueError(f"重复 wordId：{path.name}:{number}")
        if validate:
            validate(entry)
        result[entry["wordId"]] = entry
    return result


def read_all(directory: Path, validate: Callable[[dict], None] | None = None) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    for letter in "abcdefghijklmnopqrstuvwxyz":
        for word_id, entry in read_bucket(directory / f"{letter}.jsonl", validate).items():
            if word_id in entries:
                raise ValueError(f"跨分桶重复 wordId：{word_id}")
            entries[word_id] = entry
    return entries


def upsert(directory: Path, entry: dict, render: Callable[[dict], str],
           validate: Callable[[dict], None] | None = None) -> Path:
    letter = bucket(entry["lemma"])
    path = directory / f"{letter}.jsonl"
    current = read_bucket(path, validate)
    current[entry["wordId"]] = entry
    lines = [render(value).strip() for value in sorted(current.values(), key=lambda value: (value["lemma"].casefold(), value["wordId"]))]
    if any("\n" in line for line in lines):
        raise ValueError("词条序列化必须每条占一行")
    write_text_atomic(path, "\n".join(lines) + ("\n" if lines else ""))
    return path


def sync_entry_files(legacy_directory: Path, directory: Path,
                     validate: Callable[[dict], None] | None = None) -> int:
    """Publish verified legacy working files to JSONL without overwriting newer rows."""
    ensure_buckets(directory)
    current = read_all(directory, validate)
    changed = 0
    for path in sorted(legacy_directory.glob("*.json")):
        entry = json.loads(path.read_text(encoding="utf-8"))
        if validate:
            validate(entry)
        prior = current.get(entry["wordId"])
        if prior == entry:
            continue
        if prior is not None and (prior["revision"] > entry["revision"] or
                                  prior["revision"] == entry["revision"]):
            raise ValueError(f"词条 JSONL 与工作文件修订冲突：{entry['wordId']}")
        upsert(directory, entry, render_entry_line, validate)
        current[entry["wordId"]] = entry
        changed += 1
    return changed


def reconcile_entry_files(working_directory: Path, directory: Path,
                          validate: Callable[[dict], None] | None = None,
                          render: Callable[[dict], str] = render_entry_line) -> dict[str, int]:
    """Reconcile compatible working files with the published JSONL revisions.

    A missing or older copy is refreshed from the newer revision. Equal-revision
    differences are always a conflict, so neither side silently wins.
    """
    ensure_buckets(directory)
    published = read_all(directory, validate)
    working_directory.mkdir(parents=True, exist_ok=True)
    written_jsonl = written_working = 0
    working: dict[str, dict] = {}
    newer_working: list[dict] = []
    for path in sorted(working_directory.glob("*.json")):
        entry = json.loads(path.read_text(encoding="utf-8"))
        if validate:
            validate(entry)
        if path.stem != entry.get("wordId"):
            raise ValueError(f"词条文件名与 wordId 不符：{path.name}")
        working[entry["wordId"]] = entry
        prior = published.get(entry["wordId"])
        if prior is None or entry["revision"] > prior["revision"]:
            newer_working.append(entry)
        elif entry["revision"] == prior["revision"] and entry != prior:
            raise ValueError(f"词条 JSONL 与工作文件修订冲突：{entry['wordId']}")
    for entry in newer_working:
        upsert(directory, entry, render, validate)
        published[entry["wordId"]] = entry
        written_jsonl += 1
    for word_id, entry in published.items():
        old = working.get(word_id)
        if old is None or old["revision"] < entry["revision"]:
            write_text_atomic(working_directory / f"{word_id}.json", render(entry).strip() + "\n")
            written_working += 1
    return {"jsonl": written_jsonl, "working": written_working}
