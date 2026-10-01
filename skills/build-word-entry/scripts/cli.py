"""Resumable word and vocabulary-list entry point."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

from adapters import SITES, collect_site
from entry import build_entry, entry_json_text, normalize_word, parse_word_list, render_entry_markdown, source_ref, stable_word_id, validate_entry, validate_entry_references, verify_observed_pronunciations_and_forms, verify_raw_confidence
from review import review_packet, validate_decisions
from link_review import pending_links, apply_links
from content_review import prepare as prepare_content_review, apply as apply_content_review, verify as verify_content_review
from utils.scripts.dictionary_records import entries as entry_records
from utils.scripts.browser_checkpoint import BrowserActionRequired, visible_browser
from utils.scripts.dictionary_graph import canonical_family_ids
from utils.scripts.file_transaction import project_lock
from utils.scripts.structured_io import json_digest, read_json, validate_json_schema, write_json, write_text_atomic, write_text_transaction
from utils.scripts.dictionary_jsonl import read_all, reconcile_entry_files, sync_entry_files
from utils.scripts.span_ops import whole_word_spans
from utils.scripts.timestamp import iso_timestamp, unique_filename_timestamp
from utils.scripts.workflow_state import WorkflowDefinition, WorkflowStateStore
from utils.scripts.word_similarity import iter_similar_pairs, meets_subsequence_threshold

NAME = "build-word-entry"
ENTRY_SCHEMA_VERSION = "1.4"
HERE = Path(__file__).resolve().parents[1]


def publish_jsonl(root: Path) -> None:
    sync_entry_files(entry_records(root),
                     root / "outputs" / "词汇星图" / "dicts",
                     lambda value: validate_entry(value, HERE / "references" / "entry.schema.json"))
WORD_STAGES = ("prepared", "validating_input", "opening_browser", "collecting_sources", "parsing_evidence", "aligning_senses", "building_candidates", "awaiting_small_ai_decision", "validating_entry", "verifying_sources", "verifying_relation_candidates", "publishing", "reviewing_existing_links", "completed")
WORD_STAGES_V13 = (*WORD_STAGES[:WORD_STAGES.index("verifying_sources")], "verifying_content", *WORD_STAGES[WORD_STAGES.index("verifying_sources"):])
WORD_STAGES_V14 = (*WORD_STAGES_V13[:WORD_STAGES_V13.index("verifying_relation_candidates")],
                   "preparing_content_review", "awaiting_content_review", "applying_content_review",
                   *WORD_STAGES_V13[WORD_STAGES_V13.index("verifying_relation_candidates"):])
BATCH_STAGES = ("prepared", "validating_list", "normalizing_list", "processing_words", "discovering_intra_list_relations", "verifying_relations", "publishing_list_result", "completed")
PAUSES = ("paused_user_browser_action", "paused_quality_review", "paused_agent_decision", "paused_word", "paused_relation_review", "paused_retryable_error")


def definition(kind: str, entry_schema_version: str = "1.2") -> WorkflowDefinition:
    stages = (WORD_STAGES_V14 if entry_schema_version == "1.4" else
              WORD_STAGES_V13 if entry_schema_version == "1.3" else WORD_STAGES) if kind == "word" else BATCH_STAGES
    transitions = {}
    for index, stage in enumerate(stages[:-1]):
        transitions[stage] = [stages[index + 1]]
    transitions[stages[-1]] = []
    if kind == "word":
        transitions["collecting_sources"] += ["paused_user_browser_action", "paused_retryable_error", "paused_quality_review"]
        transitions["awaiting_small_ai_decision"].append("paused_agent_decision")
        for stage in ("validating_entry", "verifying_content", "verifying_sources", "applying_content_review", "verifying_relation_candidates", "publishing"):
            if stage not in transitions:
                continue
            transitions[stage].append("paused_quality_review")
        transitions["reviewing_existing_links"] += ["paused_relation_review", "paused_quality_review"]
        if entry_schema_version == "1.4":
            transitions["awaiting_content_review"].append("paused_agent_decision")
        transitions.update({"paused_user_browser_action": ["collecting_sources"],
                            "paused_retryable_error": ["collecting_sources"],
                            "paused_agent_decision": ["validating_entry", "applying_content_review"] if entry_schema_version == "1.4" else ["validating_entry"],
                            "paused_relation_review": ["reviewing_existing_links"],
                            "paused_quality_review": ["collecting_sources", "validating_entry", "applying_content_review", "reviewing_existing_links"] if entry_schema_version == "1.4" else ["collecting_sources", "validating_entry", "reviewing_existing_links"]})
    else:
        transitions["processing_words"].append("paused_word")
        transitions["verifying_relations"].append("paused_relation_review")
        transitions["publishing_list_result"].append("paused_relation_review")
        transitions.update({"paused_word": ["processing_words"],
                            "paused_relation_review": ["verifying_relations", "publishing_list_result"]})
    return WorkflowDefinition.build(name=NAME, transitions=transitions)


def paths(root: Path, run_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"\d{8}T\d{6}(?:_\d+)?", run_id):
        raise ValueError("无效 run ID")
    return root / "logs" / NAME / "runs" / run_id, root / "outputs" / NAME / "runs" / run_id


def store(root: Path, run_id: str, kind: str, entry_schema_version: str = "1.2") -> WorkflowStateStore:
    run_dir = paths(root, run_id)[0]
    return WorkflowStateStore(root=root, workflow=NAME, run_id=run_id,
                              definition=definition(kind, entry_schema_version), events_dir=run_dir / "events")


def read_run(root: Path, run_id: str):
    log, out = paths(root, run_id)
    request = read_json(log / "request.json")
    kind = request["kind"]
    workflow = store(root, run_id, kind, request.get("entrySchemaVersion", "1.2"))
    return log, out, request, workflow, workflow.load()


def next_run_id(root: Path) -> str:
    parent = root / "logs" / NAME / "runs"
    parent.mkdir(parents=True, exist_ok=True)
    return unique_filename_timestamp(path.name for path in parent.iterdir())


def new_run(root: Path, request: dict) -> str:
    if request["kind"] in ("word", "batch") and "entrySchemaVersion" not in request:
        request = {**request, "entrySchemaVersion": ENTRY_SCHEMA_VERSION}
    while True:
        run_id = next_run_id(root)
        log, _ = paths(root, run_id)
        try:
            log.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            continue
    write_json(log / "request.json", request)
    now = iso_timestamp()
    state = {
        "schema_version": "1.0", "workflow": NAME, "run_id": run_id,
        "status": "prepared", "current_stage": "prepared", "resume_stage": None,
        "current_object_id": request.get("word"), "current_batch_id": request.get("parentRunId"),
        "completed_steps": [], "pending_decisions": [], "error": None,
        "created_at": now, "updated_at": now, "last_heartbeat_at": now,
        "event_sequence": 0, "retry_count": 0,
    }
    store(root, run_id, request["kind"], request.get("entrySchemaVersion", "1.2")).create(state)
    return run_id


def move(workflow, state, target: str):
    return workflow.transition(state, target, stage=target, completed_step=state["status"])


def pause(workflow, state, status: str, code: str, message: str, resume: str):
    workflow.pause(state, status=status, error_code=code, message=message, resume_stage=resume)
    return 3


def _collect(root: Path, run_id: str, workflow, state, word: str) -> tuple[list[dict] | None, int]:
    log, _ = paths(root, run_id)
    evidence_file = log / "evidence.json"
    if evidence_file.is_file():
        return read_json(evidence_file), 0
    results = []
    try:
        with visible_browser() as browser:
            context = browser.new_context()
            page = context.new_page()
            for site in SITES:
                try:
                    results.append(collect_site(page, site, word))
                except BrowserActionRequired as exc:
                    # Keep the visible page available while the user solves the gate.
                    workflow.pause(state, status="paused_user_browser_action", error_code="human_gate", message=str(exc), resume_stage="collecting_sources")
                    print(json.dumps({"status": "paused_user_browser_action", "run_id": run_id, "message": str(exc)}, ensure_ascii=False), flush=True)
                    try:
                        page.wait_for_function("!(/captcha|verify you are human|checking your browser|sign in to continue|log in to continue/i.test(document.title + ' ' + document.body.innerText.slice(0, 4000)))", timeout=600000)
                    except Exception:
                        return None, 3
                    state = workflow.resume(workflow.load())
                    results.append(collect_site(page, site, word))
                except Exception as exc:
                    results.append({"site": site, "url": SITES[site].format(word=word), "collectedAt": iso_timestamp(), "coverage": "failed", "fragments": [], "error": str(exc)[:200]})
            context.close()
    except Exception as exc:
        return None, pause(workflow, workflow.load(), "paused_retryable_error", "browser_error", str(exc), "collecting_sources")
    write_json(evidence_file, results)
    if not any(source["coverage"] == "extracted" for source in results):
        return None, pause(workflow, workflow.load(), "paused_quality_review", "no_source_coverage", "四站均未取得可用证据", "collecting_sources")
    return results, 0


def decision_template(evidence: list[dict], word: str | None = None,
                      entry_schema_version: str = "1.2") -> dict:
    senses = []
    forms = [word] if word else []
    forms.extend(candidate["form"] for source in evidence for candidate in source.get("inflectionCandidates", []))
    preferred = next((site for site in ("cambridge", "oxford", "longman")
                      if any(source.get("site") == site and source.get("senseCandidates") for source in evidence)), None)
    for source_index, source in enumerate(evidence):
        if source.get("site") != preferred:
            continue
        for candidate in source.get("senseCandidates", []):
            example = candidate.get("example", "")
            example_fragment = candidate.get("exampleFragment")
            valid_example_fragment = example_fragment is not None and example_fragment < len(source["fragments"])
            example_pointer = {"source": source_index, "fragment": example_fragment}
            example_item = {"text": example, "sourceSupported": bool(example)}
            if example and valid_example_fragment:
                example_item["evidence"] = [example_pointer]
            if entry_schema_version in ("1.3", "1.4"):
                translation = candidate.get("exampleTranslationZh", "")
                paired_translation = bool(translation and valid_example_fragment and
                                          translation in source["fragments"][example_fragment]["summary"])
                example_item["translationZh"] = {"text": translation,
                    "sourceSupported": paired_translation,
                    "evidence": [example_pointer] if paired_translation else []}
                example_item["emphasis"] = {"en": whole_word_spans(example, forms), "zh": []}
            sense = {
                "partOfSpeech": candidate.get("partOfSpeech") or "",
                "definitionZh": candidate.get("definitionZh") or "",
                "definitionEn": candidate.get("definitionEn") or "",
                "zhSourceSupported": bool(candidate.get("definitionZh")),
                "enSourceSupported": bool(candidate.get("definitionEn")),
                "evidence": [{"source": source_index, "fragment": candidate["fragment"]}],
                "grammarTags": [], "registerTags": [],
                "examples": [example_item],
                "collocations": [], "phrases": [],
            }
            # Exact English-definition matches can be cited across dictionaries
            # without asking an agent to infer semantic equivalence.
            normalize = lambda value: re.sub(r"\W+", " ", value.casefold(), flags=re.UNICODE).strip()
            definition_key = normalize(candidate.get("definitionEn", ""))
            pos_key = normalize(candidate.get("partOfSpeech", ""))
            if definition_key:
                matches = [{"source": other_index, "fragment": other_candidate["fragment"]}
                           for other_index, other in enumerate(evidence) if other.get("site") in ("cambridge", "oxford", "longman") and other.get("site") != preferred
                           for other_candidate in other.get("senseCandidates", [])
                           if normalize(other_candidate.get("definitionEn", "")) == definition_key
                           and normalize(other_candidate.get("partOfSpeech", "")) == pos_key]
                if matches:
                    sense["alignmentEvidence"] = matches
                    sense["enEvidence"] = [sense["evidence"][0], *matches]
            if not sense["zhSourceSupported"]:
                sense["zhConfidence"] = None
            if not sense["enSourceSupported"]:
                sense["enConfidence"] = None
            if not example:
                sense["examples"][0]["confidence"] = None
            senses.append(sense)
    if not senses:
        senses.append({"partOfSpeech": "", "definitionZh": "", "definitionEn": "",
                       "zhConfidence": None, "enConfidence": None,
                       "examples": [{"text": "", "confidence": None}],
                       "collocations": [], "phrases": []})
    return {"senses": senses}


def expand_decision(log: Path, response: dict) -> dict:
    if "edits" not in response:
        validate_json_schema(response, HERE / "references" / "decision.schema.json")
        return response
    validate_json_schema(response, HERE / "references" / "decision-patch.schema.json")
    proposal = read_json(log / "decision-template.json")
    senses = proposal["senses"]
    for change in sorted(response["edits"], key=lambda value: value["path"].endswith("Confidence") or value["path"].endswith("/confidence")):
        sense_index = change["senseIndex"]
        confidence_path = change["path"].endswith("Confidence") or change["path"].endswith("/confidence")
        if confidence_path and (isinstance(change["value"], bool) or not isinstance(change["value"], (int, float))):
            raise ValueError("置信度补丁必须是数值")
        if not confidence_path and not isinstance(change["value"], str):
            raise ValueError("文本补丁必须是字符串")
        if sense_index >= len(senses):
            raise ValueError("补丁义项索引越界")
        path = change["path"].split("/")
        if len(path) == 1:
            if path[0] in ("definitionZh", "definitionEn") and senses[sense_index].get(path[0]) != change["value"]:
                flag = "zhSourceSupported" if path[0] == "definitionZh" else "enSourceSupported"
                confidence = "zhConfidence" if path[0] == "definitionZh" else "enConfidence"
                if senses[sense_index].get(flag):
                    senses[sense_index][flag] = False
                    senses[sense_index][confidence] = None
            senses[sense_index][path[0]] = change["value"]
        else:
            example_index = int(path[1])
            if example_index >= len(senses[sense_index]["examples"]):
                raise ValueError("补丁例句索引越界")
            if path[2] == "text" and senses[sense_index]["examples"][example_index].get("text") != change["value"]:
                if senses[sense_index]["examples"][example_index].get("sourceSupported"):
                    senses[sense_index]["examples"][example_index]["sourceSupported"] = False
                    senses[sense_index]["examples"][example_index]["confidence"] = None
            senses[sense_index]["examples"][example_index][path[2]] = change["value"]
    for update in response.get("usageUpdates", []):
        sense_index, kind, item_index = update["senseIndex"], update["kind"], update["itemIndex"]
        if sense_index >= len(senses) or item_index >= len(senses[sense_index][kind]):
            raise ValueError("译文更新索引越界")
        usage = senses[sense_index][kind][item_index]
        translation = usage.setdefault("translationZh", {})
        if "translationZh" in update and translation.get("text") != update["translationZh"]:
            translation.update({"text": update["translationZh"], "sourceSupported": False, "evidence": []})
            translation.pop("confidence", None)
        if "confidence" in update:
            translation["confidence"] = update["confidence"]
        if kind == "examples":
            emphasis = usage.setdefault("emphasis", {"en": [], "zh": []})
            if "enSpans" in update:
                emphasis["en"] = update["enSpans"]
            if "zhSpans" in update:
                emphasis["zh"] = update["zhSpans"]
        elif "enSpans" in update or "zhSpans" in update:
            raise ValueError("搭配和短语不保存加粗区间")
    for addition in response.get("alignmentEvidenceAdditions", []):
        sense_index = addition["senseIndex"]
        if sense_index >= len(senses):
            raise ValueError("新增证据的义项索引越界")
        pointer = {"source": addition["source"], "fragment": addition["fragment"]}
        alignment = senses[sense_index].setdefault("alignmentEvidence", [])
        if pointer not in alignment:
            alignment.append(pointer)
        if senses[sense_index].get("enSourceSupported"):
            english = senses[sense_index].setdefault("enEvidence", list(senses[sense_index].get("evidence", [])))
            if pointer not in english:
                english.append(pointer)
    dropped = set(response.get("dropSenseIndexes", []))
    if any(index >= len(senses) for index in dropped):
        raise ValueError("删除义项索引越界")
    proposal["senses"] = [sense for index, sense in enumerate(senses) if index not in dropped]
    if "relationSenseMappings" in response:
        if any(mapping["senseIndex"] in dropped or mapping["senseIndex"] >= len(senses)
               for mapping in response["relationSenseMappings"]):
            raise ValueError("关系候选映射到无效义项")
        proposal["relationSenseMappings"] = [
            {**mapping, "senseIndex": mapping["senseIndex"] - sum(index < mapping["senseIndex"] for index in dropped)}
            for mapping in response["relationSenseMappings"]]
    if "relationTypeOverrides" in response:
        proposal["relationTypeOverrides"] = response["relationTypeOverrides"]
    for key in ("relationDecisions", "derivativeDecisions"):
        if key in response:
            proposal[key] = response[key]
    if "legacyUsageUpdates" in response:
        proposal["legacyUsageUpdates"] = response["legacyUsageUpdates"]
    if "evidenceDigest" in response:
        proposal["evidenceDigest"] = response["evidenceDigest"]
    if response.get("needsReview"):
        proposal["needsReview"] = True
        proposal["reviewReason"] = response.get("reviewReason", "")
    validate_json_schema(proposal, HERE / "references" / "decision.schema.json")
    return proposal


def assess_field_gaps(entry: dict, evidence: list[dict], decision: dict | None = None) -> list[dict]:
    """Classify empty fields without treating absence of evidence as proven absence."""
    gaps = []
    checks = (
        ("pronunciations", "pronunciationCandidates", True),
        ("inflections", "inflectionCandidates",
         any(sense["partOfSpeech"].casefold() in {"adjective", "adj", "noun", "verb"}
             for sense in entry["senses"])),
    )
    for field, candidate_key, applicable in checks:
        if entry[field] or not applicable:
            continue
        sources = [source["site"] for source in evidence if source.get(candidate_key)]
        gaps.append({"field": field, "status": "missing" if sources else "pending_review",
                     "reason": "candidate_not_published" if sources else "field_not_confirmed",
                     "sourceSites": sorted(set(sources))})
    derivative_candidates = [candidate for source in evidence for candidate in source.get("derivativeCandidates", [])]
    derivative_decisions = (decision or {}).get("derivativeDecisions", [])
    if not entry["derivatives"] and derivative_candidates and not derivative_decisions:
        gaps.append({"field": "derivatives", "status": "missing",
                     "reason": "candidate_not_published", "sourceSites": sorted({source["site"] for source in evidence if source.get("derivativeCandidates")})})
    elif not entry["derivatives"] and any(value["action"] == "uncertain" for value in derivative_decisions):
        gaps.append({"field": "derivatives", "status": "pending_review",
                     "reason": "field_not_confirmed", "sourceSites": sorted({source["site"] for source in evidence if source.get("derivativeCandidates")})})
    elif not entry["derivatives"] and not derivative_candidates:
        gaps.append({"field": "derivatives", "status": "pending_review",
                     "reason": "field_not_confirmed", "sourceSites": []})
    return gaps


def advance_word(root: Path, run_id: str, decision: dict | None = None) -> int:
    log, out, request, workflow, state = read_run(root, run_id)
    word = normalize_word(request["word"])
    if state["status"].startswith("paused_"):
        if state["status"] == "paused_relation_review" or (state["status"] == "paused_quality_review" and state.get("resume_stage") == "reviewing_existing_links"):
            if decision is None:
                return 3
            validate_json_schema(decision, HERE / "references" / "link-decision.schema.json")
            write_json(log / "link-decisions.json", decision)
            state = workflow.resume(state)
        elif state["status"] == "paused_agent_decision" and decision is None:
            return 3
        elif state["status"] in ("paused_quality_review", "paused_retryable_error", "paused_user_browser_action") and decision is None:
            if state["status"] == "paused_retryable_error":
                state = workflow.checkpoint(state, event="retry_requested", updates={"retry_count": int(state.get("retry_count", 0)) + 1})
            state = workflow.resume(state)
        elif decision is not None:
            if state.get("resume_stage") == "applying_content_review":
                validate_json_schema(decision, HERE / "references" / "content-review-decision.schema.json")
                write_json(log / "content-review-decision.json", decision)
            else:
                write_json(log / "agent-response.json", decision)
            state = workflow.resume(state)
    while state["status"] != "completed":
        stage = state["status"]
        if stage == "prepared":
            state = move(workflow, state, "validating_input")
        elif stage == "validating_input":
            state = move(workflow, state, "opening_browser")
        elif stage == "opening_browser":
            state = move(workflow, state, "collecting_sources")
        elif stage == "collecting_sources":
            evidence, code = _collect(root, run_id, workflow, state, word)
            if code:
                return code
            state = workflow.load()
            state = move(workflow, state, "parsing_evidence")
        elif stage == "parsing_evidence":
            state = move(workflow, state, "aligning_senses")
        elif stage == "aligning_senses":
            state = move(workflow, state, "building_candidates")
        elif stage == "building_candidates":
            evidence = read_json(log / "evidence.json")
            template = decision_template(evidence, word, request.get("entrySchemaVersion", "1.2"))
            write_json(log / "decision-template.json", template)
            prior_path = entry_records(root) / f"{stable_word_id(word)}.json"
            prior_entry = read_json(prior_path) if prior_path.is_file() else None
            legacy_usage = ([{"itemId": item["itemId"], "kind": group, "text": item["text"],
                              "senseId": sense["senseId"]}
                             for sense in prior_entry["senses"] for group in ("examples", "collocations", "phrases")
                             for item in sense[group]
                             if "translationZh" not in item]
                            if prior_entry and request.get("entrySchemaVersion") in ("1.3", "1.4") else [])
            relation_groups = [{"source": source_index, "groupIndex": group["groupIndex"],
                                "partOfSpeech": group["partOfSpeech"], "gloss": group["gloss"],
                                "candidates": sum(candidate.get("groupIndex") == group["groupIndex"]
                                                  for candidate in source.get("externalCandidates", []))}
                               for source_index, source in enumerate(evidence)
                               for group in source.get("relationGroups", [])]
            write_json(log / "generation_packet.json", {"word": word, "hints": {"partOfSpeech": request.get("partOfSpeechHint"), "meaning": request.get("meaningHint")}, "evidence": evidence, "relationGroups": relation_groups,
                                                      "legacyUsage": legacy_usage,
                                                      "decisionTemplate": "decision-template.json", "reviewTemplate": "relation-review-template.json", "instruction": "校对义项并核对 Oxford、Longman 对应义项；相同英文释义的证据由脚本自动对齐，其余仅在能确认同义项时用 alignmentEvidenceAdditions 增补，不按站点顺序硬合并；有冲突时标记 needsReview。逐项提交关系及派生词判断。1.3 版用 usageUpdates 补齐缺失的中译、置信度和中文加粗区间；英文区间由脚本预填。缺少来源中译时使用 AI 生成并注明置信度。"})
            candidate_review = review_packet(word, evidence)
            validate_json_schema(candidate_review, HERE / "references" / "relation-review-template.schema.json")
            write_json(log / "relation-review-template.json", candidate_review)
            packet = read_json(log / "generation_packet.json")
            packet["evidenceDigest"] = hashlib.sha256((log / "evidence.json").read_bytes()).hexdigest()
            write_json(log / "generation_packet.json", packet)
            state = move(workflow, state, "awaiting_small_ai_decision")
        elif stage == "awaiting_small_ai_decision":
            answer = log / "agent-response.json"
            if not answer.is_file():
                return pause(workflow, state, "paused_agent_decision", "agent_decision_needed", "需要依据 generation_packet.json 提交结构化义项判断", "validating_entry")
            state = move(workflow, state, "validating_entry")
        elif stage == "validating_entry":
            try:
                answer = expand_decision(log, read_json(log / "agent-response.json"))
                if answer.get("needsReview"):
                    return pause(workflow, state, "paused_quality_review", "semantic_conflict", answer.get("reviewReason") or "义项或来源存在待复核冲突", "validating_entry")
                if request.get("entrySchemaVersion") in ("1.3", "1.4") and answer.get("evidenceDigest") != hashlib.sha256(
                        (log / "evidence.json").read_bytes()).hexdigest():
                    raise ValueError("判断未绑定本次证据摘要，请复核本次证据后提交 evidenceDigest")
                validate_decisions(read_json(log / "relation-review-template.json"), answer)
                existing_path = entry_records(root) / f"{stable_word_id(word)}.json"
                existing = read_json(existing_path) if existing_path.is_file() else None
                write_json(log / "base-revision.json", {"revision": existing["revision"] if existing else 0, "sha256": hashlib.sha256(existing_path.read_bytes()).hexdigest() if existing else None})
                entry = build_entry(word, answer, read_json(log / "evidence.json"), existing,
                                    request.get("entrySchemaVersion", "1.2"))
                rendered = validate_entry(entry, HERE / "references" / "entry.schema.json")
                write_text_atomic(log / "candidate.json", rendered)
            except (ValueError, IndexError, KeyError) as exc:
                return pause(workflow, state, "paused_quality_review", "invalid_entry", str(exc), "validating_entry")
            state = move(workflow, state, "verifying_content" if request.get("entrySchemaVersion") in ("1.3", "1.4") else "verifying_sources")
        elif stage == "verifying_content":
            candidate = read_json(log / "candidate.json")
            verification_gaps = verify_observed_pronunciations_and_forms(candidate, read_json(log / "evidence.json"))
            try:
                write_text_atomic(log / "candidate.json", validate_entry(candidate, HERE / "references" / "entry.schema.json"))
                write_json(log / "verification-gaps.json", verification_gaps)
            except ValueError as exc:
                return pause(workflow, state, "paused_quality_review", "invalid_content", str(exc), "validating_entry")
            state = move(workflow, state, "verifying_sources")
        elif stage == "verifying_sources":
            candidate = read_json(log / "candidate.json")
            verification_file = log / "verification-gaps.json"
            if request.get("entrySchemaVersion") in ("1.3", "1.4") and not verification_file.is_file():
                verification_gaps = verify_observed_pronunciations_and_forms(candidate, read_json(log / "evidence.json"))
                write_text_atomic(log / "candidate.json", validate_entry(candidate, HERE / "references" / "entry.schema.json"))
                write_json(verification_file, verification_gaps)
            for sense in candidate["senses"]:
                values = [sense["definitionZh"], sense["definitionEn"], *sense["examples"],
                          *sense["collocations"], *sense["phrases"]]
                values.extend(item["translationZh"] for group in ("examples", "collocations", "phrases")
                              for item in sense[group] if "translationZh" in item)
                for value in values:
                    if value["generationMethod"] == "source_supported" and not value["sourceRefs"]:
                        return pause(workflow, state, "paused_quality_review", "missing_source", "来源支持项缺少来源", "validating_entry")
            evidence = read_json(log / "evidence.json")
            gaps = {"sourceCoverage": [{"site": source["site"], "status": source["coverage"],
                                        "reason": source.get("error", ""), "truncatedSelectors": source.get("truncatedSelectors", [])} for source in evidence],
                    "fieldGaps": assess_field_gaps(candidate, evidence, expand_decision(log, read_json(log / "agent-response.json")))}
            if request.get("entrySchemaVersion") in ("1.3", "1.4"):
                gaps["verificationGaps"] = read_json(verification_file)
            validate_json_schema(gaps, HERE / "references" / "gaps.schema.json")
            write_json(log / "gaps.json", gaps)
            if any(gap["status"] == "missing" for gap in gaps["fieldGaps"]):
                return pause(workflow, state, "paused_quality_review", "confirmed_field_missing",
                             "证据中存在候选，但正式词条缺少对应字段", "validating_entry")
            state = move(workflow, state, "preparing_content_review" if request.get("entrySchemaVersion") == "1.4" else "verifying_relation_candidates")
        elif stage == "preparing_content_review":
            candidate = read_json(log / "candidate.json")
            packet = prepare_content_review(candidate, hashlib.sha256((log / "evidence.json").read_bytes()).hexdigest())
            validate_json_schema(packet, HERE / "references" / "content-review-template.schema.json")
            write_json(log / "content-review-candidate.json", candidate)
            write_json(log / "content-review-template.json", packet)
            state = move(workflow, state, "awaiting_content_review")
        elif stage == "awaiting_content_review":
            if not (log / "content-review-decision.json").is_file():
                return pause(workflow, state, "paused_agent_decision", "content_review_needed",
                             "需要逐义项核对释义、例句与译文", "applying_content_review")
            state = move(workflow, state, "applying_content_review")
        elif stage == "applying_content_review":
            try:
                packet = read_json(log / "content-review-template.json")
                response = read_json(log / "content-review-decision.json")
                validate_json_schema(response, HERE / "references" / "content-review-decision.schema.json")
                candidate = read_json(log / "content-review-candidate.json")
                reviewed_at = iso_timestamp()
                gaps = apply_content_review(candidate, packet, response, run_id, reviewed_at)
                write_text_atomic(log / "candidate.json", validate_entry(candidate, HERE / "references" / "entry.schema.json"))
                write_json(log / "content-review-receipt.json", {"decisionDigest": json_digest(response),
                    "candidateDigest": packet["candidateDigest"], "reviewedAt": reviewed_at})
                gap_data = read_json(log / "gaps.json")
                gap_data["contentGaps"] = gaps
                validate_json_schema(gap_data, HERE / "references" / "gaps.schema.json")
                write_json(log / "gaps.json", gap_data)
            except (ValueError, KeyError, IndexError) as exc:
                return pause(workflow, state, "paused_quality_review", "invalid_content_review", str(exc), "applying_content_review")
            state = move(workflow, state, "verifying_relation_candidates")
        elif stage == "verifying_relation_candidates":
            candidate = read_json(log / "candidate.json")
            sense_ids = {sense["senseId"] for sense in candidate["senses"]}
            if any(relation["sourceSenseId"] not in sense_ids or not relation["sourceRefs"]
                   for relation in candidate.get("pendingRelations", [])):
                return pause(workflow, state, "paused_quality_review", "invalid_relation_candidate",
                             "待采集关系缺少来源义项或来源定位", "validating_entry")
            if any(relation.get("linkStatus") == "lemma_only" and
                   (relation["sourceSenseId"] not in sense_ids or not relation["sourceRefs"] or
                    relation["verificationStatus"] != "agent_reviewed")
                   for relation in candidate["relationships"]):
                return pause(workflow, state, "paused_quality_review", "invalid_relation_decision",
                             "已确认词面关系缺少义项、来源或 Agent 判断", "validating_entry")
            try:
                validate_entry_references(root, candidate)
            except ValueError as exc:
                return pause(workflow, state, "paused_quality_review", "invalid_relation_reference", str(exc), "validating_entry")
            state = move(workflow, state, "publishing")
        elif stage == "publishing":
            candidate_path = log / "candidate.json"
            candidate = read_json(candidate_path)
            if request.get("entrySchemaVersion") == "1.4":
                from rule_inflections import append_rule_forms, candidates_for_entry
                if append_rule_forms(candidate, candidates_for_entry(candidate)):
                    validate_entry(candidate, HERE / "references" / "entry.schema.json")
            out.mkdir(parents=True, exist_ok=True)
            target = entry_records(root) / f"{candidate['wordId']}.json"
            baseline = read_json(log / "base-revision.json")
            with project_lock(root / "logs" / NAME / "dictionary.lock", f"{NAME}:{run_id}"):
                current_hash = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
                entries_dir = target.parent
                for relation in candidate.get("pendingRelations", []):
                    relation["status"] = ("pending_relation_review" if
                        (entries_dir / f"{stable_word_id(relation['targetLemma'])}.json").is_file()
                        else "pending_collection")
                candidate_text = entry_json_text(candidate)
                already_published = target.is_file() and target.read_text(encoding="utf-8") == candidate_text
                if current_hash != baseline["sha256"] and not already_published:
                    return pause(workflow, state, "paused_quality_review", "revision_conflict", "词库词条在本次运行期间已修改", "validating_entry")
                stored = {path: read_json(path) for path in entries_dir.glob("*.json") if path != target}
                family_ids = canonical_family_ids({entry["wordId"]: entry for entry in [*stored.values(), candidate]}, normalize_word)
                candidate["familyId"] = family_ids[candidate["wordId"]]
                candidate_text = entry_json_text(candidate)
                already_published = target.is_file() and target.read_text(encoding="utf-8") == candidate_text
                write_text_atomic(candidate_path, candidate_text)
                updates = {out / "entry.json": candidate_text}
                if not already_published:
                    updates[target] = candidate_text
                for source_path, source_entry in stored.items():
                    changed = False
                    canonical = family_ids[source_entry["wordId"]]
                    if source_entry["familyId"] != canonical:
                        source_entry["familyId"] = canonical
                        changed = True
                    for relation in source_entry.get("pendingRelations", []):
                        if relation["targetLemma"] == word and relation["status"] == "pending_collection":
                            relation["status"] = "pending_relation_review"
                            changed = True
                    if changed:
                        source_entry["revision"] += 1
                        source_entry["updatedAt"] = iso_timestamp()
                        updates[source_path] = validate_entry(source_entry, HERE / "references" / "entry.schema.json")
                write_text_transaction(updates)
                publish_jsonl(root)
            write_json(out / "gaps.json", read_json(log / "gaps.json"))
            write_text_atomic(out / "entry.md", render_entry_markdown(candidate, read_json(out / "gaps.json")))
            state = move(workflow, state, "reviewing_existing_links")
        elif stage == "reviewing_existing_links":
            packet_path = log / "incoming-link-review.json"
            if not packet_path.is_file():
                write_json(packet_path, pending_links(root, word))
            link_packet = read_json(packet_path)
            if link_packet:
                response_path = log / "link-decisions.json"
                if not response_path.is_file():
                    return pause(workflow, state, "paused_relation_review", "link_decision_needed",
                                 "请核对已建目标词条的义项链接", "reviewing_existing_links")
                try:
                    response = read_json(response_path)
                    validate_json_schema(response, HERE / "references" / "link-decision.schema.json")
                    apply_links(root, run_id, link_packet, response, HERE / "references" / "entry.schema.json")
                    publish_jsonl(root)
                except (ValueError, KeyError, IndexError) as exc:
                    write_json(packet_path, pending_links(root, word))
                    return pause(workflow, state, "paused_quality_review", "invalid_link_decision",
                                 str(exc), "reviewing_existing_links")
            state = move(workflow, state, "completed")
        else:
            raise ValueError(f"未知状态：{stage}")
    return 0


def _spelling_exclusions(root: Path, words: list[str]) -> set[tuple[str, str]]:
    """Keep aliases and inflections in their existing relation categories."""
    word_set = set(words)
    excluded = set()
    for word in words:
        entry_path = entry_records(root) / f"{stable_word_id(word)}.json"
        entry = read_json(entry_path)
        forms = [item["text"] for item in entry.get("aliases", [])]
        forms.extend(item["form"]["text"] for item in entry.get("inflections", []))
        for form in forms:
            try:
                normalized = normalize_word(form)
            except ValueError:
                continue
            if normalized in word_set and normalized != word:
                excluded.add(tuple(sorted((word, normalized))))
    from utils.scripts.dictionary_spelling import exclusions, is_excluded
    library = read_all(root / "outputs" / "词汇星图" / "dicts")
    pair_exclusions, family = exclusions(library)
    for i, left in enumerate(words):
        for right in words[i + 1:]:
            if is_excluded(stable_word_id(left), stable_word_id(right), pair_exclusions, family):
                excluded.add(tuple(sorted((left, right))))
    return excluded


def _relation_candidates(root: Path, rows: list[dict]) -> list[dict]:
    words = [row["word"] for row in rows]
    word_set = set(words)
    evidence = {}
    for row in rows:
        child = row.get("runId")
        if child:
            file = paths(root, child)[0] / "evidence.json"
            evidence[row["word"]] = read_json(file) if file.is_file() else []
    index: dict[str, set[str]] = defaultdict(set)
    for word in words:
        index["prefix:" + word[:4]].add(word)
        stem = re.sub(r"(ation|ment|ness|ing|ed|er|ly|s)$", "", word)
        if len(stem) >= 3:
            index["stem:" + stem].add(word)
    pair_reasons: dict[tuple[str, str], set[str]] = defaultdict(set)
    for key, members in index.items():
        ordered = sorted(members)
        for i, left in enumerate(ordered):
            for right in ordered[i + 1:]:
                if key.startswith("stem:"):
                    pair_reasons[(left, right)].add("word_family_candidate")
                elif key.startswith("prefix:") and len(left) >= 5 and len(right) >= 5:
                    pair_reasons[(left, right)].add("shared_prefix")
    spelling_exclusions = _spelling_exclusions(root, words)
    for pair in iter_similar_pairs(words):
        if pair not in spelling_exclusions:
            pair_reasons[pair].add("spelling_similarity")
    for left in words:
        entry_path = entry_records(root) / f"{stable_word_id(left)}.json"
        if entry_path.is_file():
            entry = read_json(entry_path)
            structured = [value["form"]["text"] for value in entry.get("inflections", [])]
            structured.extend(value["word"] for value in entry.get("derivatives", []))
            for sense in entry.get("senses", []):
                structured.extend(value["text"] for group in ("collocations", "phrases") for value in sense.get(group, []))
            tokens = set(re.findall(r"[a-z]+(?:[-'][a-z]+)*", " ".join(structured).lower()))
            for right in (tokens & word_set) - {left}:
                pair_reasons[tuple(sorted((left, right)))].add("entry_form_or_phrase")
            for relation in entry.get("relationships", []):
                target = relation.get("targetLemma")
                if relation.get("linkStatus") == "lemma_only" and target in word_set and target != left:
                    pair_reasons[tuple(sorted((left, target)))].add("confirmed_lemma_relation")
        for source in evidence.get(left, []):
            tokens = set(re.findall(r"[a-z]+(?:[-'][a-z]+)*", " ".join(fragment["summary"].lower() for fragment in source.get("fragments", []))))
            for right in (tokens & word_set) - {left}:
                pair_reasons[tuple(sorted((left, right)))].add("thesaurus_candidate" if source["site"] == "thesaurus" else "source_cooccurrence")
    return [{"left": left, "right": right, "reasons": sorted(reasons)} for (left, right), reasons in sorted(pair_reasons.items()) if reasons]


def verify_word_snapshot(snapshot: dict, current: dict) -> None:
    def content_preserved(original: dict, latest: dict) -> bool:
        stable_fields = ("itemId", "text", "generationMethod", "verificationStatus", "verificationRef", "confidence")
        if any(original.get(field) != latest.get(field) for field in stable_fields):
            return False
        def locations(value: dict) -> set[tuple[str, str, str, str]]:
            return {(ref["site"], ref["url"], ref["locator"], ref["summary"]) for ref in value["sourceRefs"]}
        return locations(original) <= locations(latest)

    if snapshot["wordId"] != current["wordId"] or snapshot["lemma"] != current["lemma"]:
        raise ValueError("总词库词条身份与运行产物不一致")
    if current["revision"] < snapshot["revision"]:
        raise ValueError("总词库修订号早于运行产物")
    if current["revision"] > snapshot["revision"]:
        # A later run has superseded this snapshot. Verify the archived result
        # with its own schema and available review records; current content may differ.
        return
    for field, key in (("senses", "senseId"), ("pronunciations", "pronunciationId"),
                       ("inflections", "formId"), ("aliases", "itemId")):
        latest = {value[key]: value for value in current[field]}
        for original in snapshot[field]:
            value = latest.get(original[key])
            if value is None:
                raise ValueError(f"总词库缺少已发布 {field}：{original[key]}")
            if field != "senses":
                if value != original:
                    raise ValueError(f"总词库已发布 {field} 内容不一致：{original[key]}")
                continue
            if value["partOfSpeech"] != original["partOfSpeech"] or any(order not in value["sourceOrders"] for order in original["sourceOrders"]):
                raise ValueError(f"总词库已发布义项来源顺序不一致：{original[key]}")
            for definition in ("definitionZh", "definitionEn"):
                if not content_preserved(original[definition], value[definition]):
                    raise ValueError(f"总词库已发布义项内容不一致：{original[key]}")
            for group in ("examples", "collocations", "phrases", "grammarTags", "registerTags"):
                actual = {item["itemId"]: item for item in value[group]}
                if any(item["itemId"] not in actual or not content_preserved(item, actual[item["itemId"]]) for item in original[group]):
                    raise ValueError(f"总词库已发布 {group} 内容不一致：{original[key]}")
                if group in ("examples", "collocations", "phrases"):
                    for old_item in original[group]:
                        new_item = actual[old_item["itemId"]]
                        if old_item.get("emphasis") != new_item.get("emphasis") or (
                            old_item.get("translationZh") and
                            (not new_item.get("translationZh") or not content_preserved(old_item["translationZh"], new_item["translationZh"]))
                        ):
                            raise ValueError(f"总词库已发布 {group} 译文或强调区间不一致：{old_item['itemId']}")
    latest_candidates = {candidate["candidateId"]: candidate for candidate in current.get("pendingRelations", [])}
    for candidate in snapshot.get("pendingRelations", []):
        latest = latest_candidates.get(candidate["candidateId"])
        old_refs = {(ref["site"], ref["url"], ref["locator"], ref["summary"]) for ref in candidate["sourceRefs"]}
        if latest is None:
            promoted = next((relation for relation in current["relationships"]
                if (relation.get("targetWordId") == stable_word_id(candidate["targetLemma"])
                    or relation.get("targetLemma") == candidate["targetLemma"])
                and relation.get("sourceSenseId") == candidate["sourceSenseId"]
                and (relation["type"] == candidate["proposedType"] or
                     (candidate["proposedType"] == "synonym_or_near_synonym" and
                      relation["type"] in ("synonym", "near_synonym")))), None)
            if promoted is None:
                raise ValueError(f"总词库缺少已发布待核验关系：{candidate['candidateId']}")
            new_refs = {(ref["site"], ref["url"], ref["locator"], ref["summary"]) for ref in promoted["sourceRefs"]}
            if not old_refs <= new_refs:
                raise ValueError(f"正式关系未保留候选来源：{candidate['candidateId']}")
            continue
        if any(candidate[key] != latest[key] for key in ("targetLemma", "sourceSenseId", "proposedType")):
            raise ValueError(f"总词库待核验关系被修改：{candidate['candidateId']}")
        new_refs = {(ref["site"], ref["url"], ref["locator"], ref["summary"]) for ref in latest["sourceRefs"]}
        if not old_refs <= new_refs:
            raise ValueError(f"待核验关系来源被修改：{candidate['candidateId']}")
    latest_relations = {relation["relationshipId"]: relation for relation in current["relationships"]}
    for original in snapshot["relationships"]:
        latest = latest_relations.get(original["relationshipId"])
        if latest is None or latest["type"] != original["type"] or latest.get("sourceSenseId") != original.get("sourceSenseId"):
            raise ValueError(f"已发布关系缺失或被改写：{original['relationshipId']}")
        if original.get("linkStatus") == "lemma_only":
            if latest.get("targetLemma") != original["targetLemma"] or latest.get("linkStatus") not in ("lemma_only", "linked"):
                raise ValueError(f"词面关系目标被改写：{original['relationshipId']}")
        elif latest.get("targetWordId") != original.get("targetWordId"):
            raise ValueError(f"已链接关系目标被改写：{original['relationshipId']}")
        old_refs = {(ref["site"], ref["url"], ref["locator"], ref["summary"]) for ref in original["sourceRefs"]}
        new_refs = {(ref["site"], ref["url"], ref["locator"], ref["summary"]) for ref in latest["sourceRefs"]}
        if not old_refs <= new_refs:
            raise ValueError(f"已发布关系来源被改写：{original['relationshipId']}")
    latest_derivatives = {value["derivativeId"]: value for value in current["derivatives"]}
    for original in snapshot["derivatives"]:
        latest = latest_derivatives.get(original["derivativeId"])
        if latest is None or latest["word"] != original["word"]:
            raise ValueError(f"已发布派生词缺失或被改写：{original['derivativeId']}")
        if original["status"] == "linked" and latest.get("targetWordId") != original.get("targetWordId"):
            raise ValueError(f"已链接派生词目标被改写：{original['derivativeId']}")
        old_refs = {(ref["site"], ref["url"], ref["locator"], ref["summary"])
                    for ref in original.get("sourceRefs", [])}
        new_refs = {(ref["site"], ref["url"], ref["locator"], ref["summary"])
                    for ref in latest.get("sourceRefs", [])}
        if not old_refs <= new_refs:
            raise ValueError(f"已发布派生词来源被改写：{original['derivativeId']}")


def advance_batch(root: Path, run_id: str, decision: dict | None = None) -> int:
    log, out, request, workflow, state = read_run(root, run_id)
    if state["status"].startswith("paused_"):
        if state["status"] == "paused_word":
            rows = read_json(log / "words.json")
            pending = next((r for r in rows if r.get("status") != "completed"), None)
            if pending and pending.get("runId"):
                code = advance_word(root, pending["runId"], decision)
                if code:
                    return 3
                pending["status"] = "completed"
                write_json(log / "words.json", rows)
            state = workflow.resume(workflow.load())
        elif state["status"] == "paused_relation_review":
            if decision is None:
                return 3
            validate_json_schema(decision, HERE / "references" / "relation-decision.schema.json")
            write_json(log / "relation-decisions.json", decision)
            state = workflow.resume(state)
        else:
            state = workflow.resume(state)
    while state["status"] != "completed":
        stage = state["status"]
        if stage == "prepared":
            state = move(workflow, state, "validating_list")
        elif stage == "validating_list":
            if "inlineText" in request:
                raw = request["inlineText"].encode("utf-8")
                kind = "text"
            else:
                raw = Path(request["inputPath"]).read_bytes()
                kind = request["format"]
            (log / "input.bin").write_bytes(raw)
            source = Path(request["inputPath"]).name if "inputPath" in request else "inline"
            write_json(log / "input-meta.json", {"sha256": hashlib.sha256(raw).hexdigest(), "format": kind, "source": source})
            state = move(workflow, state, "normalizing_list")
        elif stage == "normalizing_list":
            raw = (log / "input.bin").read_bytes()
            rows = parse_word_list(raw, read_json(log / "input-meta.json")["format"], request.get("wordColumn"), request.get("partOfSpeechColumn"), request.get("meaningColumn"))
            write_json(log / "words.json", rows)
            state = move(workflow, state, "processing_words")
        elif stage == "processing_words":
            rows = read_json(log / "words.json")
            for row in rows:
                if row.get("status") == "completed":
                    continue
                if "runId" not in row:
                    child_request = {"kind": "word", "word": row["word"], "parentRunId": run_id,
                                     "entrySchemaVersion": request.get("entrySchemaVersion", "1.2"),
                                     "partOfSpeechHint": row.get("partOfSpeechHint", ""), "meaningHint": row.get("meaningHint", "")}
                    row["runId"] = new_run(root, child_request)
                    write_json(log / "words.json", rows)
                code = advance_word(root, row["runId"])
                if code:
                    return pause(workflow, state, "paused_word", "word_pending", f"单词 {row['word']} 运行暂停：{row['runId']}", "processing_words")
                row["status"] = "completed"
                write_json(log / "words.json", rows)
            state = move(workflow, state, "discovering_intra_list_relations")
        elif stage == "discovering_intra_list_relations":
            candidates = _relation_candidates(root, read_json(log / "words.json"))
            write_json(log / "relation-candidates.json", candidates)
            review_packet = []
            for pair in candidates:
                if set(pair["reasons"]) == {"spelling_similarity"}:
                    continue
                sides = {}
                for word in (pair["left"], pair["right"]):
                    entry = read_json(entry_records(root) / f"{stable_word_id(word)}.json")
                    sides[word] = [{"senseId": sense["senseId"], "partOfSpeech": sense["partOfSpeech"],
                                    "definitionEn": sense["definitionEn"]["text"],
                                    "dictionaryRefs": [ref for ref in sense["definitionEn"]["sourceRefs"]
                                                       if ref["site"] in ("cambridge", "oxford", "longman")]} for sense in entry["senses"]]
                review_packet.append({**pair, "senses": sides})
            write_json(log / "relation-review-packet.json", review_packet)
            state = move(workflow, state, "verifying_relations")
        elif stage == "verifying_relations":
            review_packet = read_json(log / "relation-review-packet.json")
            if review_packet and not (log / "relation-decisions.json").is_file():
                return pause(workflow, state, "paused_relation_review", "relation_decision_needed", "请逐义项核验表内关系候选", "verifying_relations")
            state = move(workflow, state, "publishing_list_result")
        elif stage == "publishing_list_result":
            rows = read_json(log / "words.json")
            candidates = read_json(log / "relation-candidates.json")
            decisions = read_json(log / "relation-decisions.json") if (log / "relation-decisions.json").is_file() else {"accepted": []}
            accepted = []
            spelling_exclusions = _spelling_exclusions(root, [row["word"] for row in rows])
            for choice in decisions.get("accepted", []):
                match = next((c for c in candidates if c["left"] == choice.get("left") and c["right"] == choice.get("right")), None)
                if not match or choice.get("type") not in ("synonym", "near_synonym", "antonym", "spelling_similar", "family"):
                    raise ValueError("关系判断不对应候选或类型无效")
                if choice["type"] == "spelling_similar":
                    pair = (match["left"], match["right"])
                    if pair in spelling_exclusions or not meets_subsequence_threshold(*pair):
                        raise ValueError("拼写相似关系未达到子列相似度阈值或属于已有词形关系")
                    continue  # This relation is verified and published by the script below.
                if choice["type"] in ("synonym", "near_synonym", "antonym") and not (choice.get("sourceSenseId") and choice.get("targetSenseId")):
                    raise ValueError("同反义关系必须同时指定两端义项 ID")
                refs = []
                for pointer in choice.get("evidence", []):
                    if pointer.get("word") not in (match["left"], match["right"]):
                        raise ValueError("关系来源指向表外单词")
                    row = next(r for r in rows if r["word"] == pointer["word"])
                    sources = read_json(paths(root, row["runId"])[0] / "evidence.json")
                    source = sources[pointer["source"]]
                    refs.append(source_ref(source["fragments"][pointer["fragment"]], source))
                if choice["type"] in ("synonym", "near_synonym", "antonym") and not refs:
                    raise ValueError("同反义关系需要具体来源定位")
                if choice.get("derivationConfirmed") and (choice["type"] != "family" or not refs):
                    raise ValueError("派生词确认必须是有来源定位的词族关系")
                accepted.append({**match, "type": choice["type"], "sourceSenseId": choice.get("sourceSenseId"), "targetSenseId": choice.get("targetSenseId"), "derivationConfirmed": bool(choice.get("derivationConfirmed", False)), "verificationStatus": "agent_reviewed", "sourceRefs": refs})
            family_groups = {row["word"]: read_json(entry_records(root) / stable_word_id(row["word"]))["familyId"] for row in rows}
            for relation in accepted:
                if relation["type"] == "family":
                    a, b = family_groups[relation["left"]], family_groups[relation["right"]]
                    family_groups = {word: a if group == b else group for word, group in family_groups.items()}
            for pair in candidates:
                if family_groups[pair["left"]] == family_groups[pair["right"]]:
                    continue
                words_pair = (pair["left"], pair["right"])
                if words_pair in spelling_exclusions or not meets_subsequence_threshold(*words_pair):
                    continue
                accepted.append({**pair, "type": "spelling_similar", "sourceSenseId": None,
                                 "targetSenseId": None, "derivationConfirmed": False,
                                 "verificationStatus": "automatic_passed", "sourceRefs": []})
            updates = {}
            with project_lock(root / "logs" / NAME / "dictionary.lock", f"{NAME}:{run_id}:relations"):
                for relation in accepted:
                    left_path = entry_records(root) / f"{stable_word_id(relation['left'])}.json"
                    right_path = entry_records(root) / f"{stable_word_id(relation['right'])}.json"
                    left_entry = updates.get(left_path) or read_json(left_path)
                    right_entry = updates.get(right_path) or read_json(right_path)
                    if relation["sourceSenseId"] and relation["sourceSenseId"] not in {s["senseId"] for s in left_entry["senses"]}:
                        raise ValueError("关系源义项不存在")
                    if relation["targetSenseId"] and relation["targetSenseId"] not in {s["senseId"] for s in right_entry["senses"]}:
                        raise ValueError("关系目标义项不存在")
                    semantic = relation["type"] in ("synonym", "near_synonym", "antonym")
                    if semantic:
                        matched_pending = []
                        for owner, other, sense_id in ((left_entry, right_entry, relation["sourceSenseId"]),
                                                       (right_entry, left_entry, relation["targetSenseId"])):
                            matched_pending.extend(candidate for candidate in owner.get("pendingRelations", [])
                                if candidate["targetLemma"] == other["lemma"] and candidate["sourceSenseId"] == sense_id
                                and (candidate["proposedType"] == relation["type"] or
                                     (candidate["proposedType"] == "synonym_or_near_synonym" and
                                      relation["type"] in ("synonym", "near_synonym"))))
                        confirmed_lemma = [item for owner, other, sense_id in
                            ((left_entry, right_entry, relation["sourceSenseId"]),
                             (right_entry, left_entry, relation["targetSenseId"]))
                            for item in owner["relationships"]
                            if item.get("linkStatus") == "lemma_only" and item.get("targetLemma") == other["lemma"]
                            and item.get("sourceSenseId") == sense_id and item["type"] == relation["type"]]
                        already_linked = [item for owner, other, sense_id, other_sense_id in
                            ((left_entry, right_entry, relation["sourceSenseId"], relation["targetSenseId"]),
                             (right_entry, left_entry, relation["targetSenseId"], relation["sourceSenseId"]))
                            for item in owner["relationships"]
                            if item.get("targetWordId") == other["wordId"] and item.get("sourceSenseId") == sense_id
                            and item.get("targetSenseId") == other_sense_id and item["type"] == relation["type"]]
                        candidate_refs = [ref for candidate in matched_pending for ref in candidate["sourceRefs"]]
                        candidate_refs += [ref for item in [*confirmed_lemma, *already_linked] for ref in item["sourceRefs"]]
                        if not (matched_pending or confirmed_lemma or already_linked):
                            raise ValueError("同反义关系缺少对应的 Thesaurus.com 候选")
                        if not any(ref["site"] == "thesaurus" for ref in candidate_refs):
                            raise ValueError("同反义关系缺少 Thesaurus.com 义项证据")
                        dictionary_refs = []
                        for owner, sense_id in ((left_entry, relation["sourceSenseId"]),
                                                (right_entry, relation["targetSenseId"])):
                            sense = next(s for s in owner["senses"] if s["senseId"] == sense_id)
                            refs = [ref for ref in sense["definitionEn"]["sourceRefs"]
                                    if ref["site"] in ("cambridge", "oxford", "longman")]
                            if not refs:
                                raise ValueError("同反义关系两端义项均需学习词典释义证据")
                            dictionary_refs.extend(refs)
                        all_refs = [*relation["sourceRefs"], *dictionary_refs, *candidate_refs]
                        relation["sourceRefs"] = list({(ref["site"], ref["url"], ref["locator"], ref["summary"]): ref
                                                       for ref in all_refs}.values())
                    digest = hashlib.sha256(f"{relation['left']}|{relation['right']}|{relation['type']}|{relation['sourceSenseId']}|{relation['targetSenseId']}".encode("utf-8")).hexdigest()[:20]
                    relation_id = "r_" + digest
                    directions = ((left_entry, right_entry, left_path, relation["sourceSenseId"], relation["targetSenseId"], relation_id),)
                    if semantic or relation["type"] == "spelling_similar":
                        reverse_id = "r_" + hashlib.sha256(f"{relation['right']}|{relation['left']}|{relation['type']}|{relation['targetSenseId']}|{relation['sourceSenseId']}".encode("utf-8")).hexdigest()[:20]
                        directions += ((right_entry, left_entry, right_path, relation["targetSenseId"], relation["sourceSenseId"], reverse_id),)
                    for owner, other, owner_path, source_sense, target_sense, directional_id in directions:
                        changed = False
                        promotable = next((item for item in owner["relationships"]
                            if item.get("linkStatus") == "lemma_only" and item.get("targetLemma") == other["lemma"]
                            and item.get("sourceSenseId") == source_sense and item["type"] == relation["type"]), None)
                        existing_link = next((item for item in owner["relationships"]
                            if item.get("targetWordId") == other["wordId"] and item.get("sourceSenseId") == source_sense
                            and item.get("targetSenseId") == target_sense and item["type"] == relation["type"]), None)
                        if promotable:
                            promotable.update({"targetWordId": other["wordId"], "targetSenseId": target_sense,
                                               "targetLemma": other["lemma"], "linkStatus": "linked", "intraList": True,
                                               "sourceRefs": relation["sourceRefs"]})
                            changed = True
                        elif existing_link:
                            merged_refs = list({(ref["site"], ref["url"], ref["locator"], ref["summary"]): ref
                                                for ref in [*existing_link["sourceRefs"], *relation["sourceRefs"]]}.values())
                            if merged_refs != existing_link["sourceRefs"] or not existing_link.get("intraList"):
                                existing_link["sourceRefs"] = merged_refs
                                existing_link["intraList"] = True
                                changed = True
                        elif not any(r["relationshipId"] == directional_id for r in owner["relationships"]):
                            item = {"relationshipId": directional_id, "type": relation["type"],
                                    "targetWordId": other["wordId"], "targetLemma": other["lemma"],
                                    "linkStatus": "linked", "intraList": True,
                                    "verificationStatus": relation["verificationStatus"], "sourceRefs": relation["sourceRefs"]}
                            if source_sense:
                                item["sourceSenseId"] = source_sense
                            if target_sense:
                                item["targetSenseId"] = target_sense
                            owner["relationships"].append(item)
                            changed = True
                        if semantic:
                            remaining = [candidate for candidate in owner.get("pendingRelations", [])
                                if not (candidate["targetLemma"] == other["lemma"] and
                                        candidate["sourceSenseId"] == source_sense and
                                        (candidate["proposedType"] == relation["type"] or
                                         (candidate["proposedType"] == "synonym_or_near_synonym" and
                                          relation["type"] in ("synonym", "near_synonym"))))]
                            if len(remaining) != len(owner.get("pendingRelations", [])):
                                owner["pendingRelations"] = remaining
                                changed = True
                        if changed:
                            if owner["schemaVersion"] in ("1.0", "1.1"):
                                owner["schemaVersion"] = "1.2"
                            owner.setdefault("pendingRelations", [])
                            for prior_relation in owner["relationships"]:
                                if prior_relation.get("targetWordId"):
                                    prior_relation.setdefault("linkStatus", "linked")
                            owner["revision"] += 1
                            owner["updatedAt"] = iso_timestamp()
                            validate_entry(owner, HERE / "references" / "entry.schema.json")
                            updates[owner_path] = owner
                    if relation["type"] == "family" and left_entry["familyId"] != right_entry["familyId"]:
                        old_families = {left_entry["familyId"], right_entry["familyId"]}
                        canonical_family = min(old_families)
                        for entry_path in left_path.parent.glob("*.json"):
                            member = updates.get(entry_path) or read_json(entry_path)
                            if member["familyId"] in old_families and member["familyId"] != canonical_family:
                                member["familyId"] = canonical_family
                                member["revision"] += 1
                                member["updatedAt"] = iso_timestamp()
                                validate_entry(member, HERE / "references" / "entry.schema.json")
                                updates[entry_path] = member
                    if relation["type"] == "family" and relation.get("derivationConfirmed"):
                        left_entry = updates.get(left_path, left_entry)
                        right_entry = updates.get(right_path, right_entry)
                        for source_entry, target_entry, source_path in ((left_entry, right_entry, left_path), (right_entry, left_entry, right_path)):
                            derivative_id = "d_" + hashlib.sha256(f"{source_entry['wordId']}|{target_entry['wordId']}".encode("utf-8")).hexdigest()[:20]
                            existing_derivative = next((value for value in source_entry["derivatives"]
                                if value["word"] == target_entry["lemma"] and value["status"] == "candidate"), None)
                            if existing_derivative:
                                existing_derivative.update({"targetWordId": target_entry["wordId"], "status": "linked"})
                                existing_derivative["sourceRefs"] = list({
                                    (ref["site"], ref["url"], ref["locator"], ref["summary"]): ref
                                    for ref in [*existing_derivative.get("sourceRefs", []), *relation["sourceRefs"]]
                                }.values())
                            elif not any(value["derivativeId"] == derivative_id for value in source_entry["derivatives"]):
                                source_entry["derivatives"].append({"derivativeId": derivative_id,
                                                                     "word": target_entry["lemma"],
                                                                     "targetWordId": target_entry["wordId"],
                                                                     "status": "linked"})
                            else:
                                continue
                            source_entry["revision"] += 1
                            source_entry["updatedAt"] = iso_timestamp()
                            validate_entry(source_entry, HERE / "references" / "entry.schema.json")
                            updates[source_path] = source_entry
                if updates:
                    write_text_transaction({path: entry_json_text(value) for path, value in updates.items()})
                    publish_jsonl(root)
            site_counts = {site: 0 for site in SITES}
            for row in rows:
                child_evidence = paths(root, row["runId"])[0] / "evidence.json"
                if child_evidence.is_file():
                    for source in read_json(child_evidence):
                        if source["site"] in site_counts and source.get("coverage") == "extracted":
                            site_counts[source["site"]] += 1
            outside = {}
            known = {row["word"] for row in rows}
            for row in rows:
                source_entry = read_json(entry_records(root) / f"{stable_word_id(row['word'])}.json")
                for candidate in source_entry.get("pendingRelations", []):
                    target_word = candidate["targetLemma"]
                    if target_word in known or (entry_records(root) / f"{stable_word_id(target_word)}.json").is_file():
                        continue
                    outside[(row["word"], target_word)] = {"fromWord": row["word"], "word": target_word,
                                                              "status": "pending_collection", "sourceRef": candidate["sourceRefs"][0]}
            result = {"input": read_json(log / "input-meta.json"), "words": rows, "intraListRelations": accepted,
                      "pendingExternalCandidates": list(outside.values()),
                      "coverage": {"completed": sum(r.get("status") == "completed" for r in rows), "total": len(rows), "sites": site_counts}}
            validate_json_schema(result, HERE / "references" / "batch-result.schema.json")
            out.mkdir(parents=True, exist_ok=True)
            write_json(out / "batch-result.json", result)
            state = move(workflow, state, "completed")
        else:
            raise ValueError(f"未知状态：{stage}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="可恢复的词表与单词 JSON 生成")
    parser.add_argument("command", choices=["start-word", "start-list", "resume", "status", "verify", "deliver"])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--run-id")
    parser.add_argument("--word")
    parser.add_argument("--pos-hint")
    parser.add_argument("--meaning-hint")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--inline-text")
    parser.add_argument("--format", choices=["text", "csv", "json", "jsonl"])
    parser.add_argument("--word-column")
    parser.add_argument("--pos-column")
    parser.add_argument("--meaning-column")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.command in ("start-word", "start-list", "resume"):
            from utils.scripts.dictionary_migration import migrate
            migrate(root, lambda value: validate_entry(value, HERE / "references" / "entry.schema.json"))
        if args.command == "start-word":
            if not args.word:
                raise ValueError("start-word 需要 --word")
            run_id = new_run(root, {"kind": "word", "word": normalize_word(args.word),
                                    "partOfSpeechHint": args.pos_hint or "", "meaningHint": args.meaning_hint or ""})
            code = advance_word(root, run_id)
        elif args.command == "start-list":
            if bool(args.input) == bool(args.inline_text):
                raise ValueError("start-list 必须且只能指定 --input 或 --inline-text")
            request = {"kind": "batch"}
            if args.input:
                suffix = args.input.suffix.lower().lstrip(".")
                request.update({"inputPath": str(args.input.resolve()), "format": args.format or (suffix if suffix in ("csv", "json", "jsonl") else "text")})
            else:
                request["inlineText"] = args.inline_text
            if args.word_column:
                request["wordColumn"] = args.word_column
            if args.pos_column:
                request["partOfSpeechColumn"] = args.pos_column
            if args.meaning_column:
                request["meaningColumn"] = args.meaning_column
            run_id = new_run(root, request)
            code = advance_batch(root, run_id)
        else:
            if not args.run_id:
                raise ValueError("需要 --run-id")
            run_id = args.run_id
            log, out, request, workflow, state = read_run(root, run_id)
            if args.command == "resume":
                decision = read_json(args.input) if args.input else None
                code = advance_word(root, run_id, decision) if request["kind"] == "word" else advance_batch(root, run_id, decision)
            elif args.command == "status":
                snapshot = dict(state)
                if request["kind"] == "batch" and (log / "words.json").is_file():
                    snapshot["words"] = [{"word": row["word"], "sourceLines": row["sourceLines"],
                                          "runId": row.get("runId"), "status": row.get("status", "pending")}
                                         for row in read_json(log / "words.json")]
                print(json.dumps(snapshot, ensure_ascii=False, indent=2))
                return 0
            elif args.command == "verify":
                if state["status"] != "completed":
                    raise ValueError("运行尚未完成")
                if request["kind"] == "word":
                    entry_path = out / "entry.json"
                    entry = read_json(entry_path)
                    validate_entry(entry, HERE / "references" / "entry.schema.json")
                    verify_raw_confidence(entry_path.read_text(encoding="utf-8"))
                    validate_json_schema(read_json(out / "gaps.json"), HERE / "references" / "gaps.schema.json")
                    if (out / "entry.md").read_text(encoding="utf-8") != render_entry_markdown(entry, read_json(out / "gaps.json")):
                        raise ValueError("可读词条与 JSON 不一致")
                    target = entry_records(root) / f"{entry['wordId']}.json"
                    verify_raw_confidence(target.read_text(encoding="utf-8"))
                    current = read_json(target)
                    validate_entry(current, HERE / "references" / "entry.schema.json")
                    validate_entry_references(root, current)
                    verify_word_snapshot(entry, current)
                    if (log / "content-review-template.json").is_file():
                        packet = read_json(log / "content-review-template.json")
                        response = read_json(log / "content-review-decision.json")
                        receipt = read_json(log / "content-review-receipt.json")
                        if packet["evidenceDigest"] != hashlib.sha256((log / "evidence.json").read_bytes()).hexdigest():
                            raise ValueError("内容审查证据摘要与本次采集不一致")
                        validate_json_schema(packet, HERE / "references" / "content-review-template.schema.json")
                        validate_json_schema(response, HERE / "references" / "content-review-decision.schema.json")
                        validate_json_schema(receipt, HERE / "references" / "content-review-receipt.schema.json")
                        if receipt["decisionDigest"] != json_digest(response) or receipt["candidateDigest"] != packet["candidateDigest"]:
                            raise ValueError("内容审查收据与决定不一致")
                        expected_gaps = verify_content_review(entry, packet, response,
                                                               read_json(log / "content-review-candidate.json"), run_id,
                                                               receipt["reviewedAt"])
                        if read_json(out / "gaps.json").get("contentGaps") != expected_gaps:
                            raise ValueError("内容缺口与审查决定不一致")
                else:
                    result = read_json(out / "batch-result.json")
                    validate_json_schema(result, HERE / "references" / "batch-result.schema.json")
                    if result["coverage"]["completed"] != result["coverage"]["total"]:
                        raise ValueError("批次未完成全部单词")
                    for row in result["words"]:
                        child_log, child_out, _, _, child_state = read_run(root, row["runId"])
                        if child_state["status"] != "completed" or not (child_out / "entry.json").is_file():
                            raise ValueError(f"批次单词运行不完整：{row['word']}")
                        current = read_json(entry_records(root) / f"{stable_word_id(row['word'])}.json")
                        validate_entry(current, HERE / "references" / "entry.schema.json")
                        validate_entry_references(root, current)
                        child_entry = read_json(child_out / "entry.json")
                        verify_word_snapshot(child_entry, current)
                        if (child_log / "content-review-template.json").is_file():
                            child_packet = read_json(child_log / "content-review-template.json")
                            child_response = read_json(child_log / "content-review-decision.json")
                            child_receipt = read_json(child_log / "content-review-receipt.json")
                            validate_json_schema(child_packet, HERE / "references" / "content-review-template.schema.json")
                            validate_json_schema(child_response, HERE / "references" / "content-review-decision.schema.json")
                            validate_json_schema(child_receipt, HERE / "references" / "content-review-receipt.schema.json")
                            if child_receipt["decisionDigest"] != json_digest(child_response) or child_receipt["candidateDigest"] != child_packet["candidateDigest"]:
                                raise ValueError("批次单词内容审查收据与决定不一致")
                            if child_packet["evidenceDigest"] != hashlib.sha256((child_log / "evidence.json").read_bytes()).hexdigest():
                                raise ValueError("批次单词内容审查证据摘要不一致")
                            expected_gaps = verify_content_review(child_entry, child_packet, child_response,
                                                                   read_json(child_log / "content-review-candidate.json"), row["runId"],
                                                                   child_receipt["reviewedAt"])
                            if read_json(child_out / "gaps.json").get("contentGaps") != expected_gaps:
                                raise ValueError("批次单词内容缺口与审查决定不一致")
                    for relation in result["intraListRelations"]:
                        left = read_json(entry_records(root) / f"{stable_word_id(relation['left'])}.json")
                        if not any(item["type"] == relation["type"] and item["targetWordId"] == stable_word_id(relation["right"])
                                   and item.get("sourceSenseId") == relation.get("sourceSenseId")
                                   and item.get("targetSenseId") == relation.get("targetSenseId") for item in left["relationships"]):
                            raise ValueError("批次关系未写入总词库")
                        if relation["type"] == "spelling_similar" and relation["verificationStatus"] == "automatic_passed":
                            if not meets_subsequence_threshold(relation["left"], relation["right"]):
                                raise ValueError("批次拼写相似关系低于子列相似度阈值")
                        if relation["type"] in ("synonym", "near_synonym", "antonym") or (
                                relation["type"] == "spelling_similar" and relation["verificationStatus"] == "automatic_passed"):
                            right = read_json(entry_records(root) / f"{stable_word_id(relation['right'])}.json")
                            if not any(item["type"] == relation["type"] and item["targetWordId"] == left["wordId"]
                                       and item.get("sourceSenseId") == relation["targetSenseId"]
                                       and item.get("targetSenseId") == relation["sourceSenseId"]
                                       for item in right["relationships"]):
                                raise ValueError("批次关系反向链接缺失")
                code = 0
            else:
                if state["status"] != "completed":
                    raise ValueError("运行尚未完成")
                if request["kind"] == "word":
                    if (out / "entry.md").read_text(encoding="utf-8") != render_entry_markdown(
                            read_json(out / "entry.json"), read_json(out / "gaps.json")):
                        raise ValueError("可读词条与 JSON 不一致")
                    print((out / "entry.md").read_text(encoding="utf-8"))
                else:
                    result = read_json(out / "batch-result.json")
                    print(json.dumps({"run_id": run_id,
                                      "result": f"outputs/{NAME}/runs/{run_id}/batch-result.json",
                                      "coverage": result["coverage"],
                                      "relations": len(result["intraListRelations"]),
                                      "pendingExternalCandidates": len(result["pendingExternalCandidates"])}, ensure_ascii=False))
                return 0
        if args.command in ("start-word", "start-list", "resume", "verify"):
            publish_jsonl(root)
            if args.command in ("start-word", "start-list", "resume"):
                from utils.scripts.dictionary_spelling import sync
                sync(root)
            if args.command == "verify":
                current = read_all(root / "outputs" / "词汇星图" / "dicts")
                for path in (entry_records(root)).glob("*.json"):
                    entry = read_json(path)
                    if current.get(entry["wordId"]) != entry:
                        raise ValueError(f"JSONL 词条与运行词条不一致：{entry['wordId']}")
        print(json.dumps({"run_id": run_id, "status": read_run(root, run_id)[-1]["status"]}, ensure_ascii=False))
        return code
    except (ValueError, KeyError, IndexError, FileNotFoundError) as exc:
        if "run_id" in locals() and args.command in ("start-list", "resume"):
            try:
                _, _, request, workflow, state = read_run(root, run_id)
                if request["kind"] == "batch" and state["status"] == "publishing_list_result":
                    pause(workflow, state, "paused_relation_review", "invalid_relation_decision", str(exc), "publishing_list_result")
                    print(json.dumps({"run_id": run_id, "status": "paused_relation_review", "error": str(exc)}, ensure_ascii=False))
                    return 3
            except Exception:
                pass
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 4 if args.command == "verify" else 2


if __name__ == "__main__":
    raise SystemExit(main())
