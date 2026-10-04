from __future__ import annotations
import importlib.util
from pathlib import Path

import json
import pytest
import subprocess
import shutil
import sys
import os

MODULE = Path(__file__).parents[1] / "scripts" / "sensitive_commit_check.py"
spec = importlib.util.spec_from_file_location("scc", MODULE)
scc = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(scc)

def _credential(value: str) -> str:
    # Construct a fake credential at runtime to exercise high-risk detection;
    # the regression source itself contains no credential assignment.
    return "API_" + f"KEY = '{value}'\n"


def test_parse_status_handles_rename() -> None:
    assert scc.parse_status(" M a.md\0R  old.md -> new.md\0") == [{"status": " M", "path": "a.md", "exists": False}, {"status": "R ", "path": "new.md", "exists": False}]

def test_scan_file_detects_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(scc, "ROOT", tmp_path)
    git(tmp_path, "init", "-q")
    (tmp_path / "config.py").write_text(_credential('abcdefghijklmnop'), encoding="utf-8")
    assert scc.scan_file({"path": "config.py"})[0]["risk_level"] == "high"

def test_office_file_is_confirmation_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(scc, "ROOT", tmp_path)
    git(tmp_path, "init", "-q")
    (tmp_path / "plan.xlsx").write_bytes(b"not scanned")
    assert scc.scan_file({"path": "plan.xlsx"})[0]["category"] == "binary_or_office_confirmation"

def test_parse_staged_status() -> None:
    assert scc.parse_status("M\tconfig.py\0") == [{"status": "M ", "path": "config.py", "exists": False}]


def _review_state(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "status": "semantic_review_required",
        "current_stage": "semantic_review_required",
        "event_sequence": 0,
        "events": [],
        "completed_steps": [],
        "scope": "staged",
        "supplemental_scope": "none",
        "policy_version": "1.0",
        "commit_files": [],
        "skill_files": [],
        "history_files": [],
        "scan_manifest": [{"path": "config.py", "scan_status": "scanned"}],
        "findings": [],
    }


def test_review_requires_complete_manifest_and_confirmation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(scc, "RUNS", tmp_path)
    state = _review_state("run-1")
    scc.save(state)
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps({"reviewed_files": [], "review_confirmed": True, "findings": [], "decisions": []}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="缺少 1 个"):
        scc.review("run-1", review_path)
    assert scc.load("run-1")["status"] == "semantic_review_required"


def test_review_reaches_approved_only_after_full_confirmation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(scc, "RUNS", tmp_path)
    state = _review_state("run-2")
    scc.save(state)
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps({
            "reviewed_files": ["config.py"],
            "review_confirmed": True,
            "findings": [],
            "decisions": [],
        }),
        encoding="utf-8",
    )
    assert scc.review("run-2", review_path)["status"] == "approved"
    assert scc.verify("run-2")[0]["can_proceed"] is True


def git(root: Path, *args: str) -> str:
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    return subprocess.check_output(
        ["git", "-c", "user.name=TEST", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", *args], cwd=root, env=env,
        text=True, encoding="utf-8",
    ).strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("logs/\n__pycache__/\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "baseline")
    return tmp_path


def findings(root: Path, scope: str = "worktree") -> list[dict]:
    changes, _ = scc.change_snapshot(root, scope)
    return scc.scan_changes(changes, scc.load_policy())[1]


def test_only_added_lines_and_unignored_new_files(repo: Path) -> None:
    (repo / "old.py").write_text(_credential('abcdefghijklmnop'), encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "old credential")
    (repo / "old.py").write_text(_credential('abcdefghijklmnop') + "safe = True\n", encoding="utf-8")
    (repo / "logs").mkdir()
    (repo / "logs/secret.txt").write_text(_credential('ignoredsecret123'), encoding="utf-8")
    assert findings(repo) == []
    (repo / "newdir").mkdir()
    (repo / "newdir/config").write_text(_credential('newcredential123'), encoding="utf-8")
    found = findings(repo)
    assert [(item["file"], item["risk_level"]) for item in found] == [("newdir/config", "high")]
    assert findings(repo, "staged") == []


def test_staged_reads_index_and_ignores_unstaged_content(repo: Path) -> None:
    target = repo / "config.py"
    target.write_text("safe = True\n", encoding="utf-8")
    git(repo, "add", ".")
    _, frozen = scc.change_snapshot(repo, "staged")
    target.write_text(_credential('unstagedsecret12'), encoding="utf-8")
    assert findings(repo, "staged") == []
    assert scc.change_snapshot(repo, "staged")[1] == frozen
    assert findings(repo)[0]["risk_level"] == "high"
    git(repo, "add", "config.py")
    target.write_text("safe = True\n", encoding="utf-8")
    assert findings(repo, "staged")[0]["risk_level"] == "high"


def test_precise_exclusions_do_not_hide_other_matches() -> None:
    from sensitive_content_scanner import scan_text
    policy = scc.load_policy()
    contact = "hytidel333" + "@" + "gmail.com"
    assert scan_text(f"申请授权请发送邮件至：{contact}。", "README.md", policy, "commit") == []
    assert scan_text(f"申请授权请发送邮件至：{contact}。", "private.md", policy, "commit")
    assert scan_text(f"记录邮箱：{contact}。", "README.md", policy, "commit")
    assert scan_text("八字段目录与八字段模板", "doc.md", policy, "commit") == []
    found = scan_text("八字段模板；八字分析", "doc.md", policy, "commit", 37)
    assert len(found) == 1 and "第 37 行" in found[0]["evidence"]
    found = scan_text(f"申请授权请发送邮件至：{contact}。 " + _credential("newcredential123"), "README.md", policy, "commit")
    assert len(found) == 1 and found[0]["risk_level"] == "high"


def test_rename_and_deletion_do_not_rescan_old_content(repo: Path) -> None:
    target = repo / "old.py"
    target.write_text(_credential('abcdefghijklmnop') + "safe = True\n" * 30, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "baseline text")
    git(repo, "mv", "old.py", "new.py")
    assert findings(repo) == []
    (repo / "new.py").write_text((repo / "new.py").read_text() + _credential('anothersecret123'), encoding="utf-8")
    found = findings(repo)
    assert len(found) == 1 and "第 32 行" in found[0]["evidence"]
    (repo / "new.py").unlink()
    assert findings(repo) == []


def test_unborn_repository_and_extensionless_text(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    (tmp_path / "CONFIG").write_text(_credential('newcredential123'), encoding="utf-8")
    assert findings(tmp_path)[0]["risk_level"] == "high"
    git(tmp_path, "add", ".")
    assert findings(tmp_path, "staged")[0]["risk_level"] == "high"


def test_new_binary_needs_confirmation(repo: Path) -> None:
    (repo / "data.bin").write_bytes(b"abc\0def")
    assert findings(repo)[0]["category"] == "content_not_inspected"
    (repo / "report.xlsx").write_bytes(b"office")
    assert any(item["category"] == "binary_or_office_confirmation" for item in findings(repo))


def install_fixture_skill(root: Path) -> Path:
    source = MODULE.parents[3]
    paths = [
        "skills/sensitive-commit-check/scripts/cli.py",
        "skills/sensitive-commit-check/scripts/sensitive_commit_check.py",
        "skills/sensitive-commit-check/references/review.schema.json",
        "utils/scripts/git_repository.py", "utils/scripts/sensitive_content_scanner.py",
        "utils/scripts/timestamp.py", "utils/scripts/run_artifact_io.py",
        "utils/references/sensitive-scan-policy.yaml",
    ]
    for relative in paths:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, destination)
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture skill")
    return root / paths[0]


def call_cli(root: Path, cli: Path, *args: str, code: int = 0) -> dict:
    result = subprocess.run([sys.executable, str(cli), *args], cwd=root,
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == code, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_all_cli_interfaces_and_decision_flow(repo: Path) -> None:
    cli = install_fixture_skill(repo)
    first = call_cli(repo, cli, "create-request", "--scope", "staged")
    assert first["status"] == "approved"
    run = first["run_id"]
    assert call_cli(repo, cli, "status", "--run-id", run)["status"] == "approved"
    assert call_cli(repo, cli, "resume", "--run-id", run)["status"] == "approved"
    assert call_cli(repo, cli, "verify", "--run-id", run)["can_proceed"]
    (repo / "note.md").write_text("出生地点：待填写\n", encoding="utf-8")
    git(repo, "add", "note.md")
    start = call_cli(repo, cli, "start", "--scope", "staged")
    assert start["status"] == "semantic_review_required"
    run = start["run_id"]
    assert call_cli(repo, cli, "verify", "--run-id", run, code=4)["valid"] is False
    assert "review" in call_cli(repo, cli, "resume", "--run-id", run)["next_action"]
    template = Path(start["review_template"])
    review = json.loads(template.read_text(encoding="utf-8"))
    review["review_confirmed"] = True
    template.write_text(json.dumps(review), encoding="utf-8")
    assert call_cli(repo, cli, "review", "--run-id", run, "--input", str(template))["status"] == "needs_user_decision"
    assert call_cli(repo, cli, "verify", "--run-id", run, code=3)["can_proceed"] is False
    state = call_cli(repo, cli, "status", "--run-id", run)
    decision = template.parent / "decision.json"
    decision.write_text(json.dumps({"decision_confirmed": True, "decisions": [
        {"finding_id": state["findings"][0]["finding_id"], "decision": "allow", "reason": "合成空模板"}
    ]}), encoding="utf-8")
    assert call_cli(repo, cli, "submit-decision", "--run-id", run, "--input", str(decision))["status"] == "approved"
    assert call_cli(repo, cli, "verify", "--run-id", run)["can_proceed"]
    for command in ("audit-skills", "audit-history"):
        assert "停用" in call_cli(repo, cli, command, code=2)["error"]
    assert "停用" in call_cli(repo, cli, "start", "--supplemental", "all", code=2)["error"]


def test_diff_change_invalidates_review_and_approval(repo: Path) -> None:
    cli = install_fixture_skill(repo)
    (repo / "note.md").write_text("出生地点：待填写\n", encoding="utf-8")
    start = call_cli(repo, cli, "start")
    run = start["run_id"]
    template = Path(start["review_template"])
    review = json.loads(template.read_text(encoding="utf-8"))
    review["review_confirmed"] = True
    template.write_text(json.dumps(review), encoding="utf-8")
    (repo / "note.md").write_text("safe\n", encoding="utf-8")
    assert "失效" in call_cli(repo, cli, "review", "--run-id", run, "--input", str(template), code=3)["error"]
    fresh = call_cli(repo, cli, "start")
    assert fresh["status"] == "approved"
    (repo / "note.md").write_text(_credential('newcredential123'), encoding="utf-8")
    assert call_cli(repo, cli, "verify", "--run-id", fresh["run_id"], code=4)["can_proceed"] is False
    blocked = call_cli(repo, cli, "start")
    assert blocked["status"] == "blocked"
    assert call_cli(repo, cli, "verify", "--run-id", blocked["run_id"], code=4)["valid"] is True


def test_checkpoint_resume_and_policy_invalidation(repo: Path, monkeypatch) -> None:
    install_fixture_skill(repo)
    monkeypatch.setattr(scc, "ROOT", repo)
    monkeypatch.setattr(scc, "RUNS", repo / "logs/sensitive-commit-check/runs")
    original = scc.scan_changes

    def fail_scan(*args):
        raise OSError("temporary scan failure")

    monkeypatch.setattr(scc, "scan_changes", fail_scan)
    monkeypatch.setattr(sys, "argv", [str(MODULE), "start", "--scope", "staged"])
    assert scc.main() == 5
    run = scc.ACTIVE_STATE["run_id"]
    assert scc.load(run)["resume_stage"] == "diff_captured"
    monkeypatch.setattr(scc, "scan_changes", original)
    assert scc.resume(run)["status"] == "approved"
    assert scc.verify(run)[0]["can_proceed"]
    policy = repo / "utils/references/sensitive-scan-policy.yaml"
    policy.write_text(policy.read_text(encoding="utf-8") + "\n# policy changed\n", encoding="utf-8")
    assert scc.verify(run)[0]["valid"] is False


def test_literal_path_and_tracked_ignored_file(repo: Path) -> None:
    (repo / ".gitignore").write_text("logs/\n__pycache__/\ntracked.txt\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("safe\n", encoding="utf-8")
    git(repo, "add", "-f", "tracked.txt")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "tracked ignored fixture")
    (repo / "tracked.txt").write_text(_credential("newcredential123"), encoding="utf-8")
    # Brackets are valid Windows filenames but special in Git pathspecs.
    (repo / "[config].txt").write_text(_credential("anothersecret123"), encoding="utf-8")
    git(repo, "add", "--", ":(literal)[config].txt")
    assert {item["file"] for item in findings(repo)} == {"tracked.txt", "[config].txt"}


def test_scan_file_compatibility_helper_is_diff_only(repo: Path, monkeypatch) -> None:
    target = repo / "config.py"
    target.write_text(_credential("oldcredential123"), encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "old fixture")
    target.write_text(target.read_text() + "safe = True\n", encoding="utf-8")
    monkeypatch.setattr(scc, "ROOT", repo)
    assert scc.scan_file({"path": "config.py"}) == []


def test_git_binary_attribute_cannot_silently_pass(repo: Path) -> None:
    (repo / ".gitattributes").write_text("*.data binary\n", encoding="utf-8")
    (repo / "config.data").write_text(_credential("newcredential123"), encoding="utf-8")
    git(repo, "add", ".")
    found = findings(repo, "staged")
    assert any(item["file"] == "config.data" and item["category"] == "content_not_inspected" for item in found)


def test_git_line_numbers_ignore_unicode_line_separators(repo: Path) -> None:
    target = repo / "config.txt"
    target.write_text("safe\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "baseline")
    target.write_text("safe\ntext\u2028" + _credential("newcredential123"), encoding="utf-8")
    found = findings(repo)
    assert len(found) == 1 and "第 2 行" in found[0]["evidence"]


def test_deterministic_high_risk_does_not_require_agent(repo: Path) -> None:
    cli = install_fixture_skill(repo)
    (repo / "config.txt").write_text(_credential("newcredential123") + "出生地点：待填写\n", encoding="utf-8")
    result = call_cli(repo, cli, "start")
    assert result["status"] == "blocked"
    assert "review_template" not in result
    assert call_cli(repo, cli, "verify", "--run-id", result["run_id"], code=4)["valid"]


@pytest.mark.parametrize("bad_input", ["outside_file", "unknown_id", "wrong_run", "invalid_risk"])
def test_review_rejects_inputs_outside_frozen_diff(repo: Path, bad_input: str) -> None:
    cli = install_fixture_skill(repo)
    (repo / "note.md").write_text("出生地点：待填写\n", encoding="utf-8")
    start = call_cli(repo, cli, "start")
    packet = json.loads(Path(start["review_packet"]).read_text(encoding="utf-8"))
    template = Path(start["review_template"])
    payload = json.loads(template.read_text(encoding="utf-8"))
    payload["review_confirmed"] = True
    payload["findings"] = [dict(packet["deterministic_findings"][0])]
    if bad_input == "outside_file":
        payload["findings"][0]["file"] = "logs/ignored.md"
    elif bad_input == "unknown_id":
        payload["findings"][0]["finding_id"] = "unknown"
    elif bad_input == "wrong_run":
        payload["run_id"] = "another-run"
    else:
        payload["findings"][0]["risk_level"] = "unknown"
    template.write_text(json.dumps(payload), encoding="utf-8")
    assert call_cli(repo, cli, "review", "--run-id", start["run_id"], "--input", str(template), code=3)["error"]
    assert call_cli(repo, cli, "status", "--run-id", start["run_id"])["status"] == "semantic_review_required"


@pytest.mark.parametrize("phase", ["archiving_agent_review", "semantic_review_completed", "archiving_user_decision", "decision_applied", "verification_completed"])
def test_recover_review_and_decision_checkpoints(repo: Path, monkeypatch, phase: str) -> None:
    install_fixture_skill(repo)
    monkeypatch.setattr(scc, "ROOT", repo)
    monkeypatch.setattr(scc, "RUNS", repo / "logs/sensitive-commit-check/runs")
    (repo / "note.md").write_text("出生地点：待填写\n", encoding="utf-8")
    git(repo, "add", "note.md")
    start = scc.start("staged")
    run = start["run_id"]
    template = Path(start["review_template"])
    payload = json.loads(template.read_text(encoding="utf-8"))
    payload["review_confirmed"] = True
    template.write_text(json.dumps(payload), encoding="utf-8")
    command, input_path = "review", template
    if phase in {"archiving_user_decision", "decision_applied"}:
        scc.review(run, template)
        item = scc.load(run)["findings"][0]
        input_path = template.parent / "decision.json"
        input_path.write_text(json.dumps({"decision_confirmed": True, "decisions": [
            {"finding_id": item["finding_id"], "decision": "allow", "reason": "合成空模板"}
        ]}), encoding="utf-8")
        command = "submit-decision"
    original = scc.advance

    def interrupted(state, target, **updates):
        original(state, target, **updates)
        if target == phase:
            raise OSError("interrupted after checkpoint")

    monkeypatch.setattr(scc, "advance", interrupted)
    monkeypatch.setattr(sys, "argv", [str(MODULE), command, "--run-id", run, "--input", str(input_path)])
    assert scc.main() == 5
    assert scc.load(run)["resume_stage"] == phase
    monkeypatch.setattr(scc, "advance", original)
    recovered = scc.resume(run)
    expected = "approved" if command == "submit-decision" else "needs_user_decision"
    assert recovered["status"] == expected
    assert scc.verify(run)[0]["valid"]


def test_resume_repairs_missing_templates_without_overwriting_edits(repo: Path) -> None:
    cli = install_fixture_skill(repo)
    (repo / "note.md").write_text("出生地点：待填写\n", encoding="utf-8")
    start = call_cli(repo, cli, "start")
    template = Path(start["review_template"])
    template.unlink()
    call_cli(repo, cli, "resume", "--run-id", start["run_id"])
    assert template.is_file()
    payload = json.loads(template.read_text(encoding="utf-8"))
    payload["review_confirmed"] = True
    template.write_text(json.dumps(payload), encoding="utf-8")
    before = template.read_bytes()
    call_cli(repo, cli, "resume", "--run-id", start["run_id"])
    assert template.read_bytes() == before


def test_verify_detects_report_archive_and_rule_changes(repo: Path) -> None:
    cli = install_fixture_skill(repo)
    start = call_cli(repo, cli, "start", "--scope", "staged")
    report = Path(start["report"])
    report.write_text("incorrect report", encoding="utf-8")
    assert call_cli(repo, cli, "verify", "--run-id", start["run_id"], code=4)["valid"] is False
    call_cli(repo, cli, "resume", "--run-id", start["run_id"])
    assert call_cli(repo, cli, "verify", "--run-id", start["run_id"])["valid"]
    scanner = repo / "utils/scripts/sensitive_content_scanner.py"
    scanner.write_text(scanner.read_text(encoding="utf-8") + "\n# rule changed\n", encoding="utf-8")
    assert call_cli(repo, cli, "verify", "--run-id", start["run_id"], code=4)["valid"] is False


def test_stale_decision_and_modified_archive_are_rejected(repo: Path) -> None:
    cli = install_fixture_skill(repo)
    (repo / "note.md").write_text("出生地点：待填写\n", encoding="utf-8")
    start = call_cli(repo, cli, "start")
    template = Path(start["review_template"])
    payload = json.loads(template.read_text(encoding="utf-8"))
    payload["review_confirmed"] = True
    template.write_text(json.dumps(payload), encoding="utf-8")
    call_cli(repo, cli, "review", "--run-id", start["run_id"], "--input", str(template))
    archive = template.parent / "agent-review.json"
    original = archive.read_bytes()
    archive.write_text("{}", encoding="utf-8")
    assert call_cli(repo, cli, "verify", "--run-id", start["run_id"], code=4)["valid"] is False
    archive.write_bytes(original)
    state = call_cli(repo, cli, "status", "--run-id", start["run_id"])
    decision = template.parent / "decision.json"
    decision.write_text(json.dumps({"decision_confirmed": True, "decisions": [
        {"finding_id": state["findings"][0]["finding_id"], "decision": "allow", "reason": "合成空模板"}
    ]}), encoding="utf-8")
    (repo / "note.md").write_text("changed\n", encoding="utf-8")
    assert "失效" in call_cli(repo, cli, "submit-decision", "--run-id", start["run_id"], "--input", str(decision), code=3)["error"]


def test_atomic_checkpoint_preserves_original_on_failure(tmp_path: Path, monkeypatch) -> None:
    import run_artifact_io
    destination = tmp_path / "state.json"
    destination.write_text("original", encoding="utf-8")

    def fail_replace(*args):
        raise OSError("replacement failed")

    monkeypatch.setattr(run_artifact_io.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement failed"):
        run_artifact_io.write_text_atomic(destination, "updated")
    assert destination.read_text() == "original"
    assert list(tmp_path.glob(".checkpoint-*.tmp")) == []


def test_non_utf8_deleted_lines_do_not_invalidate_utf8_additions(repo: Path) -> None:
    target = repo / "config.txt"
    target.write_bytes(b"\xff\nsafe\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "old non-UTF8 text")
    target.write_text("safe\nmore\n", encoding="utf-8")
    assert findings(repo) == []
    target.write_text("safe\n" + _credential("newcredential123"), encoding="utf-8")
    found = findings(repo)
    assert len(found) == 1 and found[0]["risk_level"] == "high"


@pytest.mark.parametrize("quote_paths", ["true", "false"])
def test_git_quoted_unicode_paths_and_prefix_configuration(repo: Path, quote_paths: str) -> None:
    target = repo / "资料 空格.txt"
    target.write_text("safe\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Unicode file")
    git(repo, "config", "core.quotePath", quote_paths)
    git(repo, "config", "diff.noprefix", "true")
    target.write_text("safe\n" + _credential("newcredential123"), encoding="utf-8")
    found = findings(repo)
    assert len(found) == 1 and found[0]["file"] == "资料 空格.txt"


def test_added_blocks_never_mix_other_file_sections() -> None:
    from git_repository import added_line_blocks
    patch = (b"diff --git a/other.txt b/other.txt\n--- a/other.txt\n+++ b/other.txt\n@@ -0,0 +1 @@\n+other\n"
             b"diff --git a/current.txt b/current.txt\n--- a/current.txt\n+++ b/current.txt\n@@ -0,0 +1 @@\n+current\n")
    record = {"path": "current.txt", "exists": True, "untracked": False, "patch": patch, "content": b"current\n"}
    assert added_line_blocks(record) == [(1, "current\n")]


def test_binary_words_in_source_do_not_hide_text_additions(repo: Path) -> None:
    (repo / "code.py").write_text("# Binary files example\n# GIT binary patch example\n" + _credential("newcredential123"), encoding="utf-8")
    git(repo, "add", ".")
    found = findings(repo, "staged")
    assert len(found) == 1 and found[0]["category"] == "api_key"
