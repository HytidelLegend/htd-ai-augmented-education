"""Shared deterministic Markdown report formatting helpers."""

from __future__ import annotations

from collections.abc import Iterable


PUNCTUATION_CONCATENATION_ANOMALIES = ("。、", "；。", "，，")


def table_cell(value: object) -> str:
    """Escape one value for a single-line Markdown table cell."""
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def markdown_table(headers: Iterable[object], rows: Iterable[Iterable[object]]) -> str:
    """Render a compact GitHub-flavored Markdown table."""
    rendered_headers = [table_cell(value) for value in headers]
    lines = [
        "| " + " | ".join(rendered_headers) + " |",
        "|" + "|".join("---" for _ in rendered_headers) + "|",
    ]
    for row in rows:
        cells = [table_cell(value) for value in row]
        if len(cells) != len(rendered_headers):
            raise ValueError("Markdown 表格行列数与表头不一致")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def blockquote(value: object) -> str:
    """Render arbitrary text as a Markdown blockquote."""
    return "\n".join("> " + line for line in str(value).splitlines())


def bullet_list(values: Iterable[object], *, empty: str) -> str:
    """Render bullets or one deterministic empty-state bullet."""
    items = [str(value) for value in values]
    return "\n".join(f"- {value}" for value in items) if items else f"- {empty}"


def inline_list(values: Iterable[object], *, empty: str, separator: str = "、") -> str:
    """Render short inline values without changing the supplied text."""
    items = [str(value) for value in values]
    return separator.join(items) if items else empty


def titled_bullet_sections(
    sections: Iterable[tuple[object, Iterable[object]]],
    *,
    heading_level: int,
    empty: str,
) -> str:
    """Render titled list fields as Markdown headings followed by bullets."""
    if heading_level < 1 or heading_level > 6:
        raise ValueError("Markdown 标题级别必须在 1 至 6 之间")
    prefix = "#" * heading_level
    return "\n\n".join(
        f"{prefix} {title}\n\n{bullet_list(values, empty=empty)}"
        for title, values in sections
    )


def find_punctuation_concatenation_anomalies(text: str) -> list[str]:
    """Return only explicit punctuation patterns caused by faulty list concatenation."""
    return [
        f"位置 {index + 1}：{text[index:index + len(pattern)]}"
        for pattern in PUNCTUATION_CONCATENATION_ANOMALIES
        for index in (
            match
            for match in range(len(text))
            if text.startswith(pattern, match)
        )
    ]
