from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


EXIT_CODES = {"ok": 0, "usage": 2, "paused": 3, "verify": 4, "runtime": 5, "dependency": 6}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="git-remote-diff CLI")
    parser.add_argument("command", choices=["create-request", "start", "status", "resume", "verify"])
    parser.add_argument("--root", default=".")
    parser.add_argument("--run-id")
    args, extra = parser.parse_known_args(argv)
    command = {"create-request": "run", "start": "run", "verify": "status"}[args.command] if args.command in {"create-request", "start", "verify"} else args.command
    cmd = [sys.executable, str(Path(__file__).with_name("git_remote_diff.py")), command, "--root", args.root]
    if args.run_id:
        cmd += ["--run-id", args.run_id]
    cmd += extra
    code = subprocess.run(cmd, check=False).returncode
    if code == 0:
        return 0
    return 4 if args.command == "verify" else 5


if __name__ == "__main__":
    raise SystemExit(main())
