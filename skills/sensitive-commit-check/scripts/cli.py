from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="sensitive-commit-check CLI")
    parser.add_argument("command", choices=["create-request", "start", "status", "resume", "verify", "submit-decision"])
    parser.add_argument("--run-id")
    parser.add_argument("--scope", choices=["worktree", "staged"], default="staged")
    parser.add_argument("--input")
    args, extra = parser.parse_known_args(argv)
    mapping = {"create-request": "start", "submit-decision": "review"}
    command = mapping.get(args.command, args.command)
    cmd = [sys.executable, str(Path(__file__).with_name("sensitive_commit_check.py")), command]
    if command == "start":
        cmd += ["--scope", args.scope]
    if args.run_id:
        cmd += ["--run-id", args.run_id]
    if args.input:
        cmd += ["--input", args.input]
    cmd += extra
    code = subprocess.run(cmd, check=False).returncode
    if code == 0:
        return 0
    if args.command == "verify":
        return 4
    if args.command == "submit-decision":
        return 3
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
