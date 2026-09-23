"""Small deterministic XHTML-to-Markdown renderer with no content cleaning."""

from __future__ import annotations

import html
import posixpath
import re
from html.parser import HTMLParser


class HtmlParseError(ValueError):
    pass


class MarkdownRenderer(HTMLParser):
    BLOCK = {"p", "div", "section", "article", "header", "footer", "aside", "blockquote", "li", "tr", "td", "th", "pre"}
    def __init__(self, base_path: str, resource_map: dict[str, str]):
        super().__init__(convert_charrefs=True)
        self.base_path = base_path
        self.resource_map = resource_map
        self.parts: list[str] = []
        self.stack: list[str] = []
        self.link_stack: list[str] = []
        self.stats = {"heading_count": 0, "paragraph_count": 0, "table_count": 0, "footnote_count": 0, "link_count": 0, "image_count": 0}

    def emit(self, value: str) -> None:
        self.parts.append(value)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower(); attrs = dict(attrs); self.stack.append(tag)
        if re.fullmatch(r"h[1-6]", tag):
            self.stats["heading_count"] += 1; self.emit("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in {"p", "div", "section", "article", "header", "footer", "aside", "blockquote", "pre"}:
            self.stats["paragraph_count"] += int(tag == "p"); self.emit("\n\n")
            if tag == "blockquote": self.emit("> ")
        elif tag == "br": self.emit("\n")
        elif tag in {"strong", "b"}: self.emit("**")
        elif tag in {"em", "i"}: self.emit("*")
        elif tag == "code": self.emit("`")
        elif tag == "a":
            self.stats["link_count"] += 1
            href = attrs.get("href", "")
            if attrs.get("epub:type") == "noteref" or "footnote" in attrs.get("class", "").split():
                self.stats["footnote_count"] += 1
                self.emit(f"[^{href.lstrip('#') or 'footnote'}]")
                self.link_stack.append("")
            else:
                self.emit("[")
                self.link_stack.append(href)
        elif tag == "img":
            self.stats["image_count"] += 1
            src = attrs.get("src", "")
            alt = attrs.get("alt", "")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(self.base_path), src))
            target = self.resource_map.get(resolved, self.resource_map.get(src, src))
            self.emit(f"![{alt}]({target})")
        elif tag == "table": self.stats["table_count"] += 1; self.emit("\n\n")
        elif tag == "tr": self.emit("@@TR@@")
        elif tag in {"td", "th"}: self.emit("@@TD@@")
        elif tag == "li": self.emit("\n- ")
        if tag in {"div", "aside", "li"} and ("footnote" in attrs.get("class", "").split() or attrs.get("epub:type") == "footnote"):
            self.stats["footnote_count"] += 1
            self.emit(f"\n\n[^{attrs.get('id', 'footnote')}]: ")
        elif tag in {"sup", "sub"}: self.emit("^")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs); self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"strong", "b"}: self.emit("**")
        elif tag in {"em", "i"}: self.emit("*")
        elif tag == "code": self.emit("`")
        elif tag == "a":
            href = self.link_stack.pop() if self.link_stack else ""
            if href:
                self.emit(f"]({href})")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "section", "article", "header", "footer", "aside", "blockquote", "pre", "tr"}: self.emit("\n")
        elif tag in {"td", "th"}: self.emit("@@ENDTD@@")
        elif tag in {"sup", "sub"}: self.emit("^")
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if self.stack and self.stack[-1] == "a" and self.link_stack and self.link_stack[-1] == "":
            return
        self.emit(data)

    def handle_entityref(self, name):
        self.emit(html.unescape(f"&{name};"))

    def render(self) -> str:
        text = "".join(self.parts)
        def table_replacement(match):
            rows = []
            for raw in match.group(0).split("@@TR@@"):
                if "@@TD@@" not in raw:
                    continue
                cells = [cell.strip() for cell in raw.split("@@TD@@")[1:] for cell in [cell.split("@@ENDTD@@", 1)[0]]]
                if cells:
                    rows.append("| " + " | ".join(cells) + " |")
            if not rows:
                return ""
            return "\n".join(rows[:1] + ["| " + " | ".join(["---"] * max(1, rows[0].count("|") - 1)) + " |"] + rows[1:])
        text = re.sub(r"@@TR@@.*?(?=\n\n|$)", table_replacement, text, flags=re.S)
        text = text.replace("@@TR@@", "").replace("@@TD@@", "").replace("@@ENDTD@@", "")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"


def render_xhtml(data: bytes, *, base_path: str, resource_map: dict[str, str]) -> tuple[str, dict[str, int]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HtmlParseError("XHTML 不是有效 UTF-8") from exc
    parser = MarkdownRenderer(base_path, resource_map)
    try:
        parser.feed(text); parser.close()
    except Exception as exc:
        raise HtmlParseError(f"XHTML 解析失败：{exc}") from exc
    return parser.render(), parser.stats
