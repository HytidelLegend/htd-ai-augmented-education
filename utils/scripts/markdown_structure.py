"""Deterministically inspect Markdown structure without changing source files."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Iterable

import yaml


ATX_HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})(?:[ \t]+(.*?))[ \t]*$")
FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
INLINE_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
REFERENCE_IMAGE = re.compile(r"!\[([^\]]*)\]\[([^\]]*)\]")
OBSIDIAN_IMAGE = re.compile(r"!\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
HTML_IMAGE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*['\"]([^'\"]+)['\"][^>]*>", re.IGNORECASE)
HTML_ALT = re.compile(r"\balt\s*=\s*['\"]([^'\"]*)['\"]", re.IGNORECASE)


class MarkdownStructureError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_atx_heading(line: str) -> tuple[int, str] | None:
    match = ATX_HEADING.fullmatch(line)
    if not match:
        return None
    body = re.sub(r"[ \t]+#+[ \t]*$", "", match.group(2) or "").strip()
    return len(match.group(1)), body


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            raw = "\n".join(lines[1:index])
            value = yaml.safe_load(raw) if raw.strip() else {}
            if not isinstance(value, dict):
                raise MarkdownStructureError("Markdown Frontmatter 必须是 YAML 对象")
            return value, "\n".join(lines[index + 1 :])
    raise MarkdownStructureError("Markdown Frontmatter 缺少结束分隔符")


def _limited_preview(lines: list[str], start: int, stop: int, limit: int) -> str:
    chunks: list[str] = []
    length = 0
    in_fence = False
    fence_char = ""
    for raw in lines[start:stop]:
        fence = FENCE.match(raw)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_char = True, marker[0]
            elif marker[0] == fence_char:
                in_fence = False
            continue
        if in_fence or parse_atx_heading(raw) is not None:
            continue
        value = raw.strip()
        if not value or value == "---":
            continue
        remaining = limit - length
        if remaining <= 0:
            break
        chunks.append(value[:remaining])
        length += min(len(value), remaining)
        if length >= limit:
            break
    return "\n".join(chunks)


def _caption_hint(lines: list[str], line_index: int) -> str:
    for candidate_index in (line_index - 1, line_index + 1):
        if 0 <= candidate_index < len(lines):
            value = lines[candidate_index].strip()
            if value and not parse_atx_heading(value) and not value.startswith(("![", "![[", "<img")):
                return value[:160]
    return ""


def _image_matches(line: str) -> list[tuple[str, str, str]]:
    matches: list[tuple[int, str, str, str]] = []
    for match in INLINE_IMAGE.finditer(line):
        matches.append((match.start(), "markdown_inline", match.group(1).strip(), match.group(0)))
    for match in REFERENCE_IMAGE.finditer(line):
        matches.append((match.start(), "markdown_reference", match.group(1).strip(), match.group(0)))
    for match in OBSIDIAN_IMAGE.finditer(line):
        matches.append((match.start(), "obsidian_embed", (match.group(2) or Path(match.group(1)).stem).strip(), match.group(0)))
    for match in HTML_IMAGE.finditer(line):
        alt_match = HTML_ALT.search(match.group(0))
        matches.append((match.start(), "html_img", alt_match.group(1).strip() if alt_match else "", match.group(0)))
    return [(kind, alt, raw) for _, kind, alt, raw in sorted(matches)]


def extract_markdown_structure(
    path: Path,
    *,
    root: Path,
    preview_chars: int = 400,
    max_heading_depth: int = 6,
    identity_path: str | None = None,
) -> dict[str, Any]:
    if preview_chars < 0:
        raise MarkdownStructureError("preview_chars 不能为负数")
    if not 1 <= max_heading_depth <= 6:
        raise MarkdownStructureError("max_heading_depth 必须是 1～6")
    resolved = path.resolve()
    project_root = root.resolve()
    if not resolved.is_file() or resolved.suffix.lower() != ".md":
        raise MarkdownStructureError(f"不是可读取的 Markdown 文件：{path}")
    try:
        relative = resolved.relative_to(project_root).as_posix()
    except ValueError as exc:
        raise MarkdownStructureError(f"输入文件必须位于项目目录内：{resolved}") from exc
    raw = resolved.read_text(encoding="utf-8-sig")
    if identity_path is not None:
        relative = identity_path
    frontmatter, _ = split_frontmatter(raw)
    lines = raw.splitlines()
    source_sha256 = sha256_file(resolved)
    headings: list[dict[str, Any]] = []
    in_fence = False
    fence_char = ""
    parents: dict[int, dict[str, Any]] = {}
    occurrences: dict[str, int] = {}
    for index, line in enumerate(lines):
        fence = FENCE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_char = True, marker[0]
            elif marker[0] == fence_char:
                in_fence = False
            continue
        if in_fence:
            continue
        heading = parse_atx_heading(line)
        if heading is None or heading[0] > max_heading_depth:
            continue
        level, title = heading
        if not title:
            continue
        occurrences[title] = occurrences.get(title, 0) + 1
        parent_chain = [parents[parent]["title"] for parent in sorted(parents) if parent < level]
        locator_key = f"{relative}|{'/'.join(parent_chain)}|{level}|{title}|{occurrences[title]}"
        entry = {
            "candidate_id": f"SEC-{hashlib.sha256(locator_key.encode('utf-8')).hexdigest()[:16]}",
            "title": title,
            "level": level,
            "occurrence": occurrences[title],
            "parent_heading_chain": parent_chain,
            "start_line": index + 1,
            "end_line": len(lines),
            "end_before_heading": None,
            "preview": "",
        }
        headings.append(entry)
        parents[level] = entry
        parents = {key: value for key, value in parents.items() if key <= level}
    for index, item in enumerate(headings):
        stop = headings[index + 1]["start_line"] - 1 if index + 1 < len(headings) else len(lines)
        item["end_line"] = stop
        if index + 1 < len(headings):
            item["end_before_heading"] = headings[index + 1]["title"]
        item["preview"] = _limited_preview(lines, item["start_line"], stop, preview_chars)
    if not headings:
        locator_key = f"{relative}|document"
        headings.append({
            "candidate_id": f"DOC-{hashlib.sha256(locator_key.encode('utf-8')).hexdigest()[:16]}",
            "title": str(frontmatter.get("title") or resolved.stem),
            "level": 0,
            "occurrence": 1,
            "parent_heading_chain": [],
            "start_line": 1,
            "end_line": len(lines),
            "end_before_heading": None,
            "preview": _limited_preview(lines, 0, len(lines), preview_chars),
        })
    images: list[dict[str, Any]] = []
    active_heading: dict[str, Any] | None = None
    heading_by_line = {item["start_line"]: item for item in headings}
    section_counts: dict[str, int] = {}
    in_fence = False
    fence_char = ""
    for line_index, line in enumerate(lines, start=1):
        fence = FENCE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_char = True, marker[0]
            elif marker[0] == fence_char:
                in_fence = False
            continue
        if in_fence:
            continue
        if line_index in heading_by_line:
            active_heading = heading_by_line[line_index]
        for kind, alt_text, raw_reference in _image_matches(line):
            section_id = active_heading["candidate_id"] if active_heading else headings[0]["candidate_id"]
            section_counts[section_id] = section_counts.get(section_id, 0) + 1
            image_key = f"{relative}|{line_index}|{section_counts[section_id]}|{raw_reference}"
            images.append({
                "figure_id": f"FIG-{hashlib.sha256(image_key.encode('utf-8')).hexdigest()[:16]}",
                "section_candidate_id": section_id,
                "heading_text": active_heading["title"] if active_heading else headings[0]["title"],
                "parent_heading_chain": active_heading["parent_heading_chain"] if active_heading else [],
                "heading_occurrence": active_heading["occurrence"] if active_heading else 1,
                "image_index_in_section": section_counts[section_id],
                "source_line_hint": line_index,
                "reference_kind": kind,
                "reference_hash": hashlib.sha256(raw_reference.encode("utf-8")).hexdigest(),
                "alt_text": alt_text[:160],
                "caption_hint": _caption_hint(lines, line_index - 1),
            })
    images_by_section: dict[str, list[dict[str, Any]]] = {}
    for image in images:
        images_by_section.setdefault(image["section_candidate_id"], []).append(image)
    for heading in headings:
        heading["images"] = images_by_section.get(heading["candidate_id"], [])
    return {
        "source_id": f"SRC-{hashlib.sha256(relative.encode('utf-8')).hexdigest()[:16]}",
        "path": relative,
        "sha256": source_sha256,
        "size_bytes": resolved.stat().st_size,
        "frontmatter": frontmatter,
        "headings": headings,
        "images": images,
    }


def resolve_markdown_inputs(
    inputs: Iterable[Path], *, root: Path, exclude: Iterable[Path] = ()
) -> list[Path]:
    root = root.resolve()
    excluded = {item.resolve() for item in exclude}
    resolved: set[Path] = set()
    for raw in inputs:
        candidate = raw if raw.is_absolute() else root / raw
        candidate = candidate.resolve()
        if not candidate.exists():
            raise MarkdownStructureError(f"输入路径不存在：{candidate}")
        if candidate.is_dir():
            resolved.update(path.resolve() for path in candidate.rglob("*.md"))
        elif candidate.suffix.lower() == ".md":
            resolved.add(candidate)
        else:
            raise MarkdownStructureError(f"只支持 Markdown 文件或目录：{candidate}")
    values = sorted(path for path in resolved if path not in excluded)
    if not values:
        raise MarkdownStructureError("没有找到可处理的 Markdown 文件")
    return values
