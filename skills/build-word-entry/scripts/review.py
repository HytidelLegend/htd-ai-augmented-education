"""Stable, compact review packets for source-backed lexical candidates."""
from __future__ import annotations

import hashlib


def candidate_id(prefix: str, lemma: str, source: int, group: int, target: str, hint: str = "") -> str:
    key = f"{lemma}|{source}|{group}|{target.casefold()}|{hint}"
    return prefix + "_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def review_packet(lemma: str, evidence: list[dict]) -> dict:
    relations: dict[str, dict] = {}
    derivatives: dict[str, dict] = {}
    for source_index, source in enumerate(evidence):
        groups = {group["groupIndex"]: group for group in source.get("relationGroups", [])}
        for candidate in source.get("externalCandidates", []):
            group_index = candidate["groupIndex"]
            hint = candidate["relationHint"]
            key = candidate_id("rc", lemma, source_index, group_index, candidate["word"], hint)
            group = groups.get(group_index, {})
            relations[key] = {"candidateId": key, "source": source_index, "groupIndex": group_index,
                              "targetLemma": candidate["word"], "sourceGloss": group.get("gloss", ""),
                              "sourcePartOfSpeech": group.get("partOfSpeech", ""),
                              "sourceHint": hint, "fragment": candidate["fragment"]}
        for candidate in source.get("derivativeCandidates", []):
            target = candidate["word"].casefold()
            key = candidate_id("dc", lemma, source_index, 0, target)
            derivatives[key] = {"candidateId": key, "source": source_index, "targetLemma": target,
                                "sourceHint": candidate.get("relationHint", "word_family"),
                                "fragment": candidate["fragment"]}
    return {"relations": list(relations.values()), "derivatives": list(derivatives.values())}


def validate_decisions(packet: dict, proposal: dict) -> None:
    for packet_key, response_key in (("relations", "relationDecisions"),
                                     ("derivatives", "derivativeDecisions")):
        expected = {candidate["candidateId"] for candidate in packet[packet_key]}
        decisions = proposal.get(response_key, [])
        actual = [decision["candidateId"] for decision in decisions]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError(f"{response_key} 必须逐项覆盖审查模板，且不能重复")
    relations = {candidate["candidateId"]: candidate for candidate in packet["relations"]}
    for decision in proposal.get("relationDecisions", []):
        if decision["action"] != "accept":
            if "type" in decision:
                raise ValueError("未接受的关系不得填写类型")
            continue
        hint = relations[decision["candidateId"]]["sourceHint"]
        kind = decision["type"]
        if (hint == "antonym") != (kind == "antonym"):
            raise ValueError("关系分类与来源同反义分组冲突")
