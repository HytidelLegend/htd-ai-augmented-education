"""Script-first, resumable workflow for mnemonic memory scenes."""
from __future__ import annotations
import argparse, hashlib, json, re, shutil, sys, uuid
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utils" / "scripts"))
from structured_io import read_json, validate_json_schema, write_json
from timestamp import iso_timestamp
from workflow_state import WorkflowDefinition, WorkflowStateStore

WORKFLOW = "build-mnemonic-keywords"
SKILL_ROOT = Path(__file__).resolve().parents[1]
REQ_SCHEMA = SKILL_ROOT / "references/request.schema.json"
OUT_SCHEMA = SKILL_ROOT / "references/output.schema.json"
AGENT_SCHEMA = SKILL_ROOT / "references/agent-response.schema.json"
PRINCIPLES_REF = ROOT / "utils/references/mnemonic-association-principles.md"
TRANSITIONS = {
    "prepared": {"validating_request"},
    "validating_request": {"normalizing_sentence", "paused_input"},
    "normalizing_sentence": {"loading_principles_reference"},
    "loading_principles_reference": {"extracting_keywords", "paused_input"},
    "extracting_keywords": {"building_generation_packet"},
    "building_generation_packet": {"paused_agent_generation"},
    "paused_agent_generation": {"validating_agent_response"},
    "validating_agent_response": {"composing_result", "paused_agent_response"},
    "composing_result": {"self_checking"},
    "self_checking": {"publishing_prompt_preview", "paused_quality_review"},
    "publishing_prompt_preview": {"preview_ready"},
    "preview_ready": {"paused_image_confirmation"},
    "paused_image_confirmation": {"invoking_imagegen", "publishing"},
    "invoking_imagegen": {"verifying_image_result", "paused_image_unavailable"},
    "verifying_image_result": {"publishing", "paused_quality_review"},
    "publishing": {"completed"},
}
for p in ("paused_input", "paused_agent_response", "paused_quality_review", "paused_image_unavailable"):
    TRANSITIONS[p] = {"prepared"}
TRANSITIONS["paused_image_unavailable"] = {"invoking_imagegen", "prepared"}
DEFINITION = WorkflowDefinition.build(name=WORKFLOW, transitions=TRANSITIONS)

def log_dir(root, run_id): return root / "logs" / WORKFLOW / "runs" / run_id
def out_dir(root, run_id): return root / "outputs" / WORKFLOW / "runs" / run_id
def store(root, run_id): return WorkflowStateStore(root=root, workflow=WORKFLOW, run_id=run_id, definition=DEFINITION, run_dir=log_dir(root, run_id), schema_path=ROOT / "utils/references/workflow-state-v1.schema.json")
def state_new(run_id):
    now = iso_timestamp(); return {"schema_version":"1.0","workflow":WORKFLOW,"run_id":run_id,"status":"prepared","current_stage":"prepared","resume_stage":None,"current_object_id":None,"current_batch_id":None,"completed_steps":[],"pending_decisions":[],"error":None,"created_at":now,"updated_at":now,"last_heartbeat_at":now,"event_sequence":0}
def advance(root, state, target, **updates): return store(root, state["run_id"]).transition(state, target, stage=target, completed_step=target, updates=updates)
def pause(root, state, status, message, resume_stage): return store(root, state["run_id"]).pause(state, status=status, error_code=status.removeprefix("paused_"), message=message, resume_stage=resume_stage)

def extract_keywords(sentence: str):
    # Deterministic candidate extraction; Agent chooses final instructional keywords.
    tokens = re.findall(r"[\u4e00-\u9fff]{1,8}|[A-Za-z][A-Za-z'-]*|\d+(?:\.\d+)?", sentence)
    stop = set("我国中国的和是为从到依次以及一个一种这个那个需要可以进行关于主要基本严格科学全民" )
    seen=[]
    for tok in tokens:
        if tok in stop or len(tok)==1 and tok in "的和是为从到": continue
        if tok not in seen: seen.append(tok)
    return [{"text": t, "source":"candidate", "role":"待判断", "familiar_object":"待转换"} for t in seen[:24]]

def sentence_valid(s):
    if not s.strip(): return False, "句子不能为空"
    # Newlines are normalized, but more than one terminal mark signals multiple sentences.
    compact = re.sub(r"\s+", " ", s).strip()
    terminals = re.findall(r"[。！？!?]", compact)
    if len(terminals) > 1: return False, "本版本仅接受一句，请合并为一个句子"
    return True, compact

def load_principles_reference():
    if not PRINCIPLES_REF.is_file():
        raise FileNotFoundError(f"共享记忆原则不存在：{PRINCIPLES_REF}")
    content = PRINCIPLES_REF.read_text(encoding="utf-8")
    if not content.strip():
        raise ValueError("共享记忆原则为空")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()

def write_packet(root, run_id, req, keywords, principles_sha256):
    packet = {"run_id":run_id,"input":req,"keyword_candidates":keywords,"instructions":{
        "one_sentence_only":True,"use_familiar_objects":True,"use_at_least_two_senses":True,
        "construction_steps":["提取关键词或代表词","适当调整要点顺序","编句子或故事","自检关键词覆盖"],
        "cue_rule":"保留完整词就覆盖完整词；保留代表字词就覆盖代表字词；每项必须提供 mnemonic_cue 且该片段必须原样出现在 mnemonic 中",
        "keyword_mapping_required":True,"scene_must_recover_source_sentence":True,
        "prefer_keywords_or_short_phrases":True,"shorten_long_terms":True,"concretize_abstract_terms":True,
        "reject_full_sentence_subpoints":True,"prefer_common_familiar_language":True,"require_logical_coherence":True,
        "preserve_hard_order":True,"allow_reorder_when_no_required_order":True,
        "allow_humorous_odd_or_sexual_association":True,"self_check_required":True,
        "scene_principles":["color","imagination","rhythm","dynamic","sensory","sexual_association","sequence_logic","coding","dimensionality"],
        "reference":"utils/references/mnemonic-association-principles.md",
        "reference_sha256":principles_sha256,"examples_reference":"skills/build-mnemonic-keywords/references/mnemonic-examples.md"}}
    write_json(log_dir(root, run_id)/"generation_packet.json", packet)

def render_prompt(req, sentence, memory):
    model = req.get("model", "image-2"); ratio=req.get("aspect_ratio", "4:3")
    negative = "抽象难以辨认的画面，过多无关物体，乱码，错误文字，水印，血腥，未成年人性内容，无法分辨的主体"
    prompt = memory["image_prompt"].strip()
    if not prompt: raise ValueError("image_prompt 不能为空")
    return {"model":model,"positive_prompt":prompt,"negative_prompt":negative,"aspect_ratio":ratio}

def build_result(root, req, sentence, keywords, response, run_id):
    selected = response.get("selected_keywords") or [x["text"] for x in keywords]
    mappings = response["keyword_mappings"]
    by_keyword = {x["keyword"]: x for x in mappings}
    keywords = []
    coverage_errors = []
    for x in selected:
        mapping = by_keyword.get(x)
        if not mapping:
            raise ValueError(f"keyword_mappings 缺少关键词：{x}")
        cue = mapping["mnemonic_cue"]
        if cue not in response["mnemonic"]:
            coverage_errors.append(f"{x} 的口诀片段“{cue}”未出现在 mnemonic 中")
        keywords.append({"text": x, "source":"agent_selected", "role":mapping["role"], "familiar_object":mapping["familiar_object"], "scene_action":mapping["scene_action"], "mnemonic_cue":cue, "cue_type":mapping["cue_type"]})
    check = dict(response["self_check"])
    principle_errors = []
    for principle, item in check.get("principle_checks", {}).items():
        if item.get("applicable") and not item.get("passed"):
            principle_errors.append(f"原则 {principle} 未通过：{item.get('reason', '未提供原因')}")
    if principle_errors:
        check["fit"] = "weak" if check["fit"] == "good" else check["fit"]
        check["reason"] = "；".join([check.get("reason", "").strip(), *principle_errors]).strip("；")
        check["recommendation"] = "请改用更常见、逻辑更通顺且负担更低的关键词、顺口溜、分类、编码或重复复述。"
    if coverage_errors:
        check["fit"] = "weak"
        check["reason"] = "；".join(coverage_errors)
        check["recommendation"] = "没有找到自然顺口且覆盖全部关键词的句子。请只记这些关键词，或改用分组、节奏朗读和遮盖复述。"
    if check["fit"] in {"weak", "bad"} and "不适合" not in check["recommendation"]:
        check["recommendation"] = "提示：这个例子不适合直接使用联想法来记忆。" + check["recommendation"]
    good = check["fit"] == "good"
    prompt = render_prompt(req, sentence, response) if good else {"model":req.get("model", "image-2"),"positive_prompt":"","negative_prompt":"","aspect_ratio":req.get("aspect_ratio", "4:3")}
    return {"status":"paused","run_id":run_id,"input":{"sentence":sentence,"subject":req.get("subject"),"sha256":hashlib.sha256(sentence.encode()).hexdigest()},"keywords":keywords,"memory":{"mnemonic":response["mnemonic"] if good else "","scene":response["scene"] if good else "","sensory_hooks":response["sensory_hooks"] if good else [],"order_note":response["order_note"] if good else ""},"prompt":prompt,"self_check":check,"image":{"decision":"not_applicable" if not good else "pending","invoked":False,"command":"/imagegen"},"publication":{}}

def write_outputs(root, result):
    d=out_dir(root,result["run_id"]); d.mkdir(parents=True,exist_ok=True)
    m=(SKILL_ROOT/"templates/result.template.md").read_text(encoding="utf-8")
    r=result; mappings="；".join(f"{x['text']} → 口诀片段“{x['mnemonic_cue']}” → {x['familiar_object']}（{x['role']}；{x['scene_action']}）" for x in r["keywords"]); hooks="；".join(r["memory"]["sensory_hooks"])
    warning = "**提示：这个例子不适合用联想记忆法。** " + r["self_check"]["recommendation"] if r["self_check"]["fit"] in {"weak", "bad"} else ""
    if r["self_check"]["fit"] in {"weak", "bad"}:
        m = "## 原句\n\n" + r["input"]["sentence"] + "\n\n## 提取出的关键词\n\n" + "、".join(x["text"] for x in r["keywords"]) + "\n\n" + warning + "\n"
        write_json(d/"result.json",r); (d/"result.md").write_text(m,encoding="utf-8")
        r["publication"]={"result_json":str(d/"result.json"),"result_md":str(d/"result.md")}; write_json(d/"result.json",r)
        return
    vals={"sentence":r["input"]["sentence"],"keyword_mappings":mappings,"mnemonic":r["memory"]["mnemonic"],"scene":r["memory"]["scene"],"sensory_hooks":hooks,"order_note":r["memory"]["order_note"],"model":r["prompt"]["model"],"aspect_ratio":r["prompt"]["aspect_ratio"],"positive_prompt":r["prompt"]["positive_prompt"],"negative_prompt":r["prompt"]["negative_prompt"],"fit":r["self_check"]["fit"],"reason":r["self_check"]["reason"],"recommendation":r["self_check"]["recommendation"],"image_result":r["image"].get("image_path") or r["image"].get("image_url") or "等待用户决定","final_warning":warning}
    for key,val in vals.items(): m=m.replace("{{ "+key+" }}",str(val))
    write_json(d/"result.json",r); (d/"result.md").write_text(m.rstrip()+"\n",encoding="utf-8")
    r["publication"]={"result_json":str(d/"result.json"),"result_md":str(d/"result.md")}; write_json(d/"result.json",r)

def cmd_start(args):
    root=Path(args.root).resolve(); req=read_json(Path(args.input).resolve()); validate_json_schema(req,REQ_SCHEMA)
    ok,s=sentence_valid(req["sentence"])
    run_id=uuid.uuid4().hex[:12]; state=state_new(run_id); store(root,run_id).create(state)
    if not ok:
        state=pause(root,state,"paused_input",s,"prepared"); write_json(log_dir(root,run_id)/"request.json",req); print(json.dumps({"status":state["status"],"run_id":run_id,"message":s},ensure_ascii=False)); return 3
    for target in ("validating_request","normalizing_sentence","loading_principles_reference"): state=advance(root,state,target)
    principles_sha256 = load_principles_reference()
    state=advance(root,state,"extracting_keywords")
    req["sentence"]=s; kw=extract_keywords(s); state=advance(root,state,"building_generation_packet"); write_json(log_dir(root,run_id)/"request.json",req); write_packet(root,run_id,req,kw,principles_sha256)
    state=pause(root,state,"paused_agent_generation","需要 Agent 根据 generation_packet.json 生成联想方案","validating_agent_response")
    print(json.dumps({"status":state["status"],"run_id":run_id,"generation_packet":str(log_dir(root,run_id)/"generation_packet.json")},ensure_ascii=False)); return 3

def cmd_resume(args):
    root=Path(args.root).resolve(); state=store(root,args.run_id).load(); d=out_dir(root,args.run_id); log=log_dir(root,args.run_id)
    if state["status"]=="paused_agent_generation":
        if not args.input: raise ValueError("首次恢复需要 --input agent-response.json")
        response=read_json(Path(args.input).resolve()); validate_json_schema(response,AGENT_SCHEMA)
        packet=read_json(log/"generation_packet.json"); req=packet["input"]; sentence=req["sentence"]; kw=packet["keyword_candidates"]
        state=advance(root,state,"validating_agent_response"); result=build_result(root,req,sentence,kw,response,args.run_id); write_outputs(root,result)
        state=advance(root,state,"composing_result"); state=advance(root,state,"self_checking")
        if result["self_check"]["fit"] != "good":
            state=pause(root,state,"paused_quality_review","没有找到自然顺口且覆盖全部关键词的句子","prepared")
        else:
            state=advance(root,state,"publishing_prompt_preview"); state=advance(root,state,"preview_ready"); state=pause(root,state,"paused_image_confirmation","等待用户确认是否调用 /imagegen","invoking_imagegen")
        print(json.dumps({"status":state["status"],"run_id":args.run_id,"result":str(d/"result.md")},ensure_ascii=False)); return 3
    result=read_json(d/"result.json")
    if state["status"] not in {"paused_image_confirmation","paused_image_unavailable"}: raise ValueError("当前运行不在图片确认阶段")
    if args.decision=="decline":
        result["status"]="completed"; result["image"]["decision"]="declined"; state=advance(root,state,"publishing"); state=advance(root,state,"completed")
    else:
        if not args.image_path and not args.image_url:
            state=pause(root,state,"paused_image_unavailable","请先调用 /imagegen，再传入 --image-path 或 --image-url","invoking_imagegen"); print(json.dumps({"status":state["status"],"run_id":args.run_id,"command":"/imagegen"},ensure_ascii=False)); return 3
        target=None
        if args.image_path:
            src=Path(args.image_path).resolve()
            if not src.is_file(): raise FileNotFoundError(f"图片文件不存在：{src}")
            target=d/src.name
            if target.exists(): raise FileExistsError(f"输出图片已存在，不覆盖：{target}")
            d.mkdir(parents=True,exist_ok=True); shutil.copy2(src,target)
        result["status"]="completed"; result["image"].update({"decision":"accepted","invoked":True,"image_path":str(target) if target else None,"image_url":args.image_url}); state=advance(root,state,"invoking_imagegen"); state=advance(root,state,"verifying_image_result"); state=advance(root,state,"publishing"); state=advance(root,state,"completed")
    write_outputs(root,result); print(json.dumps({"status":state["status"],"run_id":args.run_id,"result":str(d/"result.md")},ensure_ascii=False)); return 0

def cmd_deliver(args):
    if args.mode != "preview": raise ValueError("deliver 目前只支持 --mode preview")
    root=Path(args.root).resolve(); state=store(root,args.run_id).load()
    if state["status"] != "paused_image_confirmation": raise ValueError("当前运行尚未准备好生图提示词")
    result=read_json(out_dir(root,args.run_id)/"result.json")
    message=(SKILL_ROOT/"templates/preview.template.md").read_text(encoding="utf-8").replace("{{ prompt }}",result["prompt"]["positive_prompt"])
    (out_dir(root,args.run_id)/"preview.md").write_text(message,encoding="utf-8")
    print(message); return 3

def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="command",required=True)
    def common(x): x.add_argument("--root",default=".")
    x=sub.add_parser("start"); common(x); x.add_argument("--input",required=True)
    x=sub.add_parser("resume"); common(x); x.add_argument("--run-id",required=True); x.add_argument("--input"); x.add_argument("--decision",choices=["accept","decline"]); x.add_argument("--image-path"); x.add_argument("--image-url")
    for name in ("status","verify"):
        x=sub.add_parser(name); common(x); x.add_argument("--run-id",required=True)
    x=sub.add_parser("deliver"); common(x); x.add_argument("--run-id",required=True); x.add_argument("--mode",choices=["preview"],required=True)
    a=p.parse_args()
    try:
        if a.command=="start": return cmd_start(a)
        if a.command=="deliver": return cmd_deliver(a)
        root=Path(a.root).resolve(); state=store(root,a.run_id).load()
        if a.command=="status": print(json.dumps(state,ensure_ascii=False)); return 0
        if a.command=="verify": validate_json_schema(read_json(out_dir(root,a.run_id)/"result.json"),OUT_SCHEMA); print(json.dumps({"status":"verified","run_id":a.run_id},ensure_ascii=False)); return 0
        if not a.decision and state["status"]!="paused_agent_generation": raise ValueError("resume 图片阶段需要 --decision")
        return cmd_resume(a)
    except Exception as e:
        print(json.dumps({"status":"error","error":str(e)},ensure_ascii=False)); return 2
if __name__=="__main__": raise SystemExit(main())
