from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="format-conversion-master CLI")
    parser.add_argument("command", choices=["create-request", "start", "status", "resume", "verify"])
    parser.add_argument("--root", default=".")
    parser.add_argument("--run-id")
    parser.add_argument("--input")
    parser.add_argument("--to", default="pdf")
    parser.add_argument("--output")
    args, extra = parser.parse_known_args(argv)
    command = "init-request" if args.command == "create-request" else ("run" if args.command == "start" else args.command)
    cmd = [sys.executable, str(Path(__file__).with_name("format_conversion.py")), command, "--root", args.root]
    for flag, value in (("--run-id", args.run_id), ("--input", args.input), ("--to", args.to), ("--output", args.output)):
        if value:
            cmd += [flag, value]
    cmd += extra
    code = subprocess.run(cmd, check=False).returncode
    if code == 0:
        return 0
    return 4 if args.command == "verify" else 5


if __name__ == "__main__":
    raise SystemExit(main())
