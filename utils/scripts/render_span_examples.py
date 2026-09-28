"""Render structured span examples into a Markdown reference section."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from span_ops import masked_text, normalize_text, validate_cloze_quality, validate_spans

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
    required = {"title", "level", "source", "marked", "masked", "positive_reason", "negative", "negative_reason"}
    for index, example in enumerate(examples, start=1):
        if not isinstance(example, dict) or not required.issubset(example):
            raise ValueError(f"第 {index} 个案例字段不完整")
        if not all(isinstance(example[key], str) and example[key].strip() for key in required):
            raise ValueError(f"第 {index} 个案例存在空字段")
        source = normalize_text(example["source"])
        if source != example["source"]:
            raise ValueError(f"第 {index} 个案例原文换行未规范化")
        try:
            spans = extract_spans(source, example["marked"])
            extract_spans(source, example["negative"])
        except ValueError as exc:
            raise ValueError(f"第 {index} 个案例：{exc}") from exc
        if masked_text(source, spans) != example["masked"]:
            raise ValueError(f"第 {index} 个案例挖空文本与正面标记不一致")
        quality = validate_cloze_quality(source, spans)
        if not quality["pass"]:
            raise ValueError(f"第 {index} 个案例正面标记未通过质量检查：{quality['failures']}")


def render(data: dict) -> str:
    blocks = [f"## {data['title']}", "", data["intro"]]
    for example in data["examples"]:
        blocks.extend([
            "",
            f"### {example['level']}·{example['title']}",
            "",
            f"原文：`{example['source']}`",
            "",
            f"正面：`{example['marked']}`",
            "",
            f"挖空：`{example['masked']}`",
            "",
            f"说明：{example['positive_reason']}",
            "",
            f"负面：`{example['negative']}`",
            "",
            f"问题：{example['negative_reason']}",
        ])
    return "\n".join(blocks)


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
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    source = Path(args.source)
    target = Path(args.target)
    data = json.loads(source.read_text(encoding="utf-8"))
    validate(data)
    update_document(target, render(data), data["id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
