"""Review and link previously confirmed lemma-only relations after a word is built."""
from __future__ import annotations

import hashlib
from pathlib import Path

from entry import entry_json_text, stable_word_id, validate_entry, validate_entry_references
from utils.scripts.dictionary_records import entries as entry_records
from utils.scripts.file_transaction import project_lock
from utils.scripts.structured_io import read_json, write_text_transaction
from utils.scripts.timestamp import iso_timestamp


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pending_links(root: Path, word: str) -> list[dict]:
    entries_dir = entry_records(root)
    packet = []
    for source_path in entries_dir.glob("*.json"):
        source = read_json(source_path)
        for relation in source.get("relationships", []):
            if relation.get("linkStatus") != "lemma_only":
                continue
            target_word = relation["targetLemma"]
            if source["lemma"] != word and target_word != word:
                continue
            target_path = entries_dir / f"{stable_word_id(target_word)}.json"
            if not target_path.is_file():
                continue
            target = read_json(target_path)
            source_sense = next((sense for sense in source["senses"]
                                 if sense["senseId"] == relation["sourceSenseId"]), None)
            if source_sense is None:
                raise ValueError("词面关系来源义项悬空")
            packet.append({
                "sourceWord": source["lemma"], "targetWord": target_word,
                "relationshipId": relation["relationshipId"], "type": relation["type"],
                "sourceSenseId": relation["sourceSenseId"],
                "sourceDefinition": source_sense["definitionEn"],
                "targetSenseHint": relation["targetSenseHint"],
                "targetSenses": [{"senseId": sense["senseId"], "partOfSpeech": sense["partOfSpeech"],
                                  "definitionEn": sense["definitionEn"]} for sense in target["senses"]],
                "sourceRefs": relation["sourceRefs"],
                "sourceSha256": _digest(source_path), "targetSha256": _digest(target_path),
            })
    return packet


def apply_links(root: Path, run_id: str, packet: list[dict], response: dict, schema_path: Path) -> None:
    expected = {(item["sourceWord"], item["relationshipId"]) for item in packet}
    choices = response["links"]
    actual = [(item["sourceWord"], item["relationshipId"]) for item in choices]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("链接判断必须逐项覆盖审查包")
    packet_by_key = {(item["sourceWord"], item["relationshipId"]): item for item in packet}
    entries_dir = entry_records(root)
    updates: dict[Path, dict] = {}
    with project_lock(root / "logs" / "build-word-entry" / "dictionary.lock", f"build-word-entry:{run_id}:links"):
        for choice in choices:
            if choice["action"] == "defer":
                continue
            item = packet_by_key[(choice["sourceWord"], choice["relationshipId"])]
            source_path = entries_dir / f"{stable_word_id(item['sourceWord'])}.json"
            target_path = entries_dir / f"{stable_word_id(item['targetWord'])}.json"
            if _digest(source_path) != item["sourceSha256"] or _digest(target_path) != item["targetSha256"]:
                raise ValueError("链接审查期间词条已修改，请重新生成审查包")
            source = updates.get(source_path) or read_json(source_path)
            target = updates.get(target_path) or read_json(target_path)
            relation = next((value for value in source["relationships"]
                             if value["relationshipId"] == item["relationshipId"]), None)
            target_sense = next((sense for sense in target["senses"]
                                 if sense["senseId"] == choice["targetSenseId"]), None)
            if relation is None or relation.get("linkStatus") != "lemma_only" or target_sense is None:
                raise ValueError("链接判断指向无效关系或目标义项")
            source_sense = next(sense for sense in source["senses"]
                                if sense["senseId"] == relation["sourceSenseId"])
            if source_sense["partOfSpeech"].split()[0].casefold() != target_sense["partOfSpeech"].split()[0].casefold():
                raise ValueError("链接两端义项词性不一致")
            if not any(ref["site"] == "thesaurus" for ref in relation["sourceRefs"]):
                raise ValueError("词面关系缺少 Thesaurus.com 候选证据")
            source_dictionary_refs = [ref for ref in source_sense["definitionEn"]["sourceRefs"]
                                      if ref["site"] in ("cambridge", "oxford", "longman")]
            target_dictionary_refs = [ref for ref in target_sense["definitionEn"]["sourceRefs"]
                                      if ref["site"] in ("cambridge", "oxford", "longman")]
            if not source_dictionary_refs or not target_dictionary_refs:
                raise ValueError("链接两端义项均需学习词典释义证据")
            refs = list({(ref["site"], ref["url"], ref["locator"], ref["summary"]): ref
                         for ref in [*relation["sourceRefs"], *source_dictionary_refs, *target_dictionary_refs]}.values())
            relation.update({"targetWordId": target["wordId"], "targetSenseId": target_sense["senseId"],
                             "linkStatus": "linked", "sourceRefs": refs})
            reverse_candidates = [value for value in target["relationships"]
                                  if value.get("targetLemma") == source["lemma"] and value["type"] == relation["type"]
                                  and value.get("sourceSenseId") == target_sense["senseId"]
                                  and (value.get("linkStatus") == "lemma_only" or
                                       value.get("targetSenseId") == source_sense["senseId"])]
            if len(reverse_candidates) > 1:
                raise ValueError("反向关系候选不唯一，需要人工消歧")
            reverse = reverse_candidates[0] if reverse_candidates else None
            if reverse and reverse.get("linkStatus") not in ("lemma_only", "linked"):
                raise ValueError("反向关系状态冲突")
            if reverse and reverse.get("linkStatus") == "linked" and (
                    reverse.get("targetWordId") != source["wordId"] or
                    reverse.get("targetSenseId") != source_sense["senseId"]):
                raise ValueError("反向关系已链接到不同义项")
            if reverse:
                reverse.update({"targetWordId": source["wordId"], "targetSenseId": source_sense["senseId"],
                                "linkStatus": "linked", "sourceRefs": refs})
            else:
                reverse_id = "r_" + hashlib.sha256(
                    f"{target['lemma']}|{target_sense['senseId']}|{source['lemma']}|{source_sense['senseId']}|{relation['type']}".encode("utf-8")
                ).hexdigest()[:20]
                target["relationships"].append({"relationshipId": reverse_id, "type": relation["type"],
                    "targetLemma": source["lemma"], "targetWordId": source["wordId"],
                    "linkStatus": "linked", "sourceSenseId": target_sense["senseId"],
                    "targetSenseId": source_sense["senseId"], "verificationStatus": "agent_reviewed",
                    "sourceRefs": refs})
            for entry_path, entry in ((source_path, source), (target_path, target)):
                entry["schemaVersion"] = "1.2"
                entry.setdefault("pendingRelations", [])
                for prior_relation in entry["relationships"]:
                    if prior_relation.get("targetWordId"):
                        prior_relation.setdefault("linkStatus", "linked")
                entry["revision"] += 1
                entry["updatedAt"] = iso_timestamp()
                validate_entry(entry, schema_path)
                validate_entry_references(root, entry)
                updates[entry_path] = entry
        write_text_transaction({path: entry_json_text(entry) for path, entry in updates.items()})
