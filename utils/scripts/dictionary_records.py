"""Logical entry records backed exclusively by the letter-sharded JSONL library.

Record handles support the small read/write interface used by resumable builders;
they are not filesystem paths and never create per-word files.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from functools import lru_cache

from utils.scripts.file_transaction import project_lock
from utils.scripts.timestamp import iso_timestamp


class EntryCollection:
    def __init__(self, root: Path):
        self.root = root
        self.directory = root / "outputs" / "词汇星图" / "dicts"

    def __truediv__(self, name: str):
        return EntryRecord(self, name.removesuffix(".json"))

    def glob(self, pattern: str):
        if pattern != "*.json":
            raise ValueError("词库仅支持遍历全部记录")
        return [EntryRecord(self, wid) for wid in sorted(self.values())]

    def values(self):
        recover(self.directory)
        signature = tuple((path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in sorted(self.directory.glob("?.jsonl")))
        return _read_values(self.directory, signature)

    def mkdir(self, **kwargs):
        from utils.scripts.dictionary_jsonl import ensure_buckets
        ensure_buckets(self.directory)


class EntryRecord:
    def __init__(self, collection: EntryCollection, word_id: str):
        self.parent = collection
        self.stem = word_id
        self.name = word_id + ".json"

    def __hash__(self):
        return hash((self.parent.directory, self.stem))

    def __eq__(self, other):
        return isinstance(other, EntryRecord) and self.parent.directory == other.parent.directory and self.stem == other.stem

    def __lt__(self, other):
        return str(self) < str(other)

    def __str__(self):
        return f"{self.parent.directory}#{self.stem}"

    def _value(self):
        values = self.parent.values()
        if self.stem not in values:
            raise FileNotFoundError(str(self))
        return values[self.stem]

    def exists(self):
        try:
            self._value()
            return True
        except FileNotFoundError:
            return False

    is_file = exists

    def read_text(self, **kwargs):
        # Keep the existing builder's digest format stable when dropping files.
        value = self._value()
        text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        import re
        return re.sub(r'("confidence": )(-?\d+(?:\.\d+)?)',
                      lambda match: match.group(1) + f"{float(match.group(2)):.2f}", text)

    def read_bytes(self):
        return self.read_text().encode("utf-8")

    def write_text(self, text, **kwargs):
        commit_records({self: text})
        return len(text)


@lru_cache(maxsize=4)
def _read_values(directory: Path, signature: tuple) -> dict:
    from utils.scripts.dictionary_jsonl import read_all
    return read_all(directory)


def entries(root: Path) -> EntryCollection:
    return EntryCollection(root)


def _journal(directory: Path) -> Path:
    return directory.parents[2] / "logs" / "dictionary-jsonl" / "pending.json"


def recover(directory: Path) -> None:
    """Finish an interrupted multi-bucket commit; never overwrite foreign edits."""
    journal = _journal(directory)
    if not journal.exists():
        return
    from utils.scripts.structured_io import write_text_transaction
    with project_lock(journal.with_suffix(".lock"), "dictionary-jsonl:recover"):
        if not journal.exists():
            return
        receipt = json.loads(journal.read_text(encoding="utf-8"))
        updates = {}
        for name, item in receipt["buckets"].items():
            if name not in {f"{letter}.jsonl" for letter in "abcdefghijklmnopqrstuvwxyz"}:
                raise ValueError("词库事务分桶无效")
            path = directory / name
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            digest = hashlib.sha256(current.encode()).hexdigest()
            if digest not in (item["before"], hashlib.sha256(item["text"].encode()).hexdigest()):
                raise ValueError("词库事务恢复冲突")
            updates[path] = item["text"]
        write_text_transaction(updates)
        journal.unlink()


def commit_records(updates: dict) -> None:
    from utils.scripts.dictionary_jsonl import bucket, read_bucket, render_entry_line
    from utils.scripts.structured_io import write_text_atomic, write_text_transaction
    records = {key: value for key, value in updates.items() if isinstance(key, EntryRecord)}
    roots = {key.parent.directory for key in records}
    if len(roots) != 1:
        raise ValueError("一次词库事务必须属于同一词库")
    directory = next(iter(roots))
    recover(directory)
    journal = _journal(directory)
    with project_lock(journal.with_suffix(".lock"), "dictionary-jsonl:commit"):
        buckets = {}
        for record, text in records.items():
            value = json.loads(text)
            if value["wordId"] != record.stem:
                raise ValueError("词库记录 ID 不一致")
            name = bucket(value["lemma"]) + ".jsonl"
            if name not in buckets:
                buckets[name] = read_bucket(directory / name)
            buckets[name][record.stem] = value
        physical = {key: value for key, value in updates.items() if not isinstance(key, EntryRecord)}
        receipt = {"status": "committing", "updatedAt": iso_timestamp(), "buckets": {}}
        for name, values in buckets.items():
            path = directory / name
            text = "".join(render_entry_line(value) + "\n" for value in sorted(values.values(), key=lambda item: (item["lemma"].casefold(), item["wordId"])))
            before = path.read_text(encoding="utf-8") if path.exists() else ""
            receipt["buckets"][name] = {"before": hashlib.sha256(before.encode()).hexdigest(), "text": text}
            physical[path] = text
        write_text_atomic(journal, json.dumps(receipt, ensure_ascii=False))
        write_text_transaction(physical)
        journal.unlink()
