#!/usr/bin/env python3
"""State-machine preflight for sensitive Git changes and reusable fixtures."""

from __future__ import annotations

import argparse
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
    build_history_index,
    cross_scope_matches,
    discover_history_files,
    discover_skill_regression_files,
    load_policy,
    scan_files,
    scan_path,
)
from timestamp import iso_timestamp, unique_filename_timestamp  # noqa: E402
from run_artifact_io import archive_json_input  # noqa: E402

RUNS = ROOT / "logs" / "sensitive-commit-check" / "runs"
TERMINAL_STATES = {"approved", "blocked", "needs_user_decision"}
ACTIVE_STATE: dict[str, Any] | None = None
ACTIVE_COMMAND: str | None = None
TRANSITIONS = {
    "created": {"status_captured"},
    "status_captured": {"commit_candidates_resolved"},
    "commit_candidates_resolved": {"supplemental_scope_resolved"},
    "supplemental_scope_resolved": {"deterministic_scan_completed"},
    "deterministic_scan_completed": {"historical_fingerprint_indexed"},
    "historical_fingerprint_indexed": {"cross_scope_matches_completed"},
    "cross_scope_matches_completed": {"semantic_review_required"},
    "semantic_review_required": {"archiving_agent_review"},
    "archiving_agent_review": {"semantic_review_completed"},
    "semantic_review_completed": {"verification_completed"},
    "verification_completed": TERMINAL_STATES,
}


def run_git(args: list[str]) -> str:
    process = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if process.returncode:
        raise RuntimeError(process.stderr.strip() or "Git 命令执行失败")
    return process.stdout


def run_dir(run_id: str) -> Path:
    return RUNS / run_id


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )


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


def scan_file(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Backward-compatible single-file helper used by existing regression tests."""
    _, findings = scan_path(ROOT / item["path"], ROOT, load_policy(), "commit")
    return findings


def _terminal_target(findings: list[dict[str, Any]]) -> str:
    if any(
        finding.get("risk_level") == "high"
        or finding.get("recommendation") in {"block", "replace_or_confirm"}
        for finding in findings
    ):
        return "blocked"
    if any(
        finding.get("risk_level") == "medium"
        or finding.get("recommendation") == "confirm"
        for finding in findings
    ):
        return "needs_user_decision"
    return "approved"


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


def write_report(state: dict[str, Any]) -> None:
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
            lines.append(
                f"- `{finding.get('file', '')}` — {finding.get('risk_level', '')} / "
                f"{finding.get('category', '')}：{finding.get('evidence', '')}"
            )
    else:
        lines.append("- 未发现敏感信息。")
    lines.extend(["", "## 按范围统计", ""])
    lines.extend(
        (f"- `{scope}`：{count}" for scope, count in sorted(scope_counts.items())),
    )
    if not scope_counts:
        lines.append("- 无")
    history_summary = state.get("history_findings_summary", {})
    lines.extend(["", "## 历史运行敏感命中", ""])
    if history_summary:
        for category, details in sorted(history_summary.items()):
            lines.append(f"- `{category}`：{details['count']} 个文件，示例路径：`{details['sample_path']}`")
    else:
        lines.append("- 未发现历史运行敏感命中。")
    (run_dir(state["run_id"]) / "report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def start(scope: str, supplemental_scope: str) -> dict[str, Any]:
    global ACTIVE_STATE
    existing = [path.name.removeprefix("SCC-") for path in RUNS.iterdir()] if RUNS.is_dir() else []
    run_id = "SCC-" + unique_filename_timestamp(existing)
    policy = load_policy()
    raw_status = run_git(["status", "--short", "-z"])
    state: dict[str, Any] = {
        "schema_version": "2.0", "workflow": "sensitive-commit-check", "run_id": run_id,
        "status": "created", "current_stage": "created", "resume_stage": None,
        "current_object_id": None, "current_batch_id": run_id, "completed_steps": [],
        "pending_decisions": [], "error": None, "created_at": iso_timestamp(),
        "updated_at": iso_timestamp(), "last_heartbeat_at": iso_timestamp(),
        "event_sequence": 0, "scope": scope, "supplemental_scope": supplemental_scope,
        "policy_version": str(policy.get("schema_version", "unknown")), "git_status": raw_status,
        "commit_files": [], "skill_files": [], "history_files": [], "findings": [],
    }
    save(state)
    ACTIVE_STATE = state
    advance(state, "status_captured")
    commit_items = resolve_commit_files(scope, raw_status)
    advance(state, "commit_candidates_resolved", commit_files=commit_items)

    include_skills = supplemental_scope in {"skills_regression", "all"}
    include_history = supplemental_scope in {"historical_runs", "all"}
    skill_paths = discover_skill_regression_files(ROOT, policy) if include_skills else []
    history_paths = discover_history_files(ROOT, policy) if include_history else []
    advance(state, "supplemental_scope_resolved",
            skill_files=[path.relative_to(ROOT).as_posix() for path in skill_paths],
            history_files=[path.relative_to(ROOT).as_posix() for path in history_paths])

    commit_records, commit_findings = scan_files(_paths_from_items(commit_items), ROOT, policy, "commit")
    skill_records, skill_findings = scan_files(skill_paths, ROOT, policy, "skills_regression")
    advance(state, "deterministic_scan_completed", scan_manifest=commit_records + skill_records,
            commit_findings=commit_findings, skill_findings=skill_findings)

    history_records, history_findings = scan_files(history_paths, ROOT, policy, "historical_runs")
    history_index = build_history_index(history_findings)
    write_json(run_dir(run_id) / "history-fingerprints.json", history_index)
    history_summary = summarize_history_findings(history_findings)
    advance(state, "historical_fingerprint_indexed", history_scan_manifest=history_records,
            history_fingerprint_count=len(history_index),
            history_finding_count=len(history_findings),
            history_findings_summary=history_summary,
            history_uninspected_count=sum(
                1 for finding in history_findings
                if finding["category"] in {"content_not_inspected", "binary_or_office_confirmation"}
            ))

    cross_findings = cross_scope_matches(commit_findings + skill_findings, history_index)
    deterministic_findings = commit_findings + skill_findings + cross_findings
    advance(state, "cross_scope_matches_completed", cross_scope_findings=cross_findings,
            findings=deterministic_findings)
    write_json(run_dir(run_id) / "review_packet.json", {
        "run_id": run_id, "files": commit_records + skill_records,
        "deterministic_findings": deterministic_findings,
        "history_summary": {"files_scanned": len(history_records), "fingerprints": len(history_index),
                            "findings": len(history_findings),
                            "findings_by_category": history_summary,
                            "uninspected": state["history_uninspected_count"]},
    })
    advance(state, "semantic_review_required")
    write_review_template(state)
    write_report(state)
    result = {"run_id": run_id, "status": state["status"],
              "review_packet": str(run_dir(run_id) / "review_packet.json"),
              "review_template": str(run_dir(run_id) / "review-template.json")}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def review(run_id: str, input_path: Path) -> dict[str, Any]:
    global ACTIVE_STATE
    state = load(run_id)
    ACTIVE_STATE = state
    if state["status"] != "semantic_review_required":
        raise RuntimeError(f"当前状态不接受 review：{state['status']}")
    payload = json.loads(input_path.read_text(encoding="utf-8"))
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
    archive_json_input(input_path, run_dir(run_id) / "agent-review.json", payload)
    advance(state, "archiving_agent_review", archived_review=str(run_dir(run_id) / "agent-review.json"))
    findings = state.get("findings", []) + payload["findings"]
    advance(state, "semantic_review_completed", findings=findings,
            reviewed_files=payload["reviewed_files"], decisions=payload["decisions"])
    advance(state, "verification_completed", verification={
        "review_paths_valid": True,
        "expected_reviewed_file_count": len(expected_paths),
        "actual_reviewed_file_count": len(reviewed_paths),
    })
    target = _terminal_target(findings)
    advance(state, target, pending_decisions=(findings if target == "needs_user_decision" else []))
    write_report(state)
    result = {"run_id": run_id, "status": target, "findings": findings,
              "report": str(run_dir(run_id) / "report.md")}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def verify(run_id: str) -> tuple[dict[str, Any], int]:
    global ACTIVE_STATE
    state = load(run_id)
    ACTIVE_STATE = state
    valid = state["status"] in TERMINAL_STATES and state.get("verification", {}).get("review_paths_valid") is True
    result = {"run_id": run_id, "status": state["status"], "valid": valid,
              "can_proceed": state["status"] == "approved" and valid}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result, 0 if valid else 1


def resume(run_id: str) -> dict[str, Any]:
    global ACTIVE_STATE
    state = load(run_id)
    ACTIVE_STATE = state
    actions = {
        "semantic_review_required": "读取 review_packet.json，填写 review-template.json 后执行 review",
        "semantic_review_completed": "执行 verification 和终态判定",
        "verification_completed": "根据 findings 推进到终态",
    }
    result = {"run_id": run_id, "status": state["status"],
              "next_action": actions.get(state["status"], "无需恢复；按当前状态处理")}
    write_report(state)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> int:
    global ACTIVE_COMMAND
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    start_parser = sub.add_parser("start")
    start_parser.add_argument("--scope", choices=["worktree", "staged"], default="worktree")
    start_parser.add_argument("--supplemental",
                              choices=["none", "skills_regression", "historical_runs", "all"],
                              default="all")
    audit_skills_parser = sub.add_parser("audit-skills")
    audit_skills_parser.add_argument("--scope", choices=["worktree", "staged"], default="staged")
    audit_history_parser = sub.add_parser("audit-history")
    audit_history_parser.add_argument("--scope", choices=["worktree", "staged"], default="staged")
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--run-id", required=True)
    review_parser = sub.add_parser("review")
    review_parser.add_argument("--run-id", required=True)
    review_parser.add_argument("--input", type=Path, required=True)
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
            start(args.scope, "skills_regression")
        elif args.command == "audit-history":
            start(args.scope, "historical_runs")
        elif args.command == "status":
            print(json.dumps(load(args.run_id), ensure_ascii=False, indent=2))
        elif args.command == "review":
            review(args.run_id, args.input)
        elif args.command == "verify":
            _, code = verify(args.run_id)
            return code
        elif args.command == "resume":
            resume(args.run_id)
        return 0
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        if ACTIVE_COMMAND == "start" and ACTIVE_STATE is not None and ACTIVE_STATE.get("status") not in TERMINAL_STATES | {"failed"}:
            mark_failed(ACTIVE_STATE, exc)
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
