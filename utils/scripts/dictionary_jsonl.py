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
    from utils.scripts.dictionary_records import recover
    recover(directory)
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
    from utils.scripts.dictionary_records import EntryCollection
    if isinstance(legacy_directory, EntryCollection):
        ensure_buckets(directory)
        read_all(directory, validate)
        return 0
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
    from utils.scripts.dictionary_records import EntryCollection, entries, commit_records
    if isinstance(working_directory, EntryCollection):
        ensure_buckets(directory)
        read_all(directory, validate)
        return {"jsonl": 0, "working": 0}
    ensure_buckets(directory)
    published = read_all(directory, validate)
    updates = {}
    sources = list(sorted(working_directory.glob("*.json")))
    for path in sources:
        entry = json.loads(path.read_text(encoding="utf-8"))
        if validate:
            validate(entry)
        if path.stem != entry.get("wordId"):
            raise ValueError(f"词条文件名与 wordId 不符：{path.name}")
        prior = published.get(entry["wordId"])
        if prior is not None and prior["revision"] == entry["revision"] and prior != entry:
            raise ValueError(f"词条 JSONL 与工作文件修订冲突：{entry['wordId']}")
        if prior is None or entry["revision"] > prior["revision"]:
            updates[entries(directory.parents[2]) / path.name] = render(entry)
    # Existing working data is a migration input only. No working copy is rebuilt.
    if updates:
        commit_records(updates)
    verified = read_all(directory, validate)
    for path in sources:
        old = json.loads(path.read_text(encoding="utf-8"))
        if verified[old["wordId"]]["revision"] < old["revision"]:
            raise ValueError("词库迁移验证失败")
    for path in sources:
        path.unlink()
    if working_directory.is_dir() and not any(working_directory.iterdir()):
        working_directory.rmdir()
    return {"jsonl": len(updates), "working": 0}
