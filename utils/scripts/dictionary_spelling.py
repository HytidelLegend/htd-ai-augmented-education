"""Deterministic, resumable global spelling relations; no agent decisions."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from functools import lru_cache

from utils.scripts.dictionary_graph import canonical_family_ids
from utils.scripts.dictionary_jsonl import read_all
from utils.scripts.dictionary_records import entries
from utils.scripts.file_transaction import project_lock
from utils.scripts.structured_io import read_json, write_json, write_text_atomic, write_text_transaction, json_digest
from utils.scripts.timestamp import iso_timestamp, unique_filename_timestamp
from utils.scripts.word_similarity import longest_common_subsequence_length
from utils.scripts.workflow_state import WorkflowDefinition, WorkflowStateStore

NAME = "dictionary-spelling"
ROOT = Path(__file__).resolve().parents[2]
STAGES = ("prepared", "snapshotting_vocabulary", "selecting_pairs", "calculating_similarity",
          "preparing_updates", "validating_updates", "committing", "verifying", "completed")


def normalize(word: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", word).strip()).casefold()


def word_id(word: str) -> str:
    return "w_" + hashlib.sha256(normalize(word).encode()).hexdigest()[:20]


def index_path(root: Path) -> Path:
    return root / "outputs" / "词汇星图" / "spelling-index.json"


def storage_signature(root: Path) -> list:
    paths = [*sorted((root / "outputs" / "词汇星图" / "dicts").glob("?.jsonl")), index_path(root)]
    return [[path.name, path.stat().st_mtime_ns, path.stat().st_size] for path in paths if path.exists()]


def load_index(root: Path) -> dict:
    path = index_path(root)
    return _read_cached(path, path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else {"schemaVersion": "1.0", "algorithmVersion": "lcs-0.75-v1",
        "threshold": {"numerator": 3, "denominator": 4}, "vocabulary": {}, "pairs": [], "updatedAt": iso_timestamp()}


@lru_cache(maxsize=2)
def _read_cached(path: Path, modified: int, size: int) -> dict:
    return read_json(path)


def exclusions(library: dict) -> tuple[set[tuple[str, str]], dict[str, str]]:
    family = canonical_family_ids(library, normalize)
    excluded = set()
    for wid, entry in library.items():
        for form in [item["text"] for item in entry.get("aliases", [])] + [item["form"]["text"] for item in entry.get("inflections", [])]:
            excluded.add(tuple(sorted((wid, word_id(form)))))
        for derivative in entry.get("derivatives", []):
            target = word_id(derivative["word"])
            excluded.add(tuple(sorted((wid, target))))
            family.setdefault(target, family[wid])
        for form in entry.get("inflections", []):
            family.setdefault(word_id(form["form"]["text"]), family[wid])
    return excluded, family


def is_excluded(left: str, right: str, excluded: set, family: dict) -> bool:
    return left == right or tuple(sorted((left, right))) in excluded or (
        left in family and right in family and family[left] == family[right])


def vocabulary(root: Path, library: dict, prior: dict) -> dict:
    words = dict(prior["vocabulary"])
    for path in sorted((root / "outputs" / "词汇星图" / "projects").glob("*/project.json")):
        for row in read_json(path)["words"]:
            lemma = normalize(row["lemma"])
            words[word_id(lemma)] = lemma
    words.update({wid: normalize(entry["lemma"]) for wid, entry in library.items()})
    return words


def workflow(root: Path, run_id: str) -> WorkflowStateStore:
    transitions = {stage: [STAGES[i + 1], "paused_retryable_error"] for i, stage in enumerate(STAGES[:-1])}
    transitions["paused_retryable_error"] = list(STAGES[:-1])
    return WorkflowStateStore(root=root, workflow=NAME, run_id=run_id,
        definition=WorkflowDefinition.build(name=NAME, transitions=transitions),
        schema_path=ROOT / "utils" / "references" / "workflow-state-v1.schema.json")


def start(root: Path, full: bool = False) -> str:
    parent = root / "logs" / NAME / "runs"
    run_id = unique_filename_timestamp(path.name for path in parent.iterdir()) if parent.exists() else unique_filename_timestamp([])
    flow = workflow(root, run_id)
    now = iso_timestamp()
    flow.create({"schema_version": "1.0", "workflow": NAME, "run_id": run_id, "status": "prepared",
        "current_stage": "prepared", "resume_stage": None, "current_object_id": None, "current_batch_id": None,
        "completed_steps": [], "pending_decisions": [], "error": None, "created_at": now,
        "updated_at": now, "last_heartbeat_at": now, "event_sequence": 0, "full": full})
    return run_id


def validate_index(value: dict) -> None:
    from utils.scripts.structured_io import validate_json_schema
    # Validate the envelope with the public schema, then its uniform pair rows in
    # a linear tight loop. Recursive JSON Schema traversal is costly for large lists.
    if not isinstance(value.get("pairs"), list):
        raise ValueError("拼写词对必须为数组")
    validate_json_schema({**value, "pairs": []}, ROOT / "utils" / "references" / "dictionary-spelling-v1.schema.json")
    for wid, lemma in value["vocabulary"].items():
        if word_id(lemma) != wid:
            raise ValueError("拼写索引词面与 ID 不一致")
    seen = set()
    for pair in value["pairs"]:
        if not isinstance(pair, dict) or set(pair) != {"leftId", "rightId", "lcsLength", "publicationStatus"}:
            raise ValueError("拼写词对字段不符合 Schema")
        if not isinstance(pair["leftId"], str) or not isinstance(pair["rightId"], str) or type(pair["lcsLength"]) is not int or pair["lcsLength"] < 1 or pair["publicationStatus"] not in ("published", "waiting_for_entries"):
            raise ValueError("拼写词对类型或状态不符合 Schema")
        key = (pair["leftId"], pair["rightId"])
        if key[0] >= key[1] or key in seen or any(wid not in value["vocabulary"] for wid in key):
            raise ValueError("拼写词对 ID 无效或重复")
        seen.add(key)
        left, right = (value["vocabulary"][wid] for wid in key)
        if 8 * pair["lcsLength"] < 3 * (len(left) + len(right)):
            raise ValueError("拼写词对未达到阈值")


def _updates(root: Path, index: dict, library: dict) -> dict:
    desired = {wid: [] for wid in library}
    excluded, family = exclusions(library)
    index["pairs"] = [pair for pair in index["pairs"] if not is_excluded(pair["leftId"], pair["rightId"], excluded, family)]
    for pair in index["pairs"]:
        a, b = pair["leftId"], pair["rightId"]
        ready = a in library and b in library
        pair["publicationStatus"] = "published" if ready else "waiting_for_entries"
        if ready:
            for owner, target in ((a, b), (b, a)):
                desired[owner].append({"relationshipId": "r_spell_" + hashlib.sha256(f"{owner}|{target}".encode()).hexdigest()[:20],
                    "type": "spelling_similar", "targetWordId": target, "targetLemma": index["vocabulary"][target],
                    "linkStatus": "linked", "verificationStatus": "automatic_passed", "sourceRefs": []})
    updates = {}
    for wid, entry in library.items():
        retained = [rel for rel in entry["relationships"] if not (rel["type"] == "spelling_similar" and (
            rel["verificationStatus"] == "automatic_passed" or is_excluded(wid,
                rel.get("targetWordId") or word_id(rel["targetLemma"]), excluded, family)))]
        retained_targets = {rel.get("targetWordId") or word_id(rel["targetLemma"])
                            for rel in retained if rel["type"] == "spelling_similar"}
        changed = copy.deepcopy(entry)
        changed["relationships"] = retained + sorted(
            (rel for rel in desired[wid] if rel["targetWordId"] not in retained_targets),
            key=lambda item: item["targetWordId"])
        if changed["relationships"] != entry["relationships"]:
            changed.update(revision=entry["revision"] + 1, updatedAt=iso_timestamp())
            updates[wid] = changed
    return updates


def advance(root: Path, run_id: str) -> dict:
    flow = workflow(root, run_id)
    log = flow.run_dir
    with project_lock(root / "logs" / NAME / "index.lock", f"{NAME}:{run_id}"), flow.lock():
        state = flow.load()
        if state["status"] == "paused_retryable_error":
            state = flow.transition(state, state["resume_stage"], stage=state["resume_stage"])
        try:
            while state["status"] != "completed":
                stage = state["status"]
                if stage == "prepared":
                    pass
                elif stage == "snapshotting_vocabulary":
                    library = read_all(root / "outputs" / "词汇星图" / "dicts")
                    prior = load_index(root)
                    words = vocabulary(root, library, prior)
                    write_json(log / "snapshot.json", {"indexDigest": json_digest(prior), "indexExists": index_path(root).exists(), "vocabulary": words,
                        "known": [] if state["full"] else list(prior["vocabulary"]), "entryDigest": json_digest(library)})
                    write_json(log / "index.json", {**prior, "pairs": [] if state["full"] else prior["pairs"], "vocabulary": words})
                elif stage == "selecting_pairs":
                    snapshot = read_json(log / "snapshot.json")
                    write_json(log / "cursor.json", {"left": 0})
                elif stage == "calculating_similarity":
                    snapshot = read_json(log / "snapshot.json")
                    index = read_json(log / "index.json")
                    known = set(snapshot["known"])
                    ordered = sorted(index["vocabulary"], key=lambda wid: (len(index["vocabulary"][wid]), index["vocabulary"][wid]))
                    order_position = {wid: i for i, wid in enumerate(ordered)}
                    counts = {wid: Counter(index["vocabulary"][wid]) for wid in ordered}
                    excluded, family = exclusions(read_all(root / "outputs" / "词汇星图" / "dicts"))
                    cursor = read_json(log / "cursor.json")["left"]
                    # Each completed block is a checkpoint. Replaying a block deduplicates pairs.
                    pairs = {(pair["leftId"], pair["rightId"]): pair for pair in index["pairs"]}
                    new_ids = set(ordered) - known
                    new_ordered = [wid for wid in ordered if wid in new_ids]
                    for i in range(cursor, len(ordered)) if new_ids else []:
                        aid = ordered[i]
                        left = index["vocabulary"][aid]
                        candidates = ordered[i + 1:] if aid in new_ids else (wid for wid in new_ordered if order_position[wid] > i)
                        for bid in candidates:
                            right = index["vocabulary"][bid]
                            total = len(left) + len(right)
                            if 8 * len(left) < 3 * total:
                                break
                            if (aid not in new_ids and bid not in new_ids) or is_excluded(aid, bid, excluded, family):
                                continue
                            overlap = sum(min(count, counts[bid].get(char, 0)) for char, count in counts[aid].items())
                            if 8 * overlap < 3 * total:
                                continue
                            lcs = longest_common_subsequence_length(left, right)
                            if 8 * lcs >= 3 * total:
                                a, b = sorted((aid, bid))
                                pairs[a, b] = {"leftId": a, "rightId": b, "lcsLength": lcs, "publicationStatus": "waiting_for_entries"}
                        if (i + 1) % 250 == 0 or i + 1 == len(ordered):
                            index["pairs"] = list(pairs.values())
                            write_text_atomic(log / "index.json", json.dumps(index, ensure_ascii=False))
                            write_json(log / "cursor.json", {"left": i + 1})
                    if not ordered:
                        write_text_atomic(log / "index.json", json.dumps(index, ensure_ascii=False))
                elif stage == "preparing_updates":
                    library = read_all(root / "outputs" / "词汇星图" / "dicts")
                    index = read_json(log / "index.json")
                    # Project additions during calculation are left for the next incremental run.
                    updates = _updates(root, index, library)
                    index["updatedAt"] = iso_timestamp()
                    write_text_atomic(log / "index.json", json.dumps(index, ensure_ascii=False))
                    write_json(log / "updates.json", updates)
                    write_json(log / "baseline.json", {wid: json_digest(library[wid]) for wid in updates})
                elif stage == "validating_updates":
                    validate_index(read_json(log / "index.json"))
                    from jsonschema import Draft202012Validator
                    validator = Draft202012Validator(read_json(ROOT / "skills" / "build-word-entry" / "references" / "entry.schema.json"))
                    for entry in read_json(log / "updates.json").values():
                        validator.validate(entry)
                elif stage == "committing":
                    with project_lock(root / "logs" / "build-word-entry" / "dictionary.lock", f"{NAME}:{run_id}:publish"):
                        snapshot = read_json(log / "snapshot.json")
                        if index_path(root).exists() and json_digest(load_index(root)) not in (snapshot["indexDigest"], json_digest(read_json(log / "index.json"))):
                            raise ValueError("拼写索引在运行期间变化，请重新 start")
                        library = read_all(root / "outputs" / "词汇星图" / "dicts")
                        updates = read_json(log / "updates.json")
                        baseline = read_json(log / "baseline.json")
                        for wid, value in updates.items():
                            if json_digest(library[wid]) not in (baseline[wid], json_digest(value)):
                                raise ValueError("拼写关系发布期间词条修订冲突，请重新 start")
                        write_text_transaction({entries(root) / wid: json.dumps(value, ensure_ascii=False) for wid, value in updates.items()})
                        write_text_atomic(index_path(root), json.dumps(read_json(log / "index.json"), ensure_ascii=False) + "\n")
                elif stage == "verifying":
                    verify(root, run_id)
                next_stage = STAGES[STAGES.index(stage) + 1]
                state = flow.transition(state, next_stage, stage=next_stage, completed_step=stage,
                    updates={"storageSignature": storage_signature(root)} if next_stage == "completed" else None)
            return {"runId": run_id, "status": "completed", "pairCount": len(load_index(root)["pairs"])}
        except Exception as exc:
            flow.pause(state, status="paused_retryable_error", error_code="spelling_failed", message=str(exc), resume_stage=state["status"])
            raise


def verify(root: Path, run_id: str) -> dict:
    index = read_json(workflow(root, run_id).run_dir / "index.json")
    validate_index(index)
    library = read_all(root / "outputs" / "词汇星图" / "dicts")
    if json_digest(load_index(root)) != json_digest(index):
        raise ValueError("正式拼写索引与运行发布结果不一致")
    excluded, family = exclusions(library)
    for pair in index["pairs"]:
        a, b = pair["leftId"], pair["rightId"]
        if is_excluded(a, b, excluded, family):
            raise ValueError("拼写词对包含别名、屈折词形或同族词")
        expected = "published" if a in library and b in library else "waiting_for_entries"
        if pair["publicationStatus"] != expected:
            raise ValueError("拼写关系发布状态与词条就绪情况不一致")
        left, right = (index["vocabulary"][wid] for wid in (pair["leftId"], pair["rightId"]))
        if pair["lcsLength"] != longest_common_subsequence_length(left, right):
            raise ValueError("拼写词对 LCS 长度不可重算")
        if pair["publicationStatus"] == "published":
            for owner, target in ((pair["leftId"], pair["rightId"]), (pair["rightId"], pair["leftId"])):
                if not any(rel["type"] == "spelling_similar" and rel.get("targetWordId") == target for rel in library[owner]["relationships"]):
                    raise ValueError("拼写关系缺少反向词条记录")
    return {"runId": run_id, "status": "verified", "pairCount": len(index["pairs"])}


def sync(root: Path, full: bool = False) -> dict:
    parent = root / "logs" / NAME / "runs"
    for path in sorted(parent.glob("*/state.json"), reverse=True):
        state = read_json(path)
        if state["status"] != "completed":
            advance(root, path.parent.name)
            break
    completed = next((read_json(path) for path in sorted(parent.glob("*/state.json"), reverse=True)
                      if read_json(path)["status"] == "completed"), None)
    if not full and completed and completed.get("storageSignature") == storage_signature(root):
        index = load_index(root)
        current_words = vocabulary(root, read_all(root / "outputs" / "词汇星图" / "dicts"), index)
        if set(current_words) == set(index["vocabulary"]):
            return {"runId": completed["run_id"], "status": "completed", "pairCount": len(index["pairs"])}
    return advance(root, start(root, full))


_adjacency_source = None
_adjacency = {}


def adjacent(index: dict, wid: str) -> list[dict]:
    global _adjacency_source, _adjacency
    if _adjacency_source is not index:
        mapping = {}
        for pair in index["pairs"]:
            mapping.setdefault(pair["leftId"], []).append(pair)
            mapping.setdefault(pair["rightId"], []).append(pair)
        _adjacency_source, _adjacency = index, mapping
    result = []
    for pair in _adjacency.get(wid, []):
        if wid in (pair["leftId"], pair["rightId"]):
            target = pair["rightId"] if pair["leftId"] == wid else pair["leftId"]
            result.append({"relationshipId": "r_spell_" + hashlib.sha256(f"{wid}|{target}".encode()).hexdigest()[:20],
                "type": "spelling_similar", "targetWordId": target, "targetLemma": index["vocabulary"][target],
                "verificationStatus": "automatic_passed", "sourceRefs": []})
    return result
