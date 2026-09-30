"""Shared non-overlapping text span validation, editing, and rendering."""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from .term_glossary import matches as glossary_matches, validate_boundaries
except ImportError:  # CLI scripts import this module from utils/scripts on sys.path.
    from term_glossary import matches as glossary_matches, validate_boundaries


def normalize_text(text: str) -> str:
    """Canonicalize imported plain text before hashing or slicing."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def text_sha256(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def math_expression_ranges(text: str) -> list[tuple[int, int]]:
    """Find complete formula right sides containing an operation or power.

    These ranges are candidate answer units, not automatic selections.  The
    conservative character set stops at prose and Chinese punctuation.
    """
    canonical = normalize_text(text)
    ranges: list[tuple[int, int]] = []
    symbol = re.compile(r"[A-Za-zΑ-Ωα-ω0-9₀-₉²³ⁿ^()+\-−×÷*/·.√]")
    operators = "+-−×÷*/^"
    for match in re.finditer(r"[=＝][ \t]*", canonical):
        start = match.end()
        end = start
        while end < len(canonical):
            char = canonical[end]
            if char == "." and not (
                end > start and end + 1 < len(canonical)
                and canonical[end - 1].isdigit() and canonical[end + 1].isdigit()
            ):
                break
            if symbol.fullmatch(char):
                end += 1
                continue
            if char in " \t":
                next_pos = end
                while next_pos < len(canonical) and canonical[next_pos] in " \t":
                    next_pos += 1
                if end > start and (
                    canonical[end - 1] in operators
                    or (next_pos < len(canonical) and canonical[next_pos] in operators)
                ):
                    end = next_pos
                    continue
            break
        expression = canonical[start:end].rstrip(" .")
        if not expression or not any(char.isalnum() for char in expression):
            continue
        if not re.search(r"[+\-−×÷*/^²³ⁿ]", expression):
            continue
        ranges.append((start, start + len(expression)))
    return ranges


def candidate_spans(text: str, *, max_candidates: int = 128, per_paragraph: int = 32, terms: list[str] | None = None) -> list[dict[str, Any]]:
    """Offer bounded, whole-text candidate ranges without choosing answers.

    Complete mathematical expressions, wrapped titles, quoted phrases, and
    single-character enumerations are suggested as units. Remaining short
    text runs are only hints; none of these candidates is selected automatically.
    """
    if max_candidates < 1 or per_paragraph < 1:
        raise ValueError("候选上限必须为正整数")
    canonical = normalize_text(text)
    protected = glossary_matches(canonical, terms or [])
    formula_ranges = math_expression_ranges(canonical)
    paragraph_candidates: list[list[tuple[int, int]]] = []
    for paragraph in re.finditer(r"[^\n]+", canonical):
        start, end = paragraph.span()
        prioritized: list[tuple[int, int]] = []
        prioritized.extend((item["start"], item["end"]) for item in protected if start <= item["start"] and item["end"] <= end)
        prioritized.extend((left, right) for left, right in formula_ranges if start <= left and right <= end)
        for pattern in (
            r"《([^《》\n]+)》",
            r"“([^“”\n]{1,24})”",
            r"‘([^‘’\n]{1,24})’",
            r"((?:[\u4e00-\u9fff]、){2,}[\u4e00-\u9fff])",
        ):
            for match in re.finditer(pattern, canonical[start:end]):
                prioritized.append((start + match.start(1), start + match.end(1)))
        term_ranges = {(item["start"], item["end"]) for item in protected}
        prioritized.sort(key=lambda bounds: (0 if bounds in term_ranges else 1, bounds[0], bounds[1]))
        accepted: list[tuple[int, int]] = []
        for left, right in prioritized:
            try:
                validate_boundaries(left, right, protected)
            except ValueError:
                continue
            if not any(left < other_right and right > other_left for other_left, other_right in accepted):
                accepted.append((left, right))
        for match in re.finditer(r"[\u4e00-\u9fff]{2,8}|[A-Za-z][A-Za-z'-]*|\d+(?:\.\d+)?", canonical[start:end]):
            left, right = start + match.start(), start + match.end()
            try:
                validate_boundaries(left, right, protected)
            except ValueError:
                continue
            if not any(left < other_right and right > other_left for other_left, other_right in accepted):
                accepted.append((left, right))
        paragraph_candidates.append(accepted[:per_paragraph])
    # Keep complete glossary terms ahead of the bounded general hints.
    chosen: list[tuple[int, int]] = [(item["start"], item["end"]) for item in protected[:max_candidates]]
    if len(chosen) == max_candidates:
        return [
            {"start": left, "end": right, "text": canonical[left:right], "role": "待判断"}
            for left, right in chosen
        ]
    selected = set(chosen)
    # Give later paragraphs a chance before filling the remaining budget.
    for index in range(per_paragraph):
        for group in paragraph_candidates:
            if index < len(group):
                if group[index] in selected:
                    continue
                chosen.append(group[index])
                selected.add(group[index])
                if len(chosen) == max_candidates:
                    return [
                        {"start": left, "end": right, "text": canonical[left:right], "role": "待判断"}
                        for left, right in sorted(chosen)
                    ]
    return [
        {"start": left, "end": right, "text": canonical[left:right], "role": "待判断"}
        for left, right in sorted(chosen)
    ]


def validate_spans(text: str, spans: list[dict[str, Any]], *, source_sha256: str | None = None, terms: list[str] | None = None) -> list[dict[str, Any]]:
    canonical = normalize_text(text)
    digest = text_sha256(canonical)
    protected = glossary_matches(canonical, terms or [])
    if source_sha256 and source_sha256 != digest:
        raise ValueError("source_sha256 与规范化后的源文本不一致")
    normalized: list[dict[str, Any]] = []
    previous_end = -1
    seen_ids: set[str] = set()
    for index, raw in enumerate(spans, start=1):
        start, end = raw.get("start"), raw.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end > len(canonical) or start >= end:
            raise ValueError(f"span-{index} 的区间无效")
        validate_boundaries(start, end, protected)
        if start < previous_end:
            raise ValueError("span 不允许重叠")
        span_id = str(raw.get("id") or f"span-{index}")
        if span_id in seen_ids:
            raise ValueError(f"span id 重复：{span_id}")
        seen_ids.add(span_id)
        item = dict(raw)
        item.update({"id": span_id, "text": canonical[start:end], "source_sha256": digest})
        normalized.append(item)
        previous_end = end
    return normalized


def validate_inline_spans(text: str, spans: list[dict[str, int]]) -> None:
    """Validate ordered, non-overlapping Python character offsets in unmodified text."""
    previous_end = 0
    if not spans:
        raise ValueError("加粗区间不能为空")
    for span in spans:
        start, end = span.get("start"), span.get("end")
        if (type(start) is not int or type(end) is not int or
                start < previous_end or start >= end or end > len(text)):
            raise ValueError("加粗区间越界、无序或重叠")
        previous_end = end


def markdown_bold_spans(text: str, spans: list[dict[str, int]]) -> str:
    """Insert Markdown emphasis after checking offsets against the original text."""
    validate_inline_spans(text, spans)
    result = []
    cursor = 0
    for span in spans:
        result.extend((text[cursor:span["start"]], "**", text[span["start"]:span["end"]], "**"))
        cursor = span["end"]
    result.append(text[cursor:])
    return "".join(result)


def whole_word_spans(text: str, words: list[str]) -> list[dict[str, int]]:
    """Find complete occurrences of supplied forms without matching inside derivatives."""
    forms = sorted({word for word in words if word}, key=len, reverse=True)
    if not forms:
        return []
    pattern = re.compile(r"(?<![\w])(?:" + "|".join(re.escape(word) for word in forms) + r")(?![\w])", re.I)
    return [{"start": match.start(), "end": match.end()} for match in pattern.finditer(text)]


def apply_operations(text: str, spans: list[dict[str, Any]], operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current = [dict(item) for item in spans]
    for operation in operations:
        op = operation.get("op")
        if op == "add":
            existing_ids = {item.get("id") for item in current}
            next_id = 1
            while f"span-{next_id}" in existing_ids:
                next_id += 1
            current.append({
                "id": f"span-{next_id}",
                "start": operation.get("start"),
                "end": operation.get("end"),
                "role": operation.get("role", "记忆要点"),
            })
        elif op == "delete":
            target = operation.get("span_id")
            current = [item for item in current if item.get("id") != target]
        elif op == "adjust":
            target = operation.get("span_id")
            found = False
            for item in current:
                if item.get("id") == target:
                    item["start"], item["end"] = operation.get("start"), operation.get("end")
                    found = True
                    break
            if not found:
                raise ValueError(f"找不到待调整的 span：{target}")
        else:
            raise ValueError(f"不支持的 span 操作：{op}")
        current = [item for item in current if item.get("start") != item.get("end")]
        current.sort(key=lambda item: (item.get("start", -1), item.get("end", -1)))
        current = validate_spans(text, current)
    return current


def masked_text(text: str, spans: list[dict[str, Any]], replacement: str = "____") -> str:
    result: list[str] = []
    cursor = 0
    for span in spans:
        result.append(text[cursor:span["start"]])
        result.append(replacement)
        cursor = span["end"]
    result.append(text[cursor:])
    return "".join(result)


def validate_masked_text(text: str, spans: list[dict[str, Any]], rendered: str) -> None:
    """Ensure the published cloze text is generated from exactly these spans."""
    expected = masked_text(text, spans)
    if rendered != expected:
        raise ValueError("挖空文本与已验证的 span 集合不一致")


def cloze_alignment(text: str, spans: list[dict[str, Any]], replacement: str = "____") -> dict[str, Any]:
    """Render a cloze text and record the one-to-one span placeholders."""
    canonical = normalize_text(text)
    items: list[dict[str, Any]] = []
    for index, span in enumerate(spans, start=1):
        items.append({
            "span_id": span["id"],
            "span_text": canonical[span["start"]:span["end"]],
            "start": span["start"],
            "end": span["end"],
            "placeholder": replacement,
            "placeholder_index": index,
        })
    return {"pass": True, "span_count": len(spans), "placeholder_count": len(items), "items": items, "text": masked_text(canonical, spans, replacement)}


def validate_cloze_alignment(text: str, spans: list[dict[str, Any]], marked: str, masked: str, replacement: str = "____") -> dict[str, Any]:
    """Ensure every marked span has exactly one ordered plain-text placeholder."""
    canonical = normalize_text(text)
    expected_marked = marked_text(canonical, spans)
    alignment = cloze_alignment(canonical, spans, replacement)
    if marked != expected_marked:
        raise ValueError("标记文本与已验证的 span 集合不一致")
    if masked != alignment["text"]:
        raise ValueError("挖空文本与已验证的 span 集合不一致")
    if alignment["span_count"] != alignment["placeholder_count"] or masked.count(replacement) != alignment["placeholder_count"]:
        raise ValueError("记忆 span 数量与下划线占位符数量不一致")
    return alignment


def marked_text(text: str, spans: list[dict[str, Any]]) -> str:
    result: list[str] = []
    cursor = 0
    for span in spans:
        result.append(text[cursor:span["start"]])
        result.append("「" + text[span["start"]:span["end"]] + "」")
        cursor = span["end"]
    result.append(text[cursor:])
    return "".join(result)


def classical_clauses(text: str) -> list[dict[str, Any]]:
    """Split recitation text into nonempty clause bodies, keeping punctuation outside."""
    canonical = normalize_text(text)
    clauses: list[dict[str, Any]] = []

    def add_clause(start: int, end: int) -> None:
        edge_quotes = "“”‘’「」『』"
        while start < end and (canonical[start].isspace() or canonical[start] in edge_quotes):
            start += 1
        while end > start and (canonical[end - 1].isspace() or canonical[end - 1] in edge_quotes):
            end -= 1
        if start < end:
            clauses.append({"id": len(clauses) + 1, "start": start, "end": end, "text": canonical[start:end]})

    start = 0
    for match in re.finditer(r"[，,；;。！？!?\n]", canonical):
        add_clause(start, match.start())
        start = match.end()
    add_clause(start, len(canonical))
    return clauses


def classical_spans(text: str, selected_clause_ids: list[int]) -> list[dict[str, Any]]:
    """Expand selected clause IDs to full spans, rejecting duplicates and bad IDs."""
    clauses = classical_clauses(text)
    if not selected_clause_ids or len(selected_clause_ids) != len(set(selected_clause_ids)):
        raise ValueError("古诗文须选择至少一个不重复的分句编号")
    by_id = {item["id"]: item for item in clauses}
    if any(item not in by_id for item in selected_clause_ids):
        raise ValueError("古诗文分句编号不存在")
    return [{"start": by_id[item]["start"], "end": by_id[item]["end"], "role": "背诵分句"} for item in sorted(selected_clause_ids)]


def validate_classical_spans(text: str, spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Require every selected span to equal one complete clause body."""
    expected = {(item["start"], item["end"]) for item in classical_clauses(text)}
    failures = ["古诗文 span 必须覆盖整个分句正文，且不得包含标点"] if any(
        (span["start"], span["end"]) not in expected for span in spans
    ) else []
    if not spans:
        failures.append("古诗文至少要挖空一个分句")
    if any(re.fullmatch(r"【[^】\n]+】", normalize_text(text)[span["start"]:span["end"]]) for span in spans):
        failures.append("标题或段落标签不应被标记")
    return {"pass": not failures, "main_clause_preserved": False, "overmarking": False, "failures": failures, "sentence_checks": []}


@lru_cache(maxsize=1)
def inference_leakage_rules() -> tuple[dict[str, str], ...]:
    """Load reusable, high-confidence answer-to-clue relations."""
    path = Path(__file__).resolve().parents[1] / "references" / "cloze-inference-rules.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    rules = data["rules"]
    for rule in rules:
        if not all(isinstance(rule.get(key), str) and rule[key] for key in ("id", "answer", "clue_pattern", "description")):
            raise ValueError("挖空推断规则缺少必要字段")
        if "clue" not in re.compile(rule["clue_pattern"]).groupindex:
            raise ValueError(f"挖空推断规则没有 clue 捕获组：{rule['id']}")
    return tuple(rules)


def validate_cloze_quality(text: str, spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Run conservative, deterministic checks against over-marking.

    The check deliberately does not attempt general Chinese grammatical
    parsing. It catches a few unambiguous naming and answer-leakage patterns
    as well as headings and excessive sentence coverage.
    """
    canonical = normalize_text(text)
    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    covered = sorted((span["start"], span["end"]) for span in spans)
    formula_ranges = math_expression_ranges(canonical)

    for match in re.finditer(r"【[^】\n]+】", canonical):
        if any(start < match.end() and end > match.start() for start, end in covered):
            failures.append("标题或段落标签不应被标记")

    boundaries = list(re.finditer(r"[^。！？；\n]+[。！？；]?", canonical))
    for index, match in enumerate(boundaries, start=1):
        start, end = match.span()
        sentence_spans = [(span_start, span_end) for span_start, span_end in covered if span_start < end and span_end > start]
        length = end - start
        marked = sum(max(0, min(end, span_end) - max(start, span_start)) for span_start, span_end in covered)
        remaining_parts: list[str] = []
        cursor = start
        for span_start, span_end in covered:
            if span_end <= start or span_start >= end:
                continue
            left = max(start, span_start)
            right = min(end, span_end)
            remaining_parts.append(canonical[cursor:left])
            cursor = right
        remaining_parts.append(canonical[cursor:end])
        remaining = "".join(remaining_parts)
        remaining_content = re.sub(r"[\s，。！？；：、“”‘’（）()【】\[\]]+", "", remaining)
        ratio = marked / length if length else 0.0
        sentence_span_count = len(sentence_spans)
        # Several short, independent answer units can legitimately occupy a
        # large share of a sentence. A single large span is a stronger signal
        # that the sentence frame itself was selected, so keep its stricter
        # threshold while allowing a bounded amount of multi-span coverage.
        # A complete relation may be slightly longer than 40% when a readable
        # subject/modal frame and an unblanked naming conclusion remain.
        relation_exception = False
        if sentence_span_count == 1 and 0.4 < ratio <= 0.5:
            span_start, span_end = sentence_spans[0]
            before = canonical[start:span_start]
            after = canonical[span_end:end]
            relation_exception = bool(
                len(re.sub(r"[\s，,：:]", "", before)) >= 6
                and re.search(r"(?:总要|必须|应当|应该|能够|可以|会)$", before)
                and re.match(r"[，,]这就是[^。！？；]+[。！？；]?$", after)
            )
        single_span_overreach = sentence_span_count == 1 and ratio > 0.4 and not relation_exception
        multi_span_overreach = sentence_span_count >= 2 and ratio > 0.85
        sentence_failures: list[str] = []
        if ratio > 0.85 or single_span_overreach:
            sentence_failures.append(f"第 {index} 句标记密度过高")
        if not remaining_content:
            sentence_failures.append(f"第 {index} 句挖空后没有可读主干")

        for span_start, span_end in sentence_spans:
            if span_start < start or span_end > end:
                continue
            before = canonical[start:span_start]
            after = canonical[span_end:end]
            answer = canonical[span_start:span_end]
            if any(
                span_start < formula_end and span_end > formula_start
                and (span_start > formula_start or span_end < formula_end)
                for formula_start, formula_end in formula_ranges
            ):
                sentence_failures.append(f"第 {index} 句的公式表达式被拆断")
            if (before.endswith("这就是") or before.endswith("这种现象叫作")) and re.match(r"^[。！？；]?$", after):
                sentence_failures.append(f"第 {index} 句的命名结论被挖空")
            # Use the full following text so a semicolon does not hide a
            # comparison that immediately repeats the answer.
            following = canonical[span_end:]
            repeated = re.match(rf"^(?:有关|相关)[，,；;]\s*({re.escape(answer)})越", following)
            repeat_start = span_end + repeated.start(1) if repeated else -1
            repeat_end = span_end + repeated.end(1) if repeated else -1
            repeat_hidden = any(left <= repeat_start and right >= repeat_end for left, right in covered)
            if repeated and not repeat_hidden:
                sentence_failures.append(f"第 {index} 句的后续比较直接泄露挖空答案")
            for rule in inference_leakage_rules():
                if answer != rule["answer"]:
                    continue
                for clue in re.finditer(rule["clue_pattern"], canonical[start:end]):
                    clue_start = start + clue.start("clue")
                    clue_end = start + clue.end("clue")
                    if not any(left <= clue_start and right >= clue_end for left, right in covered):
                        sentence_failures.append(f"第 {index} 句的原因判据直接泄露挖空答案：{rule['description']}")
                        break
        checks.append({"sentence": index, "marked_ratio": round(ratio, 3), "main_clause_remaining": bool(remaining_content), "pass": not sentence_failures})
        failures.extend(sentence_failures)

    return {
        "pass": not failures,
        "main_clause_preserved": not any("主干" in item or "命名结论" in item for item in failures),
        "overmarking": any("密度" in item for item in failures),
        "failures": failures,
        "sentence_checks": checks,
    }
