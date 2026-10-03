"""Adapt shared terms plus a per-run hotword list without changing the glossary."""
from pathlib import Path
from .term_glossary import load_terms
from .volcengine_asr import Hotword, parse_hotwords, PUNCTUATION_RE
from .file_transaction import file_sha256


def merge_hotwords(shared: Path, extra: Path | None = None) -> tuple[list[Hotword], list[dict]]:
    terms = load_terms(shared)
    invalid = [term for term in terms if PUNCTUATION_RE.search(term)]
    if invalid:
        raise ValueError("共享术语不能直接提交 ASR，请明确选择适用词表：" + ", ".join(invalid))
    values = {term.casefold(): Hotword(term) for term in terms}
    sources = [{"path": str(shared), "sha256": file_sha256(shared)}]
    if extra and extra.resolve() != shared.resolve():
        sources.append({"path": str(extra), "sha256": file_sha256(extra)})
        for term in parse_hotwords(extra):
            values[term.word.casefold()] = term
    if not values or len(values) > 5000:
        raise ValueError("ASR 热词数量必须为 1～5000；不能静默截断")
    return list(values.values()), sources
