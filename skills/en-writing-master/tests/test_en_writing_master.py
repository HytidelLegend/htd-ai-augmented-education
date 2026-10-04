from __future__ import annotations
import json, sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import cli

def request(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def agent_review(essay: str, value: int = 8, *, organization: bool = True) -> dict:
    """Build a complete Agent-authored review for the current scoring contract."""
    sentence_list = cli.sentences(essay)
    paragraphs = [part.strip() for part in essay.split("\n\n") if part.strip()]
    review = {
        "value": value,
        "dimension_scores": [
            {"id": "content", "score": 3, "reason": "主题相关，立场基本明确。"},
            {"id": "organization", "score": 3, "reason": "结构基本连贯，衔接仍可加强。"},
            {"id": "language", "score": 2, "reason": "存在可改进的语言表达。"},
        ],
        "overall_evaluation": "文章回应主题并表达了基本观点。",
        "sentence_reviews": [
            {"sentence_index": index, "original": sentence, "status": "revise" if index == 1 else "keep",
             "evaluation": "需要进一步检查表达。" if index == 1 else "句意清楚。",
             "suggestion": "检查语法和搭配。" if index == 1 else "",
             **({"revised": sentence} if index == 1 else {})}
            for index, sentence in enumerate(sentence_list, 1)
        ],
    }
    if organization:
        review["organization_analysis"] = {
            "paragraph_count": len(paragraphs), "recommended_paragraph_count": 3,
            "status": "基本连贯", "summary": "段落组织基本完整，论证可继续展开。",
            "paragraph_reviews": [{"paragraph_index": index, "role": "论证段", "status": "基本合适", "evidence": paragraph[:40]}
                                  for index, paragraph in enumerate(paragraphs, 1)],
            "logic_issues": [], "suggestions": ["补充更具体的论据。"],
        }
    return review

def test_all_primary_interfaces(tmp_path: Path, capsys) -> None:
    assert cli.main(["list-tools", "--root", str(tmp_path)]) == 0
    req=request(tmp_path/"tool.json",{"tool_id":"demo-tool","tool_name":"示例工具","source_type":"inline","content":"Use a clear thesis and coherent paragraphs."})
    assert cli.main(["create-tool","--root",str(tmp_path),"--input",str(req)]) == 0
    assert cli.list_tools(tmp_path)["tools"] == ["demo-tool"]
    essay = "Education matters."
    for mode, extra in (("write", {"topic": "Education"}),
                        ("grade", {"essay": essay, "agent_review": agent_review(essay)}),
                        ("polish", {"essay": essay, "polished_essay": essay,
                                    "agent_review": {**agent_review(essay, organization=False),
                                                     "before": agent_review(essay, organization=False),
                                                         "after": agent_review(essay), "changes": []}})):
        inp=request(tmp_path/f"{mode}.json",{"tool_id":"demo-tool",**extra})
        assert cli.main([mode,"--root",str(tmp_path),"--input",str(inp)]) == 0
    latest = sorted((tmp_path / "logs" / "en-writing-master" / "runs").iterdir())[-1].name
    assert cli.main(["status", "--root", str(tmp_path), "--run-id", latest]) == 0
    assert cli.main(["verify", "--root", str(tmp_path), "--run-id", latest]) == 0
    assert cli.main(["resume", "--root", str(tmp_path), "--run-id", latest]) == 0
    start_req = request(tmp_path / "start.json", {"tool_id": "demo-tool", "topic": "Education"})
    assert cli.main(["start", "--mode", "write", "--root", str(tmp_path), "--input", str(start_req)]) == 0
    capsys.readouterr()


def test_markdown_output_format_is_exactly_the_published_template(tmp_path: Path, capsys) -> None:
    req = {"tool_id": "demo-markdown", "tool_name": "示例工具", "source_type": "inline", "content": "Use a clear thesis and coherent paragraphs."}
    assert cli.run_mode(tmp_path, "create-tool", req)["status"] == "completed"
    input_path = request(tmp_path / "write.json", {
        "tool_id": "demo-markdown",
        "topic": "Education",
        "full_score": 15,
        "target_score": 8,
        "requirements": {"word_count_min": 120, "word_count_max": 180},
        "agent_review": agent_review("Education matters.", organization=False),
    })

    assert cli.main([
        "write", "--root", str(tmp_path), "--input", str(input_path), "--output-format", "markdown"
    ]) == 0
    emitted = capsys.readouterr().out
    runs = sorted((tmp_path / "logs" / "en-writing-master" / "runs").iterdir())
    states = [json.loads((run / "state.json").read_text(encoding="utf-8")) for run in runs]
    state = next(item for item in reversed(states) if item["mode"] == "write")
    published = Path(state["output_path"]) / "result.md"

    assert emitted == published.read_text(encoding="utf-8")
    assert emitted.startswith("# 英语作文\n")
    assert "## 预期得分与原因" in emitted
    assert "## 使用到的高级词汇、短语、句型、表达" in emitted

    for mode, payload, heading in (
        ("grade", {"tool_id": "demo-markdown", "topic": "Education", "essay": "Education matters.", "agent_review": agent_review("Education matters.")}, "# 批改结果\n"),
        ("polish", {"tool_id": "demo-markdown", "topic": "Education", "essay": "Education matters.", "polished_essay": "Education matters.", "agent_review": {**agent_review("Education matters.", organization=False), "before": agent_review("Education matters.", organization=False), "after": agent_review("Education matters."), "changes": []}}, "# 润色版本\n"),
    ):
        mode_input = request(tmp_path / f"{mode}.json", payload)
        assert cli.main([mode, "--root", str(tmp_path), "--input", str(mode_input), "--output-format", "markdown"]) == 0
        mode_emitted = capsys.readouterr().out
        states = [json.loads((run / "state.json").read_text(encoding="utf-8")) for run in sorted((tmp_path / "logs" / "en-writing-master" / "runs").iterdir())]
        mode_state = next(item for item in reversed(states) if item["mode"] == mode)
        assert mode_emitted == (Path(mode_state["output_path"]) / "result.md").read_text(encoding="utf-8")
        assert mode_emitted.startswith(heading)
        assert "## 使用到的高级词汇、短语、句型、表达" in mode_emitted

    deliver_input = request(tmp_path / "deliver.json", {
        "tool_id": "demo-markdown", "topic": "Education", "essay": "Education matters.", "agent_review": agent_review("Education matters.")
    })
    assert cli.main([
        "deliver", "--mode", "grade", "--root", str(tmp_path), "--input", str(deliver_input)
    ]) == 0
    delivered = capsys.readouterr().out
    assert delivered.startswith("# 批改结果\n")
    assert "| 类型 | 表达 | 中文含义 | 使用案例 |" in delivered


def test_language_items_only_report_matches_and_render_fallback_is_explicit(tmp_path: Path) -> None:
    req = {"tool_id": "demo-language", "tool_name": "示例工具", "source_type": "inline", "content": "competitive advantage 竞争优势"}
    assert cli.run_mode(tmp_path, "create-tool", req)["status"] == "completed"
    graded = cli.run_mode(tmp_path, "grade", {"tool_id": "demo-language", "essay": "Education matters.", "agent_review": agent_review("Education matters.")})
    assert graded["status"] == "completed"
    assert graded["result"]["language_items"] == []
    output = Path(graded["output_path"]) / "result.md"
    assert "未匹配到工具词库表达" in output.read_text(encoding="utf-8")

def test_rendered_language_table_has_consistent_columns_and_escaped_pipes() -> None:
    result = {
        "mode": "grade",
        "language_items": [{
            "type": "phrase",
            "expression": "a | b",
            "meaning_zh": "含义",
            "usage_case": "A\nB",
            "used_in_text": True,
        }],
        "score": {"full_score": 15, "value": 8, "dimension_scores": []},
        "target_info": {},
        "overall_evaluation": "评价",
        "improvement_advice": "建议",
        "sentence_reviews": [],
        "organization_analysis": {
            "paragraph_count": 1, "recommended_paragraph_count": 3, "status": "基本连贯",
            "summary": "段落结构简单。",
            "paragraph_reviews": [{"paragraph_index": 1, "role": "主体", "status": "基本合适", "evidence": "Education matters."}],
            "logic_issues": [], "suggestions": ["补充论据。"],
        },
    }
    rendered = cli.render_result(result)
    cli._verify_rendered_markdown(result, rendered)
    assert "a \\| b" in rendered
    assert "A<br>B" in rendered


def test_missing_tool_and_missing_full_score_pause(tmp_path: Path) -> None:
    state=cli.run_mode(tmp_path,"write",{"tool_id":"absent","topic":"x"})
    assert state["status"] == "paused_missing_tool"
    req={"tool_id":"demo","tool_name":"Demo","source_type":"inline","content":"Rule"}
    assert cli.run_mode(tmp_path,"create-tool",req)["status"] == "completed"
    state=cli.run_mode(tmp_path,"write",{"tool_id":"demo","topic":"x","target_score":12})
    assert state["status"] == "paused_missing_full_score"

def test_grade_polish_and_language_contract(tmp_path: Path) -> None:
    req={"tool_id":"demo","tool_name":"Demo","source_type":"inline","content":"Use a clear thesis and coherent paragraphs."}
    assert cli.run_mode(tmp_path,"create-tool",req)["status"] == "completed"
    essay = "In my opinion, a master degree is useful for students. First, it gives deeper knowledge and more chances to find a good job. For example, some companies prefer candidates with a master's degree. However, a degree cannot guarantee success. Students also need strong practical ability and a right attitude. In brief, effort and experience are important in the long run."
    graded=cli.run_mode(tmp_path,"grade",{"tool_id":"demo","topic":"master degree job","essay":essay,"agent_review":agent_review(essay)})
    assert graded["status"] == "completed"
    result=graded["result"]
    assert result["score"]["full_score"] == 15
    assert result["score"]["dimension_scores"]
    assert result["sentence_reviews"][0]["status"] == "revise"
    assert all(set(("type","expression","meaning_zh","usage_case")) <= set(x) for x in result["language_items"])
    polished_essay = essay.replace("a master degree", "a master's degree")
    polished=cli.run_mode(tmp_path,"polish",{"tool_id":"demo","topic":"master degree job","essay":essay,"polished_essay":polished_essay,"feedback":"修正冠词、搭配和固定表达","agent_review":{**agent_review(essay, organization=False),"before":agent_review(essay, 8, organization=False),"after":agent_review(polished_essay, 9),"changes":[{"optimization_index":1,"summary":"修正冠词","locations":[{"sentence_index":1,"original":"a master degree","revised":"a master's degree","reason":"补充所有格标记"}]}]}})
    assert polished["status"] == "completed"
    assert polished["result"]["changes"]
    assert "master's degree" in polished["result"]["polished_essay"]
    assert "master's graduate" not in polished["result"]["polished_essay"]
    assert polished["result"]["score_delta"]["delta"] >= 1


def test_polish_reports_unchanged_reason_and_grade_lineage(tmp_path: Path) -> None:
    req={"tool_id":"demo","tool_name":"Demo","source_type":"inline","content":"Use a clear thesis and coherent paragraphs."}
    assert cli.run_mode(tmp_path,"create-tool",req)["status"] == "completed"
    essay = "Education matters."
    graded=cli.run_mode(tmp_path,"grade",{"tool_id":"demo","essay":essay,"agent_review":agent_review(essay)})
    polished=cli.run_mode(tmp_path,"polish",{"tool_id":"demo","essay":essay,"polished_essay":essay,"grade_run_id":graded["run_id"],"agent_review":{**agent_review(essay, organization=False),"before":agent_review(essay, organization=False),"after":agent_review(essay),"changes":[]}})
    assert polished["status"] == "completed"
    result=polished["result"]
    assert result["provenance"]["grade_run_id"] == graded["run_id"]
    assert result["improvement_status"] == "unchanged"
    assert result["no_improvement_reason"]


def test_target_band_and_quality_checks(tmp_path: Path) -> None:
    req={"tool_id":"demo","tool_name":"Demo","source_type":"inline","content":"Use a clear thesis and coherent paragraphs."}
    assert cli.run_mode(tmp_path,"create-tool",req)["status"] == "completed"
    written=cli.run_mode(tmp_path,"write",{
        "tool_id":"demo", "topic":"master degree job", "full_score":15, "target_score":8,
        "requirements":{"word_count_min":120,"word_count_max":180},
        "agent_review": agent_review("Education matters.", 8, organization=False),
    })
    assert written["status"] == "completed"
    result=written["result"]
    assert result["target_band"] == {"target":8,"lower":7,"upper":9,"tolerance":1}
    assert result["quality_checks"]["target_match"] is True
    assert result["score_preview"]["value"] in {7, 8, 9}
    assert cli._target_info({"target_score": 8, "target_tolerance": 0}, 15)[1]["tolerance"] == 0


def test_scores_are_integers_with_half_up_rounding(tmp_path: Path) -> None:
    req={"tool_id":"demo","tool_name":"Demo","source_type":"inline","content":"Use a clear thesis and coherent paragraphs."}
    assert cli.run_mode(tmp_path,"create-tool",req)["status"] == "completed"
    graded=cli.run_mode(tmp_path,"grade",{"tool_id":"demo","essay":"Education matters.","agent_review":agent_review("Education matters.")})
    result=graded["result"]
    assert type(result["score"]["value"]) is int
    assert all(type(item["score"]) is int and type(item["max_score"]) is int for item in result["score"]["dimension_scores"])
    assert cli.integer_score("7.5", field="test", round_fraction=True) == 8
    polished=cli.run_mode(tmp_path,"polish",{"tool_id":"demo","essay":"Education matters.","polished_essay":"Education matters.","agent_review":{**agent_review("Education matters.", organization=False),"before":agent_review("Education matters.", organization=False),"after":agent_review("Education matters."),"changes":[]}})
    delta=polished["result"]["score_delta"]
    assert all(type(delta[key]) is int for key in ("before", "after", "delta"))


def test_grade_reports_spelling_issues_and_hides_internal_metadata(tmp_path: Path) -> None:
    req = {"tool_id": "demo-issues", "tool_name": "Demo", "source_type": "inline", "content": "Use a clear thesis and coherent paragraphs."}
    assert cli.run_mode(tmp_path, "create-tool", req)["status"] == "completed"
    essay = "A higher degree is usually neccessary. A student may feel lonely at the begining."
    graded = cli.run_mode(tmp_path, "grade", {"tool_id": "demo-issues", "target_score": 8, "full_score": 15, "essay": essay, "agent_review": agent_review(essay)})
    assert graded["status"] == "completed"
    result = graded["result"]
    assert any(item["status"] == "revise" for item in result["sentence_reviews"])
    rendered = Path(graded["output_path"], "result.md").read_text(encoding="utf-8")
    assert "## 改进建议" in rendered
    assert "neccessary" in rendered
    assert "质量检查：{" not in rendered
    assert "运行血缘：{" not in rendered
    assert "## 使用到的高级词汇、短语、句型、表达" in rendered


def test_writing_scope_excludes_translation_and_writes_manifest_bundle(tmp_path: Path) -> None:
    source = """# 四****级****写****作****考****前****预****测

### 题目

Directions: write an essay.

（二）参考译文

这是一段不应进入写作工具的翻译。

（三）亮点词汇

competitive advantage 竞争优势

# 四****级****翻****译****考****前****预****测

### 翻译题

这整章都应排除。
"""
    state = cli.run_mode(tmp_path, "create-tool", {"tool_id": "scope-tool", "tool_name": "Scope", "source_type": "markdown", "content": source, "ignore_translation": True})
    assert state["status"] == "completed"
    tool_dir = tmp_path / "skills" / "en-writing-master" / "tools" / "scope-tool"
    assert {p.name for p in tool_dir.iterdir()} == {"tool.json", "TOOL.md", "writing-rules.md", "scoring-rubric.md", "language-bank.md", "source-notes.md"}
    tool = json.loads((tool_dir / "tool.json").read_text(encoding="utf-8"))
    assert tool["scoring"]["levels"]
    assert any(level["min"] <= 8 <= level["max"] for level in tool["scoring"]["levels"])
    joined = "\n".join(item["content"] for item in tool["writing_rules"])
    assert "翻译" not in joined and "参考译文" not in joined
    assert tool["source"]["selection_audit"]["removed_translation_subsections"] == 1
    counts = tool["source"]["selection_audit"]["counts"]
    assert counts["source_blocks"] == counts["included_blocks"] + counts["excluded_blocks"] + counts["ignored_before_writing_scope"]
    assert tool["language_bank"]["phrases"][0]["expression"] == "competitive advantage"
    assert tool["language_bank"]["phrases"][0]["meaning_zh"] == "竞争优势"
    assert not any(item["usage_case"].startswith("Use ") for values in tool["language_bank"].values() for item in values)
    assert state["completed_steps"][-1] == "completed"
    assert cli.main(["verify", "--root", str(tmp_path), "--run-id", state["run_id"]]) == 0


def test_translation_chapter_split_across_plain_and_markdown_titles_is_excluded(tmp_path: Path) -> None:
    source = """第1章 四级写作考前预测
# 第1章
# 四级写作考前预测
## 一 情景作文：教育
Directions: Write an essay about education and give a concrete example.
（三）亮点词汇
postgraduate program 研究生教育
in mounting numbers 越来越多的
（五）句型拓展
① Education opens the door to opportunity. 教育开启机会之门。

第2章 四级翻译考前预测
# 第2章
# 四级翻译考前预测
## 第一节
### 一 历史：秦始皇
Directions: translate a passage from Chinese into English.
秦始皇是秦代的第一位皇帝。

附录 2023年新增写作预测范文
# 附录
# 2023年新增写作预测范文
## 一 应用文：求职信
Directions: Write an email of application with enough supporting details.
"""
    state = cli.run_mode(tmp_path, "create-tool", {"tool_id": "chapter-scope", "tool_name": "Scope", "source_type": "markdown", "content": source, "ignore_translation": True})
    assert state["status"] == "completed"
    tool = json.loads((tmp_path / "skills/en-writing-master/tools/chapter-scope/tool.json").read_text(encoding="utf-8"))
    titles = [item["title"] for item in tool["writing_rules"]]
    assert "第2章" not in titles
    assert not any("翻译" in title for title in titles)
    assert "一 应用文：求职信" in titles
    assert all(item["content"].strip() and item["content"].splitlines()[0].lstrip("# ") != item["title"] for item in tool["writing_rules"])
    phrases = {item["expression"]: item for item in tool["language_bank"]["phrases"]}
    assert phrases["postgraduate program"]["meaning_zh"] == "研究生教育"
    assert phrases["in mounting numbers"]["meaning_zh"] == "越来越多的"
    patterns = tool["language_bank"]["sentence_patterns"]
    assert any(item["expression"] == "Education opens the door to opportunity." and item["meaning_zh"] == "教育开启机会之门。" for item in patterns)
