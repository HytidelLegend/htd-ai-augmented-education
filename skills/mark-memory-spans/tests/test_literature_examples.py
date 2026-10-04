"""Regression checks for literature references and the key-points CLI."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utils" / "scripts"))
from render_span_examples import extract_spans, render, validate
from span_ops import candidate_spans, masked_text

SOURCE = ROOT / "skills" / "mark-memory-spans" / "references" / "literature-span-examples.json"
MARKDOWN = SOURCE.with_name("span-examples.md")
CLI = ROOT / "skills" / "mark-memory-spans" / "scripts" / "cli.py"


def test_literature_reference_round_trip_and_candidates() -> None:
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    validate(data)
    assert len(data["examples"]) == 3
    assert render(data) in MARKDOWN.read_text(encoding="utf-8")
    for example in data["examples"]:
        spans = extract_spans(example["source"], example["marked"])
        assert masked_text(example["source"], spans) == example["masked"]
        assert example["negative"] != example["marked"]

    combined = "\n".join(example["source"] for example in data["examples"])
    candidates = candidate_spans(combined)
    values = {item["text"] for item in candidates}
    assert {"朝花夕拾", "从百草园到三味书屋", "三起三落", "贾、史、王、薛", "红楼梦"} <= values
    assert any(item["start"] > combined.index("《红楼梦》") for item in candidates)


def test_candidate_budget_reaches_later_paragraphs_and_late_titles() -> None:
    text = "\n".join("背景资料" + "甲乙丙丁，" * 40 + f"《终章{index}》" for index in range(6))
    candidates = candidate_spans(text, max_candidates=6, per_paragraph=4)
    assert [item["text"] for item in candidates] == [f"终章{index}" for index in range(6)]
    assert all(text[item["start"]:item["end"]] == item["text"] for item in candidates)


def test_key_points_cli_interfaces(tmp_path: Path) -> None:
    reference_dir = tmp_path / "utils" / "references"
    reference_dir.mkdir(parents=True)
    shutil.copyfile(ROOT / "utils" / "references" / "workflow-state-v1.schema.json", reference_dir / "workflow-state-v1.schema.json")
    example = json.loads(SOURCE.read_text(encoding="utf-8"))["examples"][0]
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"text": example["source"]}, ensure_ascii=False), encoding="utf-8")

    def invoke(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(CLI), *args, "--root", str(tmp_path)], capture_output=True, text=True, encoding="utf-8")

    started = invoke("start", "--input", str(request))
    assert started.returncode == 3, started.stdout + started.stderr
    run_id = json.loads(started.stdout)["run_id"]
    assert json.loads(invoke("status", "--run-id", run_id).stdout)["status"] == "paused_agent_selection"

    spans = extract_spans(example["source"], example["marked"])
    response = tmp_path / "response.json"
    response.write_text(json.dumps({
        "mode": "key_points",
        "spans": [{"start": item["start"], "end": item["end"], "role": "文学常识"} for item in spans],
        "cloze_check": {"pass": True, "reason": "主干可读", "main_clause_preserved": True, "overmarking": False},
    }, ensure_ascii=False), encoding="utf-8")
    resumed = invoke("resume", "--run-id", run_id, "--input", str(response))
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert json.loads(invoke("status", "--run-id", run_id).stdout)["status"] == "completed"
    assert invoke("verify", "--run-id", run_id).returncode == 0
    assert example["marked"] in invoke("deliver", "--run-id", run_id).stdout

    result_path = tmp_path / "outputs" / "mark-memory-spans" / "runs" / run_id / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    edit_request = tmp_path / "edit-request.json"
    edit_request.write_text(json.dumps({
        "text": example["source"],
        "source_sha256": result["source"]["sha256"],
        "spans": result["spans"],
        "operations": [{"op": "delete", "span_id": result["spans"][-1]["id"]}],
    }, ensure_ascii=False), encoding="utf-8")
    edited = invoke("edit", "--input", str(edit_request))
    assert edited.returncode == 0, edited.stdout + edited.stderr
    edited_run = json.loads(edited.stdout)["run_id"]
    assert invoke("verify", "--run-id", edited_run).returncode == 0
