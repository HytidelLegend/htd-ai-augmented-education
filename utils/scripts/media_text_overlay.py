"""Deterministic ASS text overlays shared by image and video skills."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any


class MediaTextOverlayError(ValueError):
    """Raised when text overlay styles, positions, or events are invalid."""


ASS_STYLE_FIELDS = (
    "Name", "Fontname", "Fontsize", "PrimaryColour", "SecondaryColour", "OutlineColour",
    "BackColour", "Bold", "Italic", "Underline", "StrikeOut", "ScaleX", "ScaleY", "Spacing",
    "Angle", "BorderStyle", "Outline", "Shadow", "Alignment", "MarginL", "MarginR", "MarginV",
    "Encoding",
)

ALIGNMENTS = {
    "bottom-left": 1,
    "bottom-center": 2,
    "bottom-right": 3,
    "center-left": 4,
    "center": 5,
    "center-right": 6,
    "top-left": 7,
    "top-center": 8,
    "top-right": 9,
}

REQUIRED_STYLE_FIELDS = {
    "font_name",
    "font_size",
    "font_weight",
    "primary_colour",
    "outline",
    "outline_colour",
    "shadow",
    "shadow_colour",
}

ASS_COLOUR_RE = re.compile(r"&H[0-9A-Fa-f]{8}")
HEX_COLOUR_RE = re.compile(r"#[0-9A-Fa-f]{6}")


def normalize_ass_colour(value: str) -> str:
    """Return an ASS colour, accepting either ``#RRGGBB`` or ``&HAABBGGRR``."""
    raw = str(value).strip()
    if ASS_COLOUR_RE.fullmatch(raw):
        return raw.upper().replace("X", "x")
    if HEX_COLOUR_RE.fullmatch(raw):
        red, green, blue = raw[1:3], raw[3:5], raw[5:7]
        return f"&H00{blue}{green}{red}".upper()
    raise MediaTextOverlayError(
        f"颜色必须是 #RRGGBB 或 &HAABBGGRR：{raw!r}"
    )


def normalize_style(
    style: dict[str, Any] | None,
    *,
    defaults: dict[str, Any] | None = None,
    label: str = "style",
) -> dict[str, Any]:
    """Merge and validate one deterministic libass text style."""
    if style is not None and not isinstance(style, dict):
        raise MediaTextOverlayError(f"{label} 必须是对象")
    merged = {**(defaults or {}), **(style or {})}
    missing = sorted(REQUIRED_STYLE_FIELDS - set(merged))
    if missing:
        raise MediaTextOverlayError(f"{label} 缺少字段：{', '.join(missing)}")
    if not isinstance(merged["font_name"], str) or not merged["font_name"].strip():
        raise MediaTextOverlayError(f"{label}.font_name 必须是非空字符串")
    if isinstance(merged["font_size"], bool) or not isinstance(merged["font_size"], int) or merged["font_size"] <= 0:
        raise MediaTextOverlayError(f"{label}.font_size 必须是正整数")
    if isinstance(merged["font_weight"], bool) or not isinstance(merged["font_weight"], int) or not 100 <= merged["font_weight"] <= 900:
        raise MediaTextOverlayError(f"{label}.font_weight 必须位于 100～900")
    for field in ("outline", "shadow"):
        value = merged[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MediaTextOverlayError(f"{label}.{field} 必须是非负整数")
    for field in ("primary_colour", "outline_colour", "shadow_colour"):
        merged[field] = normalize_ass_colour(str(merged[field]))
    merged["font_name"] = merged["font_name"].strip()
    return merged


def normalize_position(
    position: dict[str, Any] | None,
    *,
    width: int,
    height: int,
    defaults: dict[str, Any] | None = None,
    label: str = "position",
) -> dict[str, Any]:
    """Resolve an absolute pixel position and a nine-point ASS anchor."""
    if position is not None and not isinstance(position, dict):
        raise MediaTextOverlayError(f"{label} 必须是对象")
    merged = {
        "x_px": width // 2,
        "y_px": height // 2,
        "alignment": "center",
        **(defaults or {}),
        **(position or {}),
    }
    for field, maximum in (("x_px", width), ("y_px", height)):
        value = merged[field]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise MediaTextOverlayError(f"{label}.{field} 必须位于 0～{maximum}")
    alignment = merged["alignment"]
    if isinstance(alignment, int) and not isinstance(alignment, bool) and 1 <= alignment <= 9:
        alignment_value = alignment
    elif isinstance(alignment, str) and alignment in ALIGNMENTS:
        alignment_value = ALIGNMENTS[alignment]
    else:
        raise MediaTextOverlayError(
            f"{label}.alignment 必须是 1～9 或以下值之一：{', '.join(ALIGNMENTS)}"
        )
    return {**merged, "alignment": alignment_value}


def parse_ass_style_line(line: str) -> dict[str, str]:
    """Parse and validate one V4+ Style line against the canonical field order."""
    if not line.startswith("Style: "):
        raise MediaTextOverlayError("ASS 样式行必须以 Style: 开头")
    values = line.removeprefix("Style: ").rstrip("\r\n").split(",")
    if len(values) != len(ASS_STYLE_FIELDS):
        raise MediaTextOverlayError(
            f"ASS 样式字段数量不一致：expected={len(ASS_STYLE_FIELDS)}，actual={len(values)}"
        )
    return dict(zip(ASS_STYLE_FIELDS, values))


def _ass_style_line(name: str, style: dict[str, Any]) -> str:
    bold = -1 if int(style["font_weight"]) >= 600 else 0
    fields = {
        "Name": name,
        "Fontname": style["font_name"],
        "Fontsize": style["font_size"],
        "PrimaryColour": style["primary_colour"],
        "SecondaryColour": "&H000000FF",
        "OutlineColour": style["outline_colour"],
        "BackColour": style["shadow_colour"],
        "Bold": bold,
        "Italic": 0,
        "Underline": 0,
        "StrikeOut": 0,
        "ScaleX": 100,
        "ScaleY": 100,
        "Spacing": 0,
        "Angle": 0,
        "BorderStyle": 1,
        "Outline": style["outline"],
        "Shadow": style["shadow"],
        # Event-level \\an tags are authoritative. Keep 2 here for compatibility
        # with existing caption artifacts and their field-order regression tests.
        "Alignment": 2,
        "MarginL": 0,
        "MarginR": 0,
        "MarginV": 0,
        "Encoding": 1,
    }
    line = "Style: " + ",".join(str(fields[field]) for field in ASS_STYLE_FIELDS) + "\n"
    parsed = parse_ass_style_line(line)
    if parsed["Outline"] != str(style["outline"]) or parsed["Shadow"] != str(style["shadow"]):
        raise MediaTextOverlayError("ASS 描边或阴影字段发生错位")
    return line


def _ass_time(milliseconds: int, *, end: bool = False) -> str:
    centiseconds = math.ceil(milliseconds / 10) if end else math.floor(milliseconds / 10)
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def _ass_text(value: str) -> str:
    return value.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\r\n", "\n").replace("\r", "\n").replace("\n", r"\N")


def render_text_ass(
    path: Path,
    items: list[dict[str, Any]],
    *,
    width: int,
    height: int,
    default_style: dict[str, Any] | None = None,
    default_position: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Render per-item text, styles, positions, timings, and list-ordered layers."""
    if width <= 0 or height <= 0:
        raise MediaTextOverlayError("ASS 画布宽高必须为正整数")
    if not isinstance(items, list) or not items:
        raise MediaTextOverlayError("文本条目不能为空")
    normalized: list[dict[str, Any]] = []
    styles: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise MediaTextOverlayError(f"items[{index}] 必须是对象")
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip():
            raise MediaTextOverlayError(f"items[{index}].text 必须是非空字符串")
        start_ms = raw.get("start_ms", 0)
        end_ms = raw.get("end_ms", 1000)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (start_ms, end_ms)):
            raise MediaTextOverlayError(f"items[{index}] 时间必须是整数毫秒")
        if start_ms < 0 or end_ms <= start_ms:
            raise MediaTextOverlayError(f"items[{index}] 时间段必须满足 0 ≤ start_ms < end_ms")
        style = normalize_style(raw.get("style"), defaults=default_style, label=f"items[{index}].style")
        position = normalize_position(
            raw.get("position"),
            width=width,
            height=height,
            defaults=default_position,
            label=f"items[{index}].position",
        )
        style_name = str(raw.get("style_name") or f"Text{index + 1:04d}")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", style_name):
            raise MediaTextOverlayError(f"items[{index}].style_name 无效")
        if style_name in styles and styles[style_name] != style:
            raise MediaTextOverlayError(f"ASS 样式名重复但属性不同：{style_name}")
        styles.setdefault(style_name, style)
        layer = raw.get("layer", index)
        if isinstance(layer, bool) or not isinstance(layer, int) or layer < 0:
            raise MediaTextOverlayError(f"items[{index}].layer 必须是非负整数")
        normalized.append(
            {
                "item_index": index,
                "text": text,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "style": style,
                "style_name": style_name,
                "position": position,
                "layer": layer,
            }
        )

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: {', '.join(ASS_STYLE_FIELDS)}
{''.join(_ass_style_line(name, style) for name, style in styles.items())}[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for item in normalized:
        position = item["position"]
        lines.append(
            f"Dialogue: {item['layer']},"
            f"{_ass_time(item['start_ms'])},"
            f"{_ass_time(item['end_ms'], end=True)},"
            f"{item['style_name']},,0,0,0,,"
            f"{{\\an{position['alignment']}\\pos({position['x_px']},{position['y_px']})}}"
            f"{_ass_text(item['text'])}\n"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8", newline="\n")
    return normalized


def render_caption_ass(
    path: Path,
    events: list[dict[str, Any]],
    *,
    width: int,
    height: int,
    style: dict[str, Any] | None = None,
    chinese_style: dict[str, Any] | None = None,
    english_style: dict[str, Any] | None = None,
    position: dict[str, Any] | None = None,
) -> None:
    """Compatibility adapter for Chinese captions and an optional English line."""
    chinese = dict(chinese_style or style or {})
    if style and not chinese_style:
        chinese.setdefault("font_weight", 400)
        chinese.setdefault("outline", 0)
        chinese.setdefault("outline_colour", chinese.get("shadow_colour", "&H80000000"))
    chinese = normalize_style(chinese, label="Chinese")
    english = normalize_style(english_style, label="English") if english_style else None
    legacy_margin = int((style or {}).get("margin_v", 30))
    placement = {
        "x_px": width // 2,
        "lowest_bottom_y_px": height - legacy_margin,
        "bilingual_line_gap_px": 6,
        **(position or {}),
    }
    x = int(placement["x_px"])
    lowest_y = int(placement["lowest_bottom_y_px"])
    gap = int(placement["bilingual_line_gap_px"])
    if not 0 <= x <= width or not 0 <= lowest_y <= height or gap < 0:
        raise MediaTextOverlayError("字幕坐标或双语行间距无效")
    chinese_y = lowest_y - (int(english["font_size"]) + gap if english else 0)
    if chinese_y <= 0:
        raise MediaTextOverlayError("字幕位置越出画面顶部")
    items: list[dict[str, Any]] = []
    for event in events:
        base = {
            "start_ms": int(event["start_ms"]),
            "end_ms": int(event["end_ms"]),
            "layer": 0,
        }
        items.append(
            {
                **base,
                "text": str(event.get("display_text", event["text"])),
                "style": chinese,
                "style_name": "Chinese",
                "position": {"x_px": x, "y_px": chinese_y, "alignment": "bottom-center"},
            }
        )
        if english:
            english_text = str(event.get("english_text", ""))
            if not english_text.strip():
                raise MediaTextOverlayError(f"caption {event.get('caption_index')} 缺少英文翻译")
            if "\n" in english_text or "\r" in english_text:
                raise MediaTextOverlayError("英文字幕必须严格为一行")
            items.append(
                {
                    **base,
                    "text": english_text,
                    "style": english,
                    "style_name": "English",
                    "position": {"x_px": x, "y_px": lowest_y, "alignment": "bottom-center"},
                }
            )
    render_text_ass(path, items, width=width, height=height)


# Existing callers import this historical name from the common implementation.
render_ass = render_caption_ass
