"""Bounded contextual 地 -> 的 request rewrites, shared by speech consumers.

Only unambiguous surface patterns are automatic. Other occurrences require a
small decision, never a guessed global substitution. All offsets are characters.
"""
from __future__ import annotations

import copy
import hashlib
import re
import unicodedata

from jsonschema import ValidationError, validate

VERSION = 1
DECISION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "decisions"],
    "properties": {
        "schema_version": {"const": 1},
        "decisions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["candidate_id", "action"],
            "properties": {"candidate_id": {"type": "string"},
                           "action": {"enum": ["replace", "keep", "uncertain"]}},
        }},
    },
}
# Protect lexical di4 uses before considering adverbial patterns.
DI_WORDS = (
    "土地", "大地", "地球", "地面", "地下", "地上", "地理", "地质", "地震",
    "地址", "地点", "地区", "地域", "地方", "地图", "地板", "地铁", "地狱",
    "地毯", "地基", "地形", "地势", "地貌", "地表", "地壳", "地层", "地带",
    "地道", "地位", "地步", "境地", "天地", "当地", "本地", "异地", "外地",
    "各地", "内地", "实地", "就地", "落地", "着地", "遍地", "满地", "耕地",
    "田地", "草地", "空地", "场地", "基地", "阵地", "领地", "目的地",
)
# These patterns generalize over following actions; ambiguous expressions such as
# "认真地理研究" are deliberately left for semantic review.
ADVERBS = (
    "欢喜", "高兴", "开心", "愉快", "快乐", "轻轻", "慢慢", "悄悄", "静静",
    "默默", "紧紧", "深深", "牢牢", "渐渐", "逐渐", "认真", "仔细", "努力",
    "耐心", "小心", "迅速", "缓缓", "不断", "不停", "坚定", "清楚", "清晰",
)
ACTIONS = ("笑", "说", "问", "答", "走", "跑", "看", "听", "读", "写", "学", "做",
           "想", "上台", "上楼", "下楼", "下台", "理解", "点头", "摇头", "回答", "解释", "检查", "等待", "完成", "工作", "观察")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scan(text: str) -> dict:
    protected, lexical_starts, lexical_internal = set(), set(), set()
    for word in DI_WORDS:
        for match in re.finditer(re.escape(word), text):
            protected.update(i for i in range(match.start(), match.end()) if text[i] == "地")
            if word.startswith("地"):
                lexical_starts.add(match.start())
            lexical_internal.update(i for i in range(match.start() + 1, match.end()) if text[i] == "地")
    candidates = []
    for index, char in enumerate(text):
        if char != "地":
            continue
        before, after = text[:index], text[index + 1:]
        action, rule = "uncertain", "semantic"
        if any(before.endswith(word) for word in ADVERBS) and any(after.startswith(word) for word in ACTIONS):
            action, rule = "replace", "adverbial_action"
        elif index not in lexical_internal and index in lexical_starts and before and re.fullmatch(r"[\u4e00-\u9fff]", before[-1]) and not (
            before.endswith((*DI_WORDS, "的", "在", "到", "从", "和", "与", "是", "有"))
        ):
            # "积极地上台" must not be swallowed by the lexical "地上".
            # Unknown cross-word boundaries need context, even inside a dictionary hit.
            pass
        elif index in protected:
            action, rule = "keep", "lexical_di"
        candidates.append({"candidate_id": f"de-{len(candidates) + 1:04d}",
                           "offset": index, "context": text[max(0, index - 24):index + 25],
                           "action": action, "rule": rule})
    return {"schema_version": VERSION, "source_sha256": digest(text), "candidates": candidates}


def template(packet: dict) -> dict:
    return {"schema_version": VERSION, "decisions": [
        {"candidate_id": c["candidate_id"], "action": "uncertain"}
        for c in packet["candidates"] if c["rule"] == "semantic"]}


def apply_decisions(packet: dict, response: dict) -> dict:
    try:
        validate(response, DECISION_SCHEMA)
    except ValidationError as exc:
        raise ValueError("语境决策 Schema 无效：" + exc.message) from exc
    expected = {c["candidate_id"] for c in packet["candidates"] if c["rule"] == "semantic"}
    ids = [d["candidate_id"] for d in response["decisions"]]
    if len(ids) != len(set(ids)) or set(ids) != expected:
        raise ValueError("语境决策必须恰好覆盖全部语义候选，不得重复或增加候选")
    result = copy.deepcopy(packet)
    actions = {d["candidate_id"]: d["action"] for d in response["decisions"]}
    for candidate in result["candidates"]:
        if candidate["candidate_id"] in actions:
            candidate["action"] = actions[candidate["candidate_id"]]
    return result


def validate_packet(text: str, packet: dict, *, require_resolved: bool = True) -> None:
    if packet.get("schema_version") != VERSION or packet.get("source_sha256") != digest(text):
        raise ValueError("语境规则版本或源文本指纹不一致")
    original = scan(text)
    if apply_decisions(original, {"schema_version": VERSION, "decisions": [
        {"candidate_id": c["candidate_id"], "action": c["action"]}
        for c in packet["candidates"] if c["rule"] == "semantic"]}) != packet:
        raise ValueError("语境候选范围或自动判断被修改")
    for candidate in packet["candidates"]:
        if require_resolved and candidate["action"] == "uncertain":
            raise ValueError("存在未确定的地字语境，请确认候选")


def rewrite(text: str, packet: dict) -> str:
    validate_packet(text, packet)
    chars = list(text)
    for candidate in packet["candidates"]:
        if candidate["action"] == "replace":
            chars[candidate["offset"]] = "的"
    return "".join(chars)


def restore_items(items: list[dict], source: str, request: str) -> list[dict]:
    """Restore text while retaining service times; fail on ambiguous alignment."""
    if source == request:
        return items
    result, cursor = copy.deepcopy(items), 0
    for item in result:
        word = str(item["text"])
        start = request.find(word, cursor)
        if start < 0:
            raise ValueError("改写请求的词级时间戳无法映射回原文")
        # Cover all spoken characters. Skipping an unchanged repeated word can
        # make an earlier 的 boundary indistinguishable from the rewritten 地.
        if any(not (c.isspace() or unicodedata.category(c).startswith("P")) for c in request[cursor:start]):
            raise ValueError("词级时间戳遗漏字符或存在重复词对齐歧义")
        item["text"] = source[start:start + len(word)]
        cursor = start + len(word)
    if any(not (c.isspace() or unicodedata.category(c).startswith("P")) for c in request[cursor:]):
        raise ValueError("词级时间戳遗漏了末尾字符")
    return result
