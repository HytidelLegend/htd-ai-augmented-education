"""Maintain project spelling pairs through a deterministic recoverable workflow."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.scripts.dictionary_spelling import sync, advance, verify, workflow


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("start", "resume", "status", "verify"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--run-id")
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    if args.command != "start" and not args.run_id:
        parser.error("需要 --run-id")
    if args.run_id and ("/" in args.run_id or "\\" in args.run_id or ".." in args.run_id):
        parser.error("无效 run ID")
    try:
        result = (sync(args.root, args.full) if args.command == "start" else
                  advance(args.root, args.run_id) if args.command == "resume" else
                  verify(args.root, args.run_id) if args.command == "verify" else workflow(args.root, args.run_id).load())
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"status": "paused_retryable_error", "error": str(exc)}, ensure_ascii=False))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
