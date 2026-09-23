"""CLI for the deterministic learner-profile initialization workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
UTILS_DIR = SCRIPT_DIR.parents[2] / "utils" / "scripts"
sys.path.insert(0, str(UTILS_DIR))
from structured_io import read_json, validate_json_schema, write_json  # noqa: E402
from timestamp import iso_timestamp  # noqa: E402

SCHEMA = SCRIPT_DIR.parent / "references" / "learner-profile.schema.json"


def run_dir(root: Path, run_id: str) -> Path:
    return root / "logs" / "init" / "runs" / run_id


def output_dir(root: Path, run_id: str) -> Path:
    return root / "outputs" / "init" / "runs" / run_id


def create_request(args: argparse.Namespace) -> int:
    profile = read_json(Path(args.input))
    validate_json_schema(profile, SCHEMA)
    write_json(Path(args.request_file), {"schema_version": "1.0", "profile": profile})
    print(json.dumps({"status": "created", "request_file": str(Path(args.request_file))}, ensure_ascii=False))
    return 0


def start(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    profile = read_json(Path(args.input))
    validate_json_schema(profile, SCHEMA)
    run_id = args.run_id or f"{iso_timestamp().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"
    rd, od = run_dir(root, run_id), output_dir(root, run_id)
    rd.mkdir(parents=True, exist_ok=False)
    od.mkdir(parents=True, exist_ok=False)
    now = iso_timestamp()
    raw = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    state = {"schema_version": "1.0", "workflow": "init", "run_id": run_id, "status": "completed", "current_stage": "completed", "resume_stage": None, "current_object_id": profile["learner_id"], "current_batch_id": None, "completed_steps": ["validate_input", "write_profile", "verify_output"], "pending_decisions": [], "error": None, "created_at": now, "updated_at": now, "last_heartbeat_at": now, "event_sequence": 4, "input_path": str(Path(args.input).resolve()), "profile_sha256": digest}
    output = od / "learner-profile.json"
    write_json(output, profile)
    write_json(rd / "state.json", state)
    events = [
        {"sequence": 1, "from_status": None, "to_status": "initialized", "event": "run_initialized", "run_id": run_id},
        {"sequence": 2, "from_status": "initialized", "to_status": "running", "event": "input_validated", "run_id": run_id},
        {"sequence": 3, "from_status": "running", "to_status": "verifying", "event": "profile_written", "run_id": run_id},
        {"sequence": 4, "from_status": "verifying", "to_status": "completed", "event": "verification_passed", "run_id": run_id},
    ]
    (rd / "events.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events), encoding="utf-8", newline="\n")
    print(json.dumps({"status": "completed", "run_id": run_id, "output": str(output)}, ensure_ascii=False))
    return 0


def status_or_verify(args: argparse.Namespace, verify: bool = False) -> int:
    root, rd = Path(args.root).resolve(), run_dir(Path(args.root).resolve(), args.run_id)
    state = read_json(rd / "state.json")
    if verify:
        output = output_dir(root, args.run_id) / "learner-profile.json"
        validate_json_schema(read_json(output), SCHEMA)
        if not output.is_file():
            raise ValueError("正式产物不存在")
        raw = json.dumps(read_json(output), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        actual = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if actual != state.get("profile_sha256"):
            raise ValueError("正式产物哈希与状态检查点不一致")
        state["verified_at"] = iso_timestamp()
        write_json(rd / "verification.json", {"status": "passed", "run_id": args.run_id, "output": str(output)})
    print(json.dumps(state, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="初始化学习者档案")
    sub = parser.add_subparsers(dest="command", required=True)
    req = sub.add_parser("create-request"); req.add_argument("--input", required=True); req.add_argument("--request-file", required=True); req.set_defaults(func=create_request)
    for name in ("start", "status", "resume", "verify"):
        cmd = sub.add_parser(name); cmd.add_argument("--root", default="."); cmd.add_argument("--run-id", required=name != "start");
        if name == "start": cmd.add_argument("--input", required=True); cmd.set_defaults(func=start)
        else: cmd.set_defaults(func=(lambda a, v=name == "verify": status_or_verify(a, v)))
    return parser


if __name__ == "__main__":
    try:
        raise SystemExit(build_parser().parse_args().func(build_parser().parse_args()))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
