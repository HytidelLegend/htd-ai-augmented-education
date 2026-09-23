"""Script-first workflow for rendering an English essay as a handwritten card photo."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utils" / "scripts"))
from structured_io import read_json, validate_json_schema, write_json
from timestamp import iso_timestamp
from workflow_state import WorkflowDefinition, WorkflowStateStore
from interaction_delivery import render_preview_delivery, validate_preview_delivery

WORKFLOW = "render-handwritten-essay-card"
SKILL_ROOT = Path(__file__).resolve().parents[1]
REQ_SCHEMA = SKILL_ROOT / "references" / "request.schema.json"
OUT_SCHEMA = SKILL_ROOT / "references" / "output.schema.json"
TRANSITIONS = {
    "prepared": {"validating_request"}, "validating_request": {"normalizing_essay", "paused_input"},
    "normalizing_essay": {"extracting_layout_requirements"}, "extracting_layout_requirements": {"composing_prompt"},
    "composing_prompt": {"validating_prompt"}, "validating_prompt": {"publishing_prompt_preview", "paused_verification"},
    "publishing_prompt_preview": {"preview_ready"}, "preview_ready": {"delivering_prompt_preview"},
    "delivering_prompt_preview": {"paused_imagen_confirmation"}, "paused_imagen_confirmation": {"invoking_imagen", "publishing"},
    "invoking_imagen": {"verifying_image_result", "paused_imagen_unavailable"},
    "verifying_image_result": {"publishing", "paused_verification"}, "publishing": {"completed", "paused_output_conflict"},
}
for status in ("paused_input", "paused_verification", "paused_imagen_unavailable", "paused_output_conflict"):
    TRANSITIONS[status] = {"prepared"}
TRANSITIONS["paused_imagen_unavailable"] = {"invoking_imagen"}
DEFINITION = WorkflowDefinition.build(name=WORKFLOW, transitions=TRANSITIONS)

def run_dir(root: Path, run_id: str) -> Path: return root / "logs" / WORKFLOW / "runs" / run_id
def out_dir(root: Path, run_id: str) -> Path: return root / "outputs" / WORKFLOW / "runs" / run_id
def store(root: Path, run_id: str) -> WorkflowStateStore:
    return WorkflowStateStore(root=root, workflow=WORKFLOW, run_id=run_id, definition=DEFINITION, run_dir=run_dir(root, run_id), schema_path=ROOT / "utils" / "references" / "workflow-state-v1.schema.json")

def advance(root: Path, state: dict, target: str, **updates) -> dict:
    return store(root, state["run_id"]).transition(state, target, stage=target, completed_step=target, updates=updates)

def pause(root: Path, state: dict, status: str, message: str, resume_stage: str) -> dict:
    return store(root, state["run_id"]).pause(state, status=status, error_code=status.removeprefix("paused_"), message=message, resume_stage=resume_stage)

def state_new(run_id: str) -> dict:
    now = iso_timestamp()
    return {"schema_version": "1.0", "workflow": WORKFLOW, "run_id": run_id, "status": "prepared", "current_stage": "prepared", "resume_stage": None, "current_object_id": None, "current_batch_id": None, "completed_steps": [], "pending_decisions": [], "error": None, "created_at": now, "updated_at": now, "last_heartbeat_at": now, "event_sequence": 0}

def render(req: dict, essay: str, run_id: str, root: Path) -> dict:
    model = req.get("model", "image-2"); ratio = req.get("aspect_ratio", "4:3")
    negative = "typed font, cursive calligraphy, decorative lettering, digital glyphs, altered wording, omitted text, duplicated text, illegible writing, watermark, extra answer sheets"
    positive = (SKILL_ROOT / "templates" / "prompt.template.md").read_text(encoding="utf-8").replace("{{ essay_text }}", essay).replace("{{ negative_prompt }}", negative)
    result = {"status": "paused", "run_id": run_id, "essay": {"word_count": len(re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)?", essay)), "paragraph_count": len([x for x in re.split(r"\n\s*\n", essay) if x.strip()]), "sha256": hashlib.sha256(essay.encode()).hexdigest()}, "prompt": {"model": model, "positive_prompt": positive, "negative_prompt": negative, "aspect_ratio": ratio}, "imagen": {"decision": "pending", "invoked": False, "command": "/imagen"}, "publication": {}}
    result["delivery"] = render_preview_delivery(
        prompt=positive,
        question="是否现在调用 `/imagen` 生成手写英语答题卡照片？",
        run_id=run_id,
        source=result["prompt"],
    )
    return result

def write_outputs(root: Path, result: dict) -> None:
    directory = out_dir(root, result["run_id"]); directory.mkdir(parents=True, exist_ok=True)
    p = result["prompt"]; i = result["imagen"]
    md = (SKILL_ROOT / "templates" / "result.template.md").read_text(encoding="utf-8")
    d = result["delivery"]
    md = md.replace("{{ prompt_code_block }}", d["prompt_code_block"]).replace("{{ confirmation_question }}", d["confirmation_question"])
    md = md.replace("{{ positive_prompt }}", p["positive_prompt"]).replace("{{ negative_prompt }}", p["negative_prompt"]).replace("{{ model }}", p["model"]).replace("{{ aspect_ratio }}", p["aspect_ratio"]).replace("{{ word_count }}", str(result["essay"]["word_count"])).replace("{{ image_result }}", str(i.get("image_path") or i.get("image_url") or "pending"))
    write_json(directory / "result.json", result); (directory / "result.md").write_text(md.rstrip() + "\n", encoding="utf-8")
    result["publication"] = {"result_json": str(directory / "result.json"), "result_md": str(directory / "result.md")}
    write_json(directory / "result.json", result)

def cmd_start(args) -> int:
    root = Path(args.root).resolve(); req = read_json(Path(args.input).resolve()); validate_json_schema(req, REQ_SCHEMA)
    essay = req.get("content") or Path(req["input_file"]).read_text(encoding="utf-8-sig")
    if not essay.strip(): raise ValueError("作文不能为空")
    run_id = uuid.uuid4().hex[:12]; state = state_new(run_id); st = store(root, run_id); st.create(state)
    for target in ("validating_request", "normalizing_essay", "extracting_layout_requirements", "composing_prompt", "validating_prompt", "publishing_prompt_preview", "preview_ready", "delivering_prompt_preview"):
        state = advance(root, state, target)
    result = render(req, essay.strip(), run_id, root); write_outputs(root, result)
    state["current_object_id"] = str(out_dir(root, run_id) / "result.json"); state = pause(root, state, "paused_imagen_confirmation", "等待用户确认是否调用 /imagen", "invoking_imagen")
    write_json(out_dir(root, run_id) / "result.json", result); print(json.dumps({"status": state["status"], "run_id": run_id, "result": str(out_dir(root, run_id) / "result.md")}, ensure_ascii=False)); return 3

def cmd_deliver(args) -> int:
    if args.mode != "preview":
        raise ValueError("deliver 目前只支持 --mode preview")
    root = Path(args.root).resolve(); state = store(root, args.run_id).load()
    result = read_json(out_dir(root, args.run_id) / "result.json")
    if state["status"] != "paused_imagen_confirmation":
        raise ValueError("当前运行尚未准备好提示词预览")
    validate_preview_delivery(result.get("delivery", {}), result["prompt"])
    delivery_path = out_dir(root, args.run_id) / "delivery.json"
    write_json(delivery_path, result["delivery"])
    print(result["delivery"]["message_markdown"])
    return 3

def cmd_resume(args) -> int:
    root = Path(args.root).resolve(); state = store(root, args.run_id).load(); result = read_json(out_dir(root, args.run_id) / "result.json")
    if state["status"] not in {"paused_imagen_confirmation", "paused_imagen_unavailable"}: raise ValueError("当前运行不在确认阶段")
    if args.decision == "decline":
        result["status"] = "completed"; result["imagen"]["decision"] = "declined"; state = advance(root, state, "publishing"); state = advance(root, state, "completed")
    else:
        if not args.image_path and not args.image_url:
            state = pause(root, state, "paused_imagen_unavailable", "请先执行 /imagen，再传入 --image-path 或 --image-url", "invoking_imagen")
            print(json.dumps({"status": state["status"], "run_id": args.run_id, "command": "/imagen"}, ensure_ascii=False)); return 3
        copied_path = None
        if args.image_path:
            source = Path(args.image_path).resolve()
            if not source.is_file(): raise FileNotFoundError(f"图片文件不存在：{source}")
            target_dir = out_dir(root, args.run_id); target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / source.name
            if target.exists(): raise FileExistsError(f"输出图片已存在，不覆盖：{target}")
            shutil.copy2(source, target); copied_path = str(target)
        result["status"] = "completed"; result["imagen"].update({"decision": "accepted", "invoked": True, "image_path": copied_path, "image_url": args.image_url}); state = advance(root, state, "invoking_imagen"); state = advance(root, state, "verifying_image_result"); state = advance(root, state, "publishing"); state = advance(root, state, "completed")
    write_outputs(root, result); write_json(out_dir(root, args.run_id) / "result.json", result); print(json.dumps({"status": state["status"], "run_id": args.run_id, "result": str(out_dir(root, args.run_id) / "result.md")}, ensure_ascii=False)); return 0

def main() -> int:
    parser = argparse.ArgumentParser(); sub = parser.add_subparsers(dest="command", required=True)
    def common(p): p.add_argument("--root", default=".")
    p = sub.add_parser("start"); common(p); p.add_argument("--input", required=True)
    p = sub.add_parser("resume"); common(p); p.add_argument("--run-id", required=True); p.add_argument("--decision", choices=["accept", "decline"], required=True); p.add_argument("--image-path"); p.add_argument("--image-url")
    p = sub.add_parser("deliver"); common(p); p.add_argument("--run-id", required=True); p.add_argument("--mode", choices=["preview"], required=True)
    for name in ("status", "verify"):
        p = sub.add_parser(name); common(p); p.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "start": return cmd_start(args)
        if args.command == "deliver": return cmd_deliver(args)
        root = Path(args.root).resolve(); state = store(root, args.run_id).load()
        if args.command == "status": print(json.dumps(state, ensure_ascii=False)); return 0
        if args.command == "verify":
            result = read_json(out_dir(root, args.run_id) / "result.json"); validate_json_schema(result, OUT_SCHEMA); validate_preview_delivery(result["delivery"], result["prompt"]); print(json.dumps({"status": "verified", "run_id": args.run_id}, ensure_ascii=False)); return 0
        return cmd_resume(args)
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)); return 2

if __name__ == "__main__": raise SystemExit(main())
