"""Script-first, recoverable English-writing workflow."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utils" / "scripts"))
from source_normalization import SourceBlock, clean_markdown, select_writing_blocks_with_audit
from language_extraction import extract_language_bank
from structured_io import read_json, validate_json_schema, write_json, write_json_transaction, write_text_transaction
from timestamp import iso_timestamp
from essay_generator import build_generation_packet, validate_agent_draft
from essay_metrics import metrics, sentences
from essay_scoring import build_evidence, build_organization_evidence, integer_score, parse_target, score_from_agent_review, target_band
from workflow_state import WorkflowDefinition, WorkflowStateStore
from run_artifact_io import archive_json_input

WORKFLOW = "en-writing-master"
SKILL_ROOT = Path(__file__).resolve().parents[1]
REQ_SCHEMA = SKILL_ROOT / "references" / "request.schema.json"
OUT_SCHEMA = SKILL_ROOT / "references" / "output.schema.json"
TOOL_SCHEMA = SKILL_ROOT / "references" / "tool.schema.json"
GENERATION_PACKET_SCHEMA = SKILL_ROOT / "references" / "generation-packet.schema.json"
TRANSITIONS = {
    "prepared": {"validating_request"},
    "validating_request": {"resolving_tool", "resolving_source", "paused_input"},
    "resolving_tool": {"loading_tool", "paused_missing_tool"},
    "loading_tool": {"planning", "planning_target", "normalizing_essay", "loading_feedback", "paused_missing_full_score"},
    "planning": {"planning_target", "normalizing_essay", "loading_feedback", "generating"},
    "planning_target": {"generating", "paused_target_mismatch"},
    "normalizing_essay": {"scoring", "paused_verification"},
    "scoring": {"validating_agent_review", "reviewing_sentences", "paused_agent_review", "verifying", "paused_verification"},
    "validating_agent_review": {"reviewing_sentences", "paused_agent_review", "paused_verification"},
    "reviewing_sentences": {"finalizing_scores"},
    "loading_feedback": {"applying_edits", "paused_input"},
    "applying_edits": {"rescoring", "paused_verification"},
    "rescoring": {"finalizing_scores"},
    "generating": {"scoring_preview", "verifying", "paused_agent_generation", "paused_verification"},
    "scoring_preview": {"finalizing_scores", "paused_agent_review", "paused_target_mismatch"},
    "finalizing_scores": {"verifying", "validating_polish_changes", "paused_agent_review", "paused_invalid_score", "paused_target_mismatch", "paused_verification"},
    "validating_polish_changes": {"verifying", "paused_verification"},
    "verifying": {"rendering_markdown", "paused_verification"},
    "rendering_markdown": {"verifying_delivery", "paused_verification"},
    "verifying_delivery": {"publishing", "paused_verification"},
    "publishing": {"completed", "paused_output_conflict"},
    "resolving_source": {"reading_text", "converting_epub", "paused_source_missing"},
    "reading_text": {"normalizing_source"}, "converting_epub": {"reading_converted_markdown", "paused_conversion"},
    "reading_converted_markdown": {"normalizing_source"}, "normalizing_source": {"selecting_writing_scope"},
    "selecting_writing_scope": {"extracting_rules", "paused_source_scope"},
    "extracting_rules": {"structuring_tool", "paused_extraction"},
    "structuring_tool": {"verifying_tool", "paused_schema"}, "verifying_tool": {"publishing", "paused_verification"},
}
for _status in ("paused_input", "paused_agent_review", "paused_agent_generation", "paused_missing_tool", "paused_missing_full_score", "paused_verification", "paused_invalid_score", "paused_output_conflict", "paused_source_missing", "paused_parse", "paused_conversion", "paused_source_scope", "paused_extraction", "paused_schema", "paused_target_mismatch"):
    TRANSITIONS[_status] = {"archiving_agent_input", "prepared"}
TRANSITIONS["archiving_agent_input"] = {"prepared"}

STATE_DEFINITION = WorkflowDefinition.build(name=WORKFLOW, transitions=TRANSITIONS)

DEFAULT_SCORING_LEVELS = [
    {"min": 13, "max": 15, "label": "优秀", "description": "任务完成充分，结构清晰，语言准确且有较丰富的表达。"},
    {"min": 10, "max": 12, "label": "良好", "description": "任务基本完成，结构和语言较稳定，仍有可提升之处。"},
    {"min": 7, "max": 9, "label": "达标", "description": "能够表达主要观点，但论证、衔接或语言准确性需要加强。"},
    {"min": 0, "max": 6, "label": "需改进", "description": "任务完成度或可读性不足，需要优先修复基本问题。"},
]


def _run_dir(root: Path, run_id: str) -> Path: return root / "logs" / WORKFLOW / "runs" / run_id
def _state_path(root: Path, run_id: str) -> Path: return _run_dir(root, run_id) / "state.json"
def _tool_dir(root: Path, tool_id: str) -> Path: return root / "skills" / WORKFLOW / "tools" / tool_id


def _store(root: Path, run_id: str) -> WorkflowStateStore:
    return WorkflowStateStore(
        root=root,
        workflow=WORKFLOW,
        run_id=run_id,
        definition=STATE_DEFINITION,
        run_dir=_run_dir(root, run_id),
        schema_path=ROOT / "utils" / "references" / "workflow-state-v1.schema.json",
    )


def _new_state(run_id: str, mode: str) -> dict:
    now = iso_timestamp()
    return {"schema_version": "1.0", "workflow": WORKFLOW, "run_id": run_id, "mode": mode, "status": "prepared", "current_stage": "prepared", "resume_stage": None, "current_object_id": None, "current_batch_id": None, "completed_steps": [], "pending_decisions": [], "error": None, "created_at": now, "updated_at": now, "last_heartbeat_at": now, "event_sequence": 0}


def _save(root: Path, state: dict) -> None:
    _state_path(root, state["run_id"]).parent.mkdir(parents=True, exist_ok=True)
    write_json(_state_path(root, state["run_id"]), state)


def _advance(root: Path, state: dict, target: str, **updates) -> dict:
    completed_step = updates.pop("completed_step", None) or target
    return _store(root, state["run_id"]).transition(state, target, stage=target, completed_step=completed_step, updates=updates)


def _pause(root: Path, state: dict, status: str, message: str, resume: str) -> dict:
    return _store(root, state["run_id"]).pause(
        state,
        status=status,
        error_code=status.removeprefix("paused_"),
        message=message,
        resume_stage=resume,
    )


def _python_executable(root: Path) -> str:
    candidate = root / "runtime" / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    return str(candidate) if candidate.is_file() else sys.executable


def _convert_epub(root: Path, path: Path) -> tuple[str, dict]:
    converter = root / "skills" / "format-conversion-master" / "scripts" / "cli.py"
    proc = subprocess.run([_python_executable(root), str(converter), "start", "--root", str(root), "--input", str(path), "--to", "md"], capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0: raise RuntimeError(proc.stderr or proc.stdout or "EPUB 转换失败")
    receipt = json.loads(proc.stdout)
    if receipt.get("status") != "completed": raise RuntimeError(receipt.get("error", {}).get("message", "EPUB 转换未完成"))
    output = receipt.get("publication", {}).get("path")
    if not output: raise RuntimeError("EPUB 转换回执缺少 Markdown 路径")
    return Path(output).read_text(encoding="utf-8"), receipt


def _read_sources(root: Path, req: dict) -> tuple[str, dict]:
    files = req.get("source_files") or ([req["input_file"]] if req.get("input_file") else [])
    chunks = [str(req["content"])] if req.get("content") else []
    meta = {"conversion_run_id": None, "source_sha256": []}
    for value in files:
        path = Path(value); path = (root / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_file(): raise FileNotFoundError(f"来源文件不存在：{path}")
        meta["source_sha256"].append(hashlib.sha256(path.read_bytes()).hexdigest())
        if path.suffix.lower() == ".epub":
            text, receipt = _convert_epub(root, path); chunks.append(text); meta["conversion_run_id"] = receipt.get("run_id")
        elif path.suffix.lower() in {".txt", ".md", ".markdown"}: chunks.append(path.read_text(encoding="utf-8-sig"))
        else: raise ValueError(f"不支持的来源格式：{path.suffix or '<无扩展名>'}")
    if not any(x.strip() for x in chunks): raise ValueError("至少提供一个非空来源")
    return "\n\n".join(chunks), meta


def _md_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def _split_md_row(row: str) -> list[str]:
    """Split a pipe-delimited Markdown row while respecting escaped pipes."""
    value = row.strip()
    if not (value.startswith("|") and value.endswith("|")):
        raise ValueError("Markdown 表格行必须以 | 开始和结束")
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in value[1:-1]:
        if char == "|" and not escaped:
            cells.append("".join(current).strip())
            current = []
            continue
        current.append(char)
        if char == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
    cells.append("".join(current).strip())
    return cells


def _verify_markdown_table(text: str, heading: str, expected_columns: list[str]) -> None:
    """Verify one generated GFM table without requiring a Markdown dependency."""
    marker = heading + "\n"
    if marker not in text:
        raise ValueError(f"结果 Markdown 缺少表格小节：{heading}")
    section = text.split(marker, 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.strip()]
    if len(rows) < 2:
        raise ValueError(f"结果 Markdown 缺少表格行：{heading}")
    header = _split_md_row(rows[0])
    separator = _split_md_row(rows[1])
    if header != expected_columns:
        raise ValueError(f"结果 Markdown 表头不匹配：{heading}")
    if len(separator) != len(expected_columns) or any(not re.fullmatch(r":?-{3,}:?", cell) for cell in separator):
        raise ValueError(f"结果 Markdown 表格分隔线不合法：{heading}")
    for row in rows[2:]:
        if len(_split_md_row(row)) != len(expected_columns):
            raise ValueError(f"结果 Markdown 表格列数不一致：{heading}")


def _section_kind(title: str, content: str = "") -> str:
    if "框架" in title:
        return "framework"
    if "词汇" in title:
        return "vocabulary"
    if re.search(r"作文|应用文|信|告示", title):
        return "essay"
    if len(re.findall(r"[A-Za-z].*[\u3400-\u9fff]", content)) >= 2:
        return "vocabulary"
    return "meta"


def _render_template(name: str, values: dict[str, object]) -> str:
    text = (SKILL_ROOT / "templates" / name).read_text(encoding="utf-8")
    for key, value in values.items(): text = text.replace("{{ " + key + " }}", str(value))
    unresolved = re.findall(r"{{\s*[^}]+\s*}}", text)
    if unresolved: raise ValueError(f"模板仍有未填充字段：{', '.join(unresolved)}")
    return text.rstrip() + "\n"


def _artifact_texts(data: dict) -> dict[str, str]:
    name = data["display_name"]
    rules = "\n\n".join(f"## {x['title']}\n\n类型：{x['section_kind']}\n\n{x['content']}\n\n来源定位：{x['source_locator']}" for x in data["writing_rules"])
    dimensions = "\n".join(f"| {x['label']} | {x['max_score']} |" for x in data["scoring"]["dimensions"])
    items = [x for values in data["language_bank"].values() for x in values]
    language_rows = "\n".join(
        f"| {_md_cell(x['type'])} | {_md_cell(x['expression'])} | {_md_cell(x['meaning_zh'])} | {_md_cell(x['usage_case'])} | {_md_cell(x['source_locator'])} |"
        for x in items
    )
    source = data["source"]
    artifacts = {
        "TOOL.md": _render_template("tool-readme.template.md", {"display_name": name}),
        "writing-rules.md": _render_template("writing-rules.template.md", {"display_name": name, "rules": rules}),
        "scoring-rubric.md": _render_template("scoring-rubric.template.md", {"display_name": name, "default_full_score": data["scoring"]["default_full_score"], "dimensions": dimensions}),
        "language-bank.md": _render_template("language-bank.template.md", {"display_name": name, "language_items": language_rows}),
        "source-notes.md": _render_template("source-notes.template.md", {"display_name": name, "source_type": source["source_type"], "source_files": json.dumps(source["source_files"], ensure_ascii=False), "source_sha256": json.dumps(source["source_sha256"], ensure_ascii=False), "converter_run_id": source.get("converter_run_id") or "无", "selection_audit": json.dumps(source.get("selection_audit", {}), ensure_ascii=False, indent=2)}),
    }
    if data.get("tool_id") == "cet4-writing":
        artifacts["writing-rules.md"] += (
            "\n\n## 工具级评阅补充规则\n\n"
            "议论文建议采用三段结构，但三段不是本工具的硬性要求。少于三段时，Agent 应结合段落功能、论证展开和结尾完整性进行判断并提出建议。"
            "段落结构、句间逻辑、语义、拼写、语法、搭配和分数必须由 Agent 判断；词数、段落数、连接词和词库命中仅为机械证据，不得单独作为评分依据。\n"
        )
    return artifacts


def _build_tool(req: dict, text: str, meta: dict) -> tuple[dict, dict[str, str]]:
    blocks, audit = select_writing_blocks_with_audit(text, ignore_translation=bool(req.get("ignore_translation", True)))
    if not blocks:
        source_type = req.get("source_type") or "auto"
        if source_type != "epub" and not any(Path(x).suffix.lower() == ".epub" for x in (req.get("source_files") or [])):
            blocks = [SourceBlock("内联写作规则", 1, clean_markdown(text).strip(), "inline")]
        else:
            raise ValueError("来源中没有找到可保留的写作章节")
    files = req.get("source_files") or ([req.get("input_file")] if req.get("input_file") else [])
    data = {"schema_version": "1.1", "tool_id": req.get("tool_id") or "new-writing-tool", "display_name": req.get("tool_name") or req.get("tool_id") or "new-writing-tool", "source": {"source_type": req.get("source_type") or "auto", "source_files": files, "source_sha256": meta["source_sha256"], "converter": "format-conversion-master" if meta.get("conversion_run_id") else None, "converter_run_id": meta.get("conversion_run_id"), "selection_policy": "writing_only" if req.get("ignore_translation", True) else "writing_and_transferable_translation", "selection_audit": audit}, "artifact_manifest": {}, "writing_rules": [{"title": b.title, "section_kind": _section_kind(b.title, b.content), "content": b.content, "source_category": "writing_direct", "writing_relevance": "high", "source_locator": b.source_locator} for b in blocks], "scoring": {"default_full_score": 15, "dimensions": [{"id": "content", "label": "内容", "max_score": 5}, {"id": "organization", "label": "结构", "max_score": 5}, {"id": "language", "label": "语言", "max_score": 5}], "levels": DEFAULT_SCORING_LEVELS}, "language_bank": extract_language_bank(blocks)}
    artifacts = _artifact_texts(data)
    data["artifact_manifest"] = {name: {"path": name, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()} for name, content in artifacts.items()}
    validate_json_schema(data, TOOL_SCHEMA)
    return data, artifacts


def _verify_tool_bundle(path: Path) -> dict:
    data = read_json(path / "tool.json"); validate_json_schema(data, TOOL_SCHEMA)
    for name, info in data["artifact_manifest"].items():
        if info["path"] != name: raise ValueError(f"工具文件路径与 manifest 键不一致：{name}")
        target = (path / info["path"]).resolve()
        if target.parent != path.resolve(): raise ValueError(f"工具文件路径越界：{name}")
        if not target.is_file(): raise ValueError(f"工具文件缺失：{name}")
        if hashlib.sha256(target.read_bytes()).hexdigest() != info["sha256"]: raise ValueError(f"工具文件哈希不一致：{name}")
    for rule in data["writing_rules"]:
        if not rule.get("content", "").strip(): raise ValueError(f"写作规则为空：{rule.get('title')}")
        if data["source"].get("selection_policy") == "writing_only" and re.search(r"翻译|(?:参考)?译文|translation", rule.get("title", "") + "\n" + rule.get("content", ""), re.I):
            raise ValueError(f"写作工具仍包含翻译范围：{rule.get('title')}")
    language_items = [item for values in data["language_bank"].values() for item in values]
    if not language_items: raise ValueError("语言库为空")
    if any(str(item.get("usage_case", "")).startswith("Use ") for item in language_items): raise ValueError("语言库仍包含通用 usage_case 占位文本")
    return data


def list_tools(root: Path) -> dict:
    base = root / "skills" / WORKFLOW / "tools"
    return {"status": "ok", "tools": sorted(p.name for p in base.iterdir() if (p / "tool.json").is_file()) if base.exists() else []}


def _write_tool(path: Path, data: dict, artifacts: dict[str, str]) -> None:
    path.mkdir(parents=True, exist_ok=True); write_json_transaction({path / "tool.json": data}); write_text_transaction({path / name: content for name, content in artifacts.items()})


def _create_tool(root: Path, req: dict, state: dict) -> dict:
    state = _advance(root, state, "resolving_source"); files = req.get("source_files") or ([req["input_file"]] if req.get("input_file") else [])
    if any(Path(x).suffix.lower() == ".epub" for x in files): state = _advance(root, state, "converting_epub")
    try: text, meta = _read_sources(root, req)
    except Exception as exc: return _pause(root, state, "paused_conversion" if state["status"] == "converting_epub" else "paused_source_missing", str(exc), state["status"])
    state = _advance(root, state, "reading_converted_markdown" if meta.get("conversion_run_id") else "reading_text", conversion_run_id=meta.get("conversion_run_id"), source_sha256=meta.get("source_sha256")); state = _advance(root, state, "normalizing_source"); state = _advance(root, state, "selecting_writing_scope")
    try: data, artifacts = _build_tool(req, text, meta)
    except Exception as exc: return _pause(root, state, "paused_extraction", str(exc), "selecting_writing_scope")
    state = _advance(root, state, "extracting_rules"); state = _advance(root, state, "structuring_tool"); path = _tool_dir(root, data["tool_id"])
    if path.exists() and any(path.iterdir()) and not req.get("replace_existing"): return _pause(root, state, "paused_output_conflict", f"工具已存在：{path}；如需重建请设置 replace_existing=true", "structuring_tool")
    state = _advance(root, state, "verifying_tool")
    try: _write_tool(path, data, artifacts); _verify_tool_bundle(path)
    except Exception as exc: return _pause(root, state, "paused_verification", str(exc), "verifying_tool")
    state = _advance(root, state, "publishing", tool_id=data["tool_id"], current_object_id=data["tool_id"], output_path=str(path)); return _advance(root, state, "completed")


def _items(tool: dict, essay: str = "") -> list:
    all_items = [x for values in tool.get("language_bank", {}).values() if isinstance(values, list) for x in values]
    def contains(expression: str, text: str) -> bool:
        return bool(re.search(rf"(?<![A-Za-z]){re.escape(expression.lower())}(?![A-Za-z])", text.lower()))
    if essay:
        all_items = [item for item in all_items if contains(item.get("expression", ""), essay)]
    selected = []
    for source in all_items[:12]:
        item = dict(source)
        expression = item.get("expression", "")
        matched_in_text = bool(essay and contains(expression, essay))
        item["selection_source"] = "matched_in_text" if matched_in_text else "tool_language_bank"
        if matched_in_text:
            match = next((sentence for sentence in sentences(essay) if contains(expression, sentence)), None)
            if match:
                item["usage_case"] = match
        selected.append(item)
    return selected


_POLISH_REPLACEMENTS = (
    (r"\bneccessary\b", "necessary", "修正拼写错误", "spelling"),
    (r"\bbegining\b", "beginning", "修正拼写错误", "spelling"),
    (r"\bcustomes\b", "customs", "修正拼写错误", "spelling"),
    (r"\bmeeting foreign culture\b", "encountering foreign cultures", "改善 culture 的搭配和复数形式", "collocation"),
    (r"\bbroaden our mind\b", "broaden our minds", "修正名词单复数", "grammar"),
    (r"\ba master's graduate(?:'s)? degree\b", "a master's degree", "修复 master's degree 的错误替换", "grammar"),
    (r"\ba master degree\b", "a master's degree", "补充 master's 所需的所有格", "grammar"),
    (r"\bget higher salary\b", "receive a higher salary", "补充冠词并改善薪资搭配", "collocation"),
    (r"\ba right attitude\b", "the right attitude", "修正固定搭配", "grammar"),
    (r"\bstrong practical ability\b", "strong practical skills", "改为自然搭配", "collocation"),
    (r"\bwhen they recruit new workers\b", "when recruiting new employees", "改为更正式、简洁的表达", "style"),
    (r"\bmore and more students enter college\b", "more and more students have entered college", "统一近年来背景下的时态", "grammar"),
    (r"\bthe study is too theoretical\b", "some coursework may be too theoretical", "明确 study 的指代", "clarity"),
    (r"\bhelps us think and solve\b", "helps students think critically and solve", "补充批判性思考并统一主语", "style"),
)


def _apply_polish(text: str) -> tuple[str, list[dict[str, str]]]:
    revised = text
    changes: list[dict[str, str]] = []
    for pattern, replacement, reason, issue_type in _POLISH_REPLACEMENTS:
        updated = re.sub(pattern, replacement, revised, flags=re.I)
        if updated != revised:
            changes.append({"original": revised, "revised": updated, "reason": reason, "issue_type": issue_type})
            revised = updated
    return revised, changes


def _review_sentence(sentence: str, index: int, agent_reviews: list[dict]) -> dict:
    """Render an Agent sentence judgment; do not diagnose language or logic here."""
    item = next((x for x in agent_reviews if x.get("sentence_index") == index), None)
    if not item:
        raise ValueError(f"agent_review 缺少第 {index} 句判断")
    return item


def _full_score(req: dict, tool: dict) -> int:
    return integer_score(req.get("full_score") or tool["scoring"]["default_full_score"], field="full_score", minimum=1)


def _quality_checks(essay: str, requirements: dict | None = None) -> dict:
    requirements = requirements or {}
    info = metrics(essay)
    minimum = int(requirements.get("word_count_min", 0) or 0)
    maximum = int(requirements.get("word_count_max", 0) or 0)
    return {
        **info,
        "within_word_limit": (not minimum or info["word_count"] >= minimum) and (not maximum or info["word_count"] <= maximum),
        "word_count_min": minimum or None,
        "word_count_max": maximum or None,
        "issue_count": 0,
    }


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _target_info(req: dict, full: int) -> tuple[int | None, dict[str, int] | None]:
    target = parse_target(req.get("target_score"), full)
    raw_tolerance = req.get("target_tolerance", 1.0)
    tolerance = 1 if raw_tolerance is None else integer_score(raw_tolerance, field="target_tolerance", minimum=0)
    return target, target_band(target, full, tolerance=tolerance)


def _load_grade_result(root: Path, req: dict) -> dict | None:
    run_id = req.get("grade_run_id")
    if not run_id:
        return None
    path = _run_dir(root, str(run_id)) / "state.json"
    if not path.is_file():
        raise ValueError(f"找不到批改运行：{run_id}")
    state = read_json(path)
    result = state.get("result")
    if not isinstance(result, dict) or result.get("mode") != "grade":
        raise ValueError(f"运行不是批改结果：{run_id}")
    expected_hash = result.get("provenance", {}).get("input_sha256")
    current_essay = req.get("essay", "")
    if expected_hash and expected_hash != _text_sha256(str(current_essay).strip()):
        raise ValueError("grade_run_id 与当前 essay 的输入哈希不一致")
    return result


def _feedback_text(req: dict, grade_result: dict | None) -> str:
    feedback = req.get("feedback", "")
    if isinstance(feedback, list):
        feedback = " ".join(str(item) for item in feedback)
    if grade_result:
        sentence_feedback = " ".join(
            str(item.get("suggestion", "")) for item in grade_result.get("sentence_reviews", []) if item.get("suggestion")
        )
        feedback = (str(feedback) + " " + str(grade_result.get("overall_evaluation", "")) + " " + sentence_feedback).strip()
    return str(feedback)


def _enhance_for_score(essay: str, requirements: dict, topic: str) -> tuple[str, list[dict[str, str]]]:
    """Apply bounded, meaning-preserving upgrades when ordinary correction does not raise the score."""
    revised = essay
    changes: list[dict[str, str]] = []
    maximum = int(requirements.get("word_count_max", 180) or 180)
    if "competitive advantage" not in revised.lower() and re.search(r"more chances", revised, re.I):
        updated = re.sub(r"more chances", "a competitive advantage", revised, count=1, flags=re.I)
        changes.append({"original": revised, "revised": updated, "reason": "增加与就业竞争直接相关的表达", "issue_type": "content"})
        revised = updated
    if "think critically" not in revised.lower() and re.search(r"think and solve", revised, re.I):
        updated = re.sub(r"think and solve", "think critically and solve", revised, count=1, flags=re.I)
        changes.append({"original": revised, "revised": updated, "reason": "提升句式和语言准确度", "issue_type": "language"})
        revised = updated
    additions = (
        " In the long run, these skills can improve a student's career choices.",
        " For example, practical skills can help students perform better in interviews.",
        " In brief, this choice should fit the student's plans.",
    )
    for addition in additions:
        if addition.strip().lower() not in revised.lower() and metrics(revised)["word_count"] + len(addition.split()) <= maximum:
            updated = revised.rstrip() + addition
            changes.append({"original": revised, "revised": updated, "reason": "补充可评分的论据或总结表达", "issue_type": "content"})
            revised = updated
            break
    return revised, changes


def _content(root: Path, req: dict, mode: str, tool: dict) -> dict:
    full = _full_score(req, tool)
    topic = req.get("topic", "")
    requirements = req.get("requirements", {}) or {}
    if mode == "write":
        target, band = _target_info(req, full)
        generated = validate_agent_draft(req.get("essay", ""), topic, requirements, full, target)
        preview = generated["score_preview"]
        target_match = generated["target_match"]
        if isinstance(req.get("agent_review"), dict):
            preview = score_from_agent_review(req["agent_review"], full_score=full, dimensions=tool["scoring"].get("dimensions"))
            target_match = band is None or band["lower"] <= preview["value"] <= band["upper"]
        score_expectation = {
            "target": target,
            "full_score": full,
            "band": band,
            "reason": "按目标分数区间生成，并在发布前进行词数与评分预检。" if target is not None else "按工具默认难度生成，并进行词数与评分预检。",
        }
        return {
            "mode": "write", "tool_id": tool["tool_id"], "topic": topic, "requirements": requirements,
            "essay": generated["essay"], "score_expectation": score_expectation, "target_score": target,
            "target_band": band, "score_preview": preview,
            "quality_checks": {**_quality_checks(generated["essay"], requirements), "target_match": target_match},
            "provenance": {"input_sha256": _text_sha256(topic), "parent_run_id": req.get("parent_run_id")},
            "language_items": _items(tool, generated["essay"]),
        }
    essay = str(req.get("essay", "")).strip()
    if not essay:
        raise ValueError("essay 不能为空")
    if mode == "grade":
        target, band = _target_info(req, full)
        review = req.get("agent_review")
        if not isinstance(review, dict):
            raise ValueError("批改必须提供 agent_review；结构、逻辑、语言和语义判断不得由脚本推断")
        _validate_organization_analysis(review.get("organization_analysis"), essay)
        evidence = build_evidence(essay, topic=topic)
        score = score_from_agent_review(review, full_score=full, dimensions=tool["scoring"].get("dimensions"))
        reviews = [_review_sentence(sentence, i, review.get("sentence_reviews", [])) for i, sentence in enumerate(sentences(essay), 1)]
        quality = _quality_checks(essay, requirements)
        quality["issue_count"] = len(review.get("issues", []))
        quality["agent_review_required"] = True
        target_info = {"target": target, "band": band, "gap": score["value"] - target if target is not None else None}
        return {
            "mode": "grade", "tool_id": tool["tool_id"], "topic": topic, "score": score,
            "target_score": target, "target_band": band, "target_info": target_info,
            "overall_evaluation": review["overall_evaluation"],
            "improvement_advice": review.get("improvement_advice", ""),
            "organization_analysis": review.get("organization_analysis", {}),
            "sentence_reviews": reviews, "quality_checks": quality, "evidence": evidence,
            "provenance": {"input_sha256": _text_sha256(essay), "parent_run_id": req.get("parent_run_id")}, "language_items": _items(tool, essay),
        }
    grade_result = _load_grade_result(root, req)
    agent_review = req.get("agent_review")
    polished = str(req.get("polished_essay", "")).strip()
    if not isinstance(agent_review, dict) or not polished:
        raise ValueError("润色必须由 Agent 提供 polished_essay 及 agent_review.before/after")
    before_review = agent_review.get("before")
    after_review = agent_review.get("after")
    if not isinstance(before_review, dict) or not isinstance(after_review, dict):
        raise ValueError("agent_review 必须包含 before 和 after 两组 Agent 判断")
    before = score_from_agent_review(before_review, full_score=full, dimensions=tool["scoring"].get("dimensions"))
    after = score_from_agent_review(after_review, full_score=full, dimensions=tool["scoring"].get("dimensions"))
    changes = agent_review.get("changes", [])
    feedback = _feedback_text(req, grade_result)
    delta = after["value"] - before["value"]
    improvement_status = "improved" if delta >= 1 else "unchanged" if delta == 0 else "regressed"
    no_improvement_reason = None
    if delta < 1:
        no_improvement_reason = "已完成可识别的语言修正，但当前评分维度未产生至少 1 分提升；建议补充具体论据或更明显的结构/句式改进。"
    return {
        "mode": "polish", "tool_id": tool["tool_id"], "topic": topic,
        "original_essay": essay, "polished_essay": polished, "changes": changes,
        "score_before": before, "score_after": after,
        "score_delta": {"before": before["value"], "after": after["value"], "delta": delta},
        "improvement_status": improvement_status, "no_improvement_reason": no_improvement_reason,
        "feedback_used": feedback, "quality_checks": _quality_checks(polished, requirements),
        "organization_analysis": after_review.get("organization_analysis", {}),
        "provenance": {"input_sha256": _text_sha256(essay), "grade_run_id": req.get("grade_run_id"), "parent_run_id": req.get("parent_run_id")},
        "language_items": _items(tool, polished),
    }


def _friendly_target_info(info: dict | None) -> str:
    if not info or info.get("target") is None:
        return "未设置目标分数。"
    target = info["target"]
    gap = info.get("gap")
    gap_text = "达到目标" if gap == 0 else (f"高于目标 {gap} 分" if gap > 0 else f"低于目标 {abs(gap)} 分")
    return f"目标分数：{target} 分；{gap_text}。"


def _friendly_target_band(band: dict | None) -> str:
    if not band:
        return "未设置目标区间。"
    return f"{band['lower']}—{band['upper']} / 目标 {band['target']} 分。"


def _friendly_changes(changes: list[dict]) -> str:
    if not changes:
        return "无需修改"
    sections = []
    for index, optimization in enumerate(changes, 1):
        optimization_index = optimization.get("optimization_index", index)
        summary = str(optimization.get("summary", "优化表达")).strip()
        rows = [f"### 优化 {optimization_index}：{summary}"]
        for location_index, location in enumerate(optimization.get("locations", []), 1):
            original = str(location.get("original", "")).replace("\n", "\n> ")
            revised = str(location.get("revised", "")).replace("\n", " ")
            reason = str(location.get("reason", "改善表达")).strip()
            rows.extend([
                "",
                f"#### 位置 {location_index}",
                "",
                f"> {original}",
                "",
                f"改动后：{revised}",
                "",
                f"改动原因：{reason}",
            ])
        sections.append("\n".join(rows))
    return "\n\n".join(sections)


def _friendly_no_improvement_reason(result: dict) -> str:
    delta = result.get("score_delta", {}).get("delta")
    reason = result.get("no_improvement_reason")
    if isinstance(delta, int) and delta > 0:
        return ""
    if not reason:
        return ""
    return f"## 未提升原因\n\n{reason}"


def _friendly_organization_analysis(analysis: dict) -> str:
    if not analysis:
        raise ValueError("批改结果缺少篇章结构与逻辑分析")
    lines = []
    for key, label in (("paragraph_count", "实际段落数"), ("recommended_paragraph_count", "推荐段落数"), ("status", "结构判断"), ("summary", "总体判断")):
        if key in analysis:
            lines.append(f"- {label}：{analysis[key]}")
    for key, label in (("missing_roles", "缺少的段落功能"), ("logic_issues", "逻辑问题"), ("suggestions", "结构建议")):
        values = analysis.get(key)
        if values:
            lines.append(f"- {label}：{'；'.join(str(item) for item in values)}")
    for item in analysis.get("paragraph_reviews", []):
        lines.append(f"- 第{item.get('paragraph_index', '?')}段：{item.get('role', '未分类')}；{item.get('status', '未判断')}；{item.get('evidence', '')}")
    return "\n".join(lines) or "篇章结构与逻辑分析不完整。"


def _validate_organization_analysis(analysis: object, essay: str) -> None:
    """Require the Agent's organization review and check it against script evidence."""
    if not isinstance(analysis, dict):
        raise ValueError("批改必须提供篇章结构与逻辑分析")
    required = ("paragraph_count", "recommended_paragraph_count", "status", "summary", "paragraph_reviews", "logic_issues", "suggestions")
    missing = [key for key in required if key not in analysis]
    if missing:
        raise ValueError(f"篇章结构与逻辑分析缺少字段：{', '.join(missing)}")
    evidence = build_organization_evidence(essay)
    if analysis["paragraph_count"] != evidence["paragraph_count"]:
        raise ValueError("篇章结构分析中的段落数与作文实际段落数不一致")
    reviews = analysis["paragraph_reviews"]
    if not isinstance(reviews, list) or len(reviews) != evidence["paragraph_count"]:
        raise ValueError("篇章结构分析必须逐段提供 paragraph_reviews")
    indexes = [item.get("paragraph_index") for item in reviews if isinstance(item, dict)]
    if indexes != list(range(1, evidence["paragraph_count"] + 1)):
        raise ValueError("篇章结构分析的段落编号必须连续且从 1 开始")
    for key in ("status", "summary"):
        if not isinstance(analysis[key], str) or not analysis[key].strip():
            raise ValueError(f"篇章结构分析字段 {key} 不能为空")
    for key in ("logic_issues", "suggestions"):
        if not isinstance(analysis[key], list):
            raise ValueError(f"篇章结构分析字段 {key} 必须是数组")
    if not analysis["suggestions"]:
        raise ValueError("篇章结构分析至少需要一条结构建议")


def _friendly_dimension_reason(item: dict, result: dict) -> str:
    label = item.get("dimension", "")
    issues = result.get("score", {}).get("issues", [])
    if label == "内容":
        return "主题切合，立场明确，并提供了例证和另一面观点。"
    if label == "结构":
        analysis = result.get("organization_analysis", {})
        return str(analysis.get("summary") or analysis.get("status") or item.get("reason", "由 Agent 根据工具规则评估。"))
    if label == "语言":
        return item.get("reason", "由 Agent 根据语言准确性、搭配和表达判断。")
    return item.get("reason", "")


def _render(result: dict) -> str:
    language_rows = "\n".join(
        f"| {_md_cell(x['type'])} | {_md_cell(x['expression'])} | {_md_cell(x['meaning_zh'])} | {_md_cell(x['usage_case'])} |"
        for x in result.get("language_items", [])
    )
    if not language_rows:
        language_rows = "| - | 未匹配到工具词库表达 | 当前文本中未识别到可复用的高级词汇、短语、句型或表达 | 建议补充更正式的词汇或句型 |"
    if result["mode"] == "write":
        return _render_template("writing-output.template.md", {
            "essay": result["essay"], "target_score": result["target_score"], "full_score": result["score_expectation"]["full_score"],
            "target_band": _friendly_target_band(result.get("target_band")), "word_count": result["quality_checks"]["word_count"],
            "score_expectation": result["score_expectation"]["reason"],
            "language_items": language_rows,
        })
    if result["mode"] == "grade":
        score = result["score"]
        dimension_rows = "\n".join(f"| {x['dimension']} | {x['score']} | {x['max_score']} | {x['reason']} |" for x in score["dimension_scores"])
        dimension_rows = "\n".join(f"| {x['dimension']} | {x['score']} | {x['max_score']} | {_friendly_dimension_reason(x, result)} |" for x in score["dimension_scores"])
        score_text = f"得分：{score['value']} / {score['full_score']}\n\n| 维度 | 得分 | 满分 | 说明 |\n|---|---:|---:|---|\n{dimension_rows}"
        reviews = "\n\n".join(_render_sentence_review(x) for x in result["sentence_reviews"])
        return _render_template("grading-output.template.md", {
            "full_score": score["full_score"], "score": score_text, "target_info": _friendly_target_info(result.get("target_info")),
            "overall_evaluation": result["overall_evaluation"],
            "organization_analysis": _friendly_organization_analysis(result.get("organization_analysis", {})),
            "improvement_advice": result.get("improvement_advice", ""), "sentence_reviews": reviews,
            "language_items": language_rows,
        })
    changes = _friendly_changes(result["changes"])
    return _render_template("polishing-output.template.md", {
        "polished_essay": result["polished_essay"], "changes": changes,
        "score_delta": (lambda d: f"润色前：{d['before']} 分；润色后：{d['after']} 分；分数变化：{d['delta']:+d} 分。")(result["score_delta"]),
        "no_improvement_reason_block": _friendly_no_improvement_reason(result),
        "language_items": language_rows,
    })


def render_result(result: dict) -> str:
    """Render the canonical user-facing Markdown for a completed result.

    Keeping this as a public wrapper makes the template contract callable by
    adapters (including the chat execution layer) instead of encouraging them
    to reconstruct a response from the JSON fields.
    """
    return _render(result)


def _render_sentence_review(review: dict) -> str:
    status_labels = {"revise": "修改", "keep": "保留", "delete": "删除"}
    status = review["status"]
    opinion = review.get("evaluation") or review.get("suggestion") or "无需修改"
    lines = [
        f"### 第 {review['sentence_index']} 句：{status_labels[status]}",
        "",
        f"> {str(review['original']).replace(chr(10), chr(10) + '> ')}",
        "",
        f"意见：{opinion}",
    ]
    if status == "revise":
        lines.extend(["", f"修改后：{review.get('revised') or ''}"])
    return "\n".join(lines)


def _verify_rendered_markdown(result: dict, text: str) -> None:
    """Verify the user-facing delivery contains the canonical Markdown table."""
    heading = "## 使用到的高级词汇、短语、句型、表达"
    _verify_markdown_table(text, heading, ["类型", "表达", "中文含义", "使用案例"])
    if text != _render(result):
        raise ValueError("结果 Markdown 不是由当前 result.json 规范渲染得到")


def _emit_cli_result(value: dict, output_format: str, *, allow_markdown: bool = True) -> None:
    """Emit either the legacy JSON receipt or the canonical Markdown result."""
    if allow_markdown and output_format == "markdown" and value.get("status") == "completed" and value.get("mode") in {"write", "grade", "polish"}:
        output_path = value.get("output_path")
        markdown_path = Path(output_path) / "result.md" if output_path else None
        if markdown_path and markdown_path.is_file():
            text = markdown_path.read_text(encoding="utf-8")
            sys.stdout.write(text)
            if not text.endswith("\n"):
                sys.stdout.write("\n")
            return
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _run(root: Path, mode: str, req: dict, run_id: str | None = None, state: dict | None = None) -> dict:
    run_id = run_id or (iso_timestamp().replace(":", "").replace("-", "") + "-" + uuid.uuid4().hex[:8])
    if state is None:
        state = _new_state(run_id, mode)
        _run_dir(root, run_id).mkdir(parents=True, exist_ok=True)
        _store(root, run_id).create(state)
        write_json(_run_dir(root, run_id) / "request.json", req)
    validate_json_schema(req, REQ_SCHEMA); state = _advance(root, state, "validating_request")
    if mode == "create-tool": return _create_tool(root, req, state)
    state = _advance(root, state, "resolving_tool"); tool_id = req.get("tool_id")
    if not tool_id: return _pause(root, state, "paused_missing_tool", "缺少 tool_id，请先指定作文工具", "resolving_tool")
    try: tool = _verify_tool_bundle(_tool_dir(root, tool_id))
    except Exception as exc: return _pause(root, state, "paused_missing_tool", str(exc), "resolving_tool")
    state = _advance(root, state, "loading_tool")
    if req.get("target_score") is not None and req.get("full_score") is None:
        return _pause(root, state, "paused_missing_full_score", "指定具体分数时必须提供满分", "planning")
    if mode == "write":
        state = _advance(root, state, "planning_target")
        state = _advance(root, state, "generating")
        if not str(req.get("essay", "")).strip():
            packet = build_generation_packet(req.get("topic", ""), req.get("requirements", {}) or {}, tool)
            validate_json_schema(packet, GENERATION_PACKET_SCHEMA)
            write_json(_run_dir(root, run_id) / "generation_packet.json", packet)
            return _pause(
                root,
                state,
                "paused_agent_generation",
                "请 Agent 根据 generation_packet.json 生成 essay，然后使用 resume --input 提交 essay",
                "generating",
            )
        result = _content(root, req, mode, tool)
        state = _advance(root, state, "scoring_preview", result=result)
        state = _advance(root, state, "finalizing_scores", result=result)
    elif mode == "grade":
        state = _advance(root, state, "normalizing_essay")
        state = _advance(root, state, "scoring")
        if not isinstance(req.get("agent_review"), dict):
            return _pause(root, state, "paused_agent_review", "批改必须由 Agent 提供结构、逻辑、语义和语言判断", "scoring")
        try:
            _validate_organization_analysis(req["agent_review"].get("organization_analysis"), str(req.get("essay", "")).strip())
        except ValueError as exc:
            return _pause(root, state, "paused_agent_review", str(exc), "validating_agent_review")
        state = _advance(root, state, "validating_agent_review")
        result = _content(root, req, mode, tool)
        state = _advance(root, state, "reviewing_sentences", result=result)
        state = _advance(root, state, "finalizing_scores", result=result)
    elif mode == "polish":
        state = _advance(root, state, "loading_feedback")
        state = _advance(root, state, "applying_edits")
        result = _content(root, req, mode, tool)
        state = _advance(root, state, "rescoring", result=result)
        state = _advance(root, state, "finalizing_scores", result=result)
        try:
            _validate_polish_changes(result.get("changes", []), result.get("original_essay"), result.get("polished_essay"))
        except ValueError as exc:
            return _pause(root, state, "paused_verification", str(exc), "validating_polish_changes")
        state = _advance(root, state, "validating_polish_changes", result=result)
    else:
        raise ValueError(f"不支持的模式：{mode}")
    if mode == "write" and req.get("target_score") is not None and not isinstance(req.get("agent_review"), dict):
        return _pause(root, state, "paused_agent_review", "目标分数写作必须由 Agent 评阅生成稿", "scoring_preview")
    if mode == "write" and req.get("target_score") is not None and not result.get("quality_checks", {}).get("target_match", False):
        return _pause(root, state, "paused_target_mismatch", "生成稿未落入目标分数区间", "planning_target")
    try:
        validate_json_schema(result, OUT_SCHEMA)
        _verify_result_semantics(result)
    except Exception as exc:
        return _pause(root, state, "paused_verification", str(exc), state["status"])
    state = _advance(root, state, "verifying", result=result)
    out = root / "outputs" / WORKFLOW / "runs" / run_id
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "result.json", result)
    state = _advance(root, state, "rendering_markdown", result=result)
    rendered = _render(result)
    _verify_rendered_markdown(result, rendered)
    (out / "result.md").write_text(rendered, encoding="utf-8")
    state = _advance(root, state, "verifying_delivery", result=result)
    state = _advance(root, state, "publishing", output_path=str(out), result=result)
    return _advance(root, state, "completed")


def run_mode(root: Path, mode: str, req: dict) -> dict: return _run(root, mode, req)


def _validate_polish_changes(changes: list[dict], original_essay: str | None = None, polished_essay: str | None = None) -> None:
    """Validate grouped polish changes before Markdown rendering."""
    if not isinstance(changes, list):
        raise ValueError("润色修改记录必须是数组")
    if original_essay is not None and polished_essay is not None:
        if changes and str(original_essay).strip() == str(polished_essay).strip():
            raise ValueError("作文未发生变化时不应提交修改位置；请明确标记为无需修改")
        if not changes and str(original_essay).strip() != str(polished_essay).strip():
            raise ValueError("作文已发生变化但缺少修改位置与原因")
    seen_indexes = set()
    for index, optimization in enumerate(changes, 1):
        if not isinstance(optimization, dict):
            raise ValueError(f"第 {index} 个优化要点格式无效")
        optimization_index = optimization.get("optimization_index")
        if type(optimization_index) is not int or optimization_index != index or optimization_index in seen_indexes:
            raise ValueError(f"第 {index} 个优化要点编号必须从 1 开始连续排列")
        seen_indexes.add(optimization_index)
        if not str(optimization.get("summary", "")).strip():
            raise ValueError(f"第 {index} 个优化要点缺少概述")
        locations = optimization.get("locations")
        if not isinstance(locations, list) or not locations:
            raise ValueError(f"第 {index} 个优化要点至少需要一个改动位置")
        for location_index, location in enumerate(locations, 1):
            if not isinstance(location, dict):
                raise ValueError(f"优化 {index} 的第 {location_index} 个位置格式无效")
            for field in ("sentence_index", "original", "revised", "reason"):
                if field not in location or location[field] in (None, ""):
                    raise ValueError(f"优化 {index} 的第 {location_index} 个位置缺少 {field}")


def _verify_result_semantics(result: dict) -> None:
    _verify_integer_score_contract(result)
    quality = result.get("quality_checks", {})
    # A grade should remain deliverable when the submitted essay violates the
    # requested word limit, so the report can surface that violation. Writing
    # and polishing still require the final text to satisfy the limit.
    if quality and quality.get("within_word_limit") is False and result.get("mode") != "grade":
        raise ValueError("作文不满足词数限制")
    if result.get("mode") == "write" and result.get("target_score") is not None:
        if not quality.get("target_match", False):
            raise ValueError("作文未落入目标分数区间")
    if result.get("mode") == "grade":
        score = result.get("score", {})
        if score.get("value", 0) < 0 or score.get("value", 0) > score.get("full_score", 0):
            raise ValueError("批改分数超出满分范围")
        target = result.get("target_info", {}).get("target")
        if target is not None:
            expected_gap = score["value"] - target
            if result.get("target_info", {}).get("gap") != expected_gap:
                raise ValueError("目标分差与实际分数不一致")
    if result.get("mode") == "polish":
        _validate_polish_changes(result.get("changes", []), result.get("original_essay"), result.get("polished_essay"))
        delta = result.get("score_delta", {}).get("delta")
        if not isinstance(delta, (int, float)):
            raise ValueError("润色结果缺少有效分数变化")
        before = result.get("score_before", {}).get("value")
        after = result.get("score_after", {}).get("value")
        if after - before != delta:
            raise ValueError("润色前后分差计算不一致")
        if result.get("improvement_status") == "improved" and delta < 1:
            raise ValueError("润色标记为 improved 但分数提升不足 1 分")
        if result.get("improvement_status") in {"unchanged", "regressed"} and not result.get("no_improvement_reason"):
            raise ValueError("分数未提升但缺少原因说明")


def _verify_integer_score_contract(result: dict) -> None:
    """Reject fractional score values even when a JSON schema accepts them."""
    mode = result.get("mode")

    def require_integer(value: object, field: str, minimum: int = 0) -> None:
        if type(value) is not int or value < minimum:
            raise ValueError(f"{field} 必须是整数分数")

    if mode == "write":
        expectation = result.get("score_expectation", {})
        require_integer(expectation.get("full_score"), "score_expectation.full_score", 1)
        if result.get("target_score") is not None:
            require_integer(result.get("target_score"), "target_score")
        band = result.get("target_band")
        if band:
            for key in ("target", "lower", "upper", "tolerance"):
                require_integer(band.get(key), f"target_band.{key}")
        preview = result.get("score_preview", {})
        if "value" in preview:
            _verify_score_block(preview, "score_preview")
    elif mode == "grade":
        _verify_score_block(result.get("score", {}), "score")
        info = result.get("target_info", {})
        if info.get("target") is not None:
            require_integer(info.get("target"), "target_info.target")
            require_integer(info.get("gap"), "target_info.gap", minimum=-10**9)
        band = info.get("band")
        if band:
            for key in ("target", "lower", "upper", "tolerance"):
                require_integer(band.get(key), f"target_info.band.{key}")
    elif mode == "polish":
        _verify_score_block(result.get("score_before", {}), "score_before")
        _verify_score_block(result.get("score_after", {}), "score_after")
        delta = result.get("score_delta", {})
        for key in ("before", "after", "delta"):
            require_integer(delta.get(key), f"score_delta.{key}")


def _verify_score_block(score: dict, field: str) -> None:
    if not isinstance(score, dict):
        raise ValueError(f"{field} 缺少评分对象")
    for key in ("value", "full_score"):
        value = score.get(key)
        if type(value) is not int or value < 0 or (key == "full_score" and value <= 0):
            raise ValueError(f"{field}.{key} 必须是整数分数")
    if score["value"] > score["full_score"]:
        raise ValueError(f"{field}.value 超出满分范围")
    for index, item in enumerate(score.get("dimension_scores", [])):
        for key in ("score", "max_score"):
            value = item.get(key)
            if type(value) is not int or value < 0 or (key == "max_score" and value <= 0):
                raise ValueError(f"{field}.dimension_scores[{index}].{key} 必须是整数分数")
        if item["score"] > item["max_score"]:
            raise ValueError(f"{field}.dimension_scores[{index}].score 超出维度满分")


def _resume(root: Path, run_id: str, req_override: dict | None = None, input_path: Path | None = None) -> dict:
    state = _store(root, run_id).load()
    if state["status"] == "completed": return state
    req = read_json(_run_dir(root, run_id) / "request.json")
    if req_override is not None:
        req = {**req, **req_override}
        if state["status"].startswith("paused_"):
            state = _store(root, run_id).transition(state, "archiving_agent_input", stage="archiving_agent_input")
        archive_json_input(input_path or Path("<resume-input>"), _run_dir(root, run_id) / "agent-response.json", req_override)
        write_json(_run_dir(root, run_id) / "request.json", req)
    if state["status"].startswith("paused_"):
        state = _store(root, run_id).transition(state, "prepared", stage="prepared", details={"resumed_from": state["status"]})
    return _run(root, state["mode"], req, run_id, state)


def _verify(root: Path, run_id: str) -> dict:
    state = _store(root, run_id).load()
    if state.get("status") != "completed": raise ValueError(f"运行尚未完成：{state.get('status')}")
    if state.get("mode") == "create-tool": _verify_tool_bundle(_tool_dir(root, state["current_object_id"]))
    else:
        out = Path(state["output_path"]); result = read_json(out / "result.json"); validate_json_schema(result, OUT_SCHEMA); _verify_result_semantics(result)
        markdown = out / "result.md"
        if not markdown.is_file(): raise ValueError("结果 Markdown 缺失")
        _verify_rendered_markdown(result, markdown.read_text(encoding="utf-8"))
    return {"status": "passed", "run_id": run_id, "state": state}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(); p.add_argument("command", choices=["list-tools", "create-tool", "write", "grade", "polish", "deliver", "start", "status", "resume", "verify"]); p.add_argument("--root", default=str(ROOT)); p.add_argument("--input"); p.add_argument("--mode"); p.add_argument("--run-id"); p.add_argument("--output-format", choices=["json", "markdown"], default="json", help="completed write/grade/polish runs: emit canonical result.md instead of the JSON receipt"); a = p.parse_args(argv); root = Path(a.root).resolve()
    try:
        if a.command == "list-tools": result = list_tools(root)
        elif a.command in {"create-tool", "write", "grade", "polish", "deliver", "start"}:
            if not a.input: raise ValueError("必须提供 --input")
            if a.command == "deliver" and a.mode not in {"write", "grade", "polish"}: raise ValueError("deliver 必须指定 --mode write|grade|polish")
            mode = a.mode or ("create-tool" if a.command in {"create-tool", "start"} else a.command); result = run_mode(root, mode, read_json(Path(a.input)))
        elif not a.run_id: raise ValueError("必须提供 --run-id")
        elif a.command == "status": result = read_json(_state_path(root, a.run_id))
        elif a.command == "resume": result = _resume(root, a.run_id, read_json(Path(a.input)) if a.input else None, Path(a.input) if a.input else None)
        else: result = _verify(root, a.run_id)
        _emit_cli_result(result, "markdown" if a.command == "deliver" else a.output_format, allow_markdown=a.command in {"write", "grade", "polish", "deliver", "start"}); return 3 if str(result.get("status", "")).startswith("paused_") else 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)); return 4 if a.command == "verify" else 2


if __name__ == "__main__": raise SystemExit(main())
