"""Deterministic word-entry construction, validation, and publication."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator

from utils.scripts.dictionary_records import entries as entry_records
from utils.scripts.structured_io import read_json
from utils.scripts.span_ops import markdown_bold_spans, validate_inline_spans, whole_word_spans
from utils.scripts.timestamp import iso_timestamp
from utils.scripts.word_similarity import meets_subsequence_threshold
from review import candidate_id as candidate_id_fn


def normalize_word(word: str) -> str:
    value = unicodedata.normalize("NFKC", word).strip().casefold()
    value = re.sub(r"\s+", " ", value)
    if not value or len(value) > 100 or not re.fullmatch(r"[a-z][a-z '\-]*", value):
        raise ValueError(f"无效英文单词：{word!r}")
    return value


def stable_word_id(word: str) -> str:
    return "w_" + hashlib.sha256(normalize_word(word).encode("utf-8")).hexdigest()[:20]


def new_id(prefix: str) -> str:
    return prefix + "_" + uuid4().hex[:20]


def source_ref(fragment: dict, source: dict) -> dict:
    return {
        "site": source["site"], "url": source["url"],
        "locator": fragment["locator"], "collectedAt": source["collectedAt"],
        "summary": fragment["summary"][:160],
    }


def item(text: str, *, refs: list[dict] | None = None, confidence: object | None = None,
         item_id: str | None = None, source_supported: bool | None = None) -> dict:
    supported = bool(refs) if source_supported is None else source_supported
    value = {"itemId": item_id or new_id("i"), "text": text.strip(),
             "generationMethod": "source_supported" if supported else "ai_generated",
             "verificationStatus": "pending", "sourceRefs": refs or []}
    if supported and not refs:
        raise ValueError("来源支持项缺少具体来源定位")
    if supported and confidence is not None:
        raise ValueError("来源支持项不能填写 AI 置信度")
    if confidence is not None:
        try:
            number = Decimal(str(confidence))
        except InvalidOperation as exc:
            raise ValueError("confidence 不是数值") from exc
        if number < 0 or number > 1 or number.as_tuple().exponent < -2:
            raise ValueError("confidence 必须在 0.00–1.00 且最多两位小数")
        value["confidence"] = number.quantize(Decimal("0.00"))
    if value["generationMethod"] == "ai_generated" and "confidence" not in value:
        raise ValueError("AI 内容必须提供 confidence")
    return value


def _retain_id(old_items: list[dict], candidate: dict) -> dict:
    for old in old_items:
        if old.get("text", "").casefold() == candidate["text"].casefold():
            candidate["itemId"] = old["itemId"]
            if old.get("verificationStatus") == "human_passed":
                return old
            break
    return candidate


def build_entry(word: str, proposal: dict, evidence: list[dict], existing: dict | None = None,
                schema_version: str = "1.2") -> dict:
    word = normalize_word(word)
    now = iso_timestamp()
    if not proposal.get("senses"):
        raise ValueError("至少需要一个义项")
    old_senses = existing.get("senses", []) if existing else []
    legacy_updates = {value["itemId"]: value for value in proposal.get("legacyUsageUpdates", [])}
    if len(legacy_updates) != len(proposal.get("legacyUsageUpdates", [])):
        raise ValueError("旧内容译文更新重复")
    used_legacy_updates: set[str] = set()
    senses = []
    matched_old_ids = set()
    for raw in proposal["senses"]:
        pos = str(raw.get("partOfSpeech", "")).strip()
        if not pos:
            raise ValueError("义项缺少词性")
        def resolve_refs(pointers: list[dict]) -> list[dict]:
            values = []
            for pointer in pointers:
                source = evidence[pointer["source"]]
                fragment = source["fragments"][pointer["fragment"]]
                values.append(source_ref(fragment, source))
            return values
        refs = resolve_refs(raw.get("evidence", []))
        source_orders = []
        for pointer in [*raw.get("evidence", []), *raw.get("alignmentEvidence", [])]:
            source = evidence[pointer["source"]]
            order = next((index for index, candidate in enumerate(source.get("senseCandidates", []))
                          if candidate["fragment"] == pointer["fragment"]), None)
            if order is None and pointer in raw.get("alignmentEvidence", []):
                raise ValueError("义项对齐指针必须指向来源义项候选")
            if order is not None:
                value = {"site": source["site"], "ordinal": order}
                if value not in source_orders:
                    source_orders.append(value)
        old = next((s for s in old_senses if s.get("senseId") == raw.get("senseId")), None)
        if raw.get("senseId") and old is None:
            raise ValueError("未知 senseId；新义项不得自行指定已有 ID")
        if old is None:
            for candidate in old_senses:
                if candidate["senseId"] in matched_old_ids or candidate["partOfSpeech"].casefold() != pos.casefold():
                    continue
                same_definition = candidate["definitionEn"]["text"].casefold().strip() == str(raw["definitionEn"]).casefold().strip()
                prior_refs = {(r["url"], r["locator"]) for r in candidate["definitionEn"]["sourceRefs"]}
                new_refs = {(r["url"], r["locator"]) for r in refs}
                if same_definition or prior_refs & new_refs:
                    old = candidate
                    break
        if old and old["senseId"] in matched_old_ids:
            raise ValueError("本次多个义项映射到同一个旧 senseId")
        if old:
            matched_old_ids.add(old["senseId"])
            source_orders = [*old["sourceOrders"], *(order for order in source_orders if order not in old["sourceOrders"])]
        def text_item(text: str, supported: bool, confidence: object | None, pointers: list[dict] | None) -> dict:
            item_refs = resolve_refs(pointers) if pointers is not None else (refs if supported else [])
            if supported and not item_refs:
                raise ValueError("来源支持项缺少具体来源定位")
            return item(text, refs=item_refs, confidence=confidence, source_supported=supported)
        def tag_item(raw_tag: str | dict) -> dict:
            if isinstance(raw_tag, str):
                return text_item(raw_tag, True, None, None)
            return text_item(raw_tag["text"], bool(raw_tag.get("sourceSupported", False)),
                             raw_tag.get("confidence"), raw_tag.get("evidence"))
        sense = {"senseId": old["senseId"] if old else new_id("s"), "partOfSpeech": pos,
                 "sourceOrders": source_orders,
                 "definitionZh": text_item(raw["definitionZh"], bool(raw.get("zhSourceSupported")), raw.get("zhConfidence"), raw.get("zhEvidence")),
                 "definitionEn": text_item(raw["definitionEn"], bool(raw.get("enSourceSupported")), raw.get("enConfidence"), raw.get("enEvidence")),
                 "grammarTags": [tag_item(tag) for tag in raw.get("grammarTags", [])],
                 "registerTags": [tag_item(tag) for tag in raw.get("registerTags", [])],
                 "examples": [], "collocations": [], "phrases": []}
        for group in ("examples", "collocations", "phrases"):
            for raw_item in raw.get(group, []):
                value = text_item(raw_item["text"], bool(raw_item.get("sourceSupported")),
                                  raw_item.get("confidence"), raw_item.get("evidence"))
                value = _retain_id(old.get(group, []) if old else [], value)
                old_value = next((prior for prior in old.get(group, []) if prior["itemId"] == value["itemId"]), None) if old else None
                if schema_version in ("1.3", "1.4"):
                    translated = raw_item.get("translationZh")
                    if translated and translated.get("text"):
                        if translated.get("sourceSupported") and not any(
                                translated["text"] in evidence[pointer["source"]]["fragments"][pointer["fragment"]]["summary"]
                                for pointer in translated.get("evidence", [])):
                            raise ValueError("来源支持的中译与引用片段不一致")
                        translation = text_item(translated["text"], bool(translated.get("sourceSupported")),
                                                translated.get("confidence"), translated.get("evidence"))
                        translation["itemId"] = "i_" + hashlib.sha256(
                            f"{value['itemId']}|translationZh".encode("utf-8")).hexdigest()[:20]
                        old_translation = old_value.get("translationZh") if old_value else None
                        if old_translation and old_translation["verificationStatus"] == "human_passed":
                            if old_translation["text"] != translation["text"]:
                                raise ValueError("新译文与人工核验译文冲突")
                            translation = old_translation
                        value["translationZh"] = translation
                    elif old_value and old_value.get("translationZh"):
                        value["translationZh"] = old_value["translationZh"]
                    else:
                        raise ValueError(f"{group} 缺少中文翻译")
                    if group == "examples":
                        value["emphasis"] = raw_item.get("emphasis") or (old_value or {}).get("emphasis")
                        if not value["emphasis"]:
                            raise ValueError("例句缺少双语加粗区间")
                sense[group].append(value)
            if old:
                used_item_ids = {value["itemId"] for value in sense[group]}
                sense[group].extend(value for value in old[group] if value["itemId"] not in used_item_ids)
            if schema_version in ("1.3", "1.4"):
                for usage in sense[group]:
                    update = legacy_updates.get(usage["itemId"])
                    if not update:
                        continue
                    used_legacy_updates.add(usage["itemId"])
                    if usage.get("translationZh", {}).get("generationMethod") == "source_supported":
                        continue
                    translation = item(update["translationZh"], confidence=update["confidence"],
                                       item_id="i_" + hashlib.sha256(
                                           f"{usage['itemId']}|translationZh".encode("utf-8")).hexdigest()[:20],
                                       source_supported=False)
                    if usage.get("translationZh", {}).get("verificationStatus") == "human_passed":
                        if usage["translationZh"]["text"] != translation["text"]:
                            raise ValueError("旧人工译文不可被覆盖")
                    else:
                        usage["translationZh"] = translation
                    if group == "examples":
                        usage["emphasis"] = {
                            "en": whole_word_spans(usage["text"], [word, *(v["form"]["text"] for v in existing.get("inflections", []))] if existing else [word]),
                            "zh": update.get("zhSpans", []),
                        }
        if not sense["examples"]:
            raise ValueError("每个义项至少需要一条例句")
        if old:
            for group in ("grammarTags", "registerTags"):
                sense[group] = [_retain_id(old.get(group, []), value) for value in sense[group]]
                used_item_ids = {value["itemId"] for value in sense[group]}
                sense[group].extend(value for value in old.get(group, []) if value["itemId"] not in used_item_ids)
            for field in ("definitionZh", "definitionEn"):
                if old[field]["verificationStatus"] == "human_passed" and old[field]["text"] != sense[field]["text"]:
                    raise ValueError("新候选与人工核验的释义不同，需要质量复核")
                sense[field] = _retain_id([old[field]], sense[field])
        senses.append(sense)
    if existing:
        used = {sense["senseId"] for sense in senses}
        senses.extend(old for old in old_senses if old["senseId"] not in used)
    if schema_version in ("1.3", "1.4"):
        for sense in senses:
            for group in ("examples", "collocations", "phrases"):
                for usage in sense[group]:
                    update = legacy_updates.get(usage["itemId"])
                    if not update or usage["itemId"] in used_legacy_updates:
                        continue
                    used_legacy_updates.add(usage["itemId"])
                    if usage.get("translationZh", {}).get("generationMethod") == "source_supported":
                        continue
                    usage["translationZh"] = item(update["translationZh"], confidence=update["confidence"],
                        item_id="i_" + hashlib.sha256(f"{usage['itemId']}|translationZh".encode("utf-8")).hexdigest()[:20],
                        source_supported=False)
                    if group == "examples":
                        usage["emphasis"] = {"en": whole_word_spans(usage["text"], [word]),
                                             "zh": update.get("zhSpans", [])}
    if set(legacy_updates) != used_legacy_updates:
        raise ValueError("旧内容译文更新包含未匹配的 itemId")
    old_pronunciations = existing.get("pronunciations", []) if existing else []
    pronunciations = list(old_pronunciations)
    existing_pronunciation_ids = {value["pronunciationId"] for value in pronunciations}
    for source in evidence:
        for candidate in source.get("pronunciationCandidates", []):
            ipa = candidate["ipa"]
            pos = candidate.get("partOfSpeech", "")
            variety = candidate["variety"]
            digest = hashlib.sha256(f"{word}|{pos}|{variety}|{ipa}".encode("utf-8")).hexdigest()[:20]
            pronunciation_id = "p_" + digest
            ref = source_ref(source["fragments"][candidate["fragment"]], source)
            if pronunciation_id in existing_pronunciation_ids:
                prior = next(value for value in pronunciations if value["pronunciationId"] == pronunciation_id)
                for destination in (prior["sourceRefs"], prior["ipa"]["sourceRefs"]):
                    if not any(value["site"] == ref["site"] and value["locator"] == ref["locator"]
                               and value["url"] == ref["url"] for value in destination):
                        destination.append(ref)
                if candidate.get("audioUrl") and not prior.get("audioUrl"):
                    prior["audioUrl"] = candidate["audioUrl"]
                continue
            value = {"pronunciationId": pronunciation_id, "ipa": item(ipa, refs=[ref]),
                     "variety": variety, "partOfSpeech": pos, "sourceRefs": [ref]}
            if candidate.get("audioUrl"):
                value["audioUrl"] = candidate["audioUrl"]
            pronunciations.append(value)
            existing_pronunciation_ids.add(pronunciation_id)
    inflections = list(existing.get("inflections", []) if existing else [])
    existing_form_ids = {value["formId"] for value in inflections}
    for source in evidence:
        for candidate in source.get("inflectionCandidates", []):
            form = candidate["form"]
            kind = candidate["kind"]
            pos = candidate.get("partOfSpeech", "")
            digest = hashlib.sha256(f"{word}|{pos}|{kind}|{form}".encode("utf-8")).hexdigest()[:20]
            form_id = "f_" + digest
            ref = source_ref(source["fragments"][candidate["fragment"]], source)
            prior = next((value for value in inflections if value["formId"] == form_id or
                          value["kind"] == kind and value["form"]["text"].casefold() == form.casefold()), None)
            if prior:
                if prior["form"]["generationMethod"] == "rule_derived":
                    prior["form"]["generationMethod"] = "source_supported"
                    prior["partOfSpeech"] = pos
                    prior.pop("derivation", None)
                for destination in (prior["sourceRefs"], prior["form"]["sourceRefs"]):
                    if not any(value["site"] == ref["site"] and value["locator"] == ref["locator"]
                               and value["url"] == ref["url"] for value in destination):
                        destination.append(ref)
                continue
            value = {"formId": form_id, "form": item(form, refs=[ref]), "kind": kind,
                     "partOfSpeech": pos, "sourceRefs": [ref]}
            if kind in ("past tense", "past participle"):
                regular = {word + "ed", word + "d" if word.endswith("e") else word + "ed"}
                if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
                    regular.add(word[:-1] + "ied")
                value["irregular"] = form.casefold() not in regular
            elif kind in ("comparative", "superlative"):
                from adapters import regular_adjective_forms
                value["irregular"] = form.casefold() != regular_adjective_forms(word).get(kind)
            inflections.append(value)
            existing_form_ids.add(form_id)
    mappings = {}
    for mapping in proposal.get("relationSenseMappings", []):
        key = (mapping["source"], mapping["groupIndex"])
        if key in mappings or mapping["senseIndex"] >= len(proposal["senses"]):
            raise ValueError("关系义项映射重复或越界")
        source = evidence[mapping["source"]]
        group = next((group for group in source.get("relationGroups", [])
                      if group["groupIndex"] == mapping["groupIndex"]), None)
        if group is None:
            raise ValueError("关系义项映射指向不存在的来源分组")
        target_pos = senses[mapping["senseIndex"]]["partOfSpeech"].casefold().split()[0]
        source_pos = group["partOfSpeech"].casefold().split()[0]
        if target_pos != source_pos:
            raise ValueError("关系义项映射的词性不一致")
        mappings[key] = senses[mapping["senseIndex"]]["senseId"]
    overrides = {}
    for override in proposal.get("relationTypeOverrides", []):
        key = (override["source"], override["groupIndex"], normalize_word(override["word"]))
        if key in overrides:
            raise ValueError("关系类型判断重复")
        overrides[key] = override["type"]
    relation_decisions = {value["candidateId"]: value for value in proposal.get("relationDecisions", [])}
    derivative_decisions = {value["candidateId"]: value for value in proposal.get("derivativeDecisions", [])}
    relationships = list(existing.get("relationships", []) if existing else [])
    for relation in relationships:
        if relation.get("targetWordId"):
            relation.setdefault("linkStatus", "linked")
    derivatives = list(existing.get("derivatives", []) if existing else [])
    old_pending = {candidate["candidateId"]: candidate for candidate in existing.get("pendingRelations", [])} if existing else {}
    pending = dict(old_pending)
    used_overrides = set()
    for source_index, source in enumerate(evidence):
        for candidate in source.get("externalCandidates", []):
            hint = candidate.get("relationHint")
            if hint not in ("synonym_or_near_synonym", "antonym"):
                continue
            group_key = (source_index, candidate["groupIndex"])
            target_word = normalize_word(candidate["word"])
            review_id = candidate_id_fn("rc", word, source_index, candidate["groupIndex"], target_word, hint)
            decision = relation_decisions.get(review_id)
            source_sense_id = mappings.get(group_key)
            if not source_sense_id:
                if decision and decision["action"] == "reject":
                    continue
                raise ValueError("关系候选缺少来源义项映射")
            override_key = (*group_key, target_word)
            proposed_type = overrides.get(override_key, hint)
            if override_key in overrides:
                used_overrides.add(override_key)
            linked = [relation for relation in relationships
                      if relation.get("targetWordId") == stable_word_id(target_word)
                      and relation.get("sourceSenseId") == source_sense_id
                      and (relation["type"] == proposed_type or
                           (proposed_type == "synonym_or_near_synonym" and
                            relation["type"] in ("synonym", "near_synonym")))]
            if linked:
                if decision and (decision["action"] == "reject" or
                                 (decision["action"] == "accept" and
                                  any(value["type"] != decision["type"] for value in linked))):
                    raise ValueError("新判断与既有已链接关系冲突")
                continue
            if (hint == "antonym" and proposed_type != "antonym") or (
                    hint != "antonym" and proposed_type == "antonym"):
                raise ValueError("关系类型判断与来源分组冲突")
            candidate_id = "c_" + hashlib.sha256(
                f"{stable_word_id(word)}|{source_sense_id}|{target_word}|{hint}".encode("utf-8")).hexdigest()[:20]
            ref = source_ref(source["fragments"][candidate["fragment"]], source)
            if decision and decision["action"] in ("accept", "reject"):
                prior_pending = pending.pop(candidate_id, None)
                prior_confirmed = [value for value in relationships
                                   if value.get("linkStatus") == "lemma_only"
                                   and value.get("targetLemma") == target_word
                                   and value.get("sourceSenseId") == source_sense_id]
                if decision["action"] == "reject":
                    if prior_confirmed:
                        raise ValueError("新判断与既有已确认关系冲突")
                    continue
                relation_type = decision["type"]
                if any(value["type"] != relation_type for value in prior_confirmed):
                    raise ValueError("新关系类型与既有已确认关系冲突")
                group = next(group for group in source.get("relationGroups", [])
                             if group["groupIndex"] == candidate["groupIndex"])
                relationship_id = "r_" + hashlib.sha256(
                    f"{word}|{source_sense_id}|{target_word}|{relation_type}".encode("utf-8")).hexdigest()[:20]
                sense = next(value for value in senses if value["senseId"] == source_sense_id)
                relation_refs = [ref, source_ref(source["fragments"][group["fragment"]], source),
                                 *sense["definitionEn"]["sourceRefs"],
                                 *(prior_pending["sourceRefs"] if prior_pending else [])]
                relation_refs = list({(value["site"], value["url"], value["locator"], value["summary"]): value
                                      for value in relation_refs}.values())
                existing_relation = next((value for value in relationships
                                          if value["relationshipId"] == relationship_id), None)
                if existing_relation:
                    existing_relation["sourceRefs"] = list({
                        (value["site"], value["url"], value["locator"], value["summary"]): value
                        for value in [*existing_relation["sourceRefs"], *relation_refs]
                    }.values())
                else:
                    relationships.append({"relationshipId": relationship_id, "type": relation_type,
                                          "targetLemma": target_word, "targetSenseHint": group["gloss"],
                                          "linkStatus": "lemma_only", "sourceSenseId": source_sense_id,
                                          "verificationStatus": "agent_reviewed", "sourceRefs": relation_refs})
                continue
            old = pending.get(candidate_id)
            if (old and proposed_type == "synonym_or_near_synonym"
                    and old["proposedType"] in ("synonym", "near_synonym")):
                proposed_type = old["proposedType"]
            refs = list(old["sourceRefs"]) if old else []
            if not any((value["site"], value["url"], value["locator"], value["summary"]) ==
                       (ref["site"], ref["url"], ref["locator"], ref["summary"]) for value in refs):
                refs.append(ref)
            pending[candidate_id] = {"candidateId": candidate_id, "targetLemma": target_word,
                                     "sourceSenseId": source_sense_id, "proposedType": proposed_type,
                                     "status": old["status"] if old else "pending_collection",
                                     "sourceRefs": refs}
    if set(overrides) != used_overrides:
        raise ValueError("关系类型判断没有对应的来源候选")
    for source_index, source in enumerate(evidence):
        for candidate in source.get("derivativeCandidates", []):
            target_word = normalize_word(candidate["word"])
            review_id = candidate_id_fn("dc", word, source_index, 0, target_word)
            decision = derivative_decisions.get(review_id)
            if not decision or decision["action"] != "direct_derivative":
                continue
            derivative_id = "d_" + hashlib.sha256(
                f"{stable_word_id(word)}|{target_word}".encode("utf-8")).hexdigest()[:20]
            if not any(value["derivativeId"] == derivative_id for value in derivatives):
                derivatives.append({"derivativeId": derivative_id, "word": target_word,
                                    "status": "candidate", "verificationStatus": "agent_reviewed",
                                    "sourceRefs": [source_ref(source["fragments"][candidate["fragment"]], source)]})
    result = {
        "schemaVersion": "1.5" if existing and existing.get("schemaVersion") == "1.5" else schema_version,
        "wordId": stable_word_id(word), "lemma": word,
        "aliases": existing.get("aliases", []) if existing else [],
        "familyId": existing.get("familyId", stable_word_id(word)) if existing else stable_word_id(word),
        "pronunciations": pronunciations,
        "inflections": inflections,
        "derivatives": derivatives,
        "pendingRelations": list(pending.values()),
        "senses": senses, "relationships": relationships,
        "revision": existing["revision"] + 1 if existing else 1,
        "createdAt": existing["createdAt"] if existing else now, "updatedAt": now,
    }
    return result


def _json_text(value: dict) -> str:
    # Decimal stays a JSON number with two fractional digits.
    markers: dict[str, str] = {}
    def default(obj):
        if isinstance(obj, Decimal):
            token = f"__decimal_{len(markers)}__"
            markers[token] = f"{obj:.2f}"
            return token
        raise TypeError(type(obj).__name__)
    rendered = json.dumps(value, ensure_ascii=False, indent=2, default=default)
    for token, number in markers.items():
        rendered = rendered.replace('"' + token + '"', number)
    def format_confidence(match):
        number = Decimal(match.group(2))
        if number < 0 or number > 1 or number.as_tuple().exponent < -2:
            raise ValueError("confidence 必须在 0.00–1.00 且最多两位小数")
        return match.group(1) + f"{number:.2f}"
    rendered = re.sub(r'("confidence"\s*:\s*)(-?\d+(?:\.\d+)?)(?=[,\s}])', format_confidence, rendered)
    return rendered + "\n"


def verify_raw_confidence(text: str) -> None:
    confidences = re.findall(r'"confidence"\s*:\s*([^,\n}]+)', text)
    if not all(re.fullmatch(r"(?:0|1)\.\d{2}", number.strip()) for number in confidences):
        raise ValueError("confidence 原始 JSON 必须是两位小数数值")


def entry_json_text(value: dict) -> str:
    return _json_text(value)


def validate_entry(entry: dict, schema: Path) -> str:
    text = _json_text(entry)
    parsed = json.loads(text, parse_float=Decimal)
    schema_value = json.loads(schema.read_text(encoding="utf-8"), parse_float=Decimal)
    errors = list(Draft202012Validator(schema_value).iter_errors(parsed))
    if errors:
        raise ValueError("词条 Schema 校验失败：" + "；".join(error.message for error in errors))
    verify_raw_confidence(text)
    if not all(s["examples"] for s in entry["senses"]):
        raise ValueError("义项缺少例句")
    ids = []
    ids.extend(value["itemId"] for value in entry["aliases"])
    ids.extend(value["pronunciationId"] for value in entry["pronunciations"])
    ids.extend(value["ipa"]["itemId"] for value in entry["pronunciations"])
    ids.extend(value["formId"] for value in entry["inflections"])
    ids.extend(value["form"]["itemId"] for value in entry["inflections"])
    ids.extend(value["derivativeId"] for value in entry["derivatives"])
    ids.extend(value["candidateId"] for value in entry.get("pendingRelations", []))
    ids.extend(value["relationshipId"] for value in entry["relationships"])
    for sense in entry["senses"]:
        ids.append(sense["senseId"])
        ids.extend(x["itemId"] for x in [sense["definitionZh"], sense["definitionEn"], *sense["examples"], *sense["collocations"], *sense["phrases"]])
        ids.extend(x["translationZh"]["itemId"] for group in ("examples", "collocations", "phrases")
                   for x in sense[group] if "translationZh" in x)
        ids.extend(x["itemId"] for x in [*sense["grammarTags"], *sense["registerTags"]])
    content_ids = {value["itemId"] for sense in entry["senses"] for value in
                   [sense["definitionZh"], sense["definitionEn"],
                    *(item for group in ("examples", "collocations", "phrases") for item in sense[group]),
                    *(item["translationZh"] for group in ("examples", "collocations", "phrases")
                      for item in sense[group] if "translationZh" in item)]}
    for value in [*entry["aliases"], *(p["ipa"] for p in entry["pronunciations"]),
                  *(f["form"] for f in entry["inflections"]),
                  *(item for sense in entry["senses"] for item in
                    [sense["definitionZh"], sense["definitionEn"], *sense["grammarTags"],
                     *sense["registerTags"], *sense["examples"], *sense["collocations"], *sense["phrases"],
                     *(value["translationZh"] for group in ("examples", "collocations", "phrases")
                       for value in sense[group] if "translationZh" in value)])]:
        if value["verificationStatus"] == "rejected":
            raise ValueError("被拒绝的内容不得发布")
        if value["verificationStatus"] == "agent_passed" and (
                entry["schemaVersion"] not in ("1.4", "1.5") or value["itemId"] not in content_ids):
            raise ValueError("Agent 内容核验状态只适用于 1.4+ 版释义、用法及译文")
        if value["verificationStatus"] != "agent_passed" and "verificationRef" in value:
            raise ValueError("未通过 Agent 内容核验的项目不得保留审查引用")
    if len(ids) != len(set(ids)):
        raise ValueError("词条内部 ID 重复")
    known_forms = set()
    if any(relation["type"] == "spelling_similar" and relation["verificationStatus"] == "automatic_passed"
           for relation in entry["relationships"]):
        for form in [*(value["text"] for value in entry["aliases"]),
                     *(value["form"]["text"] for value in entry["inflections"])]:
            try:
                known_forms.add(normalize_word(form))
            except ValueError:
                continue
    for relation in entry["relationships"]:
        if relation["type"] != "spelling_similar" or relation["verificationStatus"] != "automatic_passed":
            continue  # Historical reviewed relations retain their original contract.
        target = relation.get("targetLemma")
        if (not target or relation.get("sourceSenseId") or relation.get("targetSenseId")
                or normalize_word(target) == entry["lemma"] or normalize_word(target) in known_forms
                or not meets_subsequence_threshold(entry["lemma"], normalize_word(target))):
            raise ValueError("自动确认的拼写相似关系须达子列阈值、保持词条级且不得重复已知词形")
    sense_ids = {sense["senseId"] for sense in entry["senses"]}
    for form in entry["inflections"]:
        derived = form["form"]["generationMethod"] == "rule_derived"
        if derived != ("derivation" in form):
            raise ValueError("规则词形必须且只能带 derivation")
        if derived:
            if (entry["schemaVersion"] != "1.5" or form["derivation"]["basisSenseId"] not in sense_ids
                    or form["form"]["verificationStatus"] != "pending"
                    or form["form"]["sourceRefs"] or form["sourceRefs"]):
                raise ValueError("规则词形缺少可核对的词性来源或状态不符")
            sense = next(sense for sense in entry["senses"] if sense["senseId"] == form["derivation"]["basisSenseId"])
            if (form["partOfSpeech"] != sense["partOfSpeech"] or
                    not all(ref in sense["definitionEn"]["sourceRefs"] for ref in form["derivation"]["basisSourceRefs"])):
                raise ValueError("规则词形词性依据与来源义项不符")
    for value in [*entry["aliases"], *(p["ipa"] for p in entry["pronunciations"]),
                  *(item for sense in entry["senses"] for item in
                    [sense["definitionZh"], sense["definitionEn"], *sense["grammarTags"],
                     *sense["registerTags"], *sense["examples"], *sense["collocations"], *sense["phrases"]])]:
        if value["generationMethod"] == "rule_derived":
            raise ValueError("规则推导仅适用于词形变化")
    if entry["schemaVersion"] in ("1.3", "1.4", "1.5"):
        forms = [entry["lemma"], *(value["form"]["text"] for value in entry["inflections"])]
        for sense in entry["senses"]:
            for example in sense["examples"]:
                emphasis = example["emphasis"]
                validate_inline_spans(example["text"], emphasis["en"])
                validate_inline_spans(example["translationZh"]["text"], emphasis["zh"])
                expected = whole_word_spans(example["text"], forms)
                if emphasis["en"] != expected:
                    raise ValueError("例句英文加粗区间与目标词及词形不一致")
                if not any("\u3400" <= character <= "\u9fff" for span in emphasis["zh"]
                           for character in example["translationZh"]["text"][span["start"]:span["end"]]):
                    raise ValueError("例句中文加粗区间须包含对应汉字")
    if any(candidate["sourceSenseId"] not in sense_ids or candidate["targetLemma"] == entry["lemma"]
           for candidate in entry.get("pendingRelations", [])):
        raise ValueError("待核验关系的来源义项或目标词无效")
    return text


def validate_entry_references(root: Path, entry: dict) -> None:
    sense_ids = {sense["senseId"] for sense in entry["senses"]}
    entries_dir = entry_records(root)
    for relation in entry["relationships"]:
        if relation.get("sourceSenseId") and relation["sourceSenseId"] not in sense_ids:
            raise ValueError("关系源义项悬空")
        if relation.get("linkStatus") == "lemma_only":
            if relation.get("targetWordId") or not relation.get("targetLemma") or not relation.get("sourceRefs"):
                raise ValueError("词面关系缺少目标或证据")
            continue
        target = entries_dir / f"{relation['targetWordId']}.json"
        if not target.is_file():
            raise ValueError("关系目标词条悬空")
        target_entry = read_json(target)
        if relation.get("targetSenseId") and relation["targetSenseId"] not in {sense["senseId"] for sense in target_entry["senses"]}:
            raise ValueError("关系目标义项悬空")
    for candidate in entry.get("pendingRelations", []):
        if candidate["sourceSenseId"] not in sense_ids:
            raise ValueError("待核验关系源义项悬空")
    for derivative in entry["derivatives"]:
        if derivative["status"] == "candidate" and derivative.get("verificationStatus") == "agent_reviewed" and not derivative.get("sourceRefs"):
            raise ValueError("已判断的派生词候选缺少来源")
        if derivative["status"] == "linked":
            target = entries_dir / f"{derivative.get('targetWordId', '')}.json"
            if not target.is_file():
                raise ValueError("派生词目标词条悬空")


def render_item(value: dict) -> str:
    if value["generationMethod"] == "ai_generated":
        return f"{value['text']}（AI 生成，置信度 {float(value['confidence']):.2f}）"
    return value["text"]


def verify_observed_pronunciations_and_forms(entry: dict, evidence: list[dict]) -> list[dict]:
    """Promote exact current-page observations and explain every pending item."""
    from utils.scripts.english_inflections import observed_regular_forms, part_of_speech, regular_forms

    parts_of_speech = {sense["partOfSpeech"].casefold() for sense in entry["senses"]}
    gaps = []
    def cited(value: dict, source: dict, candidate: dict) -> bool:
        fragment = source["fragments"][candidate["fragment"]]
        return any(ref["site"] == source["site"] and ref["url"] == source["url"]
                   and ref["locator"] == fragment["locator"] for ref in value["sourceRefs"])

    for pronunciation in entry["pronunciations"]:
        ipa = pronunciation["ipa"]
        if ipa["verificationStatus"] == "human_passed":
            continue
        observed = any(
            candidate["ipa"] == ipa["text"] and candidate["variety"] == pronunciation["variety"]
            and candidate.get("partOfSpeech", "").casefold() == pronunciation.get("partOfSpeech", "").casefold()
            and source["fragments"][candidate["fragment"]]["summary"] == ipa["text"]
            and cited(ipa, source, candidate) and cited(pronunciation, source, candidate)
            and source["coverage"] == "extracted"
            for source in evidence for candidate in source.get("pronunciationCandidates", [])
        )
        valid_pos = not pronunciation.get("partOfSpeech") or pronunciation["partOfSpeech"].casefold() in parts_of_speech
        valid_audio = not pronunciation.get("audioUrl") or pronunciation["audioUrl"].startswith("https://")
        valid_ipa = bool(re.fullmatch(r"[^<>\d]{1,100}", ipa["text"]))
        ipa["verificationStatus"] = "automatic_passed" if observed and valid_pos and valid_audio and valid_ipa else "pending"
        if ipa["verificationStatus"] == "pending":
            reason = ("source_candidate_mismatch" if not observed else
                      "part_of_speech_mismatch" if not valid_pos else
                      "audio_url_invalid" if not valid_audio else "ipa_format_invalid")
            gaps.append({"field": "pronunciations", "itemId": ipa["itemId"], "status": "pending_review",
                         "reason": reason, "sourceSites": sorted({ref["site"] for ref in ipa["sourceRefs"]})})

    for inflection in entry["inflections"]:
        form = inflection["form"]
        if form["verificationStatus"] == "human_passed":
            continue
        observed = []
        for source in evidence:
            for candidate in [*source.get("inflectionCandidates", []),
                              *observed_regular_forms(source, entry["lemma"])]:
                if (candidate["form"].casefold() == form["text"].casefold()
                        and candidate["kind"] == inflection["kind"]
                        and candidate.get("partOfSpeech", "").casefold() == inflection.get("partOfSpeech", "").casefold()
                        and cited(form, source, candidate) and cited(inflection, source, candidate)
                        and re.search(rf"(?<!\w){re.escape(form['text'])}(?!\w)",
                                      source["fragments"][candidate["fragment"]]["summary"], re.I)):
                    observed.append(source["fragments"][candidate["fragment"]]["locator"])
        pos = part_of_speech(inflection.get("partOfSpeech", ""))
        valid_pos = not pos or any(part_of_speech(label) == pos for label in parts_of_speech)
        regular = regular_forms(entry["lemma"], pos).get(inflection["kind"])
        plausible = (regular == form["text"].casefold()
                     or any("irreg-infls" in locator for locator in observed))
        form["verificationStatus"] = "automatic_passed" if observed and valid_pos and plausible else "pending"
        if form["verificationStatus"] == "pending":
            reason = ("source_candidate_mismatch" if not observed else
                      "part_of_speech_mismatch" if not valid_pos else "spelling_rule_unconfirmed")
            gaps.append({"field": "inflections", "itemId": form["itemId"], "status": "pending_review",
                         "reason": reason, "sourceSites": sorted({ref["site"] for ref in form["sourceRefs"]})})
    return gaps


def render_entry_markdown(entry: dict, gaps: dict) -> str:
    """Render every learner-facing field and its review/provenance context."""
    site_names = {"cambridge": "Cambridge", "oxford": "Oxford", "longman": "Longman",
                  "thesaurus": "Thesaurus.com"}
    verification = {"pending": "待核验", "automatic_passed": "自动通过", "agent_passed": "Agent 已核验",
                    "agent_reviewed": "Agent 已审查", "human_passed": "人工通过"}
    relation_names = {"synonym": "同义词", "near_synonym": "近义词", "antonym": "反义词",
                       "spelling_similar": "拼写相似（可能易混）", "family": "词族",
                      "synonym_or_near_synonym": "同义／近义待判"}
    link_names = {"lemma_only": "仅词面，目标义项待链接", "linked": "已链接"}

    def refs(value: dict) -> str:
        seen = set()
        links = []
        for ref in value.get("sourceRefs", []):
            key = (ref["site"], ref["url"], ref["locator"])
            if key in seen:
                continue
            seen.add(key)
            label = f"{site_names[ref['site']]} {ref['locator']}"
            links.append(f"[{label}](<{ref['url']}>)")
        return "、".join(links) if links else "无"

    def content(value: dict) -> str:
        status = verification.get(value["verificationStatus"], value["verificationStatus"])
        return f"{render_item(value)}（{status}；来源：{refs(value)}）"

    lines = [f"# {entry['lemma']}", "",
             f"- 词条 ID：`{entry['wordId']}`；词族 ID：`{entry['familyId']}`",
             f"- Schema：{entry['schemaVersion']}；修订：{entry['revision']}；创建：{entry['createdAt']}；更新：{entry['updatedAt']}",
             "", "## 拼写变体", ""]
    lines.extend(f"- {content(alias)}" for alias in entry["aliases"])
    if not entry["aliases"]:
        lines.append("- 无")

    lines.extend(["", "## 读音", ""])
    for pronunciation in entry["pronunciations"]:
        variety = {"uk": "英音", "us": "美音", "other": "其他"}.get(pronunciation["variety"], pronunciation["variety"])
        audio = f"；[音频](<{pronunciation['audioUrl']}>)" if pronunciation.get("audioUrl") else ""
        ipa = pronunciation["ipa"]
        review = verification.get(ipa["verificationStatus"], ipa["verificationStatus"])
        lines.append(f"- {variety} · {pronunciation.get('partOfSpeech') or '未标词性'}：/{render_item(ipa)}/"
                     f"（{review}；来源：{refs(pronunciation)}）{audio}")
    if not entry["pronunciations"]:
        lines.append("- 暂无已发布读音")

    lines.extend(["", "## 词形变化", ""])
    for form in entry["inflections"]:
        regularity = "不规则" if form.get("irregular") else "规则" if "irregular" in form else "未标规则性"
        displayed = (f"{render_item(form['form'])}（规则推导 · 待核验；词性依据："
                     f"{refs({'sourceRefs': form['derivation']['basisSourceRefs']})}）") if form["form"]["generationMethod"] == "rule_derived" else content(form["form"])
        lines.append(f"- {form['kind']} · {form.get('partOfSpeech') or '未标词性'}：{displayed}；{regularity}")
    if not entry["inflections"]:
        lines.append("- 暂无已发布词形")

    lines.extend(["", "## 派生词", ""])
    for derivative in entry["derivatives"]:
        state = "已链接" if derivative["status"] == "linked" else "候选词面，目标词条待建"
        target = f"；目标词条 ID：`{derivative['targetWordId']}`" if derivative.get("targetWordId") else ""
        review = verification.get(derivative.get("verificationStatus"), derivative.get("verificationStatus", "待核验"))
        lines.append(f"- **{derivative['word']}**：{state}；{review}{target}；来源：{refs(derivative)}")
    if not entry["derivatives"]:
        lines.append("- 暂无已确认派生词")

    lines.extend(["", "## 义项", ""])
    for number, sense in enumerate(entry["senses"], 1):
        lines.extend([f"### {number}. {sense['partOfSpeech']} · `{sense['senseId']}`", "",
                      f"- 中文释义：{content(sense['definitionZh'])}",
                      f"- 英文释义：{content(sense['definitionEn'])}"])
        orders = "、".join(f"{site_names[x['site']]} #{x['ordinal']}" for x in sense["sourceOrders"])
        lines.append(f"- 来源候选顺序（从 0 起）：{orders or '无'}")
        for key, label in (("grammarTags", "语法标签"), ("registerTags", "语域标签"),
                           ("examples", "例句"), ("collocations", "搭配"), ("phrases", "短语")):
            values = sense[key]
            lines.append(f"- {label}：")
            for value in values:
                if key == "examples" and "emphasis" in value:
                    original = markdown_bold_spans(value["text"], value["emphasis"]["en"])
                    if value["generationMethod"] == "ai_generated":
                        original += f"（AI 生成，置信度 {float(value['confidence']):.2f}）"
                    status = verification.get(value["verificationStatus"], value["verificationStatus"])
                    lines.append(f"  - {original}（{status}；来源：{refs(value)}）")
                else:
                    lines.append(f"  - {content(value)}")
                if "translationZh" in value:
                    translation = value["translationZh"]
                    translated = (markdown_bold_spans(translation["text"], value["emphasis"]["zh"])
                                  if key == "examples" else translation["text"])
                    status = verification.get(translation["verificationStatus"], translation["verificationStatus"])
                    generated = (f"；AI 生成，置信度 {float(translation['confidence']):.2f}"
                                 if translation["generationMethod"] == "ai_generated" else "")
                    lines.append(f"    - 译文：{translated}（{status}{generated}；来源：{refs(translation)}）")
            if not values:
                lines.append("  - 暂无")
        lines.append("")

    sense_numbers = {sense["senseId"]: index for index, sense in enumerate(entry["senses"], 1)}
    lines.extend(["## 已判断关联词", ""])
    for relation in entry["relationships"]:
        source = (f"义项 {sense_numbers[relation['sourceSenseId']]}"
                  if relation.get("sourceSenseId") in sense_numbers else "词条级关系")
        target = relation.get("targetLemma") or relation.get("targetWordId", "未知目标")
        detail = [link_names.get(relation.get("linkStatus"), "状态未标注"),
                  verification.get(relation["verificationStatus"], relation["verificationStatus"])]
        if relation.get("targetSenseHint"):
            detail.append(f"目标义项提示：{relation['targetSenseHint']}")
        if relation.get("targetWordId"):
            detail.append(f"目标词条 ID：`{relation['targetWordId']}`")
        if relation.get("targetSenseId"):
            detail.append(f"目标义项 ID：`{relation['targetSenseId']}`")
        if relation.get("intraList"):
            detail.append("本批词表内关系")
        basis = ("判据：最长公共子列相似度 ≥ 0.75" if relation["type"] == "spelling_similar"
                 and relation["verificationStatus"] == "automatic_passed" else f"来源：{refs(relation)}")
        lines.append(f"- {source} · {relation_names.get(relation['type'], relation['type'])} **{target}**"
                     f"（{'；'.join(detail)}；{basis}）")
    if not entry["relationships"]:
        lines.append("- 无")

    lines.extend(["", "## 待核验关联词", ""])
    for relation in entry.get("pendingRelations", []):
        source = f"义项 {sense_numbers.get(relation['sourceSenseId'], '未知')}"
        lines.append(f"- {source} · {relation_names.get(relation['proposedType'], relation['proposedType'])} "
                     f"**{relation['targetLemma']}**（{relation['status']}；来源：{refs(relation)}）")
    if not entry.get("pendingRelations"):
        lines.append("- 无")

    lines.extend(["", "## 来源覆盖与字段缺口", ""])
    for site in gaps["sourceCoverage"]:
        truncation = f"；采集截断：{', '.join(site['truncatedSelectors'])}" if site.get("truncatedSelectors") else ""
        reason = f"；{site['reason']}" if site.get("reason") else ""
        lines.append(f"- {site_names.get(site['site'], site['site'])}：{site['status']}{reason}{truncation}")
    for gap in gaps.get("fieldGaps", []):
        lines.append(f"- {gap['field']}：{gap['status']}（{gap['reason']}；来源：{', '.join(gap['sourceSites']) or '无'}）")
    if not gaps.get("fieldGaps"):
        lines.append("- 字段缺口：无")
    for gap in gaps.get("verificationGaps", []):
        lines.append(f"- {gap['field']} · `{gap['itemId']}`：{gap['status']}（{gap['reason']}；来源：{', '.join(gap['sourceSites']) or '无'}）")
    for gap in gaps.get("contentGaps", []):
        lines.append(f"- {gap['field']} · `{gap['itemId']}`：待核验（Agent 暂缓）")
    return "\n".join(lines) + "\n"


def parse_word_list(raw: bytes, kind: str, word_column: str | None = None, pos_column: str | None = None, meaning_column: str | None = None) -> list[dict]:
    text = raw.decode("utf-8-sig")
    rows = []
    if kind == "text":
        for line_no, line in enumerate(text.splitlines(), 1):
            if line.strip():
                rows.append({"word": line.strip(), "line": line_no})
    elif kind == "csv":
        if not word_column:
            raise ValueError("CSV 必须指定 word_column")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or word_column not in reader.fieldnames:
            raise ValueError("CSV 缺少指定的单词列")
        for optional in (pos_column, meaning_column):
            if optional and optional not in reader.fieldnames:
                raise ValueError(f"CSV 缺少指定的提示列：{optional}")
        prior_line = 1
        for row in reader:
            line_no = prior_line + 1
            prior_line = reader.line_num
            if None in row:
                raise ValueError(f"CSV 第 {line_no} 行列数超过表头")
            if (row.get(word_column) or "").strip():
                rows.append({"word": row[word_column].strip(), "line": line_no,
                             "partOfSpeechHint": (row.get(pos_column) or "") if pos_column else "",
                             "meaningHint": (row.get(meaning_column) or "") if meaning_column else ""})
    elif kind in ("json", "jsonl"):
        if kind == "json":
            payload = json.loads(text)
            records = payload.get("words") if isinstance(payload, dict) else payload
            if not isinstance(records, list):
                raise ValueError("JSON 词表必须是数组或含 words 数组的对象")
            numbered = enumerate(records, 1)
        else:
            numbered = ((number, json.loads(line)) for number, line in enumerate(text.splitlines(), 1) if line.strip())
        for number, record in numbered:
            if isinstance(record, str):
                record = {"word": record}
            if not isinstance(record, dict) or not isinstance(record.get("word"), str):
                raise ValueError(f"JSON 词表第 {number} 项缺少 word")
            meanings = record.get("core_meanings", [])
            if meanings and (not isinstance(meanings, list) or not all(isinstance(item, dict) for item in meanings)):
                raise ValueError(f"JSON 词表第 {number} 项 core_meanings 无效")
            first = meanings[0] if meanings else {}
            rows.append({"word": record["word"], "line": number,
                         "partOfSpeechHint": str(record.get("partOfSpeechHint") or record.get("pos") or first.get("pos") or ""),
                         "meaningHint": str(record.get("meaningHint") or record.get("definition_zh") or first.get("definition_zh") or "")})
    else:
        raise ValueError("词表格式只能是 text、csv、json 或 jsonl")
    seen = {}
    normalized = []
    for row in rows:
        row["word"] = normalize_word(row["word"])
        if row["word"] not in seen:
            row["sourceLines"] = [row["line"]]
            seen[row["word"]] = row
            normalized.append(row)
        else:
            seen[row["word"]]["sourceLines"].append(row["line"])
    if not normalized:
        raise ValueError("词表没有单词")
    return normalized
