"""Exercise the local dictionary API without network or real state writes."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
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
from utils.scripts.dictionary_jsonl import read_bucket, reconcile_entry_files, render_entry_line, upsert, bucket
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
    for path in (ROOT / "outputs" / "词汇星图" / "entries").glob("*.json"):
        shutil.copy2(path, root / "outputs" / "英文词典" / "entries" / path.name)


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        fixture(root)
        store = DictionaryStore(root)
        assert store.migrate_entries()["copied"] == 2
        assert store.migrate_entries()["identical"] == 2
        newer_root = root / "newer_case"
        fixture(newer_root)
        newer_store = DictionaryStore(newer_root)
        newer_store.migrate_entries()
        published_newer = json.loads(json.dumps(next(iter(newer_store.entries().values()))))
        published_newer["revision"] += 1
        upsert(newer_store.dicts_dir, published_newer, render_entry_line)
        assert newer_store.migrate_entries()["identical"] == 2
        assert json.loads((newer_store.entries_dir / f"{published_newer['wordId']}.json").read_text(encoding="utf-8"))["revision"] == published_newer["revision"]
        sample = json.loads(json.dumps(next(iter(store.entries().values()))))
        scratch_work = root / "scratch" / "entries"
        scratch_dict = root / "scratch" / "dicts"
        upsert(scratch_dict, sample, render_entry_line)
        assert reconcile_entry_files(scratch_work, scratch_dict)["working"] == 1
        newer = json.loads(json.dumps(sample))
        newer["revision"] += 1
        upsert(scratch_dict, newer, render_entry_line)
        assert reconcile_entry_files(scratch_work, scratch_dict)["working"] == 1
        assert json.loads((scratch_work / f"{sample['wordId']}.json").read_text(encoding="utf-8"))["revision"] == newer["revision"]
        newer["revision"] += 1
        (scratch_work / f"{sample['wordId']}.json").write_text(render_entry_line(newer) + "\n", encoding="utf-8")
        assert reconcile_entry_files(scratch_work, scratch_dict)["jsonl"] == 1
        assert read_bucket(scratch_dict / f"{bucket(sample['lemma'])}.jsonl")[sample["wordId"]]["revision"] == newer["revision"]
        divergent = json.loads(json.dumps(newer))
        divergent["aliases"] = ["conflict"]
        (scratch_work / f"{sample['wordId']}.json").write_text(render_entry_line(divergent) + "\n", encoding="utf-8")
        try:
            reconcile_entry_files(scratch_work, scratch_dict)
        except ValueError:
            pass
        else:
            raise AssertionError("同修订词条冲突未阻止")
        first = next((root / "outputs" / "英文词典" / "entries").glob("*.json"))
        edited = json.loads(first.read_text(encoding="utf-8"))
        edited["revision"] += 1
        first.write_text(json.dumps(edited, ensure_ascii=False), encoding="utf-8")
        try:
            store.migrate_entries()
        except ConflictError:
            pass
        else:
            raise AssertionError("迁移冲突未阻止")

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
            assert boot["ui"]["activeStageId"] == "all" and len(boot["words"]) == 2
            assert boot["projects"][0]["projectId"] == "default"
            assert len(list(dict_dir.glob("?.jsonl"))) == 26
            assert call(base, "/api/dictionary/search?q=pass")[1]
            assert len(call(base, "/api/words?stage=all")[1]) == 2
            assert len([item for item in call(base, "/api/words?stage=sample")[1] if not item["outsideStage"]]) == 1
            word = boot["words"][0]
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
            assert len(call(base, "/api/projects")[1]) == 2
            renamed = call(base, f"/api/projects/{created['projectId']}", "PUT", {"name": "第二项目", "expectedRevision": 1})[1]
            assert renamed["name"] == "第二项目" and renamed["revision"] == 2
            assert call(base, f"/api/projects/{created['projectId']}", "PUT", {"name": "旧版本", "expectedRevision": 1})[0] == 409
            assert len(call(base, f"/api/graph?project={created['projectId']}")[1]["nodes"]) == 1
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
            for kind in ("synonym", "near_synonym"):
                relation = next(item for item in source_entry["relationships"] if item["type"] == kind)
                relation.update(targetWordId=target_entry["wordId"], targetLemma=target_entry["lemma"],
                                targetSenseId=target_entry["senses"][0]["senseId"], linkStatus="linked")
            focused = store.graph(selected=source_entry["wordId"])
            assert {"synonym", "near_synonym"} <= {edge["type"] for edge in focused["edges"]}
            assert focused["placeholderCount"] > 0
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
            assert {node["wordId"] for node in overview["nodes"]} == {row["wordId"] for row in boot["projects"][0]["words"]}
            searched = store.graph(search="pass")
            assert searched["nodes"] and all("pass" in node["lemma"] for node in searched["nodes"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print("dictionary API smoke: passed")


if __name__ == "__main__":
    main()
