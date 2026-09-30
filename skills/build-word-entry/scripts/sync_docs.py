"""Idempotently generate this skill's catalog row and structured scenario."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DOC = ROOT / "docs" / "Skills_说明书.md"
ROW = "| `build-word-entry` | 从单词或 UTF-8 词表生成可恢复、可校验的英语词条和批次关系 | 用户要建立词条、批量处理词表或恢复运行时 | `python skills/build-word-entry/scripts/cli.py start-word --root . --word bank` |"
SECTION = """### build-word-entry

#### 具体场景示例

```yaml
scenario_examples:
  - id: build-dictionary-entry
    user_request: "请从这份词表建立可追溯的英语词条"
    when_to_call: "用户提供单词或 UTF-8 词表并要求生成、更新词条时"
    invocation: "start-word/start-list → status → resume → verify → deliver"
    expected_output: "生成词条 JSON、带 AI 置信度标注的可读结果及可恢复的批次报告"
```

入口为 `runtime/.venv/Scripts/python.exe skills/build-word-entry/scripts/cli.py`，支持 `start-word`、`start-list`、`resume`、`status`、`verify` 和 `deliver`。文本词表一行一词；CSV 指定单词列，可选词性和释义提示列，去重后保存全部原始行号。当前不处理学龄段标签。单词与批次各有显式状态机；证据采集使用四站可见浏览器，脚本预填 `decision-template.json`，人工登录或验证码操作暂停；Agent 只校对并补足少量结构化义项判断，必要时按内容项给出证据索引。Cambridge 候选按词性保留英美 IPA 和音频来源 URL。AI 生成释义与例句通过结构检查后可入库，保留 `pending`、`confidence` 和生成方式，展示脚本追加 `（AI 生成，置信度 0.85）`。`gaps.json` 记录来源覆盖、采集截断和待补字段；词族与直接派生词分开确认，表外词只留待采集引用。正式批次结果只含输入文件名，不泄漏本地绝对路径；原始网页快照和浏览器会话不保存。正式结果位于 `outputs/build-word-entry/runs/<run-id>/`，状态与中间证据位于 `logs/build-word-entry/runs/<run-id>/`。

"""


def main() -> None:
    text = DOC.read_text(encoding="utf-8")
    if ROW not in text:
        marker = "### AI 辅助教学"
        text = text.replace(marker, ROW + "\n\n" + marker, 1)
    marker = "### en-writing-master\n"
    index = text.find(marker, text.find("## AI 辅助学习\n"))
    if index < 0:
        raise ValueError("说明书缺少 AI 辅助学习详细章节插入点")
    prior = text.find("### build-word-entry\n")
    if prior >= 0:
        text = text[:prior] + SECTION + text[index:]
    else:
        text = text[:index] + SECTION + text[index:]
    DOC.write_text(text.replace("\r\n", "\n"), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
