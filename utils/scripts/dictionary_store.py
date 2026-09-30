"""Validated, revisioned dictionary files and graph projections.

The entry files remain the sole authority for lexical facts. Search and graph
responses are rebuilt from them; application states never copy definitions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import unicodedata
import uuid
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from utils.scripts.dictionary_contract import WORD_ID, default_project, default_state, validate
from utils.scripts.dictionary_graph import candidate_node_ids, project_graph
from utils.scripts.dictionary_jsonl import bucket, ensure_buckets, read_all, read_bucket, reconcile_entry_files, render_entry_line, upsert
from utils.scripts.file_transaction import project_lock
from utils.scripts.structured_io import write_text_atomic
from utils.scripts.timestamp import iso_timestamp

RELATION_TYPES = ("family", "synonym", "near_synonym", "antonym", "spelling_similar")
SEMANTIC_TYPES = {"synonym", "near_synonym", "antonym"}
STATE_NAMES = {"learning": "learning-state.json", "favorites": "favorites-state.json", "ui": "ui-state.json"}


class ConflictError(ValueError):
    pass


def normalize_lemma(word: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", word).strip().split()).casefold()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def word_id(lemma: str) -> str:
    normalized = normalize_lemma(lemma)
    if not normalized or len(normalized) > 100 or not re.fullmatch(r"[a-z][a-z '\-]*", normalized):
        raise ValueError(f"无效英文单词：{lemma}")
    return "w_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


class DictionaryStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.data = self.root / "outputs" / "词汇星图"
        self.entries_dir = self.data / "entries"
        self.dicts_dir = self.data / "dicts"
        self.projects_dir = self.data / "projects"
        self.lock = threading.RLock()
        schema_path = self.root / "skills" / "build-word-entry" / "references" / "entry.schema.json"
        self.entry_validator = Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
        graph_schema = self.root / "utils" / "references" / "dictionary-graph-v1.schema.json"
        self.graph_validator = Draft202012Validator(json.loads(graph_schema.read_text(encoding="utf-8")))
        self._entries_cache: dict[str, dict[str, Any]] | None = None
        self._entries_signature: tuple[tuple[str, int, int], ...] | None = None
        config_path = self.root / "applications" / "词汇星图" / "config.yaml"
        self.config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        validate("dictionary-config-v1.schema.json", self.config)

    def migrate_entries(self) -> dict[str, int]:
        """Copy verified legacy entries; never overwrite either location."""
        sources = [self.root / "outputs" / "英文词典" / "entries"]
        copied = identical = 0
        with self.lock:
            ensure_buckets(self.dicts_dir)
            existing = read_all(self.dicts_dir, self.entry_validator.validate)
            for source in sources:
                for path in sorted(source.glob("*.json")):
                    entry = self._read_entry(path)
                    prior = existing.get(entry["wordId"])
                    if prior is not None:
                        if prior["revision"] < entry["revision"] or (
                            prior["revision"] == entry["revision"] and prior != entry
                        ):
                            raise ConflictError(f"迁移冲突：{path.name} 与 JSONL 词条内容不同")
                        identical += 1
                        continue
                    upsert(self.dicts_dir, entry, render_entry_line, self.entry_validator.validate)
                    existing[entry["wordId"]] = entry
                    copied += 1
            reconciled = reconcile_entry_files(self.entries_dir, self.dicts_dir, self.entry_validator.validate)
            copied += reconciled["jsonl"]
            self._entries_cache = None
        return {"copied": copied, "identical": identical}

    def _read_entry(self, path: Path) -> dict[str, Any]:
        raw = path.read_text(encoding="utf-8")
        if any(not re.fullmatch(r"(?:0|1)\.\d{2}", value.strip()) for value in
               re.findall(r'"confidence"\s*:\s*([^,\n}]+)', raw)):
            raise ValueError(f"置信度须保留两位小数：{path.name}")
        entry = json.loads(raw)
        self.entry_validator.validate(entry)
        if path.stem != entry["wordId"]:
            raise ValueError(f"词条文件名与 wordId 不符：{path.name}")
        return entry

    def entries(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            paths = sorted(self.dicts_dir.glob("?.jsonl"))
            signature = tuple((path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in paths)
            if self._entries_cache is not None and signature == self._entries_signature:
                return self._entries_cache
            entries = read_all(self.dicts_dir, self.entry_validator.validate)
            for entry in entries.values():
                senses = {sense["senseId"] for sense in entry["senses"]}
                for relation in entry["relationships"]:
                    if relation.get("sourceSenseId") and relation["sourceSenseId"] not in senses:
                        raise ValueError(f"关系源义项悬空：{entry['wordId']}")
                    target = relation.get("targetWordId")
                    if target:
                        if target not in entries or (relation.get("targetSenseId") and relation["targetSenseId"] not in
                                                     {sense["senseId"] for sense in entries[target]["senses"]}):
                            raise ValueError(f"关系目标悬空：{entry['wordId']}")
                for derivative in entry["derivatives"]:
                    if derivative.get("targetWordId") and derivative["targetWordId"] not in entries:
                        raise ValueError(f"派生词目标悬空：{entry['wordId']}")
            self._entries_cache = entries
            self._entries_signature = signature
            return entries

    def entry(self, word_id: str) -> dict[str, Any]:
        if not re.fullmatch(WORD_ID, word_id):
            raise ValueError("无效 wordId")
        entry = self.entries().get(word_id)
        if entry is None:
            raise FileNotFoundError(word_id)
        return entry

    def _project_path(self, project_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9_-]+", project_id):
            raise ValueError("无效 projectId")
        return self.projects_dir / project_id / "project.json"

    def _default_project(self) -> dict[str, Any]:
        source = self.root / "tmp" / "dicts" / "test_20_words.jsonl"
        if source.is_file():
            rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
            lemmas = [row["word"] for row in rows]
            name = "20 词测试"
        else:
            lemmas = [entry["lemma"] for entry in self.entries().values()]
            name = "默认项目"
        unique = list(dict.fromkeys(normalize_lemma(lemma) for lemma in lemmas))
        return default_project("default", name,
                               [{"wordId": word_id(lemma), "lemma": lemma} for lemma in unique], iso_timestamp())

    def project(self, project_id: str) -> dict[str, Any]:
        path = self._project_path(project_id)
        if project_id == "default" and not path.is_file():
            value = self._default_project()
            validate("dictionary-project-v1.schema.json", value)
            write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        if not path.is_file():
            raise FileNotFoundError(project_id)
        value = json.loads(path.read_text(encoding="utf-8"))
        validate("dictionary-project-v1.schema.json", value)
        if value["projectId"] != project_id or len({row["wordId"] for row in value["words"]}) != len(value["words"]):
            raise ValueError("项目词表重复或 ID 不一致")
        if any(word_id(row["lemma"]) != row["wordId"] for row in value["words"]):
            raise ValueError("项目词表的 wordId 与 lemma 不一致")
        return value

    def projects(self) -> list[dict[str, Any]]:
        self.project("default")
        return [self.project(path.parent.name) for path in sorted(self.projects_dir.glob("*/project.json"))]

    def create_project(self, name: str, raw: str = "", kind: str = "text") -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise ValueError("项目名称无效")
        if kind == "jsonl":
            records = [json.loads(line) for line in raw.splitlines() if line.strip()]
        elif kind == "json":
            parsed = json.loads(raw)
            records = parsed.get("words") if isinstance(parsed, dict) else parsed
        elif kind == "text":
            records = [line.strip() for line in raw.splitlines() if line.strip()]
        else:
            raise ValueError("词表格式只能是 text、json 或 jsonl")
        if not isinstance(records, list):
            raise ValueError("项目词表必须为数组")
        lemmas = []
        for number, record in enumerate(records, 1):
            lemma = record if isinstance(record, str) else record.get("word") if isinstance(record, dict) else None
            if not isinstance(lemma, str) or not lemma.strip():
                raise ValueError(f"项目词表第 {number} 项缺少 word")
            normalized = normalize_lemma(lemma)
            bucket(normalized)
            lemmas.append(normalized)
        unique = list(dict.fromkeys(lemmas))
        value = default_project(uuid.uuid4().hex, name.strip(),
                                [{"wordId": word_id(lemma), "lemma": lemma} for lemma in unique], iso_timestamp())
        validate("dictionary-project-v1.schema.json", value)
        with self.lock, project_lock(self.root / "logs" / "词汇星图" / "store.lock", "dictionary:projects"):
            write_text_atomic(self._project_path(value["projectId"]), json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        return value

    def rename_project(self, project_id: str, name: str, revision: int) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise ValueError("项目名称无效")
        with self.lock, project_lock(self.root / "logs" / "词汇星图" / "store.lock", "dictionary:projects"):
            value = self.project(project_id)
            if value["revision"] != revision:
                raise ConflictError("项目版本冲突")
            value.update(name=name.strip(), revision=revision + 1, updatedAt=iso_timestamp())
            validate("dictionary-project-v1.schema.json", value)
            write_text_atomic(self._project_path(project_id), json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            return value

    def search(self, query: str) -> list[dict[str, Any]]:
        term = normalize_lemma(query)
        if not term:
            return []
        known = self.entries()
        found: dict[str, dict[str, Any]] = {}
        for project in self.projects():
            for row in project["words"]:
                if term not in normalize_lemma(row["lemma"]):
                    continue
                result = found.setdefault(row["wordId"], {**row, "built": row["wordId"] in known, "projects": []})
                result["projects"].append({"projectId": project["projectId"], "name": project["name"]})
        return sorted(found.values(), key=lambda item: (normalize_lemma(item["lemma"]) != term, item["lemma"]))[:100]

    def stage(self, stage_id: str) -> dict[str, Any] | None:
        if stage_id == "all":
            return None
        if not stage_id or not all(ch.isalnum() or ch in "_-" for ch in stage_id):
            raise ValueError("无效 stageId")
        path = self.data / "stages" / f"{stage_id}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        validate("dictionary-stage-v1.schema.json", value)
        if value["stageId"] != stage_id:
            raise ValueError("stageId 与文件名不符")
        entries = self.entries()
        for word_id, choice in value["words"].items():
            if word_id not in entries:
                raise ValueError(f"阶段引用悬空：{word_id}")
            if choice["sourceEntryRevision"] != entries[word_id]["revision"]:
                raise ConflictError(f"阶段引用的词条修订号已变化：{word_id}")
            senses = {sense["senseId"]: sense for sense in entries[word_id]["senses"]}
            chosen = choice["coreSenseIds"] + choice["moreSenseIds"]
            if len(chosen) != len(set(chosen)) or any(sid not in senses for sid in chosen):
                raise ValueError(f"阶段义项引用无效：{word_id}")
            for sense_id, content_ids in choice["contentIdsBySense"].items():
                if sense_id not in chosen:
                    raise ValueError(f"阶段内容不属于所选义项：{sense_id}")
                sense = senses[sense_id]
                valid = {item["itemId"] for group in ("examples", "collocations", "phrases", "grammarTags", "registerTags")
                         for item in sense[group]}
                valid.update((sense["definitionZh"]["itemId"], sense["definitionEn"]["itemId"]))
                if any(item_id not in valid for item_id in content_ids):
                    raise ValueError(f"阶段内容引用悬空：{sense_id}")
            relationships = {r["relationshipId"]: r for r in entries[word_id]["relationships"]}
            for rel_id in choice["relationshipIds"]:
                relation = relationships.get(rel_id)
                if not relation or (relation["type"] in SEMANTIC_TYPES and relation.get("sourceSenseId") not in chosen):
                    raise ValueError(f"阶段关联词引用无效：{rel_id}")
        return value

    def stages(self) -> list[dict[str, Any]]:
        result = [{"stageId": "all", "label": "所有单词（测试）", "revision": 0}]
        for path in sorted((self.data / "stages").glob("*.json")):
            value = self.stage(path.stem)
            if value:
                result.append({"stageId": value["stageId"], "label": value["label"], "revision": value["revision"]})
        return result

    def add_to_stage(self, stage_id: str, word_id: str, expected_revision: int) -> dict[str, Any]:
        if stage_id == "all":
            raise ValueError("测试阶段无需加入单词")
        with self.lock, project_lock(self.root / "logs" / "词汇星图" / "store.lock", f"dictionary:stage:{stage_id}"):
            stage = self.stage(stage_id)
            if stage is None or stage["revision"] != expected_revision:
                raise ConflictError("阶段版本冲突，请重新载入")
            entry = self.entry(word_id)
            if word_id in stage["words"]:
                return stage
            core, more = self.selected_senses(entry, None)
            if not core and not more:
                raise ValueError("当前词条没有可加入阶段的义项")
            selected = core + more
            stage["words"][word_id] = {
                "sourceEntryRevision": entry["revision"],
                "coreSenseIds": [sense["senseId"] for sense in core],
                "moreSenseIds": [sense["senseId"] for sense in more],
                "contentIdsBySense": {sense["senseId"]: [item["itemId"] for group in
                    ("examples", "collocations", "phrases", "grammarTags", "registerTags") for item in sense[group]]
                    for sense in selected},
                "relationshipIds": [rel["relationshipId"] for rel in entry["relationships"]
                    if rel["verificationStatus"] != "pending" and
                    (rel["type"] == "spelling_similar" or rel.get("sourceSenseId") in
                     {sense["senseId"] for sense in selected})],
            }
            stage["revision"] += 1
            stage["updatedAt"] = iso_timestamp()
            validate("dictionary-stage-v1.schema.json", stage)
            self.stage(stage_id)  # Validate the old file's references before replacing it.
            write_text_atomic(self.data / "stages" / f"{stage_id}.json", json.dumps(stage, ensure_ascii=False, indent=2) + "\n")
            return stage

    def state(self, kind: str, project_id: str | None = None) -> dict[str, Any]:
        if kind not in STATE_NAMES:
            raise ValueError("无效状态类型")
        if kind == "learning":
            project_id = project_id or self.state("ui")["activeProjectId"]
            self.project(project_id)
            path = self.projects_dir / project_id / "learning-state.json"
            if project_id == "default" and not path.exists():
                legacy = self.data / STATE_NAMES["learning"]
                if legacy.exists():
                    value = json.loads(legacy.read_text(encoding="utf-8"))
                    validate("dictionary-learning-v1.schema.json", value)
                    write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        else:
            path = self.data / STATE_NAMES[kind]
        if not path.exists():
            return default_state(kind, iso_timestamp())
        value = json.loads(path.read_text(encoding="utf-8"))
        if kind == "ui" and "activeProjectId" not in value:
            value["activeProjectId"] = "default"
        validate(f"dictionary-{kind}-v1.schema.json", value)
        return value

    def save_state(self, kind: str, value: dict[str, Any], expected_revision: int,
                   project_id: str | None = None) -> dict[str, Any]:
        if kind not in STATE_NAMES:
            raise ValueError("无效状态类型")
        if kind == "learning" and project_id is None:
            project_id = self.state("ui")["activeProjectId"]
        with self.lock, project_lock(self.root / "logs" / "词汇星图" / "store.lock", f"dictionary:{kind}"):
            current = self.state(kind, project_id)
            if current["revision"] != expected_revision:
                raise ConflictError(f"状态版本冲突：当前 {current['revision']}，请求 {expected_revision}")
            result = copy.deepcopy(value)
            result.update(schemaVersion="1.0", revision=expected_revision + 1,
                          updatedAt=iso_timestamp())
            validate(f"dictionary-{kind}-v1.schema.json", result)
            known = self.entries()
            if kind in ("learning", "favorites") and any(wid not in known for wid in result["words"]):
                raise ValueError("学习或收藏状态存在悬空 wordId")
            if kind == "ui":
                self.project(result["activeProjectId"])
                self.stage(result["activeStageId"])
                tabs = result["tabs"]
                if not tabs or tabs[0]["tabId"] != "graph" or tabs[0]["kind"] != "graph" or not tabs[0]["pinned"]:
                    raise ValueError("图谱标签必须固定在首位")
                ids = [tab["tabId"] for tab in tabs]
                if len(ids) != len(set(ids)) or result["activeTabId"] not in ids:
                    raise ValueError("标签 ID 重复或当前标签不存在")
                if any(tab["kind"] == "word" and tab["wordId"] not in known for tab in tabs):
                    raise ValueError("词条标签引用悬空")
                graph_ids = set(known) | candidate_node_ids(known, normalize_lemma)
                graph_ids.update(row["wordId"] for project in self.projects() for row in project["words"])
                if any(wid not in graph_ids for wid in result["graph"]["positions"]):
                    raise ValueError("图谱位置引用悬空")
            target = (self.projects_dir / project_id / "learning-state.json" if kind == "learning" and project_id
                      else self.data / STATE_NAMES[kind])
            write_text_atomic(target, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            return result

    def patch_learning(self, word_id: str, level: str, revision: int, project_id: str | None = None) -> dict[str, Any]:
        self.entry(word_id)
        if level not in ("unfamiliar", "seen", "familiar"):
            raise ValueError("无效熟悉度")
        project_id = project_id or self.state("ui")["activeProjectId"]
        state = self.state("learning", project_id)
        state["words"][word_id] = level
        return self.save_state("learning", state, revision, project_id)

    def patch_favorite(self, word_id: str, favorited: bool, revision: int) -> dict[str, Any]:
        self.entry(word_id)
        state = self.state("favorites")
        if favorited:
            now = iso_timestamp()
            state["words"].setdefault(word_id, {"favoritedAt": now, "lastOpenedAt": now})
        else:
            state["words"].pop(word_id, None)
        return self.save_state("favorites", state, revision)

    def opened(self, word_id: str, revision: int) -> dict[str, Any]:
        self.entry(word_id)
        state = self.state("favorites")
        if word_id in state["words"]:
            state["words"][word_id]["lastOpenedAt"] = iso_timestamp()
            return self.save_state("favorites", state, revision)
        return state

    @staticmethod
    def selected_senses(entry: dict[str, Any], stage: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        senses = [sense for sense in entry["senses"] if sense["definitionZh"]["verificationStatus"] != "rejected"]
        if stage is None:
            verified = [sense for sense in senses if sense["definitionZh"]["verificationStatus"] not in ("pending", "rejected")]
            core_id = (verified or senses)[0]["senseId"] if senses else None
            return ([s for s in senses if s["senseId"] == core_id],
                    [s for s in senses if s["senseId"] != core_id])
        choice = stage["words"].get(entry["wordId"])
        if not choice:
            return [], []
        by_id = {s["senseId"]: s for s in senses}
        def project(sid: str) -> dict[str, Any]:
            sense = copy.deepcopy(by_id[sid])
            allowed = set(choice["contentIdsBySense"].get(sid, []))
            for group in ("examples", "collocations", "phrases", "grammarTags", "registerTags"):
                sense[group] = [item for item in sense[group] if item["itemId"] in allowed]
            return sense
        return ([project(sid) for sid in choice["coreSenseIds"] if sid in by_id],
                [project(sid) for sid in choice["moreSenseIds"] if sid in by_id])

    def summary(self, entry: dict[str, Any], stage: dict[str, Any] | None) -> dict[str, Any]:
        core, more = self.selected_senses(entry, stage)
        return {"wordId": entry["wordId"], "lemma": entry["lemma"], "familyId": entry["familyId"],
                "core": [{"senseId": s["senseId"], "text": s["definitionZh"]["text"],
                          "partOfSpeech": s["partOfSpeech"],
                          "verificationStatus": s["definitionZh"]["verificationStatus"]} for s in core],
                "more": [{"senseId": s["senseId"], "text": s["definitionZh"]["text"]} for s in more],
                "senseIds": [s["senseId"] for s in core + more], "outsideStage": stage is not None and not (core or more)}

    def bootstrap(self) -> dict[str, Any]:
        ui = self.state("ui")
        return {"config": self.config, "stages": self.stages(), "projects": self.projects(),
                "learning": self.state("learning", ui["activeProjectId"]),
                "favorites": self.state("favorites"), "ui": ui,
                "words": self.word_summaries(ui["activeStageId"])}

    def word_summaries(self, stage_id: str) -> list[dict[str, Any]]:
        stage = self.stage(stage_id)
        return [self.summary(e, stage) for e in self.entries().values()]

    def word_page(self, word_id: str, stage_id: str) -> dict[str, Any]:
        entry = copy.deepcopy(self.entry(word_id))
        stage = self.stage(stage_id)
        core, more = self.selected_senses(entry, stage)
        selected = {s["senseId"] for s in core + more}
        relations = [r for r in entry["relationships"] if r["type"] in RELATION_TYPES and
                     r["verificationStatus"] != "pending" and
                     (r["type"] == "spelling_similar" or r.get("sourceSenseId") in selected) and
                     (stage is None or r["relationshipId"] in stage["words"].get(word_id, {}).get("relationshipIds", []))]
        known = self.entries()
        by_lemma = {normalize_lemma(value["lemma"]): wid for wid, value in known.items()}
        for form in entry["inflections"]:
            form["targetWordId"] = by_lemma.get(normalize_lemma(form["form"]["text"]))
        relations = [{**r, "targetLemma": r.get("targetLemma") or known.get(r.get("targetWordId"), {}).get("lemma")}
                     for r in relations]
        candidates = [r for r in entry.get("pendingRelations", []) if r["sourceSenseId"] in selected]
        return {"entry": entry, "coreSenses": core, "moreSenses": more,
                "relationships": relations, "pendingRelations": candidates,
                "outsideStage": stage is not None and word_id not in stage["words"]}

    def candidate(self, lemma: str) -> dict[str, Any]:
        normalized = normalize_lemma(lemma)
        if not normalized or len(normalized) > 100:
            raise ValueError("无效单词")
        hits = []
        for entry in self.entries().values():
            for relation in entry["relationships"] + entry.get("pendingRelations", []):
                if normalize_lemma(relation.get("targetLemma", "")) == normalized and not relation.get("targetWordId"):
                    source_sense = next((sense for sense in entry["senses"]
                                         if sense["senseId"] == relation.get("sourceSenseId")), None)
                    hits.append({"sourceWordId": entry["wordId"], "sourceLemma": entry["lemma"],
                                 "sourceSenseId": relation.get("sourceSenseId"),
                                 "sourceSenseText": source_sense["definitionZh"]["text"] if source_sense else None,
                                 "type": relation.get("type", relation.get("proposedType")),
                                 "status": "待核验" if relation in entry.get("pendingRelations", []) else
                                           relation.get("verificationStatus", "待核验"),
                                 "sourceRefs": relation.get("sourceRefs", [])})
            for derivative in entry.get("derivatives", []):
                if normalize_lemma(derivative.get("word", "")) == normalized and not derivative.get("targetWordId"):
                    hits.append({"sourceWordId": entry["wordId"], "sourceLemma": entry["lemma"],
                                 "sourceSenseId": None, "type": "family", "status": derivative["status"],
                                 "sourceRefs": derivative.get("sourceRefs", [])})
            for form in entry.get("inflections", []):
                if normalize_lemma(form["form"]["text"]) == normalized:
                    hits.append({"sourceWordId": entry["wordId"], "sourceLemma": entry["lemma"],
                                 "sourceSenseId": None, "type": "family", "status": form["form"]["verificationStatus"],
                                 "sourceRefs": form["form"].get("sourceRefs", [])})
        if not hits:
            if not any(normalized == normalize_lemma(row["lemma"]) for project in self.projects() for row in project["words"]):
                raise FileNotFoundError(lemma)
        return {"lemma": lemma, "associations": hits}

    def graph(self, stage_id: str = "all", selected: str | None = None,
              visible: dict[str, bool] | None = None, show_others: bool = False,
              search: str = "", limit: int = 500, show_outside: bool = False,
              project_id: str | None = None) -> dict[str, Any]:
        entries = self.entries()
        project = self.project(project_id or self.state("ui")["activeProjectId"])
        stage = self.stage(stage_id)
        visible = {key: bool((visible or {}).get(key, True)) for key in RELATION_TYPES}
        summaries = {wid: self.summary(entry, stage) for wid, entry in entries.items()}
        result = project_graph(entries, summaries, normalize=normalize_lemma, selected=selected,
                               visible=visible, show_others=show_others, search=search,
                               limit=limit, show_outside=show_outside, stage=stage,
                               project_words=project["words"])
        self.graph_validator.validate(result)
        return result
