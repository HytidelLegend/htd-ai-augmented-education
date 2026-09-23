from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utils" / "scripts"))
from git_repository import GitCommandError, is_repository, redact_remote_url, run_git, status_entries  # noqa: E402
from run_state import read_json, timestamp, write_json  # noqa: E402

WORKFLOW = "git-remote-diff"


def run_dir(root: Path, run_id: str) -> Path:
    return root / "logs" / WORKFLOW / "runs" / run_id


def state_base(run_id: str) -> dict[str, Any]:
    return {"schema_version": "1.0", "workflow": WORKFLOW, "run_id": run_id,
            "status": "running", "stage": "INIT", "completed_steps": [],
            "pending_confirmation": None, "errors": [], "heartbeat_at": timestamp()}


def save(run_root: Path, state: dict[str, Any]) -> None:
    state["heartbeat_at"] = timestamp()
    write_json(run_root / "state.json", state)
    with (run_root / "events.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"timestamp": state["heartbeat_at"], "status": state.get("status"), "stage": state.get("stage")}, ensure_ascii=False) + "\n")


def parse_name_status(text: str) -> list[dict[str, str | None]]:
    result = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        status = fields[0]
        result.append({"status": status, "path": fields[-1], "old_path": fields[-2] if status.startswith("R") and len(fields) > 2 else None})
    return result


def render_markdown(data: dict[str, Any]) -> str:
    c = data["comparison"]
    lines = ["# Git 远端差异报告", "", "## 结论", "", f"当前仓库与云端最新版{'一致' if c['status'] == 'consistent' else '不一致'}。", "",
             f"- 本地分支：`{data['local']['branch']}`", f"- 云端分支：`{data['remote']['remote_ref']}`",
             f"- 本地领先：{c['ahead']} 个提交", f"- 本地落后：{c['behind']} 个提交",
             f"- 工作区：{'干净' if data['local']['working_tree_clean'] else '存在未提交变化'}", ""]
    lines += ["## 提交状态", "", "| 项目 | 数量 |", "|---|---:|", f"| 本地领先提交 | {c['ahead']} |", f"| 本地落后提交 | {c['behind']} |", f"| 是否分叉 | {'是' if c['diverged'] else '否'} |", ""]
    for title, key in [("已提交历史中的文件差异", "committed"), ("已暂存变化", "staged"), ("未暂存变化", "unstaged"), ("未跟踪文件", "untracked")]:
        lines += [f"## {title}", ""]
        items = data["differences"][key]
        if key == "untracked":
            lines += ["| 文件 |", "|---|"]
            lines += [f"| `{x['path']}` |" for x in items] or ["| 无 |"]
        else:
            lines += ["| 状态 | 文件 |", "|---|---|"]
            lines += [f"| {x['status'].strip()} | `{x['path']}` |" for x in items] or ["| — | 无 |"]
        lines.append("")
    lines += ["## 检查信息", "", f"- Fetch 状态：{data['fetch']['status']}", f"- 检查时间：`{data['checked_at']}`"]
    if data.get("errors"):
        lines += ["", "## 错误", ""] + [f"- {e}" for e in data["errors"]]
    return "\n".join(lines) + "\n"


def execute(root: Path, run_id: str | None = None) -> dict[str, Any]:
    root = root.resolve()
    run_id = run_id or (timestamp().replace("-", "").replace(":", "").replace("+", "") + "-" + uuid.uuid4().hex[:8])
    directory = run_dir(root, run_id); directory.mkdir(parents=True, exist_ok=True)
    previous = directory / "state.json"
    if previous.exists():
        state = read_json(previous)
        if state.get("status") == "complete" and (directory / "diff.json").exists():
            return read_json(directory / "diff.json")
        state["resumed_from"] = state.get("stage")
    else:
        state = state_base(run_id)
    state["repo_root"] = str(root); state["status"] = "running"; save(directory, state)
    if not is_repository(root):
        state.update(status="paused_not_repository", stage="PAUSED_NOT_REPOSITORY", pending_confirmation="当前项目不是 Git 仓库")
        save(directory, state); return state
    state["stage"] = "REMOTE_DISCOVERY"; save(directory, state)
    try:
        remotes = run_git(root, ["remote"]).splitlines()
        remote = "origin" if "origin" in remotes else (remotes[0] if remotes else None)
        if not remote: raise RuntimeError("没有配置 Git 远端")
        remote_url = run_git(root, ["remote", "get-url", remote])
        branch = run_git(root, ["symbolic-ref", f"refs/remotes/{remote}/HEAD"], check=False)
        default_branch = branch.rsplit("/", 1)[-1] if branch else run_git(root, ["remote", "show", remote], check=False).split("HEAD branch:")[-1].splitlines()[0].strip()
        if not default_branch: raise RuntimeError("无法确定远端默认分支")
        state["stage"] = "FETCH_REMOTE"; save(directory, state)
        run_git(root, ["fetch", "--prune", remote])
        remote_ref = f"{remote}/{default_branch}"
        local_branch = run_git(root, ["branch", "--show-current"]) or "(detached)"
        head = run_git(root, ["rev-parse", "HEAD"])
        remote_head = run_git(root, ["rev-parse", remote_ref])
        counts = run_git(root, ["rev-list", "--left-right", "--count", f"HEAD...{remote_ref}"]).split()
        ahead, behind = int(counts[0]), int(counts[1])
        committed = parse_name_status(run_git(root, ["diff", "--name-status", f"HEAD...{remote_ref}"]))
        status = status_entries(root)
        data = {"schema_version": "1.0", "run_id": run_id, "repo_root": str(root), "remote": {"name": remote, "url": redact_remote_url(remote_url), "default_branch": default_branch, "remote_ref": remote_ref}, "local": {"branch": local_branch, "head": head, "remote_head": remote_head, "working_tree_clean": not status}, "comparison": {"status": "consistent" if not committed and ahead == 0 and behind == 0 and not status else "different", "ahead": ahead, "behind": behind, "diverged": ahead > 0 and behind > 0, "committed_consistent": not committed and ahead == 0 and behind == 0}, "differences": {"committed": committed, "staged": [x for x in status if x["status"][0:1] not in (" ", "?")], "unstaged": [x for x in status if len(x["status"]) > 1 and x["status"][1] not in (" ", "?")], "untracked": [x for x in status if x["status"] == "??"]}, "fetch": {"status": "success", "fetched_at": timestamp()}, "errors": [], "checked_at": timestamp()}
        write_json(directory / "diff.json", data); (directory / "diff.md").write_text(render_markdown(data), encoding="utf-8", newline="\n")
        state.update(status="complete", stage="COMPLETE", completed_steps=["repo_check", "remote_discovery", "fetch", "compare", "render"]); save(directory, state); return data
    except (GitCommandError, RuntimeError, ValueError) as exc:
        state.update(status="paused_fetch_failed" if state["stage"] == "FETCH_REMOTE" else "paused_error", stage="PAUSED", errors=[str(exc)], pending_confirmation=str(exc)); save(directory, state); return state


def main() -> None:
    parser = argparse.ArgumentParser(description="比较 Git 本地仓库与远端默认分支并生成 JSON/Markdown 报告")
    parser.add_argument("command", choices=["run", "resume", "status"]); parser.add_argument("--root", default="."); parser.add_argument("--run-id")
    args = parser.parse_args(); root = Path(args.root).resolve()
    if args.command in {"resume", "status"} and not args.run_id:
        parser.error(f"{args.command} requires --run-id")
    if args.command == "run": result = execute(root)
    elif args.command == "resume": result = execute(root, args.run_id)
    else: result = read_json(run_dir(root, args.run_id) / "state.json")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
