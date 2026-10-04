"""Regression checks for complete classical clause selection."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utils" / "scripts"))
from span_ops import classical_clauses, classical_spans, marked_text, masked_text, validate_classical_spans


def test_classical_clause_keeps_punctuation_and_internal_enumeration() -> None:
    source = "土地平旷，屋舍俨然，有良田、美池、桑竹之属。"
    clauses = classical_clauses(source)
    assert [item["text"] for item in clauses] == ["土地平旷", "屋舍俨然", "有良田、美池、桑竹之属"]
    spans = classical_spans(source, [3])
    assert validate_classical_spans(source, spans)["pass"]
    assert marked_text(source, spans) == "土地平旷，屋舍俨然，「有良田、美池、桑竹之属」。"
    assert masked_text(source, spans) == "土地平旷，屋舍俨然，____。"


def test_classical_clause_rejects_partial_span() -> None:
    source = "山不在高，有仙则名。"
    assert not validate_classical_spans(source, [{"start": 0, "end": 2}])["pass"]


def test_quotes_stay_outside_spans_and_all_clauses_can_be_blank() -> None:
    source = "“先天下之忧而忧，后天下之乐而乐。”"
    clauses = classical_clauses(source)
    assert [item["text"] for item in clauses] == ["先天下之忧而忧", "后天下之乐而乐"]
    spans = classical_spans(source, [1, 2])
    assert validate_classical_spans(source, spans)["pass"]
    assert masked_text(source, spans) == "“____，____。”"


def test_classical_boundary_punctuation_and_newline() -> None:
    source = "甲；乙。丙？丁！戊\n己、庚"
    assert [item["text"] for item in classical_clauses(source)] == ["甲", "乙", "丙", "丁", "戊", "己、庚"]


def test_paused_quality_review_survives_invalid_response(tmp_path: Path) -> None:
    reference = tmp_path / "utils" / "references"
    reference.mkdir(parents=True)
    shutil.copyfile(ROOT / "utils" / "references" / "workflow-state-v1.schema.json", reference / "workflow-state-v1.schema.json")
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"text": "山不在高，有仙则名。"}, ensure_ascii=False), encoding="utf-8")
    script = ROOT / "skills" / "mark-memory-spans" / "scripts" / "cli.py"

    def invoke(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(script), *args, "--root", str(tmp_path)], capture_output=True, text=True, encoding="utf-8")

    started = invoke("start", "--input", str(request))
    assert started.returncode == 3
    run_id = json.loads(started.stdout)["run_id"]
    quality_error = tmp_path / "quality-error.json"
    quality_error.write_text(json.dumps({"mode": "classical_recitation", "selected_clause_ids": [99]}), encoding="utf-8")
    assert invoke("resume", "--run-id", run_id, "--input", str(quality_error)).returncode == 3
    schema_error = tmp_path / "schema-error.json"
    schema_error.write_text("{}", encoding="utf-8")
    assert invoke("resume", "--run-id", run_id, "--input", str(schema_error)).returncode == 2
    state = json.loads(invoke("status", "--run-id", run_id).stdout)
    assert state["status"] == "paused_quality_review"
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"mode": "classical_recitation", "selected_clause_ids": [1]}), encoding="utf-8")
    assert invoke("resume", "--run-id", run_id, "--input", str(good)).returncode == 0
    assert invoke("verify", "--run-id", run_id).returncode == 0
    assert "「山不在高」" in invoke("deliver", "--run-id", run_id).stdout
    result_path = tmp_path / "outputs" / "mark-memory-spans" / "runs" / run_id / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    source = result["source"]["text"]
    base_edit = {"text": source, "source_sha256": result["source"]["sha256"], "spans": result["spans"], "mode": "classical_recitation"}
    partial = tmp_path / "partial-edit.json"
    partial.write_text(json.dumps({**base_edit, "operations": [{"op": "adjust", "span_id": result["spans"][0]["id"], "start": 0, "end": 2}]}, ensure_ascii=False), encoding="utf-8")
    assert invoke("edit", "--input", str(partial)).returncode == 2
    second = classical_clauses(source)[1]
    whole = tmp_path / "whole-edit.json"
    whole.write_text(json.dumps({**base_edit, "operations": [{"op": "adjust", "span_id": result["spans"][0]["id"], "start": second["start"], "end": second["end"]}]}, ensure_ascii=False), encoding="utf-8")
    edited = invoke("edit", "--input", str(whole))
    assert edited.returncode == 0
    assert invoke("verify", "--run-id", json.loads(edited.stdout)["run_id"]).returncode == 0
    result["cloze_check"]["main_clause_preserved"] = True
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    assert invoke("verify", "--run-id", run_id).returncode == 2


def test_parallel_starts_get_distinct_run_ids(tmp_path: Path) -> None:
    reference = tmp_path / "utils" / "references"
    reference.mkdir(parents=True)
    shutil.copyfile(ROOT / "utils" / "references" / "workflow-state-v1.schema.json", reference / "workflow-state-v1.schema.json")
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"text": "山不在高，有仙则名。"}, ensure_ascii=False), encoding="utf-8")
    script = ROOT / "skills" / "mark-memory-spans" / "scripts" / "cli.py"

    def start() -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(script), "start", "--root", str(tmp_path), "--input", str(request)], capture_output=True, text=True, encoding="utf-8")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: start(), range(2)))
    assert [result.returncode for result in results] == [3, 3]
    run_ids = [json.loads(result.stdout)["run_id"] for result in results]
    assert len(set(run_ids)) == 2
