"""Publish conservative, explicitly pending rule-derived inflections.

This workflow needs no Agent decision. Its plan is durable and can be resumed
after interruption; every candidate cites the source-backed POS sense.
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

from entry import validate_entry
from utils.scripts.dictionary_records import entries as entry_records
from utils.scripts.dictionary_jsonl import reconcile_entry_files, sync_entry_files
from utils.scripts.english_inflections import RULE_VERSION, rule_derived_forms
from utils.scripts.file_transaction import project_lock
from utils.scripts.span_ops import whole_word_spans
from utils.scripts.structured_io import read_json, write_json, write_text_transaction
from utils.scripts.timestamp import iso_timestamp, unique_filename_timestamp
from utils.scripts.workflow_state import WorkflowDefinition, WorkflowStateStore

NAME = "build-word-entry-rule-inflections"
DEFINITION = WorkflowDefinition.build(name=NAME, transitions={
    "prepared": ["deriving"], "deriving": ["validating"],
    "validating": ["publishing"], "publishing": ["completed"], "completed": [],
})
ENTRY_SCHEMA = ROOT / "skills" / "build-word-entry" / "references" / "entry.schema.json"


def _paths(root: Path, run_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"\d{8}T\d{6}(?:_\d+)?", run_id):
        raise ValueError("无效 run ID")
    return root / "logs" / NAME / "runs" / run_id, root / "outputs" / NAME / "runs" / run_id


def _flow(root: Path, run_id: str) -> WorkflowStateStore:
    log, _ = _paths(root, run_id)
    return WorkflowStateStore(root=root, workflow=NAME, run_id=run_id,
                              definition=DEFINITION, events_dir=log / "events")


def _id(prefix: str, key: str) -> str:
    return prefix + "_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def candidates_for_entry(entry: dict) -> list[dict]:
    present = {(row["kind"], row["form"]["text"].casefold()) for row in entry["inflections"]}
    planned: dict[tuple[str, str], dict] = {}
    for sense in entry["senses"]:
        source = sense["definitionEn"]
        if source["generationMethod"] != "source_supported" or not source["sourceRefs"]:
            continue
        for kind, (text, rule_id) in rule_derived_forms(entry["lemma"], sense["partOfSpeech"]).items():
            key = (kind, text.casefold())
            if key in present or key in planned:
                continue
            planned[key] = {"kind": kind, "text": text, "partOfSpeech": sense["partOfSpeech"],
                            "derivation": {"ruleId": rule_id, "ruleVersion": RULE_VERSION,
                                           "basisSenseId": sense["senseId"],
                                           "basisSourceRefs": source["sourceRefs"]}}
    return list(planned.values())


def _plan(root: Path, project_id: str) -> dict:
    project = read_json(root / "outputs" / "词汇星图" / "projects" / project_id / "project.json")
    rows = []
    for word in project["words"]:
        path = entry_records(root) / f"{word['wordId']}.json"
        if not path.is_file():
            continue
        entry = read_json(path)
        planned = candidates_for_entry(entry)
        if planned:
            rows.append({"wordId": word["wordId"], "lemma": entry["lemma"],
                         "revision": entry["revision"], "forms": planned})
    return {"projectId": project_id, "entries": rows}


def append_rule_forms(entry: dict, candidates: list[dict]) -> int:
    known = {(row["kind"], row["form"]["text"].casefold()) for row in entry["inflections"]}
    added = 0
    for candidate in candidates:
        kind, value, pos = candidate["kind"], candidate["text"], candidate["partOfSpeech"]
        if (kind, value.casefold()) in known:
            continue
        form_id = _id("f", f"{entry['lemma']}|{pos}|{kind}|{value}")
        entry["inflections"].append({"formId": form_id,
            "form": {"itemId": _id("i", form_id), "text": value,
                     "generationMethod": "rule_derived", "verificationStatus": "pending", "sourceRefs": []},
            "kind": kind, "partOfSpeech": pos, "irregular": False,
            "derivation": candidate["derivation"], "sourceRefs": []})
        known.add((kind, value.casefold()))
        added += 1
    if not added:
        return 0
    forms = [entry["lemma"], *(row["form"]["text"] for row in entry["inflections"])]
    for sense in entry["senses"]:
        for example in sense["examples"]:
            if "emphasis" in example:
                example["emphasis"]["en"] = whole_word_spans(example["text"], forms)
    entry["schemaVersion"] = "1.5"
    return added


def _updated(entry: dict, candidates: list[dict]) -> dict:
    if not append_rule_forms(entry, candidates):
        return entry
    entry["revision"] += 1
    entry["updatedAt"] = iso_timestamp()
    return entry


def resume(root: Path, run_id: str) -> dict:
    log, out = _paths(root, run_id)
    flow = _flow(root, run_id)
    state = flow.load()
    project_id = state["current_object_id"]
    if state["status"] == "prepared":
        state = flow.transition(state, "deriving", stage="deriving")
    if state["status"] == "deriving":
        if not (log / "plan.json").is_file():
            with project_lock(root / "logs" / "build-word-entry" / "dictionary.lock", f"{NAME}:{run_id}"):
                reconcile_entry_files(entry_records(root),
                                      root / "outputs" / "词汇星图" / "dicts",
                                      lambda entry: validate_entry(entry, ENTRY_SCHEMA))
            write_json(log / "plan.json", _plan(root, project_id))
        state = flow.transition(state, "validating", stage="validating")
    if state["status"] == "validating":
        plan = read_json(log / "plan.json")
        if plan["projectId"] != project_id:
            raise ValueError("计划项目不匹配")
        for row in plan["entries"]:
            entry = read_json(entry_records(root) / f"{row['wordId']}.json")
            if entry["revision"] != row["revision"]:
                raise ValueError(f"词条版本已变化，请重新开始：{row['lemma']}")
            valid = _updated(entry, row["forms"])
            validate_entry(valid, ENTRY_SCHEMA)
        state = flow.transition(state, "publishing", stage="publishing")
    if state["status"] == "publishing":
        plan = read_json(log / "plan.json")
        entries_dir = entry_records(root)
        with project_lock(root / "logs" / "build-word-entry" / "dictionary.lock", f"{NAME}:{run_id}"):
            updates = {}
            for row in plan["entries"]:
                path = entries_dir / f"{row['wordId']}.json"
                entry = read_json(path)
                if entry["revision"] == row["revision"] + 1 and all(
                    any(form["kind"] == item["kind"] and form["form"]["text"] == item["text"]
                        for form in entry["inflections"]) for item in row["forms"]):
                    continue
                if entry["revision"] != row["revision"]:
                    raise ValueError(f"词条版本已变化，请重新开始：{row['lemma']}")
                updates[path] = validate_entry(_updated(entry, row["forms"]), ENTRY_SCHEMA)
            if updates:
                write_text_transaction(updates)
            sync_entry_files(entries_dir, root / "outputs" / "词汇星图" / "dicts",
                             lambda entry: validate_entry(entry, ENTRY_SCHEMA))
        out.mkdir(parents=True, exist_ok=True)
        result = {"runId": run_id, "projectId": project_id,
                  "entryCount": len(plan["entries"]),
                  "formCount": sum(len(row["forms"]) for row in plan["entries"])}
        write_json(out / "result.json", result)
        flow.transition(state, "completed", stage="completed")
        return result
    if state["status"] == "completed":
        return read_json(out / "result.json")
    raise ValueError(f"未知工作流阶段：{state['status']}")


def start(root: Path, project_id: str) -> dict:
    if not re.fullmatch(r"[a-z0-9_-]+", project_id):
        raise ValueError("无效 project ID")
    parent = root / "logs" / NAME / "runs"
    parent.mkdir(parents=True, exist_ok=True)
    run_id = unique_filename_timestamp(path.name for path in parent.iterdir())
    log, _ = _paths(root, run_id)
    log.mkdir()
    now = iso_timestamp()
    _flow(root, run_id).create({"schema_version": "1.0", "workflow": NAME,
        "run_id": run_id, "status": "prepared", "current_stage": "prepared", "resume_stage": None,
        "current_object_id": project_id, "current_batch_id": None, "completed_steps": [],
        "pending_decisions": [], "error": None, "created_at": now, "updated_at": now,
        "last_heartbeat_at": now, "event_sequence": 0, "retry_count": 0})
    return resume(root, run_id)


def verify(root: Path, run_id: str) -> dict:
    log, out = _paths(root, run_id)
    if _flow(root, run_id).load()["status"] != "completed":
        raise ValueError("运行尚未完成")
    plan, result = read_json(log / "plan.json"), read_json(out / "result.json")
    if result["formCount"] != sum(len(row["forms"]) for row in plan["entries"]):
        raise ValueError("结果数量与计划不符")
    for row in plan["entries"]:
        entry = read_json(entry_records(root) / f"{row['wordId']}.json")
        validate_entry(entry, ENTRY_SCHEMA)
        for item in row["forms"]:
            if not any(form["kind"] == item["kind"] and form["form"]["text"] == item["text"]
                       for form in entry["inflections"]):
                raise ValueError(f"缺少词形：{row['lemma']} / {item['text']}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("start", "resume", "verify"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--project", default="default")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    try:
        root = args.root.resolve()
        if args.command == "start":
            result = start(root, args.project)
        elif args.command == "resume":
            result = resume(root, args.run_id)
        else:
            result = verify(root, args.run_id)
        if args.command in ("start", "resume"):
            from utils.scripts.dictionary_spelling import sync
            sync(root)
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, KeyError, FileNotFoundError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
