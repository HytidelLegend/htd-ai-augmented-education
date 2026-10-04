#!/usr/bin/env python3
"""State-machine preflight for sensitive Git changes and reusable fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
UTILS = ROOT / "utils" / "scripts"
if str(UTILS) not in sys.path:
    sys.path.insert(0, str(UTILS))

from sensitive_content_scanner import (  # noqa: E402
    load_policy,
    scan_changes,
)
from git_repository import change_snapshot  # noqa: E402
from timestamp import iso_timestamp, unique_filename_timestamp  # noqa: E402
from run_artifact_io import archive_json_input, write_text_atomic  # noqa: E402

try:
    from jsonschema import ValidationError, validate
except ImportError:  # pragma: no cover - runtime requirements provide jsonschema.
    ValidationError = ValueError
    validate = None

RUNS = ROOT / "logs" / "sensitive-commit-check" / "runs"
TERMINAL_STATES = {"approved", "blocked", "needs_user_decision"}
ACTIVE_STATE: dict[str, Any] | None = None
ACTIVE_COMMAND: str | None = None
TRANSITIONS = {
    "created": {"status_captured"},
    "status_captured": {"commit_candidates_resolved"},
    "commit_candidates_resolved": {"diff_captured"},
    "diff_captured": {"deterministic_scan_completed"},
    "deterministic_scan_completed": {"semantic_review_required", "verification_completed"},
    # Legacy runs retain their frozen results and original transitions.
    "supplemental_scope_resolved": {"deterministic_scan_completed"},
    "historical_fingerprint_indexed": {"cross_scope_matches_completed"},
    "cross_scope_matches_completed": {"semantic_review_required"},
    "semantic_review_required": {"archiving_agent_review"},
    "archiving_agent_review": {"semantic_review_completed"},
    "semantic_review_completed": {"verification_completed"},
    "verification_completed": TERMINAL_STATES,
    "needs_user_decision": {"archiving_user_decision"},
    "archiving_user_decision": {"decision_applied"},
    "decision_applied": {"verification_completed"},
    "failed": {"created", "status_captured", "commit_candidates_resolved", "diff_captured", "deterministic_scan_completed",
               "semantic_review_required", "archiving_agent_review", "semantic_review_completed",
               "archiving_user_decision", "decision_applied", "verification_completed"},
}
SCAN_STATES = {"created", "status_captured", "commit_candidates_resolved", "diff_captured", "deterministic_scan_completed"}


def snapshot_fingerprint(scope: str) -> tuple[list[dict], str]:
    changes, digest = change_snapshot(ROOT, scope)
    binding = hashlib.sha256(digest.encode())
    for relative in ("utils/references/sensitive-scan-policy.yaml", "utils/scripts/sensitive_content_scanner.py",
                     "utils/scripts/git_repository.py", "skills/sensitive-commit-check/scripts/sensitive_commit_check.py",
                     "skills/sensitive-commit-check/references/review.schema.json"):
        content = (ROOT / relative).read_bytes()
        binding.update(len(content).to_bytes(8, "big"))
        binding.update(content)
    return changes, binding.hexdigest()


def require_current_diff(state: dict[str, Any]) -> list[dict] | None:
    if state.get("diff_fingerprint"):
        changes, current = snapshot_fingerprint(state["scope"])
        if current != state["diff_fingerprint"]:
            raise RuntimeError("Git 差异或扫描规则已变化，本次审查失效；请重新 start")
        return changes
    stage = state.get("resume_stage") if state.get("status") == "failed" else state.get("status")
    if state.get("schema_version") == "2.1" and stage not in {"created", "status_captured", "commit_candidates_resolved"}:
        raise RuntimeError("本次运行缺少差异指纹；请重新 start")
    return None


def run_git(args: list[str]) -> str:
    process = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if process.returncode:
        raise RuntimeError(process.stderr.strip() or "Git 命令执行失败")
    return process.stdout


def run_dir(run_id: str) -> Path:
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("run-id 必须是运行目录名称")
    return RUNS / run_id


def write_json(path: Path, payload: Any) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def save(state: dict[str, Any]) -> None:
    directory = run_dir(state["run_id"])
    directory.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = iso_timestamp()
    state["last_heartbeat_at"] = state["updated_at"]
    write_json(directory / "state.json", state)


def mark_failed(state: dict[str, Any], error: Exception) -> None:
    """Persist a resumable failure instead of leaving a guessed intermediate state."""
    previous = str(state.get("status", "created"))
    state["status"] = "failed"
    state["current_stage"] = "failed"
    state["error"] = str(error)
    state["resume_stage"] = previous
    state.setdefault("events", []).append({
        "sequence": int(state.get("event_sequence", 0)) + 1,
        "timestamp": iso_timestamp(),
        "from": previous,
        "to": "failed",
    })
    state["event_sequence"] = int(state.get("event_sequence", 0)) + 1
    save(state)


def load(run_id: str) -> dict[str, Any]:
    path = run_dir(run_id) / "state.json"
    if not path.is_file():
        raise RuntimeError(f"找不到运行检查点：{run_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def advance(state: dict[str, Any], target: str, **updates: Any) -> None:
    current = state["status"]
    if target not in TRANSITIONS.get(current, set()):
        raise RuntimeError(f"非法状态迁移：{current} → {target}")
    state["status"] = target
    state["current_stage"] = target
    state.update(updates)
    state.setdefault("completed_steps", []).append(target)
    state["event_sequence"] = int(state.get("event_sequence", 0)) + 1
    state.setdefault("events", []).append({
        "sequence": state["event_sequence"], "timestamp": iso_timestamp(),
        "from": current, "to": target,
    })
    save(state)


def parse_status(raw: str) -> list[dict[str, Any]]:
    records = raw.split("\0")
    items: list[dict[str, Any]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if "\t" in record:
            code, path = record.split("\t", 1)
            code = code.ljust(2)
        else:
            code, path = record[:2], record[3:]
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        if "R" in code or "C" in code:
            if index < len(records) and records[index]:
                index += 1
        items.append({"status": code, "path": path, "exists": (ROOT / path).is_file()})
    return items


def resolve_commit_files(scope: str, raw_status: str) -> list[dict[str, Any]]:
    all_items = parse_status(raw_status)
    if scope == "worktree":
        return all_items
    staged_paths = {
        item for item in run_git(["diff", "--cached", "--name-only", "-z"]).split("\0") if item
    }
    return [item for item in all_items if item["path"] in staged_paths]


def _paths_from_items(items: list[dict[str, Any]]) -> list[Path]:
    return [ROOT / item["path"] for item in items if item.get("exists")]


def scan_file(item: dict[str, Any], scope: str = "worktree") -> list[dict[str, Any]]:
    """Compatibility helper, restricted to this file's current Git additions."""
    changes, _ = change_snapshot(ROOT, scope)
    _, findings = scan_changes([change for change in changes if change["path"] == item["path"]], load_policy())
    return findings


def _finding_id(finding: dict[str, Any]) -> str:
    existing = finding.get("finding_id")
    if existing:
        return str(existing)
    identity = {
        "file": finding.get("file"),
        "source_scope": finding.get("source_scope"),
        "category": finding.get("category"),
        "fingerprint": finding.get("fingerprint"),
        "evidence": finding.get("evidence"),
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for finding in findings:
        item = dict(finding)
        item["finding_id"] = _finding_id(item)
        normalized.append(item)
    return normalized


def _decision_map(decisions: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(item["finding_id"]): str(item["decision"])
        for item in decisions
        if item.get("finding_id") and item.get("decision")
    }


def _effective_finding(finding: dict[str, Any]) -> dict[str, Any]:
    """Apply semantic review to non-high findings while preserving original evidence."""
    if (
        finding.get("risk_level") == "high"
        or finding.get("recommendation") in {"block", "replace_or_confirm"}
    ):
        return finding
    review = finding.get("semantic_review")
    if not isinstance(review, dict):
        return finding
    effective = dict(finding)
    for key in ("risk_level", "category", "recommendation", "confidence"):
        if key in review:
            effective[key] = review[key]
    return effective


def _pending_findings(
    findings: list[dict[str, Any]], decisions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    resolved = _decision_map(decisions or [])
    return [
        finding for finding in findings
        if (
            _effective_finding(finding).get("risk_level") == "medium"
            or _effective_finding(finding).get("recommendation") == "confirm"
        )
        and resolved.get(_finding_id(finding)) != "allow"
    ]


def _terminal_target(
    findings: list[dict[str, Any]], decisions: list[dict[str, Any]] | None = None,
) -> str:
    resolved = _decision_map(decisions or [])
    if any(
        _effective_finding(finding).get("risk_level") == "high"
        or _effective_finding(finding).get("recommendation") in {"block", "replace_or_confirm"}
        for finding in findings
    ):
        return "blocked"
    if any(decision == "block" for decision in resolved.values()):
        return "blocked"
    if _pending_findings(findings, decisions):
        return "needs_user_decision"
    return "approved"


def _merge_review_findings(
    findings: list[dict[str, Any]], review_findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = _normalize_findings(findings)
    by_id = {item["finding_id"]: item for item in merged}
    for review_finding in review_findings:
        item = dict(review_finding)
        target_id = str(item.get("finding_id", ""))
        if target_id and target_id in by_id:
            by_id[target_id]["semantic_review"] = {
                key: value for key, value in item.items() if key != "finding_id"
            }
            continue
        normalized = _normalize_findings([item])[0]
        if normalized["finding_id"] not in by_id:
            merged.append(normalized)
            by_id[normalized["finding_id"]] = normalized
    return merged


def summarize_history_findings(findings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate history findings without copying sensitive evidence into state or reports."""
    summary: dict[str, dict[str, Any]] = {}
    for finding in findings:
        category = str(finding.get("category", "unknown"))
        item = summary.setdefault(category, {"count": 0, "sample_path": str(finding.get("file", ""))})
        item["count"] += 1
    return summary


def write_review_template(state: dict[str, Any]) -> None:
    write_json(run_dir(state["run_id"]) / "review-template.json", {
        "run_id": state["run_id"],
        "reviewed_files": [item["path"] for item in state.get("scan_manifest", [])],
        "review_confirmed": False,
        "findings": [],
        "decisions": [],
    })


def ensure_review_artifacts(state: dict[str, Any]) -> None:
    """Recover generated inputs without overwriting a user's edited template."""
    folder = run_dir(state["run_id"])
    if not (folder / "review_packet.json").is_file():
        write_json(folder / "review_packet.json", {
            "run_id": state["run_id"], "files": state["scan_manifest"], "deterministic_findings": state["findings"],
            "history_summary": {"files_scanned": 0, "fingerprints": 0, "findings": 0,
                                "findings_by_category": {}, "uninspected": 0}})
    if not (folder / "review-template.json").is_file():
        write_review_template(state)


def render_report(state: dict[str, Any]) -> str:
    findings = state.get("findings", [])
    scope_counts: dict[str, int] = {}
    for finding in findings:
        source_scope = str(finding.get("source_scope", "unknown"))
        scope_counts[source_scope] = scope_counts.get(source_scope, 0) + 1
    lines = [
        "# Sensitive Commit Check", "", f"- Run ID: `{state['run_id']}`",
        f"- Status: `{state['status']}`", f"- Commit scope: `{state['scope']}`",
        f"- Supplemental scope: `{state['supplemental_scope']}`",
        f"- Policy version: `{state['policy_version']}`", "", "## 扫描摘要", "",
        f"- Git 候选文件：{len(state.get('commit_files', []))}",
        f"- Skill 样例／回归文件：{len(state.get('skill_files', []))}",
        f"- 历史运行文件：{len(state.get('history_files', []))}",
        f"- 历史脱敏指纹：{state.get('history_fingerprint_count', 0)}",
        f"- 历史敏感命中：{state.get('history_finding_count', 0)}",
        f"- 跨范围匹配：{len(state.get('cross_scope_findings', []))}", "", "## Findings", "",
    ]
    if findings:
        for finding in findings:
            effective = _effective_finding(finding)
            lines.append(
                f"- `{finding.get('file', '')}` — {effective.get('risk_level', '')} / "
                f"{effective.get('category', '')}：{finding.get('evidence', '')}"
            )
    else:
        lines.append("- 未发现敏感信息。")
    lines.extend(["", "## 按范围统计", ""])
    lines.extend(
        (f"- `{scope}`：{count}" for scope, count in sorted(scope_counts.items())),
    )
    if not scope_counts:
        lines.append("- 无")
    decisions = state.get("user_decisions", [])
    lines.extend(["", "## 用户决策", ""])
    if decisions:
        for decision in decisions:
            lines.append(
                f"- `{decision.get('finding_id', '')}` — {decision.get('decision', '')}："
                f"{decision.get('reason', '')}"
            )
    else:
        lines.append("- 无")
    history_summary = state.get("history_findings_summary", {})
    lines.extend(["", "## 历史运行敏感命中", ""])
    if history_summary:
        for category, details in sorted(history_summary.items()):
            lines.append(f"- `{category}`：{details['count']} 个文件，示例路径：`{details['sample_path']}`")
    else:
        lines.append("- 本次未扫描历史运行内容。" if state.get("diff_fingerprint") else "- 未发现历史运行敏感命中。")
    return "\n".join(lines) + "\n"


def write_report(state: dict[str, Any]) -> None:
    write_text_atomic(run_dir(state["run_id"]) / "report.md", render_report(state))


def start(scope: str, supplemental_scope: str = "none") -> dict[str, Any]:
    global ACTIVE_STATE
    if supplemental_scope != "none":
        raise ValueError("补充扫描已停用；本 Skill 只检查 Git 差异")
    existing = [path.name.removeprefix("SCC-") for path in RUNS.iterdir()] if RUNS.is_dir() else []
    run_id = "SCC-" + unique_filename_timestamp(existing)
    policy = load_policy()
    state: dict[str, Any] = {
        "schema_version": "2.1", "workflow": "sensitive-commit-check", "run_id": run_id,
        "status": "created", "current_stage": "created", "resume_stage": None,
        "current_object_id": None, "current_batch_id": run_id, "completed_steps": [],
        "pending_decisions": [], "error": None, "created_at": iso_timestamp(),
        "updated_at": iso_timestamp(), "event_sequence": 0, "scope": scope,
        "supplemental_scope": "none", "policy_version": str(policy.get("schema_version", "unknown")),
        "commit_files": [], "skill_files": [], "history_files": [], "findings": [],
    }
    save(state)
    ACTIVE_STATE = state
    advance(state, "status_captured", git_status=run_git(["status", "--short", "-z", "--untracked-files=all"]))
    return continue_scan(state)


def continue_scan(state: dict[str, Any]) -> dict[str, Any]:
    run_id, scope = state["run_id"], state["scope"]
    if state["status"] == "created":
        advance(state, "status_captured", git_status=run_git(["status", "--short", "-z", "--untracked-files=all"]))
    require_current_diff(state)
    changes, fingerprint = snapshot_fingerprint(scope)
    policy = load_policy(ROOT / "utils/references/sensitive-scan-policy.yaml")
    if state["status"] == "status_captured":
        advance(state, "commit_candidates_resolved", commit_files=[{"path": item["path"], "exists": item["exists"]} for item in changes])
    if state["status"] == "commit_candidates_resolved":
        advance(state, "diff_captured", diff_fingerprint=fingerprint)
    if state["status"] == "diff_captured":
        records, findings = scan_changes(changes, policy)
        advance(state, "deterministic_scan_completed", scan_manifest=records, findings=_normalize_findings(findings))
    records, findings = state["scan_manifest"], state["findings"]
    require_current_diff(state)
    # Regex credentials and exact exclusions need no Agent judgment. Only medium
    # findings go into semantic review (including uninspectable new content).
    if _terminal_target(findings) != "blocked" and any(item["risk_level"] == "medium" for item in findings):
        write_json(run_dir(run_id) / "review_packet.json", {
            "run_id": run_id, "files": records, "deterministic_findings": findings,
            "history_summary": {"files_scanned": 0, "fingerprints": 0, "findings": 0,
                                "findings_by_category": {}, "uninspected": 0},
        })
        advance(state, "semantic_review_required")
        write_review_template(state)
        result = {"run_id": run_id, "status": state["status"],
                  "review_packet": str(run_dir(run_id) / "review_packet.json"),
                  "review_template": str(run_dir(run_id) / "review-template.json")}
    else:
        advance(state, "verification_completed", verification={"review_paths_valid": True,
                "expected_reviewed_file_count": 0, "actual_reviewed_file_count": 0})
        advance(state, _terminal_target(findings))
        result = {"run_id": run_id, "status": state["status"],
                  "report": str(run_dir(run_id) / "report.md")}
    write_report(state)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def validate_review_payload(state: dict[str, Any], payload: Any) -> tuple[set, set]:
    schema_path = ROOT / "skills" / "sensitive-commit-check" / "references" / "review.schema.json"
    if validate is not None:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        try:
            validate(instance=payload, schema=schema)
        except ValidationError as exc:
            raise RuntimeError(f"review JSON 不符合 schema：{exc.message}") from exc
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("findings"), list)
        or not isinstance(payload.get("reviewed_files"), list)
        or not isinstance(payload.get("decisions"), list)
        or payload.get("review_confirmed") is not True
    ):
        raise RuntimeError("review JSON 必须包含完整数组，并将 review_confirmed 设置为 true")
    required = {"file", "risk_level", "category", "evidence", "recommendation", "confidence"}
    for finding in payload["findings"]:
        if not isinstance(finding, dict) or not required.issubset(finding):
            raise RuntimeError("每个 finding 必须包含完整字段")
    expected_paths = {item["path"] for item in state.get("scan_manifest", [])}
    reviewed_paths = set(payload["reviewed_files"])
    missing_paths = sorted(expected_paths - reviewed_paths)
    unexpected_paths = sorted(reviewed_paths - expected_paths)
    if missing_paths or unexpected_paths:
        raise RuntimeError(
            "reviewed_files 必须完整且仅包含扫描清单文件；"
            f"缺少 {len(missing_paths)} 个，多出 {len(unexpected_paths)} 个"
        )
    if payload.get("run_id", state["run_id"]) != state["run_id"]:
        raise RuntimeError("review JSON 不属于本次运行")
    by_id = {item["finding_id"]: item for item in _normalize_findings(state.get("findings", []))}
    for finding in payload["findings"]:
        if finding["file"] not in expected_paths:
            raise RuntimeError("finding 超出本次差异清单")
        if finding["risk_level"] not in {"low", "medium", "high"}:
            raise RuntimeError("finding 的 risk_level 无效")
        if finding["confidence"] not in {"low", "medium", "high"}:
            raise RuntimeError("finding 的 confidence 无效")
        if finding["recommendation"] not in {"allow", "allow_with_warning", "confirm", "block", "replace_or_confirm"}:
            raise RuntimeError("finding 的 recommendation 无效")
        target_id = finding.get("finding_id")
        if target_id and (target_id not in by_id or by_id[target_id]["file"] != finding["file"]):
            raise RuntimeError("finding_id 未知或与文件不一致")
        if state.get("diff_fingerprint") and not target_id:
            raise RuntimeError("语义判断必须引用本次扫描的 finding_id")
    return expected_paths, reviewed_paths


def archived_payload(state: dict[str, Any], key: str) -> dict[str, Any]:
    path = Path(state[key])
    if path.resolve().parent != run_dir(state["run_id"]).resolve():
        raise RuntimeError("审查归档不在本次运行目录")
    content = path.read_bytes()
    expected = state.get(key + "_sha256")
    if expected and hashlib.sha256(content).hexdigest() != expected:
        raise RuntimeError("审查归档已变化")
    return json.loads(content.decode("utf-8-sig"))


def finish_verification(state: dict[str, Any]) -> None:
    require_current_diff(state)
    if state["status"] == "semantic_review_completed":
        payload = archived_payload(state, "archived_review")
        expected, reviewed = validate_review_payload(state, payload)
        advance(state, "verification_completed", verification={
            "review_paths_valid": True, "expected_reviewed_file_count": len(expected),
            "actual_reviewed_file_count": len(reviewed)})
    elif state["status"] == "decision_applied":
        verification = dict(state.get("verification", {}))
        verification["user_decisions_valid"] = True
        advance(state, "verification_completed", verification=verification)
    if state["status"] == "verification_completed":
        target = _terminal_target(state["findings"], state.get("user_decisions", []))
        pending = _pending_findings(state["findings"], state.get("user_decisions", [])) if target == "needs_user_decision" else []
        advance(state, target, pending_decisions=pending)
    write_report(state)


def finish_review(state: dict[str, Any]) -> dict[str, Any]:
    if state["status"] == "archiving_agent_review":
        payload = archived_payload(state, "archived_review")
        validate_review_payload(state, payload)
        findings = _merge_review_findings(state.get("findings", []), payload["findings"])
        advance(state, "semantic_review_completed", findings=findings,
                reviewed_files=payload["reviewed_files"], agent_decisions=payload["decisions"])
    finish_verification(state)
    result = {"run_id": state["run_id"], "status": state["status"], "findings": state["findings"],
              "report": str(run_dir(state["run_id"]) / "report.md")}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def review(run_id: str, input_path: Path) -> dict[str, Any]:
    global ACTIVE_STATE
    state = load(run_id)
    ACTIVE_STATE = state
    if state["status"] != "semantic_review_required":
        raise RuntimeError(f"当前状态不接受 review：{state['status']}")
    require_current_diff(state)
    payload = json.loads(input_path.read_text(encoding="utf-8-sig"))
    validate_review_payload(state, payload)
    archived = run_dir(run_id) / "agent-review.json"
    archive_json_input(input_path, archived, payload)
    advance(state, "archiving_agent_review", archived_review=str(archived),
            archived_review_sha256=hashlib.sha256(archived.read_bytes()).hexdigest())
    return finish_review(state)


def validate_user_decisions(state: dict[str, Any], payload: Any) -> list[dict]:
    decisions = payload.get("decisions") if isinstance(payload, dict) else None
    if (
        not isinstance(decisions, list)
        or not decisions
        or payload.get("decision_confirmed") is not True
    ):
        raise RuntimeError("decision JSON 必须包含非空 decisions，并将 decision_confirmed 设置为 true")

    findings = _normalize_findings(state.get("findings", []))
    finding_by_id = {item["finding_id"]: item for item in findings}
    fingerprint_to_ids: dict[str, list[str]] = {}
    for finding in findings:
        fingerprint = finding.get("fingerprint")
        if fingerprint:
            fingerprint_to_ids.setdefault(str(fingerprint), []).append(finding["finding_id"])

    normalized_decisions: list[dict[str, Any]] = []
    for decision in decisions:
        if not isinstance(decision, dict):
            raise RuntimeError("每个 decision 必须是对象")
        finding_id = str(decision.get("finding_id", ""))
        if finding_id not in finding_by_id:
            legacy_matches = fingerprint_to_ids.get(finding_id, [])
            if len(legacy_matches) == 1:
                finding_id = legacy_matches[0]
            else:
                raise RuntimeError(f"decision 引用了未知或不唯一的 finding_id：{finding_id}")
        action = str(decision.get("decision", ""))
        reason = str(decision.get("reason", "")).strip()
        if action not in {"allow", "block"} or not reason:
            raise RuntimeError("decision 必须为 allow 或 block，且 reason 不能为空")
        finding = _effective_finding(finding_by_id[finding_id])
        if action == "allow" and (
            finding.get("risk_level") == "high"
            or finding.get("recommendation") in {"block", "replace_or_confirm"}
        ):
            raise RuntimeError("高风险或历史指纹匹配不能通过用户确认放行")
        normalized_decisions.append({
            "finding_id": finding_id,
            "decision": action,
            "reason": reason,
        })

    return normalized_decisions


def finish_decision(state: dict[str, Any]) -> dict[str, Any]:
    if state["status"] == "archiving_user_decision":
        payload = archived_payload(state, "archived_user_decision")
        normalized_decisions = validate_user_decisions(state, payload)
        prior = {
            item["finding_id"]: item
            for item in state.get("user_decisions", [])
            if item.get("finding_id")
        }
        for decision in normalized_decisions:
            prior[decision["finding_id"]] = decision
        user_decisions = list(prior.values())
        advance(state, "decision_applied", user_decisions=user_decisions)
    finish_verification(state)
    result = {"run_id": state["run_id"], "status": state["status"],
              "pending_decisions": state.get("pending_decisions", []),
              "report": str(run_dir(state["run_id"]) / "report.md")}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def submit_decision(run_id: str, input_path: Path) -> dict[str, Any]:
    global ACTIVE_STATE
    state = load(run_id)
    ACTIVE_STATE = state
    if state["status"] != "needs_user_decision":
        raise RuntimeError(f"当前状态不接受用户决策：{state['status']}")
    require_current_diff(state)
    payload = json.loads(input_path.read_text(encoding="utf-8-sig"))
    validate_user_decisions(state, payload)
    existing_archives = list(run_dir(run_id).glob("user-decision-*.json"))
    archived = run_dir(run_id) / f"user-decision-{len(existing_archives) + 1:03d}.json"
    archive_json_input(input_path, archived, payload)
    advance(state, "archiving_user_decision", archived_user_decision=str(archived),
            archived_user_decision_sha256=hashlib.sha256(archived.read_bytes()).hexdigest())
    return finish_decision(state)


def verify(run_id: str) -> tuple[dict[str, Any], int]:
    global ACTIVE_STATE
    state = load(run_id)
    ACTIVE_STATE = state
    valid = state["status"] in TERMINAL_STATES and state.get("verification", {}).get("review_paths_valid") is True
    if state.get("schema_version") == "2.1" and not state.get("diff_fingerprint"):
        valid = False
    if state.get("diff_fingerprint"):
        try:
            changes = require_current_diff(state)
            current_records, current_findings = scan_changes(changes, load_policy(ROOT / "utils/references/sensitive-scan-policy.yaml"))
            valid = valid and state.get("scan_manifest") == current_records
            valid = valid and state.get("commit_files") == [{"path": item["path"], "exists": item["exists"]} for item in changes]
            expected_findings = _normalize_findings(current_findings)
            valid = valid and state["status"] == _terminal_target(state["findings"], state.get("user_decisions", []))
            valid = valid and (run_dir(run_id) / "report.md").read_bytes() == render_report(state).encode("utf-8")
            if state.get("archived_review"):
                payload = archived_payload(state, "archived_review")
                validate_review_payload(state, payload)
                valid = valid and state.get("reviewed_files") == payload["reviewed_files"]
                expected_findings = _merge_review_findings(expected_findings, payload["findings"])
            valid = valid and state["findings"] == expected_findings
            if state.get("archived_user_decision"):
                validate_user_decisions(state, archived_payload(state, "archived_user_decision"))
        except (RuntimeError, OSError, ValueError, KeyError, TypeError):
            valid = False
    result = {"run_id": run_id, "status": state["status"], "valid": valid,
              "can_proceed": state["status"] == "approved" and valid}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not valid or state["status"] == "blocked":
        return result, 4
    if state["status"] == "needs_user_decision":
        return result, 3
    return result, 0


def resume(run_id: str) -> dict[str, Any]:
    global ACTIVE_STATE
    state = load(run_id)
    ACTIVE_STATE = state
    require_current_diff(state)
    if state["status"] == "failed" and state.get("resume_stage") in TRANSITIONS["failed"]:
        advance(state, state["resume_stage"], error=None, resume_stage=None)
    if state["status"] in SCAN_STATES:
        return continue_scan(state)
    if state["status"] in {"archiving_agent_review", "semantic_review_completed"}:
        return finish_review(state)
    if state["status"] in {"archiving_user_decision", "decision_applied"}:
        return finish_decision(state)
    if state["status"] == "verification_completed":
        finish_verification(state)
    if state["status"] == "semantic_review_required" and state.get("diff_fingerprint"):
        ensure_review_artifacts(state)
    if state["status"] == "failed":
        raise RuntimeError("该检查点不能安全恢复；请重新 start")
    actions = {
        "semantic_review_required": "读取 review_packet.json，填写 review-template.json 后执行 review",
        "semantic_review_completed": "执行 verification 和终态判定",
        "verification_completed": "根据 findings 推进到终态",
        "needs_user_decision": "填写用户决策 JSON 后执行 submit-decision",
    }
    result = {"run_id": run_id, "status": state["status"],
              "next_action": actions.get(state["status"], "无需恢复；按当前状态处理")}
    write_report(state)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> int:
    global ACTIVE_COMMAND, ACTIVE_STATE
    ACTIVE_STATE = None
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    start_parser = sub.add_parser("start")
    start_parser.add_argument("--scope", choices=["worktree", "staged"], default="worktree")
    start_parser.add_argument("--supplemental",
                              choices=["none", "skills_regression", "historical_runs", "all"],
                              default="none")
    audit_skills_parser = sub.add_parser("audit-skills")
    audit_skills_parser.add_argument("--scope", choices=["worktree", "staged"], default="staged")
    audit_history_parser = sub.add_parser("audit-history")
    audit_history_parser.add_argument("--scope", choices=["worktree", "staged"], default="staged")
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--run-id", required=True)
    review_parser = sub.add_parser("review")
    review_parser.add_argument("--run-id", required=True)
    review_parser.add_argument("--input", type=Path, required=True)
    decision_parser = sub.add_parser("submit-decision")
    decision_parser.add_argument("--run-id", required=True)
    decision_parser.add_argument("--input", type=Path, required=True)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--run-id", required=True)
    resume_parser = sub.add_parser("resume")
    resume_parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    ACTIVE_COMMAND = args.command
    try:
        if args.command == "start":
            start(args.scope, args.supplemental)
        elif args.command == "audit-skills":
            raise ValueError("audit-skills 已停用；请使用 start 检查 Git 差异")
        elif args.command == "audit-history":
            raise ValueError("audit-history 已停用；请使用 start 检查 Git 差异")
        elif args.command == "status":
            print(json.dumps(load(args.run_id), ensure_ascii=False, indent=2))
        elif args.command == "review":
            review(args.run_id, args.input)
        elif args.command == "submit-decision":
            submit_decision(args.run_id, args.input)
        elif args.command == "verify":
            _, code = verify(args.run_id)
            return code
        elif args.command == "resume":
            resume(args.run_id)
        return 0
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        if ACTIVE_STATE is not None and ACTIVE_STATE.get("status") not in TERMINAL_STATES | {"failed"} and (
            ACTIVE_COMMAND in {"start", "resume"}
            or (ACTIVE_COMMAND in {"review", "submit-decision"} and ACTIVE_STATE["status"] in {
                "archiving_agent_review", "semantic_review_completed", "archiving_user_decision", "decision_applied", "verification_completed"})
        ):
            mark_failed(ACTIVE_STATE, exc)
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2 if isinstance(exc, ValueError) else 5


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
