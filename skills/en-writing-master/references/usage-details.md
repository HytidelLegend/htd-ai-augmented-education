# en-writing-master 使用与协议补充

入口为 `python skills/en-writing-master/scripts/cli.py`，支持 `list-tools`、`create-tool`、`write`、`grade`、`polish`、`start`、`status`、`resume` 和 `verify`。创建工具可同时读取多个 `.txt`、`.md`、`.markdown`、`.epub` 文件及内联文本；仅 EPUB 来源通过 `format-conversion-master` 转为 Markdown。运行产物位于 `outputs/en-writing-master/runs/<run-id>/`，状态位于 `logs/en-writing-master/runs/<run-id>/`。

写作、批改和润色必须先找到并校验 `skills/en-writing-master/tools/<tool-id>/tool.json`。开源仓库不自带具体作文工具，用户须通过“新增工具”功能提供有权使用的资料；`tools/README.md` 说明本地工具和版权边界。工具目录由 `tool.json`、`TOOL.md`、`writing-rules.md`、`scoring-rubric.md`、`language-bank.md` 和 `source-notes.md` 组成；`tool.json` 保存各文件哈希、来源筛选审计和语言项目来源定位。`write` 首次运行由脚本生成 `generation_packet.json` 后进入 `paused_agent_generation`，Agent 提交 essay 后恢复；脚本不得根据话题硬编码作文。没有工具或用户给出具体目标分数但未提供满分时，状态机暂停并以退出码 3 表示需要用户输入。议论文目标分数默认使用 ±1 分区间；批改报告实际分数与目标差距；润色必须读取批改反馈并重新评分，若未至少提升 1 分必须说明原因。润色修改按优化要点分组，每个优化要点可包含多个句子位置，并在 `validating_polish_changes` 阶段由脚本校验；分数提升时不展示未提升原因，也不展示内部评分状态。工具创建支持从 EPUB 解析写作内容并按请求排除翻译内容，提取完整去重的词、词组和句型；Markdown 表格和 JSON/YAML 片段由脚本根据模板生成。

Skill 运行时产生的请求快照统一写入 `logs/en-writing-master/runs/<run-id>/request.json`，Agent 通过 `resume --input` 提交的内容归档为同一目录下的 `agent-response.json`，不写入项目根目录或 `runtime/`；用户提供的输入文件位置保持不变，仅生成运行目录内的归档副本。

对话交付必须使用 `deliver --mode write|grade|polish` 读取已验证的 `result.md`；`write`、`grade` 和 `polish` 默认 JSON 输出仅用于机器处理和状态恢复。`deliver` 会强制校验高级表达小节、固定表头及 `result.json`/`result.md` 一致性。
