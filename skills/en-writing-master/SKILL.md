---
name: en-writing-master
category: ai_assisted_learning
description: 英语作文工具箱，支持按作文工具写作、批改、润色和创建新工具。当用户提出英语作文、作文评分、逐句修改、作文润色或添加写作规则时使用。
---

# 英语作文工具箱

## 边界

本 Skill 只使用 `skills/en-writing-master/tools/` 中已经存在且通过校验的工具。开源仓库不自带具体作文工具；用户必须使用“新增工具”功能并提供有权使用的资料。写作、批改、润色时若没有可用工具，必须暂停并提示先创建工具；本次不会凭空编造评分标准。

创建工具可接收 `.txt`、`.md`、内联文本或多个来源文件。只有来源包含 `.epub` 时，才通过 `format-conversion-master` 的公开 `start --to md` 入口转成 Markdown；请求 `ignore_translation=true` 时不得保留翻译章节或参考译文小节。

## CLI

```powershell
runtime/.venv/Scripts/python.exe skills/en-writing-master/scripts/cli.py list-tools --root .
runtime/.venv/Scripts/python.exe skills/en-writing-master/scripts/cli.py create-tool --root . --input request.json
runtime/.venv/Scripts/python.exe skills/en-writing-master/scripts/cli.py write --root . --input request.json
runtime/.venv/Scripts/python.exe skills/en-writing-master/scripts/cli.py grade --root . --input request.json
runtime/.venv/Scripts/python.exe skills/en-writing-master/scripts/cli.py polish --root . --input request.json
# 对话交付：只返回已验证的统一 Markdown 模板
runtime/.venv/Scripts/python.exe skills/en-writing-master/scripts/cli.py deliver --root . --mode write --input request.json
runtime/.venv/Scripts/python.exe skills/en-writing-master/scripts/cli.py status --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/en-writing-master/scripts/cli.py resume --root . --run-id <run-id>
# 暂停后补交 Agent 判断
runtime/.venv/Scripts/python.exe skills/en-writing-master/scripts/cli.py resume --root . --run-id <run-id> --input review.json
runtime/.venv/Scripts/python.exe skills/en-writing-master/scripts/cli.py verify --root . --run-id <run-id>
```

也支持 `start --mode create-tool|write|grade|polish`，便于统一调度。

写作请求使用 `target_score`、`full_score`、`target_tolerance` 和 `requirements.word_count_min/max`；批改请求使用 `topic`、`essay` 和 Agent 填写的 `agent_review`；润色请求使用 `polished_essay` 以及 Agent 提供的 `agent_review.before/after`。脚本只准备词数、段落数、句子切分和词库命中等机械证据，不得通过特征数量推断分数。

## 状态机

写作、批改和润色均使用脚本状态机并记录阶段、完成步骤、运行血缘和质量检查。`write` 在 `generating` 阶段由脚本生成 `generation_packet.json`，随后进入 `paused_agent_generation`；Agent 根据题目和工具规则生成 `essay`，通过 `resume` 提交后继续机械校验和评分预览。脚本不得根据题目关键词选择或拼接作文模板。批改在 `scoring` 后必须经过 `validating_agent_review`，取得包含篇章结构与逻辑分析的 Agent 判断，再进入 `reviewing_sentences → finalizing_scores`；缺少或不完整时进入 `paused_agent_review`。脚本只准备段落数、逐段原文、句子数、首末句和连接词等机械证据；结构判断、段落功能、句间逻辑、语义、拼写、语法、搭配和最终分数必须由 Agent 判断，不能由脚本的计数器、正则规则或启发式指标代替。三种交付流程在评分校验后统一经过 `verifying → rendering_markdown → verifying_delivery → publishing → completed`。创建工具遵循：`prepared → validating_request → resolving_source → reading_text | converting_epub → reading_converted_markdown → normalizing_source → selecting_writing_scope → extracting_rules → structuring_tool → verifying_tool → publishing → completed`。缺少工具、缺少满分、缺少 Agent 判断、篇章分析缺失、来源范围不明确、输出冲突、解析错误、目标区间不满足、非整数评分和验证错误均进入 `paused_*`。

润色在 `finalizing_scores` 后还必须经过 `validating_polish_changes`，由脚本校验按优化要点分组的修改记录；每个优化要点至少包含一个改动位置，每个位置必须包含原句、改动后版本和改动原因。请求快照只保存到 `logs/en-writing-master/runs/<run-id>/request.json`；`resume --input` 的 Agent 输入归档为同一目录下的 `agent-response.json`。外部输入可以位于任意位置（包括旧的 `runtime/*.json`），但 Skill 不得在 `runtime/` 写入或更新运行中间文件。

## 输出约定

正式产物写入 `outputs/en-writing-master/runs/<run-id>/`，状态和日志写入 `logs/en-writing-master/runs/<run-id>/`。写作、批改和润色的输出末尾必须包含“使用到的高级词汇、短语、句型、表达”，列出中文含义和使用案例。用户指定输出路径时，经过验证后发布到该路径且不覆盖已有文件。

批改默认将议论文按 15 分制、读后续写按 25 分制处理；所有维度小分、总分、满分、目标分、目标区间、分差和润色增量均为整数。比例换算产生半分时使用“四舍五入，半分进一”；用户给出具体分数但未给满分时必须暂停询问。目标分数默认按 ±1 分形成目标区间，例如 8/15 对应 7—9/15；批改仍报告实际整数分数，并同时报告与目标的整数分差。润色必须读取已有批改意见或直接反馈，完成后重新批改；默认以至少提升 1 分为目标，若未提升必须输出原因。

工具目录固定包含 `tool.json`、`TOOL.md`、`writing-rules.md`、`scoring-rubric.md`、`language-bank.md` 和 `source-notes.md`；`tool.json` 是机器可读 manifest，并保存其他文件的哈希及来源筛选审计。通用 schema、模板、脚本和工具函数放在本 Skill 的 `references/`、`templates/`、`scripts/` 和共享 `utils/` 下。工具创建必须从来源中提取完整、去重且带来源定位的可复用语言项目；若来源没有可抽取项目，脚本提供最小通用兜底项目。Markdown 表格由脚本渲染并转义，不由 Agent 手写。

批改输出必须包含满分、Agent 给出的维度评分、评分证据、整体评价、篇章结构与逻辑分析，以及逐句 `keep/revise/delete` 结果。三段结构仅在工具规则明确要求时作为硬性要求；对于 `cet4-writing`，三段是推荐结构，少于三段应提示并影响结构判断，但不能自动判定任务失败。脚本只校验 Agent 判断的完整性和分数范围，不重新计算分数。润色输出按优化要点分组，每个优化要点可包含多个句子位置；每个位置展示原句、改动后版本和改动原因。分数提升时不展示未提升原因；分数未提升或下降时才展示原因。内部评分状态只用于状态机和验证，不直接展示给用户。

用户可见交付必须使用自然中文：逐句批改中每句独立为“第 X 句：[修改/保留/删除]”小节，原句使用 `>` 引用块，必须显示“意见：”，仅修改句显示“修改后：”。批改分数低于 14/15 时必须提供单独的整体改进建议，并明确指出可识别的拼写、语法、搭配或表述问题。用户可见文本不得输出内部质量检查 JSON、运行血缘、哈希、状态机阶段、工具来源定位或其他实现元数据；这些内容仅保留在 `result.json` 和日志中。写作、批改、润色三种结果末尾均必须保留“使用到的高级词汇、短语、句型、表达”小节，统一使用“类型、表达、中文含义、使用案例”四列表格，不再输出“是否实际使用”列。

## 对话交付协议（强制）

`result.json` 是机器读取的唯一结果，`result.md` 是用户看到的唯一交付文本；二者都必须来自同一次已完成且通过 schema/语义校验的运行。对话执行层必须遵循以下顺序：

1. 将用户请求转换为 JSON，调用本 Skill 的 `cli.py deliver --mode write|grade|polish`，不得直接在对话中编写作文、评分或润色结果。
2. `deliver` 成功后只读取并返回 `output_path/result.md`；不得根据 `result.json` 自行拼接栏目，也不得只摘录作文正文、评分结论或修改后版本。
3. 最终回复必须以 Markdown 正文形式原样返回 `result.md` 的完整内容（包括标题、评分/修改结果和“使用到的高级词汇、短语、句型、表达”表格），不得放入代码块、转义 Markdown 标记、添加前言/后记或另一套 Markdown 标题；`result.md` 不得包含内部质量检查、运行血缘或其他实现元数据。
4. 如果运行状态为 `paused_*`，只能返回状态 JSON 中的错误、恢复阶段和需要用户补充的字段；不得伪造部分结果。恢复后重新读取新的 `result.md`。

`deliver` 是对话适配器的标准入口：它只在 `write`、`grade`、`polish` 成功完成且 `result.md` 存在并通过交付校验时输出模板文本；暂停、验证失败和 `create-tool` 仍输出 JSON 回执。这样同一功能的多次调用都使用同一模板和同一 schema，不依赖 Agent 的临场格式化。

执行层不得绕过模板直接返回自由格式答案。批改只在发现明确问题时给出修改；重复或无信息句应标记为 `delete`。润色必须记录实际修改，若没有必要修改则明确写“无需修改”。发布前必须通过输出 schema 校验。
