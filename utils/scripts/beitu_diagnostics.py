"""Validate, archive, and analyze 背图工具 diagnostic JSONL events."""
from __future__ import annotations
import argparse, json, shutil, sys
from pathlib import Path
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from utils.scripts.workflow_checkpoint import WorkflowCheckpoint, create_run_directory
from utils.scripts.run_state import write_json

SCHEMA = ROOT / "utils" / "references" / "beitu-diagnostic-event-v1.schema.json"

def _sequence_issues(rows: list[dict]) -> list[str]:
    sequences = [row["sequence"] for row in rows]
    issues: list[str] = []
    if any(current <= previous for previous, current in zip(sequences, sequences[1:])):
        issues.append("sequence 非递增")
    if sequences and sequences[0] != 1:
        issues.append(f"sequence 从 {sequences[0]} 开始")
    expected = list(range(sequences[0], sequences[0] + len(sequences))) if sequences else []
    if sequences != expected:
        issues.append("sequence 存在缺口")
    return issues

def validate(path: Path, *, strict: bool = True) -> int:
    validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip(): continue
        value = json.loads(line); errors = list(validator.iter_errors(value))
        if errors: raise ValueError(f"第 {number} 行 schema 无效：{errors[0].message}")
        rows.append(value)
    issues = _sequence_issues(rows)
    if strict and issues:
        raise ValueError("诊断事件 " + "；".join(issues))
    return len(rows)

def archive(workspace: Path, run_id: str) -> int:
    source = workspace / ".__beitu_diagnostics__" / "runs" / run_id / "events.jsonl"
    if not source.is_file(): raise FileNotFoundError(f"诊断记录不存在：{source}")
    count = validate(source, strict=False)
    target = ROOT / "logs" / "背图工具" / "runs" / run_id / "events.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(source, target)
    return count

ANALYSIS_STATES = (
    "prepared", "discovering_runs", "loading_events", "correlating_projects",
    "checking_save_invariants", "checking_final_files", "rendering_report", "completed",
)

def _read_project_files(workspace: Path) -> dict[str, dict]:
    projects: dict[str, dict] = {}
    for project_file in workspace.glob("*/project.json"):
        if project_file.parent.name.startswith("."):
            continue
        try:
            project = json.loads(project_file.read_text(encoding="utf-8"))
            if isinstance(project.get("projectId"), str):
                projects[project["projectId"]] = {
                    "path": str(project_file),
                    "rectangleCount": len(project.get("rectangles", [])),
                    "revision": project.get("revision"),
                    "lastWriterId": project.get("lastWriterId"),
                }
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return projects

def _analyze(workspace: Path, flow: WorkflowCheckpoint) -> dict:
    """Correlate verified saves with later switches/final files.

    The state list is deliberately explicit so a future diagnostic UI can resume
    or report the exact phase that failed without inferring it from log text.
    """
    runs_root = workspace / ".__beitu_diagnostics__" / "runs"
    flow.move("discovering_runs")
    paths = sorted(runs_root.glob("*/events.jsonl")) if runs_root.is_dir() else []
    flow.move("loading_events")
    runs: list[dict] = []
    all_events: list[dict] = []
    log_issues: list[dict] = []
    for path in paths:
        count = validate(path, strict=False)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for issue in _sequence_issues(rows):
            log_issues.append({"path": str(path), "runId": path.parent.name, "code": "diagnostic_log_gap", "message": issue})
        for row in rows:
            row["runId"] = path.parent.name
        runs.append({"runId": path.parent.name, "path": str(path), "eventCount": count})
        all_events.extend(rows)
    flow.move("correlating_projects")
    by_project: dict[str, list[dict]] = {}
    by_run_project: dict[tuple[str, str], list[dict]] = {}
    for event in all_events:
        by_project.setdefault(event["projectId"], []).append(event)
        by_run_project.setdefault((event["runId"], event["projectId"]), []).append(event)
    flow.move("checking_save_invariants")
    findings: list[dict] = []
    for event in all_events:
        if event["event"] == "save_failed":
            findings.append({
                "code": "project_revision_conflict" if event.get("conflictCode") == "PROJECT_REVISION_CONFLICT" else "save_failed",
                "projectId": event["projectId"], "saveRunId": event["runId"],
                "saveSequence": event["sequence"], "operationId": event.get("operationId"),
                "expectedRevision": event.get("expectedRevision", event.get("baseRevision")),
                "actualRevision": event.get("actualRevision"),
                "serviceWorkspace": event.get("serviceWorkspace"),
                "servicePid": event.get("servicePid"),
                "requestSource": event.get("requestSource"), "conflictStage": event.get("conflictStage"),
                "message": "保存被拒绝，未完成提交；缺失字段表示历史日志未记录，不能据此推断并发写入",
            })
    verified = [event for event in all_events if event["event"] == "save_verified" and event["result"] == "success"]
    for saved in verified:
        later_empty = [event for event in by_run_project.get((saved["runId"], saved["projectId"]), [])
                       if event["event"] == "project_switched" and event["rectangleCount"] == 0
                       and event["sequence"] > saved["sequence"]]
        if later_empty:
            findings.append({
                "code": "save_verified_then_reopened_empty",
                "projectId": saved["projectId"], "saveRunId": saved["runId"],
                "saveSequence": saved["sequence"], "switchSequence": later_empty[0]["sequence"],
                "message": "保存回读验证成功，但后续切换读到 0 个矩形",
            })
    flow.move("checking_final_files")
    project_files = _read_project_files(workspace)
    for saved in verified:
        final = project_files.get(saved["projectId"])
        if final and final["rectangleCount"] == 0:
            findings.append({
                "code": "final_file_empty_after_verified_save",
                "projectId": saved["projectId"], "saveRunId": saved["runId"],
                "saveSequence": saved["sequence"], "path": final["path"],
                "message": "当前 project.json 为空矩形数组，覆盖了已验证的保存结果",
            })
        if final and isinstance(final.get("revision"), int) and isinstance(saved.get("revision"), int) and final["revision"] < saved["revision"]:
            findings.append({
                "code": "revision_regressed_after_verified_save",
                "projectId": saved["projectId"], "saveRunId": saved["runId"],
                "saveRevision": saved["revision"], "finalRevision": final["revision"],
                "path": final["path"],
                "message": "当前 project.json 修订号低于已验证保存，存在回退或覆盖",
            })
    flow.move("rendering_report")
    writers = sorted({event["writerId"] for event in all_events if event.get("writerId")})
    flow.move("completed")
    report = {
        "status": "findings" if findings or log_issues else "ok", "state": "completed", "stateHistory": flow.history,
        "workspace": str(workspace), "runCount": len(runs), "eventCount": len(all_events),
        "runs": runs, "projectCount": len(project_files), "projects": project_files,
        "writerIds": writers, "verifiedSaveCount": len(verified), "logIssues": log_issues, "findings": findings,
    }
    return report

def analyze(workspace: Path, run_dir: Path | None = None) -> dict:
    transitions = {state: (ANALYSIS_STATES[index + 1], "invalid")
                   for index, state in enumerate(ANALYSIS_STATES[:-1])}
    flow = WorkflowCheckpoint(transitions, run_dir)
    try:
        report = _analyze(workspace, flow)
    except (OSError, ValueError, KeyError, TypeError) as error:
        flow.move("invalid", errorCode=str(error))
        report = {"status": "invalid", "state": flow.state, "stateHistory": flow.history,
                  "errorCode": str(error), "findings": [], "logIssues": []}
    if run_dir:
        write_json(run_dir / "report.json", report)
    return report

def main() -> int:
    parser = argparse.ArgumentParser(); sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("verify"); check.add_argument("path", type=Path)
    move = sub.add_parser("archive"); move.add_argument("--workspace", type=Path, required=True); move.add_argument("--run-id", required=True)
    inspect = sub.add_parser("analyze"); inspect.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "verify":
        print(json.dumps({"status":"ok", "eventCount":validate(args.path)}, ensure_ascii=False))
    elif args.command == "archive":
        print(json.dumps({"status":"ok", "eventCount":archive(args.workspace, args.run_id)}, ensure_ascii=False))
    else:
        report = analyze(args.workspace, create_run_directory(ROOT / "logs" / "背图工具" / "runs"))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if report["status"] == "invalid" else 4 if report["status"] == "findings" else 0
    return 0

if __name__ == "__main__": raise SystemExit(main())
