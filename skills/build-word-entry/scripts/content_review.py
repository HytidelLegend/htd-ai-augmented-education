"""Prepare and validate compact, evidence-bound content review decisions."""
from __future__ import annotations

import json

from utils.scripts.structured_io import json_digest
from utils.scripts.timestamp import iso_timestamp


def digest(value: object) -> str:
    return json_digest(value)


def reviewable_items(entry: dict) -> list[dict]:
    rows = []
    for sense in entry["senses"]:
        values = [("definitionZh", sense["definitionZh"]),
                  ("definitionEn", sense["definitionEn"])]
        for group in ("examples", "collocations", "phrases"):
            for usage in sense[group]:
                values.extend(((group, usage), (f"{group}.translationZh", usage["translationZh"])))
        for field, value in values:
            rows.append({"senseId": sense["senseId"], "itemId": value["itemId"],
                         "field": field, "text": value["text"],
                         "generationMethod": value["generationMethod"],
                         "confidence": value.get("confidence"),
                         "sourceRefs": value["sourceRefs"]})
    return rows


def prepare(entry: dict, evidence_digest: str) -> dict:
    rows = reviewable_items(entry)
    return {"evidenceDigest": evidence_digest, "candidateDigest": digest(entry),
            "senses": [{"senseId": sense["senseId"],
                        "items": [row for row in rows if row["senseId"] == sense["senseId"]]}
                       for sense in entry["senses"]]}


def packet_items(packet: dict) -> list[dict]:
    return [item for sense in packet["senses"] for item in sense["items"]]


def apply(entry: dict, packet: dict, decision: dict, run_id: str,
          reviewed_at: str | None = None) -> list[dict]:
    if decision["evidenceDigest"] != packet["evidenceDigest"] or decision["candidateDigest"] != packet["candidateDigest"]:
        raise ValueError("内容审查未绑定本次证据与候选词条")
    if digest(entry) != packet["candidateDigest"] or prepare(entry, packet["evidenceDigest"]) != packet:
        raise ValueError("内容审查后候选词条已改变")
    senses = {sense["senseId"] for sense in entry["senses"]}
    approved = decision["approvedSenseIds"]
    deferred = decision["deferredItemIds"]
    items = {item["itemId"]: item for item in packet_items(packet)}
    if len(approved) != len(set(approved)) or not set(approved) <= senses:
        raise ValueError("内容审查的义项 ID 重复或无效")
    if len(deferred) != len(set(deferred)) or not set(deferred) <= set(items):
        raise ValueError("内容审查的暂缓内容 ID 重复或无效")
    if any(items[item_id]["senseId"] not in approved for item_id in deferred):
        raise ValueError("暂缓内容所属义项未被审查")
    if set(approved) != senses:
        raise ValueError("内容审查必须覆盖所有义项")
    by_id = {}
    review_time = reviewed_at or iso_timestamp()
    for sense in entry["senses"]:
        for value in (sense["definitionZh"], sense["definitionEn"]):
            by_id[value["itemId"]] = value
        for group in ("examples", "collocations", "phrases"):
            for usage in sense[group]:
                by_id[usage["itemId"]] = usage
                by_id[usage["translationZh"]["itemId"]] = usage["translationZh"]
    gaps = []
    for item_id, row in items.items():
        value = by_id[item_id]
        if item_id in deferred:
            if value["verificationStatus"] == "human_passed":
                raise ValueError("已人工核验内容不能暂缓")
            value["verificationStatus"] = "pending"
            value.pop("verificationRef", None)
            gaps.append({"field": row["field"], "itemId": item_id,
                         "status": "pending_review", "reason": "agent_deferred"})
            continue
        if value["verificationStatus"] == "human_passed":
            continue
        value["verificationStatus"] = "agent_passed"
        value["verificationRef"] = {"runId": run_id,
            "decisionId": "cr_" + digest([run_id, item_id, packet["evidenceDigest"], packet["candidateDigest"]])[:20],
            "evidenceDigest": packet["evidenceDigest"],
            "candidateDigest": packet["candidateDigest"], "reviewedAt": review_time}
    return gaps


def verify(entry: dict, packet: dict, decision: dict, original: dict, run_id: str,
           reviewed_at: str) -> list[dict]:
    if prepare(original, packet["evidenceDigest"]) != packet:
        raise ValueError("内容审查模板与候选快照不一致")
    expected = json.loads(json.dumps(original))
    gaps = apply(expected, packet, decision, run_id, reviewed_at)
    actual = {row["itemId"]: row for row in reviewable_items(entry)}
    projected = {row["itemId"]: row for row in reviewable_items(expected)}
    if actual != projected:
        raise ValueError("已发布内容与审查候选不一致")
    def values(value: dict) -> dict:
        result = {}
        for sense in value["senses"]:
            for item in (sense["definitionZh"], sense["definitionEn"]):
                result[item["itemId"]] = item
            for group in ("examples", "collocations", "phrases"):
                for usage in sense[group]:
                    result[usage["itemId"]] = usage
                    result[usage["translationZh"]["itemId"]] = usage["translationZh"]
        return result
    actual_values, expected_values = values(entry), values(expected)
    if actual_values.keys() != expected_values.keys():
        raise ValueError("已发布内容 ID 与审查决定不一致")
    for item_id, value in actual_values.items():
        predicted = expected_values[item_id]
        if value["verificationStatus"] != predicted["verificationStatus"]:
            raise ValueError(f"内容审查状态与决定不一致：{item_id}")
        if value["verificationStatus"] == "agent_passed":
            ref, expected_ref = value.get("verificationRef"), predicted["verificationRef"]
            if not ref or any(ref.get(key) != expected_ref[key] for key in
                              ("runId", "decisionId", "evidenceDigest", "candidateDigest", "reviewedAt")):
                raise ValueError(f"内容审查引用无效：{item_id}")
    return gaps
