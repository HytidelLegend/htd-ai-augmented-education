"""Persistent vocabulary plans backed by method-specific scheduling state machines."""
from __future__ import annotations

import copy
import hashlib
import json
import random
import subprocess
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from utils.scripts.dictionary_store import ConflictError
from utils.scripts.file_transaction import project_lock
from utils.scripts.structured_io import write_text_atomic
from utils.scripts.timestamp import iso_timestamp, unique_filename_timestamp


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "utils" / "references" / "dictionary-plan-v1.schema.json"
SCHEDULE_SCHEMA = ROOT / "skills" / "schedule-ebbinghaus-plan" / "references" / "result.schema.json"
HULU_SCHEDULE_SCHEMA = ROOT / "utils" / "references" / "hulu-schedule-v1.schema.json"


def day_at(start: str, offset: int) -> str:
    return (date.fromisoformat(start) + timedelta(days=offset)).isoformat()


def plan_progress(plan: dict[str, Any]) -> int:
    return len({word for passed in plan["rounds"][-1]["passed"].values() for word in passed})


def round_complete(plan: dict[str, Any], round_: dict[str, Any], today: str | None = None,
                   page_size: int = 10) -> bool:
    if (today or iso_timestamp()[:10]) < round_["reviewEndDate"]:
        return False
    if plan["method"] == "hulu":
        return all(
            all(5 * len(set(page) & set(round_["passed"].get(day_at(round_["startDate"], day["day"] - 1), []))) >= 4 * len(page)
                for page in [visible_order(plan, round_, day_at(round_["startDate"], day["day"] - 1), page_size)[i:i + page_size]
                             for i in range(0, len(day["item_ids"]), page_size)])
            for day in plan["schedule"]["days"])
    return all(set(plan["wordIds"][item - 1] for item in day["item_ids"]).issubset(
        set(round_["passed"].get(day_at(round_["startDate"], day["day"] - 1), [])))
        for day in plan["schedule"]["days"])


def visible_order(plan: dict[str, Any], round_: dict[str, Any], day: str, page_size: int) -> list[str]:
    offset = (date.fromisoformat(day) - date.fromisoformat(round_["startDate"])).days
    schedule = plan["schedule"]["days"]
    if offset < 0 or offset >= len(schedule):
        return []
    words = [plan["wordIds"][number - 1] for number in schedule[offset]["item_ids"]]
    if not round_["shuffle"]["enabled"]:
        return words
    seed = int(hashlib.sha256(f'{plan["planId"]}:{round_["number"]}:{day}'.encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    if round_["shuffle"]["betweenGroups"]:
        rng.shuffle(words)
    elif round_["shuffle"]["withinGroup"]:
        words = [word for start in range(0, len(words), page_size)
                 for word in rng.sample(words[start:start + page_size], len(words[start:start + page_size]))]
    return words


class PlanStore:
    def __init__(self, root: Path, dictionary: Any):
        self.root = root
        self.dictionary = dictionary
        self.directory = root / "outputs" / "词汇星图" / "plans"

    def _path(self, plan_id: str) -> Path:
        if not plan_id.startswith("plan_") or len(plan_id) != 37 or not all(
                ch in "0123456789abcdef" for ch in plan_id[5:]):
            raise ValueError("无效 planId")
        return self.directory / f"{plan_id}.json"

    def get(self, plan_id: str) -> dict[str, Any]:
        path = self._path(plan_id)
        if not path.exists():
            raise FileNotFoundError(plan_id)
        plan = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(plan)
        schema = HULU_SCHEDULE_SCHEMA if plan["method"] == "hulu" else SCHEDULE_SCHEMA
        Draft202012Validator(json.loads(schema.read_text(encoding="utf-8"))).validate(plan["schedule"])
        return plan

    def list(self) -> list[dict[str, Any]]:
        return [self.get(path.stem) for path in sorted(self.directory.glob("plan_*.json"))]

    def _save(self, plan: dict[str, Any]) -> dict[str, Any]:
        Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(plan)
        schema = HULU_SCHEDULE_SCHEMA if plan["method"] == "hulu" else SCHEDULE_SCHEMA
        Draft202012Validator(json.loads(schema.read_text(encoding="utf-8"))).validate(plan["schedule"])
        write_text_atomic(self._path(plan["planId"]), json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        return plan

    def _schedule(self, request: dict[str, Any], word_ids: list[str] | None = None) -> tuple[dict[str, Any], list[str], str, str | None]:
        words = word_ids if word_ids is not None else [row["wordId"] for row in self.dictionary.project(request["projectId"])["words"]]
        if not words:
            raise ValueError("项目没有单词")
        method = request.get("method")
        if method not in ("ebbinghaus", "hulu"):
            raise ValueError("该记忆方法尚未实现")
        start = date.fromisoformat(request["startDate"]).isoformat()
        if ("dailyItems" in request) == ("firstPassDays" in request):
            raise ValueError("每天单词数与首遍天数只能选择一种作为输入")
        requested = request.get("dailyItems") or request.get("firstPassDays")
        if type(requested) is not int or requested < 1:
            raise ValueError("每天单词数或首遍天数必须是正整数")
        if "firstPassDays" in request and requested > len(words):
            raise ValueError("首遍天数不能超过单词数")
        skill_request = {"n": len(words)}
        if method == "ebbinghaus":
            skill_request["completion_mode"] = "first_pass"
        skill_request["daily_items" if "dailyItems" in request else "d"] = requested
        if method == "hulu":
            from utils.scripts.hulu_schedule import run_schedule
            return run_schedule(self.root, skill_request), words, start, None
        runs_root = self.root / "logs" / "词汇星图" / "runs"
        run_id = unique_filename_timestamp([path.name for path in runs_root.iterdir()] if runs_root.is_dir() else [])
        input_path = runs_root / run_id / "schedule-request.json"
        write_text_atomic(input_path, json.dumps(skill_request, ensure_ascii=False) + "\n")
        skill_name = "schedule-ebbinghaus-plan"
        command = [sys.executable, str(ROOT / "skills" / skill_name / "scripts" / "cli.py"),
                   "start", "--root", str(self.root), "--input", str(input_path)]
        run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=120)
        if run.returncode:
            raise ValueError(f"排程状态机未完成：{run.stdout or run.stderr}")
        receipt = json.loads(run.stdout)
        if receipt.get("status") != "completed":
            raise ValueError(f"排程状态机未完成：{receipt.get('status')}")
        schedule_run_id = receipt["run_id"]
        schedule = json.loads((self.root / "outputs" / skill_name / "runs" / schedule_run_id / "result.json").read_text(encoding="utf-8"))
        return schedule, words, start, schedule_run_id

    def prepare_for_project_words(self, plan: dict[str, Any], word_ids: list[str]) -> dict[str, Any]:
        """Run the existing schedule state machine before changing a project."""
        revised = copy.deepcopy(plan)
        daily_items = max(1, (len(plan["wordIds"]) + len(plan["schedule"]["batches"]) - 1) // len(plan["schedule"]["batches"]))
        request = {"projectId": plan["projectId"], "method": plan["method"],
                   "startDate": plan["rounds"][0]["startDate"], "dailyItems": daily_items}
        schedule, words, start, run_id = self._schedule(request, word_ids)
        revised.update(wordIds=words, schedule=schedule, scheduleRunId=run_id,
                       rounds=[self._first_round(start, schedule)], revision=plan["revision"] + 1)
        return revised

    @staticmethod
    def _first_round(start: str, schedule: dict[str, Any]) -> dict[str, Any]:
        first_pass_days = len(schedule["batches"])
        last_review_day = (max(day["day"] for day in schedule["days"] if day["review_item_ids"])
                           if schedule.get("method") != "hulu" else first_pass_days)
        return {"number": 1, "startDate": start,
                "firstPassEndDate": day_at(start, first_pass_days - 1),
                "reviewEndDate": day_at(start, last_review_day - 1),
                "passed": {}, "shuffle": {"enabled": False, "withinGroup": False, "betweenGroups": False}}

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        schedule, words, start, schedule_run_id = self._schedule(request)
        project = self.dictionary.project(request["projectId"])
        plan_id = "plan_" + uuid.uuid4().hex
        plan = {"schemaVersion": "1.0", "revision": 1, "planId": plan_id,
                "projectId": project["projectId"], "name": request.get("name", "").strip() or f'{project["name"]} · {"葫芦背书法" if request["method"] == "hulu" else "艾宾浩斯"} · {start}',
                "method": request["method"], "createdAt": iso_timestamp(), "wordIds": words,
                "scheduleRunId": schedule_run_id, "schedule": schedule, "rounds": [self._first_round(start, schedule)]}
        return self._save(plan)

    def delete(self, plan_id: str, expected_revision: int) -> None:
        with project_lock(self.root / "logs" / "词汇星图" / "store.lock", f"dictionary:plan:{plan_id}"):
            plan = self.get(plan_id)
            if plan["revision"] != expected_revision:
                raise ConflictError("计划版本冲突")
            self._path(plan_id).unlink()

    def update(self, plan_id: str, expected_revision: int, action: str, request: dict[str, Any]) -> dict[str, Any]:
        with project_lock(self.root / "logs" / "词汇星图" / "store.lock", f"dictionary:plan:{plan_id}"):
            plan = copy.deepcopy(self.get(plan_id))
            if plan["revision"] != expected_revision:
                raise ConflictError("计划版本冲突")
            round_ = plan["rounds"][-1]
            if action == "pass":
                day, word_id = request["day"], request["wordId"]
                if word_id not in visible_order(plan, round_, day, 1):
                    raise ValueError("单词不属于当天计划")
                passed = round_["passed"].setdefault(day, [])
                if word_id not in passed:
                    passed.append(word_id)
            elif action == "retry":
                day, word_id = request["day"], request["wordId"]
                if word_id not in visible_order(plan, round_, day, 1):
                    raise ValueError("单词不属于当天计划")
                if word_id in round_["passed"].get(day, []):
                    round_["passed"][day].remove(word_id)
            elif action == "shuffle":
                shuffle = round_["shuffle"]
                for key in ("enabled", "withinGroup", "betweenGroups"):
                    if key in request:
                        if type(request[key]) is not bool:
                            raise ValueError("乱序按钮状态必须为布尔值")
                        shuffle[key] = request[key]
            elif action == "nextRound":
                if not round_complete(plan, round_, page_size=self.dictionary.config["study"]["wordsPerPage"]):
                    raise ValueError("当前轮尚未完成")
                start = day_at(round_["reviewEndDate"], 1)
                first_pass_days = len(plan["schedule"]["batches"])
                end_day = (first_pass_days if plan["method"] == "hulu" else
                           max(day["day"] for day in plan["schedule"]["days"] if day["review_item_ids"]))
                plan["rounds"].append({"number": round_["number"] + 1, "startDate": start,
                                       "firstPassEndDate": day_at(start, first_pass_days - 1),
                                       "reviewEndDate": day_at(start, end_day - 1),
                                       "passed": {}, "shuffle": {"enabled": False, "withinGroup": False, "betweenGroups": False}})
            elif action == "rename":
                name = request["name"].strip()
                if not name or len(name) > 100:
                    raise ValueError("计划名无效")
                plan["name"] = name
            elif action == "reconfigure":
                name = request.get("name", "").strip()
                if not name or len(name) > 100:
                    raise ValueError("计划名无效")
                schedule, words, start, run_id = self._schedule(request)
                plan.update({"projectId": request["projectId"], "method": request["method"], "wordIds": words,
                             "scheduleRunId": run_id, "schedule": schedule,
                             "rounds": [self._first_round(start, schedule)]})
                plan["name"] = name
            else:
                raise ValueError("无效计划操作")
            plan["revision"] += 1
            return self._save(plan)
