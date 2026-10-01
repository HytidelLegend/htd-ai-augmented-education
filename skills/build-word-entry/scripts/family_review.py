"""Recoverable Agent review of missing inflections and derivatives.

The automatic status belongs to the *review application*. An empty field remains
empty when the cited evidence does not support a concrete form or derivative.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(Path(__file__).parent)]

from entry import item, source_ref, validate_entry, verify_observed_pronunciations_and_forms
from utils.scripts.dictionary_records import entries as entry_records
from utils.scripts.dictionary_jsonl import sync_entry_files
from utils.scripts.file_transaction import project_lock
from utils.scripts.structured_io import json_digest, read_json, validate_json_schema, write_json, write_text_transaction
from utils.scripts.timestamp import iso_timestamp, unique_filename_timestamp
from utils.scripts.workflow_state import WorkflowDefinition, WorkflowStateStore

NAME = "build-word-entry-family-review"
DEFINITION = WorkflowDefinition.build(name=NAME, transitions={
    "prepared": ["preparing_packet"],
    "preparing_packet": ["awaiting_agent_review"],
    "awaiting_agent_review": ["paused_agent_review", "applying_review", "completed"],
    "paused_agent_review": ["applying_review"],
    "applying_review": ["completed", "paused_quality_review"],
    "paused_quality_review": ["applying_review"],
    "completed": [],
})
SCHEMA = ROOT / "utils" / "references" / "dictionary-family-review-v1.schema.json"
DECISION_SCHEMA = ROOT / "skills" / "build-word-entry" / "references" / "family-review-decision.schema.json"


def workflow(root: Path, run_id: str) -> WorkflowStateStore:
    return WorkflowStateStore(root=root, workflow=NAME, run_id=run_id, definition=DEFINITION,
                              events_dir=root / "logs" / NAME / "runs" / run_id / "events")


def paths(root: Path, run_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"\d{8}T\d{6}(?:_\d+)?", run_id):
        raise ValueError("无效 run ID")
    return root / "logs" / NAME / "runs" / run_id, root / "outputs" / NAME / "runs" / run_id


def _latest_source_runs(root: Path, lemmas: set[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for directory in sorted((root / "logs" / "build-word-entry" / "runs").glob("*")):
        files = [directory / name for name in ("request.json", "state.json", "gaps.json", "evidence.json")]
        if not all(path.is_file() for path in files):
            continue
        request = read_json(files[0])
        if request.get("kind") == "word" and request.get("word") in lemmas and read_json(files[1]).get("status") == "completed":
            result[request["word"]] = directory
    return result


def _candidate_id(word_id: str, field: str, source: dict, candidate: dict) -> str:
    key = [word_id, field, source["site"], candidate]
    return "fc_" + json_digest(key)[:20]


def prepare_packet(root: Path, project_id: str) -> dict:
    from utils.scripts.english_inflections import observed_regular_forms
    if not re.fullmatch(r"[a-z0-9_-]+", project_id):
        raise ValueError("无效 project ID")
    project = read_json(root / "outputs" / "词汇星图" / "projects" / project_id / "project.json")
    words = {row["lemma"]: row["wordId"] for row in project["words"]}
    sources = _latest_source_runs(root, set(words))
    rows = []
    for lemma, word_id in words.items():
        run = sources.get(lemma)
        if not run:
            continue
        gaps = read_json(run / "gaps.json").get("fieldGaps", [])
        evidence_file = run / "evidence.json"
        evidence = read_json(evidence_file)
        evidence_digest = hashlib.sha256(evidence_file.read_bytes()).hexdigest()
        entry_path = entry_records(root) / f"{word_id}.json"
        entry = read_json(entry_path)
        review_path = root / "outputs" / "词汇星图" / "family-reviews" / f"{word_id}.json"
        prior_reviews = read_json(review_path).get("reviews", []) if review_path.is_file() else []
        fields_to_review = {gap["field"] for gap in gaps if gap["status"] == "pending_review"
                            and gap["field"] in ("derivatives", "inflections")}
        if any(observed_regular_forms(source, lemma) for source in evidence):
            fields_to_review.add("inflections")
        for field in sorted(fields_to_review):
            candidates = []
            candidate_key = "derivativeCandidates" if field == "derivatives" else "inflectionCandidates"
            for source in evidence:
                observed = observed_regular_forms(source, lemma) if field == "inflections" else []
                for candidate in [*source.get(candidate_key, []), *observed]:
                    reference = source_ref(source["fragments"][candidate["fragment"]], source)
                    candidates.append({"candidateId": _candidate_id(word_id, field, source, candidate),
                                       "value": candidate["word"] if field == "derivatives" else candidate["form"],
                                       "kind": candidate.get("kind"), "partOfSpeech": candidate.get("partOfSpeech"),
                                       "sourceRef": reference})
            prior = next((value for value in prior_reviews if value["field"] == field and
                          value["sourceRunId"] == run.name and
                          value["reviewRef"]["evidenceDigest"] == evidence_digest), None)
            published_forms = {(value["kind"], value["form"]["text"].casefold())
                               for value in entry.get("inflections", [])
                               if value["form"]["generationMethod"] == "source_supported"}
            all_forms_published = all((value["kind"], value["value"].casefold()) in published_forms
                                      for value in candidates)
            reviewed_ids: set[str] = set()
            if prior:
                prior_packet = root / "logs" / NAME / "runs" / prior["reviewRef"]["runId"] / "packet.json"
                if prior_packet.is_file():
                    old_fields = read_json(prior_packet).get("fields", [])
                    old = next((value for value in old_fields if value["wordId"] == word_id
                                and value["field"] == field), None)
                    if old:
                        reviewed_ids = {value["candidateId"] for value in old["candidates"]}
            if prior and (field == "derivatives" or all_forms_published or
                          {value["candidateId"] for value in candidates} <= reviewed_ids):
                continue
            rows.append({"wordId": word_id, "lemma": lemma, "field": field,
                         "sourceRunId": run.name, "entryRevision": entry["revision"],
                         "evidenceDigest": evidence_digest,
                         "candidates": candidates})
    packet = {"schemaVersion": "1.0", "projectId": project_id, "fields": rows}
    packet["packetDigest"] = json_digest(packet)
    return packet


def _add_candidate(entry: dict, field: str, candidate: dict, evidence: list[dict]) -> None:
    value, ref = candidate["value"], candidate["sourceRef"]
    if field == "derivatives":
        derivative_id = "d_" + hashlib.sha256(f"{entry['wordId']}|{value}".encode()).hexdigest()[:20]
        if not any(row["derivativeId"] == derivative_id for row in entry["derivatives"]):
            entry["derivatives"].append({"derivativeId": derivative_id, "word": value,
                "status": "candidate", "verificationStatus": "agent_reviewed", "sourceRefs": [ref]})
        return
    kind, pos = candidate["kind"], candidate.get("partOfSpeech") or ""
    form_id = "f_" + hashlib.sha256(f"{entry['lemma']}|{pos}|{kind}|{value}".encode()).hexdigest()[:20]
    prior = next((row for row in entry["inflections"] if row["formId"] == form_id or
                  row["kind"] == kind and row["form"]["text"].casefold() == value.casefold()), None)
    if prior:
        if prior["form"]["generationMethod"] != "rule_derived":
            return
        prior["form"]["generationMethod"] = "source_supported"
        prior["form"]["sourceRefs"] = [ref]
        prior["sourceRefs"] = [ref]
        prior["partOfSpeech"] = pos
        prior.pop("derivation", None)
        form = prior
    else:
        form = {"formId": form_id, "form": item(value, refs=[ref]), "kind": kind,
                "partOfSpeech": pos, "sourceRefs": [ref]}
        entry["inflections"].append(form)
    verification_gaps = verify_observed_pronunciations_and_forms(entry, evidence)
    if any(row["field"] == "inflections" and row["itemId"] == form["form"]["itemId"]
           for row in verification_gaps):
        raise ValueError(f"词形候选未通过脚本校验：{entry['lemma']} / {value}")


def apply_review(root: Path, run_id: str) -> dict:
    log, out = paths(root, run_id)
    packet, decision = read_json(log / "packet.json"), read_json(log / "decision.json")
    validate_json_schema(decision, DECISION_SCHEMA)
    if packet["packetDigest"] != json_digest({key: value for key, value in packet.items() if key != "packetDigest"}):
        raise ValueError("候选包摘要不符")
    if decision["packetDigest"] != packet["packetDigest"]:
        raise ValueError("审核判断与候选包摘要不符")
    expected = {(row["wordId"], row["field"]): row for row in packet["fields"]}
    actual = {(row["wordId"], row["field"]): row for row in decision["reviews"]}
    if len(actual) != len(decision["reviews"]) or set(actual) != set(expected):
        raise ValueError("审核判断必须逐项且不重复地覆盖候选包")
    reviewed_at = iso_timestamp()
    result = []
    with project_lock(root / "logs" / "build-word-entry" / "dictionary.lock", f"{NAME}:{run_id}"):
        applied: dict[str, dict] = {}
        for row in expected.values():
            path = root / "outputs" / "词汇星图" / "family-reviews" / f"{row['wordId']}.json"
            if not path.is_file():
                break
            saved = read_json(path)
            record = next((value for value in saved["reviews"] if value["field"] == row["field"]), None)
            if not record or record["reviewRef"]["runId"] != run_id or record["reviewRef"]["decisionDigest"] != json_digest(decision):
                break
            applied[row["wordId"]] = saved
        else:
            sync_entry_files(entry_records(root),
                             root / "outputs" / "词汇星图" / "dicts",
                             lambda value: validate_entry(value, ROOT / "skills" / "build-word-entry" / "references" / "entry.schema.json"))
            result = [{"wordId": row["wordId"], "field": row["field"],
                       "outcome": "published" if actual[key]["candidateIds"] else "no_supported_candidate"}
                      for key, row in expected.items()]
            out.mkdir(parents=True, exist_ok=True)
            write_json(out / "result.json", {"runId": run_id, "reviews": result})
            write_json(out / "review-records.json", applied)
            return {"runId": run_id, "reviews": result}
        entries: dict[str, dict] = {}
        reviews: dict[str, dict] = {}
        updates: dict[Path, str] = {}
        for key, row in expected.items():
            response = actual[key]
            available = {candidate["candidateId"]: candidate for candidate in row["candidates"]}
            chosen = response["candidateIds"]
            if len(chosen) != len(set(chosen)) or any(candidate_id not in available for candidate_id in chosen):
                raise ValueError("审核引用了无效候选")
            if (response["action"] == "publish") != bool(chosen):
                raise ValueError("发布判断与候选 ID 数量不一致")
            source = root / "logs" / "build-word-entry" / "runs" / row["sourceRunId"]
            if hashlib.sha256((source / "evidence.json").read_bytes()).hexdigest() != row["evidenceDigest"]:
                raise ValueError("原始证据已变化，请重新准备审核包")
            entry_path = entry_records(root) / f"{row['wordId']}.json"
            entry = read_json(entry_path)
            if entry["revision"] != row["entryRevision"]:
                raise ValueError(f"词条版本已变化，请重新准备审核包：{row['lemma']}")
            entries[row["wordId"]] = entry
        for key, row in expected.items():
            response = actual[key]
            entry_path = entry_records(root) / f"{row['wordId']}.json"
            entry = entries[row["wordId"]]
            candidates = {candidate["candidateId"]: candidate for candidate in row["candidates"]}
            if response["candidateIds"]:
                evidence = read_json(root / "logs" / "build-word-entry" / "runs" / row["sourceRunId"] / "evidence.json")
                for candidate_id in response["candidateIds"]:
                    _add_candidate(entry, row["field"], candidates[candidate_id], evidence)
                entry["revision"] += 1
                entry["updatedAt"] = reviewed_at
                updates[entry_path] = validate_entry(entry, ROOT / "skills" / "build-word-entry" / "references" / "entry.schema.json")
            review_path = root / "outputs" / "词汇星图" / "family-reviews" / f"{row['wordId']}.json"
            review = reviews.get(row["wordId"]) or (read_json(review_path) if review_path.is_file() else {
                "schemaVersion": "1.0", "wordId": row["wordId"], "reviews": []}
            )
            record = {"field": row["field"], "verificationStatus": "automatic_passed",
                      "outcome": "published" if response["candidateIds"] else "no_supported_candidate",
                      "sourceRunId": row["sourceRunId"], "candidateIds": response["candidateIds"],
                      "reviewRef": {"runId": run_id, "decisionDigest": json_digest(decision),
                                    "evidenceDigest": row["evidenceDigest"], "reviewedAt": reviewed_at}}
            review["reviews"] = [value for value in review["reviews"] if value["field"] != row["field"]] + [record]
            validate_json_schema(review, SCHEMA)
            reviews[row["wordId"]] = review
            updates[review_path] = json.dumps(review, ensure_ascii=False, indent=2) + "\n"
            result.append({"wordId": row["wordId"], "field": row["field"], "outcome": record["outcome"]})
        write_text_transaction(updates)
        sync_entry_files(entry_records(root),
                         root / "outputs" / "词汇星图" / "dicts",
                         lambda value: validate_entry(value, ROOT / "skills" / "build-word-entry" / "references" / "entry.schema.json"))
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "result.json", {"runId": run_id, "reviews": result})
    write_json(out / "review-records.json", reviews)
    return {"runId": run_id, "reviews": result}


def start(root: Path, project_id: str) -> str:
    parent = root / "logs" / NAME / "runs"
    parent.mkdir(parents=True, exist_ok=True)
    run_id = unique_filename_timestamp(path.name for path in parent.iterdir())
    log, _ = paths(root, run_id)
    log.mkdir()
    now = iso_timestamp()
    state = {"schema_version": "1.0", "workflow": NAME, "run_id": run_id,
        "status": "prepared", "current_stage": "prepared", "resume_stage": None,
        "current_object_id": project_id, "current_batch_id": None,
        "completed_steps": [], "pending_decisions": [], "error": None,
        "created_at": now, "updated_at": now, "last_heartbeat_at": now,
        "event_sequence": 0, "retry_count": 0}
    flow = workflow(root, run_id)
    flow.create(state)
    state = flow.transition(state, "preparing_packet", stage="preparing_packet")
    packet = prepare_packet(root, project_id)
    write_json(log / "packet.json", packet)
    from utils.scripts.english_inflections import regular_forms
    template_reviews = []
    for row in packet["fields"]:
        selected, seen = [], set()
        if row["field"] == "inflections":
            current_entry = read_json(entry_records(root) / f"{row['wordId']}.json")
            seen = {(value["kind"], value["form"]["text"].casefold())
                    for value in current_entry.get("inflections", [])
                    if value["form"]["generationMethod"] == "source_supported"}
            for candidate in row["candidates"]:
                key = (candidate["kind"], candidate["value"].casefold())
                expected = regular_forms(row["lemma"], candidate.get("partOfSpeech") or "").get(candidate["kind"])
                if key not in seen and expected == candidate["value"].casefold():
                    selected.append(candidate["candidateId"])
                    seen.add(key)
        template_reviews.append({"wordId": row["wordId"], "field": row["field"],
                                 "action": "publish" if selected else "no_supported_candidate",
                                 "candidateIds": selected,
                                 "reason": "已在来源例句或词形栏观察到规则形式" if selected else
                                           "现有来源未确认可发布的具体词形或直接派生关系"})
    write_json(log / "decision-template.json", {"packetDigest": packet["packetDigest"],
                                            "reviews": template_reviews})
    state = flow.transition(state, "awaiting_agent_review", stage="awaiting_agent_review")
    if not packet["fields"]:
        out = paths(root, run_id)[1]
        out.mkdir(parents=True, exist_ok=True)
        write_json(out / "result.json", {"runId": run_id, "reviews": []})
        flow.transition(state, "completed", stage="completed")
        return run_id
    flow.pause(state, status="paused_agent_review", error_code="review_required",
               message="请审核 packet.json 中每个字段的候选", resume_stage="applying_review")
    return run_id


def resume(root: Path, run_id: str, input_path: Path) -> dict:
    log, _ = paths(root, run_id)
    decision = read_json(input_path)
    validate_json_schema(decision, DECISION_SCHEMA)
    flow = workflow(root, run_id)
    state = flow.load()
    if state["status"] not in ("paused_agent_review", "paused_quality_review"):
        raise ValueError("运行当前不等待审核")
    write_json(log / "decision.json", decision)
    state = flow.resume(state)
    try:
        result = apply_review(root, run_id)
    except (ValueError, KeyError, FileNotFoundError) as exc:
        flow.pause(state, status="paused_quality_review", error_code="invalid_review",
                   message=str(exc), resume_stage="applying_review")
        raise
    flow.transition(state, "completed", stage="completed")
    return result


def verify(root: Path, run_id: str) -> None:
    state = workflow(root, run_id).load()
    if state["status"] != "completed":
        raise ValueError("审核尚未完成")
    log, out = paths(root, run_id)
    packet, result = read_json(log / "packet.json"), read_json(out / "result.json")
    if packet["packetDigest"] != json_digest({key: value for key, value in packet.items() if key != "packetDigest"}):
        raise ValueError("候选包摘要不符")
    if not packet["fields"]:
        if result["reviews"]:
            raise ValueError("空候选包的审核结果应为空")
        return
    decision = read_json(log / "decision.json")
    validate_json_schema(decision, DECISION_SCHEMA)
    if decision["packetDigest"] != packet["packetDigest"] or len(result["reviews"]) != len(packet["fields"]):
        raise ValueError("审核产物与候选包不一致")
    snapshots = read_json(out / "review-records.json") if (out / "review-records.json").is_file() else None
    for row in packet["fields"]:
        review = (snapshots[row["wordId"]] if snapshots is not None else
                  read_json(root / "outputs" / "词汇星图" / "family-reviews" / f"{row['wordId']}.json"))
        validate_json_schema(review, SCHEMA)
        record = next(value for value in review["reviews"] if value["field"] == row["field"])
        if record["reviewRef"]["runId"] != run_id or record["reviewRef"]["decisionDigest"] != json_digest(decision):
            raise ValueError("审核引用不一致")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("start", "resume", "status", "verify"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--project", default="default")
    parser.add_argument("--run-id")
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.command == "start":
            run_id = start(root, args.project)
            status = workflow(root, run_id).load()["status"]
            print(json.dumps({"runId": run_id, "status": status}))
            return 3 if status != "completed" else 0
        if not args.run_id:
            raise ValueError("需要 --run-id")
        if args.command == "status":
            print(json.dumps(workflow(root, args.run_id).load(), ensure_ascii=False))
        elif args.command == "resume":
            if not args.input:
                raise ValueError("需要 --input")
            result = resume(root, args.run_id, args.input)
            from utils.scripts.dictionary_spelling import sync
            sync(root)
            print(json.dumps(result, ensure_ascii=False))
        else:
            verify(root, args.run_id)
            print(json.dumps({"runId": args.run_id, "verified": True}))
    except (ValueError, KeyError, FileNotFoundError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
