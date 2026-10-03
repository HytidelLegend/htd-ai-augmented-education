from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_DIR / "scripts" / "convert_copy_to_transcript.py"
SPEC = importlib.util.spec_from_file_location("convert_copy_to_transcript", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(SKILL_DIR / "scripts"))
SPEC.loader.exec_module(MODULE)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    references = root / "utils" / "references"
    references.mkdir(parents=True)
    source_references = MODULE.DEFAULT_PROJECT_DIR / "utils" / "references"
    for name in ("workflow-state-v1.schema.json", "artifact-manifest-v1.schema.json"):
        shutil.copy2(source_references / name, references / name)
    return root


def prepared_run(root: Path, text: str) -> tuple[str, Path]:
    receipt = MODULE.prepare(root=root, input_file=None, text=text)
    return receipt["run_id"], Path(receipt["output_dir"])


def test_custom_output_root_uses_shared_tts_workspace(tmp_path: Path) -> None:
    root = project_root(tmp_path)
    output_root = root / "outputs/convert-copy-to-transcript/runs"
    receipt = MODULE.prepare(
        root=root,
        input_file=None,
        text="自定义输出目录。",
        output_root=output_root,
    )
    workspace = Path(receipt["workspace_dir"])
    assert workspace.parent == output_root
    assert workspace.name == receipt["run_id"]
    assert Path(receipt["output_dir"]) == workspace / "transcript"
    assert (MODULE.open_workspace(workspace).inputs_dir / "input.txt").is_file()
    assert (workspace / "pipeline-manifest.json").is_file()


def test_slides_video_workspace_uses_stable_transcript_and_tts_dirs(tmp_path: Path) -> None:
    root = project_root(tmp_path)
    workspace = root / "outputs/convert-copy-to-transcript/runs/20260825T120000"
    workspace.mkdir(parents=True)
    receipt = MODULE.prepare(
        root=root,
        input_file=None,
        text="统一目录。",
        workspace_dir=workspace,
        workspace_run_id="20260825T120000",
    )
    assert Path(receipt["workspace_dir"]) == workspace
    assert Path(receipt["output_dir"]) == workspace / "transcript"
    manifest = json.loads((workspace / "pipeline-manifest.json").read_text(encoding="utf-8"))
    assert manifest["layout_profile"] == "education"
    assert (workspace / "speech").is_dir()
    assert not (workspace / "tts").exists()


def decision_dir(output: Path) -> Path:
    workspace = MODULE.open_workspace(output.parent)
    return workspace.logs_dir / "generated" if workspace.layout_profile == "education" else output / "generated"


def fill_decisions(output: Path, replacements: dict[str, str]) -> None:
    scan = json.loads((decision_dir(output) / "rule-scan.json").read_text(encoding="utf-8"))
    path = decision_dir(output) / "semantic-decisions.json"
    decisions = json.loads(path.read_text(encoding="utf-8"))
    by_id = {item["candidate_id"]: item for item in decisions["decisions"]}
    for candidate in scan["semantic_candidates"]:
        if candidate["kind"] in replacements:
            by_id[candidate["candidate_id"]]["replacement"] = replacements[candidate["kind"]]
    write_json(path, decisions)


def approve_run(root: Path, run_id: str) -> dict:
    state = MODULE.status(root=root, run_id=run_id)
    return MODULE.approve(
        root=root,
        run_id=run_id,
        confirmed_by="pytest-user-confirmation",
        preview_sha256=state["preview_sha256"],
    )


def test_full_workflow_covers_markdown_formula_numbers_pronoun_and_abbreviation(tmp_path: Path) -> None:
    root = project_root(tmp_path)
    source = root / "sample.md"
    text = (
        "# 标题【不发音】\n"
        "1. 因此，AI 会帮助 ta 完成 2 个任务。\n"
        "- 公式是 $x^2+y^2=z^2$。\n"
        "这是 **English**、（括号）和《书名》。\n"
        "---\n"
    )
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(text, encoding="utf-8", newline="\n")
    original_bytes = source.read_bytes()

    receipt = MODULE.prepare(root=root, input_file=source, text=None)
    run_id = receipt["run_id"]
    output = Path(receipt["output_dir"])
    assert receipt["status"] == "awaiting_semantic_decisions"
    assert source.read_bytes() == original_bytes
    assert (MODULE.open_workspace(output.parent).inputs_dir / source.name).read_bytes() == original_bytes
    assert (MODULE.open_workspace(output.parent).inputs_dir / "input-metadata.json").is_file()
    assert not (output / "approved" / "transcript.txt").exists()

    scan = json.loads((decision_dir(output) / "rule-scan.json").read_text(encoding="utf-8"))
    kinds = {item["kind"] for item in scan["semantic_candidates"]}
    assert {"formula", "number", "pronoun_ta", "abbreviation", "written_expression"} <= kinds
    fill_decisions(
        output,
        {
            "formula": "x 的平方加 y 的平方等于 z 的平方",
            "number": "两",
            "pronoun_ta": "他",
            "written_expression": "所以",
        },
    )
    preview_receipt = MODULE.apply_decisions(root=root, run_id=run_id)
    preview = (output / "generated" / "transcript-preview.txt").read_text(encoding="utf-8")
    assert preview_receipt["status"] == "awaiting_transcript_approval"
    assert preview == (
        "第一，所以，AI 会帮助 他 完成 两 个任务。\n"
        "公式是 x 的平方加 y 的平方等于 z 的平方。\n"
        "这是 English、括号和书名。\n"
        "\n"
    )
    assert "semantic:formula" in (output / "generated" / "difference-report.md").read_text(encoding="utf-8")

    with pytest.raises(MODULE.TranscriptWorkflowError, match="未确认"):
        MODULE.verify(root=root, run_id=run_id)
    approval = approve_run(root, run_id)
    duplicate = MODULE.approve(
        root=root,
        run_id=run_id,
        confirmed_by="ignored-on-idempotent-call",
        preview_sha256=approval["preview_sha256"],
    )
    assert duplicate == approval
    final = MODULE.verify(root=root, run_id=run_id)
    assert final["status"] == "completed"
    assert final["pending_decisions"] == []
    assert approval["approved_transcript_sha256"]


def test_markdown_speakable_content_selection_is_deterministic(tmp_path: Path) -> None:
    root = project_root(tmp_path)
    source = (
        "# 文档标题\n"
        "2 个前置信息。\n"
        "# 开头\n"
        "开场正文。\n"
        "## 小节标题\n"
        "> {{display:2000ms}}\n"
        "> 引用中的 ta 和 9 不朗读。\n"
        ">\n"
        ">> 嵌套引用也不朗读。\n"
        "{{display:1500ms}}\n"
        "保留甲{{display:300ms}}乙。\n"
        "结尾正文。\n"
    )
    run_id, output = prepared_run(root, source)
    scan = json.loads((decision_dir(output) / "rule-scan.json").read_text(encoding="utf-8"))
    assert scan["content_selection"]["start_heading_found"] is True
    assert scan["summary"]["excluded_ranges"] == 8
    assert not scan["semantic_candidates"]

    MODULE.apply_decisions(root=root, run_id=run_id)
    preview = (output / "generated" / "transcript-preview.txt").read_text(encoding="utf-8")
    assert preview == "开场正文。\n保留甲乙。\n结尾正文。\n"
    report = (output / "generated" / "difference-report.md").read_text(encoding="utf-8")
    assert "## 排除的非朗读内容" in report
    assert "excluded_before_start_heading" in report
    assert "excluded_blockquote_line" in report


def test_missing_start_heading_processes_whole_document_but_drops_headings(tmp_path: Path) -> None:
    root = project_root(tmp_path)
    run_id, output = prepared_run(root, "# 普通标题\n第一段。\n###### 六级标题\n第二段。\n")
    scan = json.loads((decision_dir(output) / "rule-scan.json").read_text(encoding="utf-8"))
    assert scan["content_selection"]["start_heading_found"] is False
    MODULE.apply_decisions(root=root, run_id=run_id)
    assert (output / "generated" / "transcript-preview.txt").read_text(encoding="utf-8") == (
        "第一段。\n第二段。\n"
    )


def test_summary_label_is_deterministically_spoken_only_at_line_start(tmp_path: Path) -> None:
    root = project_root(tmp_path)
    run_id, output = prepared_run(
        root,
        "总结：第一点。\n  总结:第二点。\n这不是总结：第三点。\n",
    )
    scan = json.loads((decision_dir(output) / "rule-scan.json").read_text(encoding="utf-8"))
    summary_edits = [
        item for item in scan["mechanical_edits"] if item["rule"] == "spoken_summary_label"
    ]
    assert len(summary_edits) == 2
    assert not scan["semantic_candidates"]

    MODULE.apply_decisions(root=root, run_id=run_id)
    preview = (output / "generated" / "transcript-preview.txt").read_text(encoding="utf-8")
    assert preview == "总结一下，第一点。\n  总结一下，第二点。\n这不是总结：第三点。\n"


def test_at_signs_distinguish_email_domains_from_social_handles(tmp_path: Path) -> None:
    root = project_root(tmp_path)
    source = (
        "邮箱是 TESTUSER@example.invalid，也可以写成 @example.invalid。\n"
        "博主是 @Hytidel聊商业，英文博主是 @OpenAI。\n"
        "链接标签是 [TESTUSER@example.invalid](mailto:TESTUSER@example.invalid) 和 [@Hytidel聊商业](https://example.com)。\n"
    )
    run_id, output = prepared_run(root, source)
    scan = json.loads((decision_dir(output) / "rule-scan.json").read_text(encoding="utf-8"))
    at_rules = [
        item["rule"] for item in scan["mechanical_edits"] if item["rule"].endswith("_at_sign")
    ]
    assert at_rules.count("email_at_sign") == 2
    assert at_rules.count("social_handle_at_sign") == 2

    MODULE.apply_decisions(root=root, run_id=run_id)
    preview = (output / "generated" / "transcript-preview.txt").read_text(encoding="utf-8")
    assert preview == (
        "邮箱是 TESTUSER艾特example.invalid，也可以写成 艾特example.invalid。\n"
        "博主是 Hytidel聊商业，英文博主是 OpenAI。\n"
        "链接标签是 TESTUSER艾特example.invalid 和 Hytidel聊商业。\n"
    )
    report = (output / "generated" / "difference-report.md").read_text(encoding="utf-8")
    assert "email_at_sign" in report
    assert "social_handle_at_sign" in report


def test_pause_markers_are_preserved_and_excluded_from_semantic_candidates(tmp_path: Path) -> None:
    root = project_root(tmp_path)
    source = "{{pause:250ms}}开头。甲{{pause:1500ms}}{{pause:500ms}}乙。{{pause:60000ms}}\n"
    run_id, output = prepared_run(root, source)
    scan = json.loads((decision_dir(output) / "rule-scan.json").read_text(encoding="utf-8"))
    assert [item["duration_ms"] for item in scan["preserved_directives"]] == [
        250,
        1500,
        500,
        60000,
    ]
    assert not scan["semantic_candidates"]
    result = MODULE.apply_decisions(root=root, run_id=run_id)
    assert result["status"] == "awaiting_transcript_approval"
    assert (output / "generated" / "transcript-preview.txt").read_text(encoding="utf-8") == source
    report = (output / "generated" / "difference-report.md").read_text(encoding="utf-8")
    assert "保留的控制指令" in report
    assert "{{pause:1500ms}}" in report
    assert result["pause_markers"]["total_duration_ms"] == 62250


@pytest.mark.parametrize("marker", ["{{pause:0ms}}", "{{pause:60001ms}}", "{{pause:1.5s}}", "{{pause:ms}}"])
def test_invalid_pause_marker_enters_paused_error(tmp_path: Path, marker: str) -> None:
    root = project_root(tmp_path)
    receipt = MODULE.prepare(root=root, input_file=None, text=f"甲{marker}乙。")
    assert receipt["status"] == "paused_error"
    assert receipt["current_stage"] == "deterministic_transform"
    assert receipt["error"]["code"] == "invalid_pause_marker"


def test_unregistered_decision_is_rejected_then_cli_resume_continues_checkpoint(tmp_path: Path) -> None:
    root = project_root(tmp_path)
    run_id, output = prepared_run(root, "AI 帮助 ta。\n")
    decisions_path = decision_dir(output) / "semantic-decisions.json"
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    decisions["decisions"].append({"candidate_id": "SEM-999", "replacement": "越界"})
    write_json(decisions_path, decisions)
    failed = subprocess.run(
        [sys.executable, str(SCRIPT), "apply-decisions", "--root", str(root), "--run-id", run_id],
        cwd=MODULE.DEFAULT_PROJECT_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert failed.returncode == 1
    assert "扫描器未登记" in failed.stderr
    assert MODULE.status(root=root, run_id=run_id)["status"] == "paused_error"

    decisions["decisions"] = [item for item in decisions["decisions"] if item["candidate_id"] != "SEM-999"]
    for item in decisions["decisions"]:
        if not item["replacement"]:
            item["replacement"] = "他"
    write_json(decisions_path, decisions)
    resumed = MODULE.resume(root=root, run_id=run_id)
    assert resumed["status"] == "awaiting_semantic_decisions"
    assert resumed["pending_decisions"]
    assert MODULE.apply_decisions(root=root, run_id=run_id)["status"] == "awaiting_transcript_approval"


def test_reapplying_decisions_records_revision_required_path(tmp_path: Path) -> None:
    root = project_root(tmp_path)
    run_id, output = prepared_run(root, "因此这是 2 项。\n")
    fill_decisions(output, {"number": "二", "written_expression": "因此"})
    first = MODULE.apply_decisions(root=root, run_id=run_id)
    assert first["status"] == "awaiting_transcript_approval"
    fill_decisions(output, {"number": "两", "written_expression": "所以"})
    second = MODULE.apply_decisions(root=root, run_id=run_id)
    assert second["status"] == "awaiting_transcript_approval"
    assert (output / "generated" / "transcript-preview.txt").read_text(encoding="utf-8") == "所以这是 两 项。\n"
    events = [
        json.loads(line)
        for path in (root / "logs" / MODULE.WORKFLOW / "runs" / run_id / "events").iterdir()
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(event.get("to_status") == "revision_required" for event in events)


def test_input_copy_hash_change_is_detected(tmp_path: Path) -> None:
    root = project_root(tmp_path)
    run_id, output = prepared_run(root, "普通文本。\n")
    copied = MODULE.open_workspace(output.parent).inputs_dir / "input.txt"
    copied.write_text("被篡改。\n", encoding="utf-8", newline="\n")
    with pytest.raises(MODULE.TranscriptWorkflowError, match="哈希已变化"):
        MODULE.apply_decisions(root=root, run_id=run_id)


@pytest.mark.parametrize("tts_schema_version", [2, 3])
def test_tts_handoff_requires_approved_hash_and_revision_reapproval(
    tmp_path: Path, tts_schema_version: int
) -> None:
    root = project_root(tmp_path)
    run_id, output = prepared_run(root, "普通文本。\n")
    MODULE.apply_decisions(root=root, run_id=run_id)
    approval = approve_run(root, run_id)
    matching = root / "matching-tts-manifest.json"
    write_json(
        matching,
        {
            "schema_version": tts_schema_version,
            "run_id": "tts-run",
            "mode": "original",
            "status": "completed",
            "input": {
                "copied_path": "tmp/tts/inputs/transcript.txt",
                "file_sha256": approval["approved_transcript_sha256"],
            },
        },
    )
    result = MODULE.verify(root=root, run_id=run_id, tts_manifest=matching)
    assert result["status"] == "completed"
    handoff = json.loads((decision_dir(output) / "tts-validation.json").read_text(encoding="utf-8"))
    assert handoff["input_file_sha256"] == approval["approved_transcript_sha256"]

    second_run, second_output = prepared_run(root, "另一段文本。\n")
    MODULE.apply_decisions(root=root, run_id=second_run)
    approve_run(root, second_run)
    revision = root / "revision-tts-manifest.json"
    write_json(
        revision,
        {
            "schema_version": tts_schema_version,
            "run_id": "tts-revision",
            "mode": "revision",
            "status": "completed",
            "revision": {"base_run_id": "tts-base"},
            "input": {
                "copied_path": "tmp/tts/inputs/revised-transcript.txt",
                "file_sha256": "0" * 64,
                "base_run_id": "tts-base",
                "base_input_path": "tmp/tts-base/inputs/transcript.txt"
            },
        },
    )
    with pytest.raises(ValueError, match="重新确认"):
        MODULE.verify(root=root, run_id=second_run, tts_manifest=revision)
    assert not (decision_dir(second_output) / "tts-validation.json").exists()


def test_stale_preview_hash_and_unapproved_completion_are_rejected(tmp_path: Path) -> None:
    root = project_root(tmp_path)
    run_id, _ = prepared_run(root, "普通文本。\n")
    MODULE.apply_decisions(root=root, run_id=run_id)
    with pytest.raises(MODULE.TranscriptWorkflowError, match="预览哈希不一致"):
        MODULE.approve(root=root, run_id=run_id, confirmed_by="user", preview_sha256="f" * 64)
    assert MODULE.status(root=root, run_id=run_id)["status"] == "awaiting_transcript_approval"


def test_approved_transcript_cannot_be_silently_modified(tmp_path: Path) -> None:
    root = project_root(tmp_path)
    run_id, output = prepared_run(root, "普通文本。\n")
    MODULE.apply_decisions(root=root, run_id=run_id)
    approve_run(root, run_id)
    approved = output / "approved" / "transcript.txt"
    approved.write_text("静默修改。\n", encoding="utf-8", newline="\n")
    with pytest.raises(MODULE.TranscriptWorkflowError, match="哈希不一致"):
        MODULE.verify(root=root, run_id=run_id)
    assert MODULE.status(root=root, run_id=run_id)["status"] == "approved"


def test_zero_candidates_auto_preview_and_invalid_commands_preserve_state(tmp_path):
    root = project_root(tmp_path)
    receipt = MODULE.prepare(root=root, input_file=None, text="你好，很高兴见到你")
    assert receipt["status"] == "awaiting_transcript_approval"
    assert Path(receipt["preview_path"]).read_text(encoding="utf-8") == "你好，很高兴见到你"
    store = MODULE.state_store(root, receipt["run_id"])
    original = store.path.read_bytes()
    result = subprocess.run([sys.executable, str(SCRIPT), "resume", "--root", str(root), "--run-id", receipt["run_id"]], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0
    assert store.path.read_bytes() == original
    assert MODULE.status(root=root, run_id=receipt["run_id"])["approval"] is None


def test_shared_next_action_uses_real_commands_and_consumer_context():
    from utils.scripts.speech_cli import transcript_next_action
    commands = MODULE.build_parser()._subparsers._group_actions[0].choices
    for status in MODULE.DEFINITION.statuses:
        assert transcript_next_action(status) in commands
    assert transcript_next_action("completed") == "verify"
    assert transcript_next_action("completed", consumer=True) == "resume"
