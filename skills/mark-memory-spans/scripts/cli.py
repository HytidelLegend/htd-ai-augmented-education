"""Script-first workflow for extracting and editing memory spans."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "utils" / "scripts"))
from span_ops import apply_operations, candidate_spans, classical_clauses, classical_spans, marked_text, masked_text, normalize_text, text_sha256, validate_classical_spans, validate_cloze_alignment, validate_cloze_quality, validate_masked_text, validate_spans
from term_glossary import load_terms, run_terms, snapshot_terms
from structured_io import read_json, validate_json_schema, write_json
from timestamp import filename_timestamp, iso_timestamp
from workflow_state import WorkflowDefinition, WorkflowStateStore

WORKFLOW = "mark-memory-spans"
SKILL_ROOT = Path(__file__).resolve().parents[1]
REQ_SCHEMA = SKILL_ROOT / "references/request.schema.json"
AGENT_SCHEMA = SKILL_ROOT / "references/agent-response.schema.json"
EDIT_SCHEMA = SKILL_ROOT / "references/edit-request.schema.json"
OUT_SCHEMA = SKILL_ROOT / "references/output.schema.json"
TRANSITIONS = {
    "prepared": {"validating_request"},
    "validating_request": {"normalizing_text"},
    "normalizing_text": {"extracting_candidates"},
    "extracting_candidates": {"building_generation_packet"},
    "building_generation_packet": {"paused_agent_selection", "validating_agent_response"},
    "paused_agent_selection": {"validating_agent_response"},
    "validating_agent_response": {"validating_mode", "paused_agent_response", "paused_quality_review"},
    "validating_mode": {"validating_clause_selection", "validating_cloze_quality", "paused_quality_review"},
    "validating_clause_selection": {"validating_cloze_quality", "paused_quality_review"},
    "validating_cloze_quality": {"validating_cloze_alignment", "paused_quality_review"},
    "validating_cloze_alignment": {"rendering_outputs", "paused_quality_review"},
    "rendering_outputs": {"verifying_outputs"},
    "verifying_outputs": {"completed"},
    "paused_agent_response": {"prepared"},
    "paused_quality_review": {"validating_agent_response"},
}
DEFINITION = WorkflowDefinition.build(name=WORKFLOW, transitions=TRANSITIONS)


def log_dir(root: Path, run_id: str) -> Path:
    return root / "logs" / WORKFLOW / "runs" / run_id


def out_dir(root: Path, run_id: str) -> Path:
    return root / "outputs" / WORKFLOW / "runs" / run_id


def glossary_terms(root: Path, run_id: str) -> list[str]:
    directory = log_dir(root, run_id)
    packet_path = directory / "generation_packet.json"
    selection_path = directory / "selection.json"
    required = (
        packet_path.is_file() and "glossary_rule" in read_json(packet_path).get("instructions", {})
    ) or (
        selection_path.is_file() and read_json(selection_path).get("glossary_required") is True
    )
    return run_terms(directory, required=required)


def saved_mode(root: Path, run_id: str) -> str:
    path = log_dir(root, run_id) / "selection.json"
    mode = read_json(path)["mode"] if path.is_file() else "key_points"
    if mode not in {"key_points", "classical_recitation"}:
        raise ValueError("运行记录中的挖空模式无效")
    return mode


def store(root: Path, run_id: str) -> WorkflowStateStore:
    return WorkflowStateStore(root=root, workflow=WORKFLOW, run_id=run_id, definition=DEFINITION, run_dir=log_dir(root, run_id), events_dir=log_dir(root, run_id))


def new_state(run_id: str) -> dict:
    now = iso_timestamp()
    return {"schema_version": "1.0", "workflow": WORKFLOW, "run_id": run_id, "status": "prepared", "current_stage": "prepared", "resume_stage": None, "current_object_id": None, "current_batch_id": None, "completed_steps": [], "pending_decisions": [], "error": None, "created_at": now, "updated_at": now, "last_heartbeat_at": now, "event_sequence": 0}


def advance(root: Path, state: dict, target: str, **updates: object) -> dict:
    return store(root, state["run_id"]).transition(state, target, stage=target, completed_step=target, updates=updates)


def pause(root: Path, state: dict, status: str, message: str, resume_stage: str) -> dict:
    return store(root, state["run_id"]).pause(state, status=status, error_code=status.removeprefix("paused_"), message=message, resume_stage=resume_stage)


def run_id(root: Path) -> str:
    base = filename_timestamp()
    runs = root / "logs" / WORKFLOW / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    index = 0
    while True:
        candidate = base if index == 0 else f"{base}_{index}"
        try:
            (runs / candidate).mkdir()
        except FileExistsError:
            index += 1
        else:
            return candidate


def write_packet(root: Path, run: str, request: dict, text: str, terms: list[str]) -> None:
    packet = {
        "run_id": run,
        "source": {"text": text, "sha256": text_sha256(text)},
        "candidate_spans": candidate_spans(text, terms=terms),
        "clause_candidates": classical_clauses(text),
        "response_templates": {
            "classical_recitation": {"mode": "classical_recitation", "selected_clause_ids": []},
            "key_points": {"mode": "key_points", "spans": [], "cloze_check": {"pass": True, "reason": "", "main_clause_preserved": True, "overmarking": False}},
        },
        "instructions": {
            "mode_selection": "Agent 根据输入语义判断：要背诵的古诗文采用 classical_recitation，其余采用 key_points。",
            "classical_response": "只提交 mode=classical_recitation 和 selected_clause_ids；脚本扩展完整分句并保留标点，顿号不切分。",
            "key_points_response": "提交 mode=key_points、spans 和 cloze_check；保留原有主干优先和避免过度标记规则。",
            "non_overlapping": True,
            "adjacent_allowed": True,
            "mark_format": "「span」",
            "cloze_replacement": "____",
            "glossary_rule": "术语表中的术语在原文区间内不可从中间切开；是否挖空仍由语义决定。",
        },
    }
    write_json(log_dir(root, run) / "generation_packet.json", packet)


def build_result(root: Path, run: str, text: str, spans: list[dict], cloze: dict, mode: str = "key_points") -> dict:
    if mode not in {"key_points", "classical_recitation"}:
        raise ValueError("未知的挖空模式")
    digest = text_sha256(text)
    spans = validate_spans(text, spans, source_sha256=digest, terms=glossary_terms(root, run))
    remaining = masked_text(text, spans)
    validate_masked_text(text, spans, remaining)
    quality = validate_classical_spans(text, spans) if mode == "classical_recitation" else validate_cloze_quality(text, spans)
    if mode == "key_points" and (not cloze.get("pass") or not cloze.get("main_clause_preserved") or cloze.get("overmarking")):
        raise ValueError("Agent 判断存在主干缺失或过度标记")
    if not quality["pass"]:
        raise ValueError("脚本检查发现挖空质量问题：" + "；".join(quality["failures"]))
    marked = marked_text(text, spans)
    alignment = validate_cloze_alignment(text, spans, marked, remaining)
    return {"status": "completed", "run_id": run, "source": {"text": text, "sha256": digest}, "spans": spans, "cloze_check": {"pass": True, "remaining_text": remaining, "reason": cloze.get("reason", ""), "main_clause_preserved": mode == "key_points", "overmarking": False}, "quality_check": quality, "markdown": marked, "masked_text": remaining, "cloze_alignment": alignment, "publication": {}}


def render_result_markdown(result: dict, mode: str = "key_points") -> str:
    template = (SKILL_ROOT / "templates" / "result.template.md").read_text(encoding="utf-8")
    main_clause_status = "不适用（古诗文整分句背诵）" if mode == "classical_recitation" else "通过"
    return template.replace("{{ marked_text }}", result["markdown"]).replace("{{ masked_text }}", result["masked_text"]).replace("{{ main_clause_status }}", main_clause_status).rstrip() + "\n"


def write_output(root: Path, result: dict, mode: str = "key_points") -> None:
    directory = out_dir(root, result["run_id"])
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "result.json", result)
    (directory / "result.md").write_text(render_result_markdown(result, mode), encoding="utf-8", newline="\n")
    result["publication"] = {"result_json": str(directory / "result.json"), "result_md": str(directory / "result.md")}
    write_json(directory / "result.json", result)


def validate_result_artifact(result: dict, mode: str = "key_points", terms: list[str] | None = None) -> dict:
    """Recompute all derived output fields before verify or deliver."""
    text = normalize_text(result["source"]["text"])
    digest = text_sha256(text)
    if result["source"]["sha256"] != digest:
        raise ValueError("产物 source_sha256 与源文本不一致")
    spans = validate_spans(text, result["spans"], source_sha256=digest, terms=terms)
    masked = masked_text(text, spans)
    marked = marked_text(text, spans)
    if result["masked_text"] != masked or result["markdown"] != marked:
        raise ValueError("产物中的标记文本或挖空文本与 span 集合不一致")
    alignment = validate_cloze_alignment(text, spans, marked, masked)
    if result["cloze_alignment"] != alignment:
        raise ValueError("产物中的 cloze_alignment 与重新计算结果不一致")
    cloze = result["cloze_check"]
    if not cloze["pass"] or cloze["remaining_text"] != masked or cloze["main_clause_preserved"] != (mode == "key_points") or cloze["overmarking"]:
        raise ValueError("产物中的 cloze_check 与模式或挖空文本不一致")
    quality = validate_classical_spans(text, spans) if mode == "classical_recitation" else validate_cloze_quality(text, spans)
    if not quality["pass"]:
        raise ValueError("产物中的 span 未通过挖空质量检查")
    if result["quality_check"] != quality:
        raise ValueError("产物中的 quality_check 与重新计算结果不一致")
    return result


def validate_published_markdown(root: Path, result: dict, mode: str = "key_points") -> Path:
    result_path = out_dir(root, result["run_id"]) / "result.md"
    if not result_path.is_file():
        raise FileNotFoundError(f"找不到 Markdown 产物：{result_path}")
    if result_path.read_text(encoding="utf-8") != render_result_markdown(result, mode):
        raise ValueError("Markdown 产物与已验证的结果或模板不一致")
    return result_path


def cmd_start(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    request = read_json(Path(args.input).resolve())
    validate_json_schema(request, REQ_SCHEMA)
    text = normalize_text(request["text"])
    run = run_id(root)
    state = new_state(run)
    store(root, run).create(state)
    for target in ("validating_request", "normalizing_text", "extracting_candidates", "building_generation_packet"):
        state = advance(root, state, target)
    write_json(log_dir(root, run) / "request.json", request)
    terms = snapshot_terms(log_dir(root, run), root / "utils/references/术语表.txt")
    write_packet(root, run, request, text, terms)
    state = pause(root, state, "paused_agent_selection", "需要 Agent 提交选定的记忆 span", "validating_agent_response")
    print(json.dumps({"status": state["status"], "run_id": run, "generation_packet": str(log_dir(root, run) / "generation_packet.json")}, ensure_ascii=False))
    return 3


def cmd_resume(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    state = store(root, args.run_id).load()
    if state["status"] not in {"paused_agent_selection", "paused_quality_review"}:
        raise ValueError("当前运行不在 Agent span 提交阶段")
    packet = read_json(log_dir(root, args.run_id) / "generation_packet.json")
    response = read_json(Path(args.input).resolve())
    if "clause_candidates" not in packet and "mode" not in response:
        response = {"mode": "key_points", **response}
    validate_json_schema(response, AGENT_SCHEMA)
    mode = response["mode"]
    if state["status"] == "paused_quality_review":
        state = store(root, args.run_id).resume(state)
    if state["status"] == "paused_agent_selection":
        state = advance(root, state, "validating_agent_response")
    state = advance(root, state, "validating_mode")
    if mode == "classical_recitation":
        state = advance(root, state, "validating_clause_selection")
    try:
        text = packet["source"]["text"]
        spans = classical_spans(text, response["selected_clause_ids"]) if mode == "classical_recitation" else response["spans"]
        cloze = {"reason": "古诗文按完整分句挖空"} if mode == "classical_recitation" else response["cloze_check"]
        result = build_result(root, args.run_id, text, spans, cloze, mode)
    except ValueError as exc:
        state = pause(root, state, "paused_quality_review", str(exc), "validating_agent_response")
        print(json.dumps({"status": state["status"], "run_id": args.run_id, "error": str(exc)}, ensure_ascii=False))
        return 3
    state = advance(root, state, "validating_cloze_quality")
    state = advance(root, state, "validating_cloze_alignment")
    write_json(log_dir(root, args.run_id) / "selection.json", {"mode": mode, "glossary_required": "glossary_rule" in packet.get("instructions", {})})
    write_json(log_dir(root, args.run_id) / "agent-response.json", response)
    write_output(root, result, mode)
    state = advance(root, state, "rendering_outputs")
    state = advance(root, state, "verifying_outputs")
    state = advance(root, state, "completed")
    print(json.dumps({"status": state["status"], "run_id": args.run_id, "result": str(out_dir(root, args.run_id) / "result.md")}, ensure_ascii=False))
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    request = read_json(Path(args.input).resolve())
    validate_json_schema(request, EDIT_SCHEMA)
    text = normalize_text(request["text"])
    digest = text_sha256(text)
    if request["source_sha256"] != digest:
        raise ValueError("编辑请求的 source_sha256 与规范化文本不一致")
    terms = load_terms(root / "utils/references/术语表.txt")
    spans = validate_spans(text, request["spans"], source_sha256=digest, terms=terms)
    spans = apply_operations(text, spans, request["operations"])
    spans = validate_spans(text, spans, source_sha256=digest, terms=terms)
    mode = request.get("mode", "key_points")
    run = run_id(root)
    snapshot_terms(log_dir(root, run), root / "utils/references/术语表.txt")
    result = build_result(root, run, text, spans, {"pass": True, "main_clause_preserved": True, "overmarking": False, "reason": "编辑后的 span 已通过模式校验"}, mode)
    state = new_state(run)
    store(root, run).create(state)
    for target in ("validating_request", "normalizing_text"):
        state = advance(root, state, target)
    for target in ("extracting_candidates", "building_generation_packet", "validating_agent_response", "validating_mode"):
        state = advance(root, state, target)
    if mode == "classical_recitation":
        state = advance(root, state, "validating_clause_selection")
    for target in ("validating_cloze_quality", "validating_cloze_alignment"):
        state = advance(root, state, target)
    write_json(log_dir(root, run) / "selection.json", {"mode": mode, "glossary_required": True})
    write_output(root, result, mode)
    for target in ("rendering_outputs", "verifying_outputs", "completed"):
        state = advance(root, state, target)
    print(json.dumps({"status": state["status"], "run_id": run, "result": str(out_dir(root, run) / "result.md")}, ensure_ascii=False))
    return 0


def cmd_deliver(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    state = store(root, args.run_id).load()
    if state["status"] != "completed":
        raise ValueError("当前运行尚未完成，不能交付")
    result = read_json(out_dir(root, args.run_id) / "result.json")
    validate_json_schema(result, OUT_SCHEMA)
    mode = saved_mode(root, args.run_id)
    validate_result_artifact(result, mode, glossary_terms(root, args.run_id))
    result_path = validate_published_markdown(root, result, mode)
    print(result_path.read_text(encoding="utf-8"), end="")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    def common(command):
        command.add_argument("--root", default=".")
    start = sub.add_parser("start"); common(start); start.add_argument("--input", required=True)
    resume = sub.add_parser("resume"); common(resume); resume.add_argument("--run-id", required=True); resume.add_argument("--input", required=True)
    edit = sub.add_parser("edit"); common(edit); edit.add_argument("--input", required=True)
    deliver = sub.add_parser("deliver"); common(deliver); deliver.add_argument("--run-id", required=True)
    for name in ("status", "verify"):
        command = sub.add_parser(name); common(command); command.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "start": return cmd_start(args)
        if args.command == "resume": return cmd_resume(args)
        if args.command == "edit": return cmd_edit(args)
        if args.command == "deliver": return cmd_deliver(args)
        root = Path(args.root).resolve()
        state = store(root, args.run_id).load()
        if args.command == "status":
            print(json.dumps(state, ensure_ascii=False)); return 0
        result = read_json(out_dir(root, args.run_id) / "result.json")
        validate_json_schema(result, OUT_SCHEMA)
        mode = saved_mode(root, args.run_id)
        validate_result_artifact(result, mode, glossary_terms(root, args.run_id))
        validate_published_markdown(root, result, mode)
        print(json.dumps({"status": "verified", "run_id": args.run_id}, ensure_ascii=False)); return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)); return 2


if __name__ == "__main__":
    raise SystemExit(main())
