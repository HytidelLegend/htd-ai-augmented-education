---
name: convert-copy-to-transcript
category: common_tool
description: 将 UTF-8 Markdown、纯文本文件或直接文案确定性转换为可朗读逐字稿；脚本生成语义候选、预览、差异报告与确认回执，Agent 仅填写候选读法。
---

# 文案转逐字稿

将 UTF-8 Markdown、纯文本文件或直接文案确定性转换为可朗读逐字稿；脚本生成语义候选、预览、差异报告与确认回执，Agent 仅填写候选读法。

## 具体场景示例

```yaml
scenario_examples:
- id: convert-copy-to-transcript
  user_request: 请把这篇 Markdown 转成朗读逐字稿
  when_to_call: 用户要求把文案转换为口播稿、逐字稿或 TTS 输入时
  invocation: start →（有语义候选时 apply-decisions）→ approve → verify → deliver
  expected_output: UTF-8 逐字稿预览、差异报告、批准稿与 handoff
```

## 项目运行契约

- `--root` 指向项目根目录，使用 `runtime/.venv/Scripts/python.exe`。
- 正式产物位于 `outputs/<skill>/runs/<run-id>/`，输入快照、状态、事件、API 回执和中间文件位于 `logs/<skill>/runs/<run-id>/`；不写入 `runtime/`。
- 所有时间字段及 run ID 均调用 `utils/scripts/timestamp.py`；同秒冲突追加数字后缀。
- `start`、`status`、`resume`、`verify`、`deliver` 为统一入口；原有专用命令仍可调用。`deliver` 仅交付 completed 运行，并重新验证，不隐式批准草稿。
- CLI 退出码遵循说明书：成功 0、输入无效 2、等待决策 3、验证失败 4、运行错误 5、依赖或配置缺失 6。`status` 查询成功返回 0。
- 确认只能依据当前稿件或试听的精确 SHA-256；用户未明确确认时不能填写确认回执。普通恢复不得越过确认门禁。
- 文件输入只读；同名目标冲突不覆盖。Mock 回归不读取密钥，不访问云端。

## 跨 Skill 契约

`handoff.json` 由共享 `utils/scripts/speech_handoff.py` 生成，按 `speech-handoff-v1.schema.json` 校验。字段为 `schema_version`、`producer_skill`、`producer_run_id`，以及 `manifest`、`approved_transcript`、`approval_receipt` 各自的 `_path` 和 `_sha256`。路径相对项目根目录。

TTS 保存上游引用快照，重新读取上游状态、执行上游 `verify` 并复核所有哈希。批准稿与确认回执必须一致；上游发生变化后拒绝继续合成。不共写上游运行目录，不自动选择最新 run。

## 输入与命令

输入接受 UTF-8 `.md`／`.txt` 文件或直接文本，二者只能选一个。只做可追溯的朗读适配，不承担改稿；Agent 不重写完整逐字稿。独立输出为 `outputs/convert-copy-to-transcript/runs/<run-id>/transcript/`，不再创建 `tmp/tts_*`。`--output-root` 只能指向本 skill 的 `outputs/.../runs`，精确 workspace 也必须位于该目录下。

Markdown 若有第一个标题文字严格等于“开头”的一级 ATX 标题 `# 开头`，仅处理标题之后的正文。多行文本通过 UTF-8 文件输入，避免 shell 转义破坏正文。

```text
runtime/.venv/Scripts/python.exe skills/convert-copy-to-transcript/scripts/cli.py start --root . --input-file copy.md
runtime/.venv/Scripts/python.exe skills/convert-copy-to-transcript/scripts/cli.py apply-decisions --root . --run-id <run-id> --decisions-file <json>
runtime/.venv/Scripts/python.exe skills/convert-copy-to-transcript/scripts/cli.py approve --root . --run-id <run-id> --confirmed-by <确认来源> --preview-sha256 <hash>
runtime/.venv/Scripts/python.exe skills/convert-copy-to-transcript/scripts/cli.py verify --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/convert-copy-to-transcript/scripts/cli.py resume --root . --run-id <run-id>
```

`prepare` 与 `start` 等价。扫描器生成候选及决策模板，Agent 仅填写 `candidate_id/replacement`；脚本验证候选范围、渲染全文与差异报告。向用户展示预览与精确哈希后，等待明确确认。转换到验证完成即结束，不反向调用 TTS。

## 转换边界


- 允许删除 Markdown 和不可发音标记、把列表序号机械转换为口语序号、为公式／数字／`ta` 选择登记后的读法，以及极少量书面词口语化。
- `#` 至 `######` 的 ATX 标题整行不进入逐字稿；允许前置空格且以 `>` 开头的引用行整行不进入逐字稿，包括空引用行和嵌套引用。
- `{{display:<整数>ms}}` 独占一行时删除整行；位于正文行内时只删除指令并保留同行正文。
- 行首允许有空白；行首的 `总结：` 或 `总结:` 确定性转换为 `总结一下，`，正文行内的同形文本不转换。
- 不进行语义性拆句、合句或重排；仅允许已登记的起始范围、标题、引用行和独占行控制指令删除对应换行。
- 英语单词保留。`AI`、`GPA` 等专有缩写默认保留原拼写，供 TTS 逐字母读取。
- `@` 按确定性上下文转换：`TEST_USER@example.invalid` 和 `@example.invalid` 等带域名后缀的邮箱形态读作“艾特”；其余 `@名称` 视为博主名，删除 `@` 并保留名称，例如 `@Hytidel聊商业` 转换为 `Hytidel聊商业`。
- `「」`、`《》`、`【】`、`[]`、`（）`、`()` 及 Markdown 的 `#`、`$`、`---`、`_` 不发音。
- Agent 只能填写 `semantic-decisions.json` 中扫描器登记的候选，不得增加候选 ID 或编辑 span。决策模板见 [assets/semantic-decisions.template.json](assets/semantic-decisions.template.json)。
- 停顿指令是确定性控制指令，不是语义候选。转换前由共享解析器保护，预览和批准稿必须逐字保留；不得让 Agent 重新填写或改写。


## 状态机与产物

`prepared → deterministic_transform → awaiting_semantic_decisions → preview_ready → awaiting_transcript_approval → approved → verified → completed`。错误进入 `paused_error`，退回进入 `revision_required`。`resume` 依据检查点恢复；重新应用决策必须重新确认。`verify` 对 approved 运行校验并完成状态迁移；对 completed 运行只读重新校验输入、扫描、决策、预览、批准稿、回执及 manifest，不写入新的 TTS 校验文件。

正式产物为 `transcript/generated/transcript-preview.txt`、`transcript/generated/difference-report.md`、`transcript/approved/transcript.txt`、`transcript/approved/approval-receipt.json`、`transcript/manifest.json` 与顶层 `handoff.json`。扫描、语义决策、请求与源文件副本位于对应日志 run。`status` 回执提供这些路径。

`verify --tts-manifest <路径>` 保留原 schema 2/3 的 TTS 输入哈希及修订 lineage 校验。修订稿创建新的转换 run，不能继承旧确认。停顿指令只接受 `{{pause:1ms}}` 至 `{{pause:60000ms}}` 的整数毫秒形式，数量、顺序、原文和时长逐项校验。

零语义候选时，start 由脚本自动应用空决策并通过现有状态机生成预览和差异报告，停在 awaiting_transcript_approval；有候选时 Agent 仅填写登记候选的 replacement。resume 只用于 paused_error，恢复后按实际状态执行命令。命令与当前状态不兼容时返回错误但保持运行状态及产物；执行阶段失败才进入 paused_error。下表由 run-text-to-speech/scripts/render_voice_docs.py 从状态定义和共享命令规则生成，使用 --check 检查。

<!-- state-commands:start -->
| 当前状态 | 下一步命令 |
|---|---|
| `approved` | `verify` |
| `awaiting_semantic_decisions` | `apply-decisions` |
| `awaiting_transcript_approval` | `approve` |
| `completed` | `verify` |
| `paused_error` | `resume` |
| `revision_required` | `apply-decisions` |
| `verified` | `verify` |
<!-- state-commands:end -->
