"""Render structured span examples into a Markdown reference section."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from span_ops import masked_text, normalize_text, validate_classical_spans, validate_cloze_alignment, validate_cloze_quality, validate_spans

MARKER = re.compile(r"「([^「」]+)」")


def section_markers(section_id: str) -> tuple[str, str]:
    if not isinstance(section_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", section_id):
        raise ValueError("案例区块 id 只能使用小写字母、数字和连字符")
    return (f"<!-- generated: {section_id}:start -->", f"<!-- generated: {section_id}:end -->")


def extract_spans(source: str, marked: str) -> list[dict]:
    """Derive offsets from the displayed markers; never trust hand-entered offsets."""
    spans: list[dict] = []
    plain_parts: list[str] = []
    cursor = 0
    for match in MARKER.finditer(marked):
        prefix = marked[cursor:match.start()]
        plain_parts.append(prefix)
        start = sum(map(len, plain_parts))
        value = match.group(1)
        spans.append({"start": start, "end": start + len(value), "role": "参考案例"})
        plain_parts.append(value)
        cursor = match.end()
    plain_parts.append(marked[cursor:])
    plain = "".join(plain_parts)
    if plain != source or "「" in plain or "」" in plain:
        raise ValueError("标记文本去除标记后与原文不一致")
    if not spans:
        raise ValueError("标记文本没有 span")
    return validate_spans(source, spans)


def validate(data: dict) -> None:
    section_markers(data.get("id", ""))
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        raise ValueError("案例源必须包含非空 title")
    if not isinstance(data.get("intro"), str) or not data["intro"].strip():
        raise ValueError("案例源必须包含非空 intro")
    examples = data.get("examples")
    if not isinstance(examples, list) or not examples:
        raise ValueError("案例源必须包含非空 examples")
    required = {"title", "level", "source", "marked", "positive_reason", "negative", "negative_reason"}
    for index, example in enumerate(examples, start=1):
        if not isinstance(example, dict) or not required.issubset(example):
            raise ValueError(f"第 {index} 个案例字段不完整")
        unexpected = set(example) - required - {"mode"}
        if unexpected:
            raise ValueError(f"第 {index} 个案例包含未使用字段：{', '.join(sorted(unexpected))}")
        if not all(isinstance(example[key], str) and example[key].strip() for key in required):
            raise ValueError(f"第 {index} 个案例存在空字段")
        source = normalize_text(example["source"])
        if source != example["source"]:
            raise ValueError(f"第 {index} 个案例原文换行未规范化")
        mode = example.get("mode", "key_points")
        if mode not in {"key_points", "classical_recitation"}:
            raise ValueError(f"第 {index} 个案例模式无效")
        try:
            spans = extract_spans(source, example["marked"])
            extract_spans(source, example["negative"])
        except ValueError as exc:
            raise ValueError(f"第 {index} 个案例：{exc}") from exc
        validate_cloze_alignment(source, spans, example["marked"], masked_text(source, spans))
        quality = validate_classical_spans(source, spans) if mode == "classical_recitation" else validate_cloze_quality(source, spans)
        if not quality["pass"]:
            raise ValueError(f"第 {index} 个案例正面标记未通过质量检查：{quality['failures']}")


def table_cell(value: str, code: bool = False) -> str:
    value = value.replace("|", "\\|").replace("\n", "<br>")
    return f"`{value}`" if code else value


def render(data: dict) -> str:
    blocks = [f"## {data['title']}", "", data["intro"], "", "| 学段 | 主题 | 原文 | 正面 | 挖空 | 正面原因 | 负面 | 负面原因 |", "|---|---|---|---|---|---|---|---|"]
    for example in data["examples"]:
        source = example["source"]
        spans = extract_spans(source, example["marked"])
        cells = [
            table_cell(example["level"]), table_cell(example["title"]),
            table_cell(source, code=True), table_cell(example["marked"], code=True),
            table_cell(masked_text(source, spans), code=True),
            table_cell(example["positive_reason"]), table_cell(example["negative"], code=True),
            table_cell(example["negative_reason"]),
        ]
        blocks.append("| " + " | ".join(cells) + " |")
    return "\n".join(blocks)


def render_document(sources: list[Path]) -> str:
    sections: list[str] = []
    section_ids: set[str] = set()
    for source in sources:
        data = json.loads(source.read_text(encoding="utf-8"))
        validate(data)
        if data["id"] in section_ids:
            raise ValueError(f"案例区块 id 重复：{data['id']}")
        section_ids.add(data["id"])
        start, end = section_markers(data["id"])
        sections.append(f"{start}\n{render(data)}\n{end}")
    return "# Span 示例\n\n" + "\n\n".join(sections) + "\n"


def update_document(target: Path, section: str, section_id: str) -> None:
    start_marker, end_marker = section_markers(section_id)
    current = target.read_text(encoding="utf-8") if target.exists() else "# Span 示例\n"
    wrapped = f"{start_marker}\n{section}\n{end_marker}"
    if current.count(start_marker) != current.count(end_marker) or current.count(start_marker) > 1:
        raise ValueError("生成区标记不完整或重复")
    if start_marker in current:
        before = current.split(start_marker, 1)[0].rstrip()
        after = current.split(end_marker, 1)[1].lstrip()
        current = before + "\n\n" + wrapped + ("\n\n" + after if after else "\n")
    else:
        current = current.rstrip() + "\n\n" + wrapped + "\n"
    target.write_text(current, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, action="append")
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    target = Path(args.target)
    sources = [Path(source) for source in args.source]
    if len(sources) > 1:
        target.write_text(render_document(sources), encoding="utf-8", newline="\n")
    else:
        data = json.loads(sources[0].read_text(encoding="utf-8"))
        validate(data)
        update_document(target, render(data), data["id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
