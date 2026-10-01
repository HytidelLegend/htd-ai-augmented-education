"""Exercise the local dictionary API without network or real state writes."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import service
from utils.scripts.dictionary_store import ConflictError, DictionaryStore
from utils.scripts.dictionary_jsonl import read_all, read_bucket, reconcile_entry_files, render_entry_line, upsert, bucket
from utils.scripts.timestamp import filename_timestamp, iso_timestamp

RUN_ID = filename_timestamp()


class FakeRuns:
    def existing(self, lemma: str) -> None:
        return None

    def start(self, lemma: str) -> dict:
        return {"jobId": "test-job", "runId": None, "status": "running", "lemma": lemma}

    def job(self, job_id: str) -> dict:
        return {"jobId": job_id, "runId": RUN_ID, "status": "completed"}

    def status(self, run_id: str) -> dict:
        return {"run_id": run_id, "status": "paused_quality_review"}

    def resume(self, run_id: str, decision: dict | None = None) -> dict:
        return {"runId": run_id, "jobId": "resume-job", "status": "running", "decisionProvided": decision is not None}


def call(base: str, path: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = Request(base + path, data=data, method=method,
                      headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def fixture(root: Path) -> None:
    (root / "skills" / "build-word-entry" / "references").mkdir(parents=True)
    (root / "applications" / "词汇星图").mkdir(parents=True)
    (root / "outputs" / "英文词典" / "entries").mkdir(parents=True)
    shutil.copy2(ROOT / "skills" / "build-word-entry" / "references" / "entry.schema.json",
                 root / "skills" / "build-word-entry" / "references" / "entry.schema.json")
    shutil.copy2(ROOT / "applications" / "词汇星图" / "config.yaml", root / "applications" / "词汇星图" / "config.yaml")
    (root / "utils" / "references").mkdir(parents=True)
    shutil.copy2(ROOT / "utils" / "references" / "dictionary-graph-v1.schema.json",
                 root / "utils" / "references" / "dictionary-graph-v1.schema.json")
    for entry in read_all(ROOT / "outputs" / "词汇星图" / "dicts").values():
        if entry["lemma"] in {"happy", "pass"}:
            path = root / "outputs" / "英文词典" / "entries" / f"{entry['wordId']}.json"
            path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")


def spelling_and_storage_regression(root: Path) -> None:
    """Exercise incremental equivalence, unbuilt ends, symmetry and crash recovery."""
    import copy
    from unittest.mock import patch
    from utils.scripts.dictionary_spelling import sync, load_index, word_id, verify, advance
    from utils.scripts.dictionary_records import entries, commit_records
    from utils.scripts.structured_io import write_text_transaction
    fixture(root)
    dictionary = DictionaryStore(root)
    dictionary.migrate_entries()
    template = copy.deepcopy(next(iter(dictionary.entries().values())))

    def make_entry(lemma: str) -> dict:
        value = copy.deepcopy(template)
        value.update(wordId=word_id(lemma), lemma=lemma, familyId="family:" + lemma,
                     aliases=[], inflections=[], derivatives=[], relationships=[], pendingRelations=[])
        return value

    quiet = make_entry("quiet")
    upsert(dictionary.dicts_dir, quiet, render_entry_line)
    project = dictionary.create_project("拼写测试", "quiet\nquite\nquiet")
    assert len(project["words"]) == 2
    pair_key = tuple(sorted((word_id("quiet"), word_id("quite"))))
    pair = next(item for item in load_index(root)["pairs"] if (item["leftId"], item["rightId"]) == pair_key)
    assert pair["publicationStatus"] == "waiting_for_entries"
    graph = dictionary.graph(project_id=project["projectId"], selected=word_id("quiet"))
    assert any(node["wordId"] == word_id("quite") and node["kind"] != "entry" for node in graph["nodes"])
    assert all(edge["type"] != "family" for edge in graph["edges"])
    assert len([edge for edge in graph["edges"] if edge["type"] == "spelling_similar"]) == 1
    assert any(item["targetLemma"] == "quite" for item in dictionary.word_page(word_id("quiet"), "all")["relationships"])
    assert dictionary.candidate("quite")["associations"]
    assert dictionary.graph(project_id=project["projectId"], selected=word_id("quite"))["nodes"]
    ui = dictionary.state("ui")
    ui["activeProjectId"] = project["projectId"]
    ui["graph"]["positions"][word_id("quite")] = {"x": 1, "y": 2}
    dictionary.save_state("ui", ui, ui["revision"])

    upsert(dictionary.dicts_dir, make_entry("quite"), render_entry_line)
    receipt = sync(root)
    verify(root, receipt["runId"])
    for owner, target in (("quiet", "quite"), ("quite", "quiet")):
        assert sum(rel["type"] == "spelling_similar" and rel.get("targetWordId") == word_id(target)
                   for rel in dictionary.entry(word_id(owner))["relationships"]) == 1
    before = read_all(dictionary.dicts_dir)
    sync(root)
    assert read_all(dictionary.dicts_dir) == before, "无变化增量不得提高词条修订"
    dictionary.create_project("新增项目", "quilt\nquiet")
    incremental = {(item["leftId"], item["rightId"], item["lcsLength"]) for item in load_index(root)["pairs"]}
    sync(root, full=True)
    assert incremental == {(item["leftId"], item["rightId"], item["lcsLength"]) for item in load_index(root)["pairs"]}

    # Newly confirmed family membership removes both directions and the index pair,
    # including a previously reviewed spelling relation retained by the old updater.
    reviewed = copy.deepcopy(dictionary.entry(word_id("quiet")))
    for relation in reviewed["relationships"]:
        if relation.get("targetWordId") == word_id("quite"):
            relation["verificationStatus"] = "agent_reviewed"
    reviewed["revision"] += 1
    upsert(dictionary.dicts_dir, reviewed, render_entry_line)
    sync(root)
    assert sum(rel["type"] == "spelling_similar" and rel.get("targetWordId") == word_id("quite")
               for rel in dictionary.entry(word_id("quiet"))["relationships"]) == 1
    quite = copy.deepcopy(dictionary.entry(word_id("quite")))
    quite["familyId"] = dictionary.entry(word_id("quiet"))["familyId"]
    quite["revision"] += 1
    upsert(dictionary.dicts_dir, quite, render_entry_line)
    sync(root)
    assert not any((item["leftId"], item["rightId"]) == pair_key for item in load_index(root)["pairs"])
    for owner, target in (("quiet", "quite"), ("quite", "quiet")):
        assert not any(rel["type"] == "spelling_similar" and rel.get("targetWordId") == word_id(target)
                       for rel in dictionary.entry(word_id(owner))["relationships"])

    # Historical vocabulary remains focusable after its project disappears,
    # even when the word has no qualifying relationship.
    isolated = dictionary.create_project("独立待建词", "xyzzy")
    dictionary.delete_project(isolated["projectId"], isolated["revision"])
    focus = dictionary.graph(project_id=project["projectId"], selected=word_id("xyzzy"))
    assert len(focus["nodes"]) == 1 and focus["nodes"][0]["wordId"] == word_id("xyzzy")

    # Exclusions also join separately built inflected forms into one family,
    # and cover aliases and unbuilt derivatives without calling the LCS judge.
    from utils.scripts.dictionary_spelling import exclusions, is_excluded
    forms = {word_id(lemma): make_entry(lemma) for lemma in ("walk", "walks", "walking", "walked", "walker", "walke")}
    forms[word_id("walk")]["inflections"] = [{"form": {"text": lemma}} for lemma in ("walks", "walking", "walked")]
    forms[word_id("walk")]["derivatives"] = [{"word": "walker"}, {"word": "walkers"}]
    forms[word_id("walk")]["aliases"] = [{"text": "walke"}]
    excluded, family = exclusions(forms)
    assert is_excluded(word_id("walks"), word_id("walking"), excluded, family)
    assert is_excluded(word_id("walking"), word_id("walkers"), excluded, family)
    assert is_excluded(word_id("walk"), word_id("walke"), excluded, family)

    # Verification must inspect the published index and both endpoints, not just
    # accept its archived checkpoint's self-consistent LCS values.
    receipt = sync(root)
    index = copy.deepcopy(load_index(root))
    from utils.scripts.dictionary_spelling import index_path, workflow
    index["pairs"][0]["publicationStatus"] = "published"
    saved = index_path(root).read_text(encoding="utf-8")
    archived = workflow(root, receipt["runId"]).run_dir / "index.json"
    saved_archive = archived.read_text(encoding="utf-8")
    index_path(root).write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    try:
        try:
            verify(root, receipt["runId"])
        except ValueError:
            pass
        else:
            raise AssertionError("正式索引篡改未检出")
        archived.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        try:
            verify(root, receipt["runId"])
        except ValueError:
            pass
        else:
            raise AssertionError("错误发布状态未检出")
    finally:
        index_path(root).write_text(saved, encoding="utf-8")
        archived.write_text(saved_archive, encoding="utf-8")

    # One bucket is written before a simulated hard interruption; reads replay intent.
    updates = {}
    for lemma in ("quiet", "pass"):
        value = copy.deepcopy(dictionary.entry(word_id(lemma)))
        value["revision"] += 1
        updates[entries(root) / value["wordId"]] = json.dumps(value, ensure_ascii=False)
    def interrupted(physical):
        first = next(iter(physical))
        write_text_transaction({first: physical[first]})
        raise OSError("SYNTHETIC_INTERRUPTION")
    try:
        with patch("utils.scripts.structured_io.write_text_transaction", side_effect=interrupted):
            commit_records(updates)
    except OSError:
        pass
    else:
        raise AssertionError("中断未发生")
    recovered = read_all(dictionary.dicts_dir)
    for record, text in updates.items():
        assert recovered[record.stem] == json.loads(text)
    assert not (root / "logs" / "dictionary-jsonl" / "pending.json").exists()
    assert not (root / "outputs" / "词汇星图" / "entries").exists()
    with patch("utils.scripts.dictionary_spelling.verify", side_effect=ValueError("SYNTHETIC_VERIFY_FAILURE")):
        try:
            sync(root, full=True)
        except ValueError:
            pass
    pending = next(path for path in (root / "logs" / "dictionary-spelling" / "runs").glob("*/state.json")
                   if json.loads(path.read_text(encoding="utf-8"))["status"] == "paused_retryable_error")
    assert advance(root, pending.parent.name)["status"] == "completed"


def overview_relation_fixture(store: DictionaryStore) -> dict:
    """Synthetic one-way candidate reproducing the reported overview pair."""
    import copy
    from utils.scripts.dictionary_store import word_id
    template = next(iter(store.entries().values()))
    for lemma in ("makeshift", "alternative"):
        entry = copy.deepcopy(template)
        entry.update(wordId=word_id(lemma), lemma=lemma, familyId="fixture:" + lemma,
                     aliases=[], inflections=[], derivatives=[], relationships=[], pendingRelations=[])
        if lemma == "makeshift":
            # Deliberately trigger the old >=5 identical-confidence warning condition.
            def uniform_ai(value):
                if isinstance(value, dict):
                    if "generationMethod" in value:
                        value.update(generationMethod="ai_generated", confidence=0.95)
                    for child in value.values():
                        uniform_ai(child)
                elif isinstance(value, list):
                    for child in value:
                        uniform_ai(child)
            uniform_ai(entry["senses"])
            entry["pendingRelations"] = [{"candidateId": "c_00000000000000000000", "targetLemma": "alternative",
                "sourceSenseId": entry["senses"][0]["senseId"], "proposedType": "synonym_or_near_synonym",
                "status": "pending_relation_review", "sourceRefs": copy.deepcopy(entry["senses"][0]["definitionZh"]["sourceRefs"])}]
        upsert(store.dicts_dir, entry, render_entry_line)
    return store.create_project("总览候选关系回归", "alternative\nmakeshift")


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        spelling_and_storage_regression(root / "spelling_regression")
        dense_root = root / "dense_spelling"
        fixture(dense_root)
        dense_store = DictionaryStore(dense_root)
        dense_store.migrate_entries()
        dense_project = dense_store.create_project("全部拼写边", "planet\nplaneta\nplanetb\nplanetc\nplanetd\nplanete")
        dense_graph = dense_store.graph(project_id=dense_project["projectId"])
        dense_edges = [edge for edge in dense_graph["edges"] if edge["type"] == "spelling_similar"]
        assert len(dense_edges) == 15, "六个相似词必须展示全部十五条边"
        assert all(sum(node["wordId"] in (edge["source"], edge["target"]) for edge in dense_edges) == 5
                   for node in dense_graph["nodes"])
        hidden = dense_store.graph(project_id=dense_project["projectId"], visible={"spelling_similar": False})
        assert len(hidden["nodes"]) == 6 and not hidden["edges"]
        assert len(dense_store.graph(project_id=dense_project["projectId"])["edges"]) == 15
        # An already visible derivative may be absent from this project's word list.
        source = next(iter(dense_store.entries().values()))
        visible_project = dense_store.create_project("可见派生节点拼写边", source["lemma"] + "\nplanet")
        source = json.loads(json.dumps(dense_store.entry(source["wordId"])))
        source["derivatives"].append({"derivativeId": "synthetic_planeta", "word": "planeta", "status": "candidate"})
        source["revision"] += 1
        source["updatedAt"] = iso_timestamp()
        upsert(dense_store.dicts_dir, source, render_entry_line)
        visible_graph = dense_store.graph(project_id=visible_project["projectId"])
        visible_by_lemma = {node["lemma"]: node["wordId"] for node in visible_graph["nodes"]}
        assert {"planet", "planeta"} <= visible_by_lemma.keys()
        expected_pair = {visible_by_lemma["planet"], visible_by_lemma["planeta"]}
        assert any(edge["type"] == "spelling_similar" and {edge["source"], edge["target"]} == expected_pair
                   for edge in visible_graph["edges"]), "可见派生节点的拼写边不能因其不在项目词表中而被遗漏"
        fixture(root)
        store = DictionaryStore(root)
        assert store.migrate_entries()["copied"] == 2
        assert store.migrate_entries()["identical"] == 0
        newer_root = root / "newer_case"
        fixture(newer_root)
        newer_store = DictionaryStore(newer_root)
        newer_store.migrate_entries()
        published_newer = json.loads(json.dumps(next(iter(newer_store.entries().values()))))
        published_newer["revision"] += 1
        upsert(newer_store.dicts_dir, published_newer, render_entry_line)
        assert newer_store.migrate_entries()["identical"] == 0
        assert json.loads((newer_store.entries_dir / f"{published_newer['wordId']}.json").read_text(encoding="utf-8"))["revision"] == published_newer["revision"]
        assert not (root / "outputs" / "词汇星图" / "entries").exists()
        assert not (root / "outputs" / "英文词典" / "entries").exists()
        conflict_root = root / "conflict_case"
        fixture(conflict_root)
        conflict_store = DictionaryStore(conflict_root)
        conflict_store.migrate_entries()
        sample = json.loads(json.dumps(next(iter(conflict_store.entries().values()))))
        legacy = conflict_root / "outputs" / "词汇星图" / "entries"
        legacy.mkdir()
        sample["aliases"] = []
        sample["updatedAt"] = "2026-01-01T00:00:00"
        (legacy / f"{sample['wordId']}.json").write_text(json.dumps(sample), encoding="utf-8")
        try:
            conflict_store.migrate_entries()
        except ValueError:
            pass
        else:
            raise AssertionError("同修订迁移冲突未阻止")
        assert list(legacy.glob("*.json")), "冲突时不得清理原文件"

        staged_entry = next(iter(store.entries().values()))
        staged_sense = staged_entry["senses"][0]
        stage = {"schemaVersion": "1.0", "revision": 1, "updatedAt": iso_timestamp(),
                 "stageId": "sample", "label": "测试阶段", "words": {staged_entry["wordId"]: {
                     "coreSenseIds": [staged_sense["senseId"]], "moreSenseIds": [],
                     "sourceEntryRevision": staged_entry["revision"],
                     "contentIdsBySense": {staged_sense["senseId"]: [staged_sense["examples"][0]["itemId"]]},
                     "relationshipIds": []}}}
        stage_path = root / "outputs" / "词汇星图" / "stages" / "sample.json"
        stage_path.parent.mkdir(parents=True)
        stage_path.write_text(json.dumps(stage, ensure_ascii=False), encoding="utf-8")
        assert store.stage("sample")["stageId"] == "sample"
        dict_dir = root / "outputs" / "词汇星图" / "dicts"
        staged_original = staged_entry.copy()
        changed = json.loads(json.dumps(staged_original))
        changed["revision"] += 1
        upsert(dict_dir, changed, render_entry_line)
        try:
            store.stage("sample")
        except ConflictError:
            pass
        else:
            raise AssertionError("阶段未发现被引用词条已修改")
        upsert(dict_dir, staged_original, render_entry_line)

        audio_requests = []
        def fake_audio(lemma, variety):
            audio_requests.append((lemma, variety))
            return b"RIFF" + b"\0" * 20, "audio/wav"
        service.fetch_audio = fake_audio
        server = ThreadingHTTPServer(("127.0.0.1", 0), service.make_handler(store, FakeRuns()))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            assert call(base, "/health")[1]["entryCount"] == 2
            boot = call(base, "/api/bootstrap")[1]
            assert boot["ui"]["activeStageId"] == "all" and boot["words"] == []
            assert boot["projects"] == [] and call(base, "/api/projects")[1][0]["projectId"] == "default"
            assert len(list(dict_dir.glob("?.jsonl"))) == 26
            assert call(base, "/api/dictionary/search?q=pass")[1]
            assert len(call(base, "/api/words?stage=all")[1]) == 2
            assert len([item for item in call(base, "/api/words?stage=sample")[1] if not item["outsideStage"]]) == 1
            word = call(base, "/api/words?stage=all&project=default")[1][0]
            assert call(base, f"/api/words/{word['wordId']}?stage=all")[1]["entry"]["wordId"] == word["wordId"]
            assert call(base, "/api/words/w_invalid?stage=all")[0] == 400
            staged_page = call(base, f"/api/words/{staged_entry['wordId']}?stage=sample")[1]
            assert len(staged_page["coreSenses"]) == 1 and len(staged_page["coreSenses"][0]["examples"]) == 1
            assert call(base, "/api/graph?family=1&synonym=1&near_synonym=1&antonym=1&spelling_similar=1")[1]["nodes"]
            outside_word = next(entry for entry in store.entries().values() if entry["wordId"] != staged_entry["wordId"])
            assert call(base, f"/api/words/{outside_word['wordId']}?stage=sample")[1]["outsideStage"]
            assert call(base, "/api/graph?stage=sample&outside=0")[1]["nodes"]
            added = call(base, "/api/stages/sample/words", "POST",
                         {"wordId": outside_word["wordId"], "expectedRevision": 1})[1]
            assert added["revision"] == 2 and outside_word["wordId"] in added["words"]
            assert call(base, "/api/stages/sample/words", "POST",
                        {"wordId": outside_word["wordId"], "expectedRevision": 1})[0] == 409
            assert not call(base, f"/api/words/{outside_word['wordId']}?stage=sample")[1]["outsideStage"]
            source = store.entry(word["wordId"])
            relation = next(item for item in source["relationships"] if item.get("targetLemma") and not item.get("targetWordId"))
            assert call(base, f"/api/candidates?lemma={quote(relation['targetLemma'])}")[1]["associations"]
            level = call(base, f"/api/learning/{word['wordId']}", "PATCH", {"level": "seen", "expectedRevision": 1})[1]
            assert level["words"][word["wordId"]] == "seen"
            created = call(base, "/api/projects", "POST", {"name": "另一个项目", "content": word["lemma"], "format": "text"})[1]
            assert created["projectId"] != "default" and len(created["words"]) == 1
            plan = call(base, "/api/plans", "POST", {"projectId": created["projectId"],
                "method": "ebbinghaus", "startDate": "2026-10-01", "dailyItems": 50})[1]
            assert call(base, "/api/today")[1]["day"] == iso_timestamp()[:10]
            assert plan["rounds"][0]["firstPassEndDate"] == "2026-10-01"
            assert len(call(base, "/api/plans")[1]) == 1
            assert call(base, f"/api/plans/{plan['planId']}")[1]["wordIds"] == [word["wordId"]]
            assert call(base, "/api/plans/date/2026-10-01")[1][0]["newWordIds"] == [word["wordId"]]
            assert call(base, f"/api/plans/{plan['planId']}/day/2026-10-01?pageSize=10")[1]["wordIds"] == [word["wordId"]]
            assert call(base, f"/api/plans/{plan['planId']}/day/2026-10-01?pageSize=0")[0] == 400
            shuffled = call(base, f"/api/plans/{plan['planId']}", "PATCH", {
                "expectedRevision": plan["revision"], "action": "shuffle", "enabled": True,
                "withinGroup": True, "betweenGroups": True})[1]
            assert shuffled["rounds"][0]["shuffle"]["betweenGroups"]
            assert call(base, f"/api/plans/{plan['planId']}/day/2026-10-01?pageSize=10")[1]["wordIds"] == [word["wordId"]]
            assert call(base, f"/api/plans/{plan['planId']}", "PATCH", {
                "expectedRevision": plan["revision"], "action": "shuffle", "enabled": False})[0] == 409
            passed_plan = call(base, f"/api/plans/{plan['planId']}", "PATCH", {
                "expectedRevision": shuffled["revision"], "action": "pass",
                "day": "2026-10-01", "wordId": word["wordId"]})[1]
            assert passed_plan["rounds"][0]["passed"]["2026-10-01"] == [word["wordId"]]
            assert call(base, "/api/plans")[1][0]["progress"] == 1
            assert call(base, f"/api/plans/{plan['planId']}", "PATCH", {
                "expectedRevision": passed_plan["revision"], "action": "retry",
                "day": "2026-10-01", "wordId": word["wordId"]})[0] == 200
            renamed_plan = call(base, f"/api/plans/{plan['planId']}", "PATCH", {
                "expectedRevision": passed_plan["revision"] + 1, "action": "rename", "name": "我的计划"})[1]
            assert renamed_plan["name"] == "我的计划"
            reconfigured = call(base, f"/api/plans/{plan['planId']}", "PATCH", {
                "expectedRevision": renamed_plan["revision"], "action": "reconfigure",
                "projectId": created["projectId"], "method": "ebbinghaus", "name": "新排程",
                "startDate": "2026-10-03", "firstPassDays": 1})[1]
            assert reconfigured["rounds"] == [{**reconfigured["rounds"][0], "passed": {}}]
            assert reconfigured["rounds"][0]["startDate"] == "2026-10-03"
            assert reconfigured["name"] == "新排程" and reconfigured["createdAt"] == plan["createdAt"]
            assert call(base, f"/api/plans/{plan['planId']}", "DELETE", {"expectedRevision": renamed_plan["revision"]})[0] == 409
            assert call(base, f"/api/plans/{plan['planId']}", "DELETE", {"expectedRevision": reconfigured["revision"]})[1]["deleted"]
            assert call(base, f"/api/plans/{plan['planId']}")[0] == 404
            old_plan = call(base, "/api/plans", "POST", {"projectId": created["projectId"],
                "method": "ebbinghaus", "startDate": "2026-08-01", "dailyItems": 1})[1]
            for scheduled in old_plan["schedule"]["days"]:
                if not scheduled["item_ids"]:
                    continue
                day = (date.fromisoformat("2026-08-01") + timedelta(days=scheduled["day"] - 1)).isoformat()
                old_plan = call(base, f"/api/plans/{old_plan['planId']}", "PATCH", {
                    "expectedRevision": old_plan["revision"], "action": "pass", "day": day,
                    "wordId": word["wordId"]})[1]
            assert old_plan["roundComplete"]
            next_round = call(base, f"/api/plans/{old_plan['planId']}", "PATCH", {
                "expectedRevision": old_plan["revision"], "action": "nextRound"})[1]
            assert next_round["rounds"][-1]["number"] == 2
            reset_rounds = call(base, f"/api/plans/{old_plan['planId']}", "PATCH", {
                "expectedRevision": next_round["revision"], "action": "reconfigure",
                "projectId": created["projectId"], "method": "ebbinghaus", "name": "重排后的计划",
                "startDate": "2026-10-05", "dailyItems": 1})[1]
            assert len(reset_rounds["rounds"]) == 1 and reset_rounds["rounds"][0]["passed"] == {}
            assert reset_rounds["progress"] == 0
            assert len(call(base, "/api/projects")[1]) == 2
            renamed = call(base, f"/api/projects/{created['projectId']}", "PUT", {"name": "第二项目", "expectedRevision": 1})[1]
            assert renamed["name"] == "第二项目" and renamed["revision"] == 2
            assert call(base, f"/api/projects/{created['projectId']}", "PUT", {"name": "旧版本", "expectedRevision": 1})[0] == 409
            single_project_graph = call(base, f"/api/graph?project={created['projectId']}")[1]
            assert [node["wordId"] for node in single_project_graph["nodes"] if node["kind"] == "entry"] == [word["wordId"]]
            assert any(node["kind"] == "inflection" for node in single_project_graph["nodes"])
            assert call(base, f"/api/projects/{created['projectId']}/learning")[1]["words"] == {}
            assert call(base, f"/api/learning/{word['wordId']}", "PATCH", {"level": "familiar", "expectedRevision": 1,
                "projectId": created["projectId"]})[1]["words"][word["wordId"]] == "familiar"
            assert store.state("learning", "default")["words"][word["wordId"]] == "seen"
            assert call(base, f"/api/learning/{word['wordId']}", "PATCH", {"level": "familiar", "expectedRevision": 1})[0] == 409
            favorite = call(base, f"/api/favorites/{word['wordId']}", "POST", {"favorited": True, "expectedRevision": 1})[1]
            assert favorite["words"][word["wordId"]]["favoritedAt"] == favorite["words"][word["wordId"]]["lastOpenedAt"]
            assert word["wordId"] in store.state("favorites")["words"]
            opened = call(base, f"/api/favorites/{word['wordId']}/opened", "POST", {"expectedRevision": favorite["revision"]})[1]
            assert opened["revision"] == favorite["revision"] + 1
            ui = boot["ui"]
            ui["graph"]["zoomPercent"] = 125
            saved_ui = call(base, "/api/state/ui", "PUT", {"state": ui, "expectedRevision": 1})[1]
            assert saved_ui["graph"]["zoomPercent"] == 125
            pending_project = call(base, "/api/projects", "POST", {"name": "待建词项目", "content": "mysteryword", "format": "text"})[1]
            pending_graph = call(base, f"/api/graph?project={pending_project['projectId']}")[1]
            pending_id = pending_graph["nodes"][0]["wordId"]
            assert pending_graph["nodes"][0]["status"] == "pending_collection"
            saved_ui["graph"]["positions"][pending_id] = {"x": 280, "y": 320}
            assert call(base, "/api/state/ui", "PUT", {"state": saved_ui,
                "expectedRevision": saved_ui["revision"]})[0] == 200
            with urlopen(f"{base}/api/audio/{word['wordId']}/uk?check=1", timeout=10) as response:
                assert response.status == 200 and response.read() == b""
            with urlopen(f"{base}/api/audio/{word['wordId']}/uk", timeout=10) as response:
                assert response.read().startswith(b"RIFF")
            assert audio_requests == [(word["lemma"], "uk")]
            assert list((root / "outputs" / "词汇星图" / "cache" / "audio").glob("*.wav"))
            assert call(base, "/api/runs", "POST", {"lemma": "example"})[1]["jobId"] == "test-job"
            assert call(base, "/api/jobs/test-job")[1]["status"] == "completed"
            assert call(base, f"/api/runs/{RUN_ID}")[1]["status"] == "paused_quality_review"
            assert call(base, f"/api/runs/{RUN_ID}/resume", "POST", {"decision": {"ok": True}})[1]["decisionProvided"]
            graph_entries = list(store.entries().values())
            source_entry, target_entry = graph_entries[0], graph_entries[1]
            antonym = json.loads(json.dumps(next(item for item in source_entry["relationships"] if item["type"] == "synonym")))
            antonym.update(type="antonym", relationshipId=antonym["relationshipId"] + "_antonym")
            source_entry["relationships"].append(antonym)
            for kind in ("synonym", "near_synonym", "antonym"):
                relation = next(item for item in source_entry["relationships"] if item["type"] == kind)
                relation.update(targetWordId=target_entry["wordId"], targetLemma=target_entry["lemma"],
                                targetSenseId=target_entry["senses"][0]["senseId"], linkStatus="linked")
            focused = store.graph(selected=source_entry["wordId"])
            assert {"synonym", "near_synonym"} <= {edge["type"] for edge in focused["edges"]}
            overview = store.graph()
            assert {"synonym", "near_synonym", "antonym"} <= {edge["type"] for edge in overview["edges"]}
            assert overview["families"] and all(edge["type"] != "family" for edge in overview["edges"])
            assert focused["placeholderCount"] > 0
            related = {source_entry["wordId"]}
            related.update(node_id for family in focused["families"]
                           if source_entry["wordId"] in family["nodeIds"] for node_id in family["nodeIds"])
            related.update(edge["target"] if edge["source"] == source_entry["wordId"] else edge["source"]
                           for edge in focused["edges"] if source_entry["wordId"] in (edge["source"], edge["target"]))
            assert {node["wordId"] for node in focused["nodes"]} <= related
            assert any(node["kind"] == "inflection" for node in focused["nodes"])
            assert any(node["kind"] == "derivative" for node in focused["nodes"])
            node_kinds = {node["wordId"]: node["kind"] for node in focused["nodes"]}
            assert all(node_kinds[node_id] in {"entry", "inflection", "derivative"}
                       for family in focused["families"] for node_id in family["nodeIds"])
            assert not {node_id for family in focused["families"] for node_id in family["nodeIds"]} & {
                node["wordId"] for node in focused["nodes"] if node["kind"] == "relation_candidate"}
            pass_entry = next(entry for entry in graph_entries if entry["lemma"] == "pass")
            pass_graph = store.graph(selected=pass_entry["wordId"])
            assert any(edge["ruleClassified"] for edge in pass_graph["edges"] if edge["type"] == "near_synonym")
            hidden = store.graph(selected=source_entry["wordId"], visible={kind: False for kind in
                ("family", "synonym", "near_synonym", "antonym", "spelling_similar")})
            assert [node["wordId"] for node in hidden["nodes"]] == [source_entry["wordId"]]
            restored = store.graph(selected=source_entry["wordId"], visible={"near_synonym": True})
            assert target_entry["wordId"] in {node["wordId"] for node in restored["nodes"]}
            overview = store.graph()
            assert {row["wordId"] for row in store.project("default")["words"]} <= {
                node["wordId"] for node in overview["nodes"]}
            assert any(node["kind"] == "inflection" for node in overview["nodes"])
            assert any(node["kind"] == "derivative" for node in overview["nodes"])
            assert any(source_entry["wordId"] in family["nodeIds"] and len(family["nodeIds"]) > 1
                       for family in overview["families"])
            searched = store.graph(search="pass")
            assert searched["nodes"] and all("pass" in node["lemma"] for node in searched["nodes"])
            config_state = call(base, "/api/config")[1]
            config_state["config"]["study"]["wordsPerPage"] = 5
            saved_config = call(base, "/api/config", "PUT", {
                "config": config_state["config"], "expectedRevision": config_state["revision"]})[1]
            assert saved_config["config"]["study"]["wordsPerPage"] == 5
            assert call(base, "/api/bootstrap")[1]["config"]["study"]["wordsPerPage"] == 5
            assert DictionaryStore(root).config["study"]["wordsPerPage"] == 5
            assert call(base, "/api/config", "PUT", {
                "config": config_state["config"], "expectedRevision": config_state["revision"]})[0] == 409
            invalid_config = json.loads(json.dumps(saved_config["config"]))
            invalid_config["study"]["wordsPerPage"] = 0
            assert call(base, "/api/config", "PUT", {
                "config": invalid_config, "expectedRevision": saved_config["revision"]})[0] == 400
            hulu_project = call(base, "/api/projects", "POST", {"name": "葫芦测试",
                "content": "alpha\nbeta\ngamma\ndelta\nepsilon", "format": "text"})[1]
            hulu = call(base, "/api/plans", "POST", {"projectId": hulu_project["projectId"],
                "method": "hulu", "startDate": "2026-08-01", "dailyItems": 5})[1]
            assert hulu["schedule"]["method"] == "hulu"
            assert hulu["scheduleRunId"] is None
            hulu_states = list((root / "logs" / "hulu-schedule" / "runs").glob("*/state.json"))
            assert hulu_states and all(json.loads(path.read_text(encoding="utf-8"))["status"] == "completed" for path in hulu_states)
            assert len(hulu["schedule"]["days"]) == 1
            assert hulu["schedule"]["days"][0]["review_item_ids"] == []
            assert hulu["rounds"][0]["reviewEndDate"] == "2026-08-01"
            assert next(item for item in call(base, "/api/plans/date/2026-08-01")[1]
                        if item["planId"] == hulu["planId"])["method"] == "hulu"
            assert call(base, f"/api/plans/{hulu['planId']}/day/2026-08-01?pageSize=5")[1]["wordIds"] == hulu["wordIds"]
            for index, word_id in enumerate(hulu["wordIds"][:4]):
                hulu = call(base, f"/api/plans/{hulu['planId']}", "PATCH", {
                    "expectedRevision": hulu["revision"], "action": "pass", "day": "2026-08-01", "wordId": word_id})[1]
                assert hulu["roundComplete"] == (index == 3)
            second = call(base, f"/api/plans/{hulu['planId']}", "PATCH", {
                "expectedRevision": hulu["revision"], "action": "nextRound"})[1]
            assert second["rounds"][-1]["number"] == 2 and second["rounds"][-1]["startDate"] == "2026-08-02"
            managed = call(base, "/api/projects", "POST", {"name": "词表编辑测试",
                "content": f"{word['lemma']}\n{outside_word['lemma']}", "format": "text"})[1]
            managed_plan = call(base, "/api/plans", "POST", {"projectId": managed["projectId"],
                "method": "hulu", "startDate": "2026-08-01", "dailyItems": 1})[1]
            project_url = f"/api/projects/{managed['projectId']}"
            assert call(base, project_url, "DELETE", {"expectedRevision": managed["revision"]})[0] == 409
            reordered = call(base, project_url, "PUT", {"name": managed["name"],
                "content": f"{outside_word['lemma']}\n{word['lemma']}\n{word['lemma']}",
                "expectedRevision": managed["revision"], "replan": False})[1]
            assert len(reordered["words"]) == 2 and reordered["words"][0]["lemma"] == outside_word["lemma"]
            assert call(base, project_url, "PUT", {"name": reordered["name"], "content": word["lemma"],
                "expectedRevision": reordered["revision"]})[0] == 409
            retained = call(base, project_url, "PUT", {"name": reordered["name"], "content": word["lemma"],
                "expectedRevision": reordered["revision"], "replan": False})[1]
            assert len(call(base, f"/api/plans/{managed_plan['planId']}")[1]["wordIds"]) == 2
            regenerated = call(base, project_url, "PUT", {"name": retained["name"],
                "content": f"{word['lemma']}\n{outside_word['lemma']}",
                "expectedRevision": retained["revision"], "replan": True})[1]
            new_plan = call(base, f"/api/plans/{managed_plan['planId']}")[1]
            assert len(new_plan["wordIds"]) == 2 and new_plan["revision"] == managed_plan["revision"] + 1
            assert new_plan["rounds"][0]["passed"] == {} and regenerated["revision"] == retained["revision"] + 1
            assert call(base, f"/api/plans/{managed_plan['planId']}", "DELETE", {"expectedRevision": new_plan["revision"]})[0] == 200
            assert call(base, project_url, "DELETE", {"expectedRevision": regenerated["revision"]})[1]["deleted"]
            assert word["wordId"] in store.entries()
            assert call(base, "/api/projects/default", "DELETE", {"expectedRevision": 1})[1]["deleted"]
            assert call(base, "/api/projects")[1] and all(project["projectId"] != "default" for project in call(base, "/api/projects")[1])
            assert call(base, "/api/bootstrap")[1]["ui"]["activeProjectId"] != "default"
            unassigned = call(base, f"/api/dictionary/search?q={outside_word['lemma']}&project={created['projectId']}")[1]
            assert any(hit["wordId"] == outside_word["wordId"] and hit["built"] and not hit["projects"]
                       for hit in unassigned)
            prefix_project = call(base, "/api/projects", "POST", {"name": "前缀排序测试",
                "content": "passion\ncompass", "format": "text"})[1]
            call(base, "/api/projects", "POST", {"name": "其他前缀",
                "content": "passage", "format": "text"})
            ordered = call(base, f"/api/dictionary/search?q=pass&project={prefix_project['projectId']}")[1]
            assert [hit["lemma"] for hit in ordered[:4]] == ["pass", "passion", "passage", "compass"]
            from utils.scripts.hulu_schedule import run_schedule
            try:
                run_schedule(root, {"n": 2, "d": 3})
            except ValueError:
                pass
            else:
                raise AssertionError("无效葫芦排程未暂停")
            paused_path = next(path for path in (root / "logs" / "hulu-schedule" / "runs").glob("*/state.json")
                               if json.loads(path.read_text(encoding="utf-8"))["status"] == "paused_retryable_error")
            (paused_path.parent / "request.json").write_text('{"n": 2, "d": 2}', encoding="utf-8")
            resumed = run_schedule(root, {}, paused_path.parent.name)
            assert len(resumed["days"]) == 2
            assert not (root / "outputs" / "词汇星图" / "entries").exists()
            pair_project = overview_relation_fixture(store)
            pair_url = f"/api/graph?project={pair_project['projectId']}"
            status, pair_graph = call(base, pair_url)
            assert status == 200, pair_graph
            assert len(pair_graph["nodes"]) == 2
            assert len(pair_graph["edges"]) == 1
            candidate_edge = pair_graph["edges"][0]
            assert candidate_edge["type"] == "near_synonym" and candidate_edge["status"] == "pending"
            assert candidate_edge["ruleClassified"]
            assert call(base, pair_url + "&near_synonym=0")[1]["edges"] == []
            assert call(base, pair_url)[1]["edges"] == pair_graph["edges"]
            # Relations to a visible family node must not be dropped merely because
            # the target is represented by its lemma rather than a full word entry.
            import copy
            alternative = copy.deepcopy(next(e for e in store.entries().values() if e["lemma"] == "alternative"))
            alternative["derivatives"] = [{"derivativeId": "overview_derivative", "word": "alternatively",
                "status": "candidate", "verificationStatus": "agent_reviewed",
                "sourceRefs": alternative["senses"][0]["definitionZh"]["sourceRefs"]}]
            alternative["revision"] += 1
            upsert(store.dicts_dir, alternative, render_entry_line)
            makeshift = copy.deepcopy(next(e for e in store.entries().values() if e["lemma"] == "makeshift"))
            relation = copy.deepcopy(makeshift["pendingRelations"][0])
            relation.update(candidateId="c_00000000000000000001", targetLemma="alternatively")
            makeshift["pendingRelations"].append(relation)
            makeshift["revision"] += 1
            upsert(store.dicts_dir, makeshift, render_entry_line)
            graph_with_derivative = call(base, pair_url)[1]
            assert len(graph_with_derivative["nodes"]) == 3
            derivative_id = next(n["wordId"] for n in graph_with_derivative["nodes"] if n["lemma"] == "alternatively")
            assert any(e["target"] == derivative_id and e["type"] == "near_synonym" for e in graph_with_derivative["edges"])
            graph_without_family = call(base, pair_url + "&family=0")[1]
            assert len(graph_without_family["nodes"]) == 2
            assert len(graph_without_family["edges"]) == 1
            # Legacy configs read with the default gap; invalid gaps are rejected.
            legacy = json.loads(json.dumps(store.config))
            del legacy["graph"]["physics"]["familyNodeGap"]
            import yaml
            config_path = root / "applications" / "词汇星图" / "config.yaml"
            config_path.write_text(yaml.safe_dump(legacy), encoding="utf-8")
            assert DictionaryStore(root).config["graph"]["physics"]["familyNodeGap"] == 8
            current = call(base, "/api/config")[1]
            assert current["config"]["graph"]["physics"]["familyNodeGap"] == 8
            for gap in (-1, 65):
                invalid = json.loads(json.dumps(current["config"]))
                invalid["graph"]["physics"]["familyNodeGap"] = gap
                assert call(base, "/api/config", "PUT", {"config": invalid,
                    "expectedRevision": current["revision"]})[0] == 400
            current["config"]["graph"]["physics"]["familyNodeGap"] = 12
            assert call(base, "/api/config", "PUT", {"config": current["config"],
                "expectedRevision": current["revision"]})[0] == 200
            assert call(base, "/api/bootstrap")[1]["config"]["graph"]["physics"]["familyNodeGap"] == 12
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print("dictionary API smoke: passed")


if __name__ == "__main__":
    main()
