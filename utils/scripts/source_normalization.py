"""Deterministic normalization and scope selection for converted study sources."""

from __future__ import annotations

import re
from dataclasses import dataclass


def normalize_for_matching(value: str) -> str:
    """Remove presentation markup so headings such as 翻****译 can be matched."""
    value = value.replace("\u00a0", " ").replace("\u200b", "")
    value = re.sub(r"[*_`~]", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def clean_markdown(value: str) -> str:
    """Make converted Markdown readable without changing its words."""
    value = value.replace("\u00a0", " ").replace("\u200b", "")
    # The source EPUB uses one strong tag per character.  These markers are
    # presentation noise for a writing tool, so remove them from the archive.
    value = re.sub(r"[*_`~]+", "", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip() + "\n"


def _is_heading(line: str) -> tuple[int, str] | None:
    match = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
    return (len(match.group(1)), normalize_for_matching(match.group(2))) if match else None


def _plain_chapter_title(line: str) -> str | None:
    """Return chapter markers emitted as ordinary paragraphs by some EPUBs."""
    marker = normalize_for_matching(line)
    match = re.match(r"^(第\s*[一二三四五六七八九十百千万\d]+\s*章(?:\s+.*)?|附录(?:\s+.*)?)$", marker)
    return marker if match else None


def _remove_translation_subsections(lines: list[str]) -> list[str]:
    result: list[str] = []
    skipping = False
    for line in lines:
        marker = normalize_for_matching(line)
        # Some EPUBs emit a chapter title as a plain paragraph immediately
        # before the corresponding XHTML heading.  Drop that marker so it
        # cannot leak into the preceding writing block.
        if re.search(r"翻译\s*考前预测", marker, re.I):
            skipping = True
            continue
        translation_marker = re.match(r"^(?:#+\s*)?(?:[（(]\s*[一二三四五六七八九十\d]+\s*[）)]\s*)?(?:参考)?译文(?:\s|$)", marker, re.I)
        if translation_marker:
            skipping = True
            continue
        if skipping and re.match(r"[（(]\s*[三四五六七八九十\d]+\s*[）)]", marker):
            skipping = False
        if not skipping:
            result.append(line)
    return result


@dataclass(frozen=True)
class SourceBlock:
    title: str
    level: int
    content: str
    source_locator: str


def select_writing_blocks_with_audit(text: str, *, ignore_translation: bool = True) -> tuple[list[SourceBlock], dict]:
    """Select writing chapters using a deterministic scope state machine."""
    lines = text.splitlines()
    blocks: list[tuple[int, str, list[str], int]] = []
    current: tuple[int, str, list[str], int] | None = None
    for index, line in enumerate(lines, 1):
        heading = _is_heading(line)
        plain_chapter = _plain_chapter_title(line)
        if plain_chapter and not heading:
            if current:
                blocks.append(current)
            current = (1, plain_chapter, [line], index)
            continue
        if heading:
            if current:
                blocks.append(current)
            current = (heading[0], heading[1], [line], index)
        elif current:
            current[2].append(line)
    if current:
        blocks.append(current)

    selected: list[SourceBlock] = []
    included_sections: list[dict] = []
    excluded_sections: list[dict] = []
    removed_translation_subsections = 0
    writing_scope = False
    excluded_level: int | None = None
    for level, title, content_lines, line_no in blocks:
        if excluded_level is not None:
            # A chapter marker can be split into a plain paragraph and a
            # following Markdown heading (e.g. "第2章 四级翻译..." then
            # "# 第2章"). Keep the exclusion active for that duplicate.
            if level <= excluded_level and re.match(r"^第\s*[一二三四五六七八九十百千万\d]+\s*章$", title):
                excluded_sections.append({"title": title, "source_locator": f"line:{line_no}", "reason": "translation_chapter_duplicate"})
                continue
            if level <= excluded_level:
                excluded_level = None
            else:
                excluded_sections.append({"title": title, "source_locator": f"line:{line_no}", "reason": "translation_scope"})
                continue
        if re.search(r"翻译|译文|translation", title, re.I):
            excluded_level = level
            excluded_sections.append({"title": title, "source_locator": f"line:{line_no}", "reason": "translation_scope"})
            continue
        if re.search(r"写作|作文|writing", title, re.I):
            writing_scope = True
        if not writing_scope:
            continue
        cleaned_lines = _remove_translation_subsections(content_lines) if ignore_translation else content_lines
        original_text = "\n".join(content_lines)
        removed_translation_subsections += len(re.findall(r"(?:参考)?译文", normalize_for_matching(original_text), re.I)) - len(re.findall(r"(?:参考)?译文", normalize_for_matching("\n".join(cleaned_lines)), re.I))
        body = clean_markdown("\n".join(cleaned_lines)).strip()
        body_lines = body.splitlines() if body else []
        if body_lines and _is_heading(body_lines[0]) and normalize_for_matching(_is_heading(body_lines[0])[1]) == title:
            body_lines = body_lines[1:]
        body = clean_markdown("\n".join(body_lines)).strip() if body_lines else ""
        # Directory-only blocks are not reusable writing rules.
        if not body or len(normalize_for_matching(body)) < 20:
            continue
        locator = f"line:{line_no}"
        selected.append(SourceBlock(title, level, body, locator))
        included_sections.append({"title": title, "source_locator": locator})
    audit = {
        "included_sections": included_sections,
        "excluded_sections": excluded_sections,
        "removed_translation_subsections": max(0, removed_translation_subsections),
        "counts": {
            "source_blocks": len(blocks),
            "included_blocks": len(selected),
            "excluded_blocks": len(excluded_sections),
            "ignored_before_writing_scope": max(0, len(blocks) - len(selected) - len(excluded_sections)),
        },
    }
    return selected, audit


def select_writing_blocks(text: str, *, ignore_translation: bool = True) -> list[SourceBlock]:
    """Backward-compatible selector returning only the selected blocks."""
    return select_writing_blocks_with_audit(text, ignore_translation=ignore_translation)[0]
