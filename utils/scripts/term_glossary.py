"""Shared glossary matching and atomic source-boundary checks."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

GLOSSARY = Path(__file__).resolve().parents[1] / "references" / "术语表.txt"


def parse_terms(content: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for number, line in enumerate(content.removeprefix("\ufeff").splitlines(), 1):
        term = line.strip()
        if not term:
            continue
        if "\ufeff" in term or "\t" in term:
            raise ValueError(f"术语表第 {number} 行含有无效字符")
        key = term.casefold() if not re.search(r"[\u3400-\u9fff]", term) else term
        if key not in seen:
            seen.add(key)
            terms.append(term)
    return terms


def load_terms(path: Path = GLOSSARY) -> list[str]:
    return parse_terms(path.read_text(encoding="utf-8"))


def snapshot_terms(run_dir: Path, source: Path = GLOSSARY) -> list[str]:
    content = source.read_text(encoding="utf-8")
    terms = parse_terms(content)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "术语表.txt").write_text(content, encoding="utf-8")
    (run_dir / "术语表.sha256").write_text(hashlib.sha256(content.encode("utf-8")).hexdigest(), encoding="ascii")
    return terms


def run_terms(run_dir: Path, *, required: bool = False) -> list[str]:
    source = run_dir / "术语表.txt"
    digest = run_dir / "术语表.sha256"
    if not source.exists() and not digest.exists():
        if required:
            raise ValueError("运行术语表快照缺失")
        return []  # Runs created before glossary support remain verifiable.
    if not source.is_file() or not digest.is_file():
        raise ValueError("运行术语表快照不完整")
    content = source.read_text(encoding="utf-8")
    if hashlib.sha256(content.encode("utf-8")).hexdigest() != digest.read_text(encoding="ascii"):
        raise ValueError("运行术语表快照哈希不一致")
    return parse_terms(content)


def matches(text: str, terms: list[str]) -> list[dict]:
    """Return nonoverlapping occurrences, favoring the longest at each position."""
    found: list[tuple[int, int, str]] = []
    for term in terms:
        english = not re.search(r"[\u3400-\u9fff]", term)
        pattern = re.escape(term)
        if english:
            pattern = rf"(?<![A-Za-z0-9_]){pattern}(?![A-Za-z0-9_])"
        for hit in re.finditer(pattern, text, re.IGNORECASE if english else 0):
            found.append((hit.start(), hit.end(), term))
    found.sort(key=lambda hit: (hit[0], -(hit[1] - hit[0])))
    chosen: list[dict] = []
    for start, end, term in found:
        if chosen and start < chosen[-1]["end"]:
            continue
        chosen.append({"start": start, "end": end, "term": term, "text": text[start:end]})
    return chosen


def validate_boundaries(start: int, end: int, occurrences: list[dict]) -> None:
    for item in occurrences:
        if start < item["end"] and end > item["start"] and (start > item["start"] or end < item["end"]):
            raise ValueError(f"区间从术语中间切开：{item['text']}（{item['start']}–{item['end']}）")


def validate_keyword(sentence: str, keyword: str, occurrences: list[dict]) -> None:
    """Reject an ambiguous source keyword if it can cut any glossary occurrence."""
    positions = list(re.finditer(re.escape(keyword), sentence, re.IGNORECASE))
    if not positions:
        return  # No source interval intersects a protected occurrence.
    if any(any(hit.start() < item["end"] and hit.end() > item["start"]
               and (hit.start() > item["start"] or hit.end() < item["end"])
               for item in occurrences) for hit in positions):
        raise ValueError(f"关键词从术语中间切开：{keyword}")
