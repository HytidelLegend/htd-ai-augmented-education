# Skills 说明书

所有 skill 都提供独立 CLI：

```text
python skills/<skill-name>/scripts/cli.py <command> ...
```

## 统一退出码

| 退出码 | 含义 |
|---:|---|
| 0 | 命令成功完成 |
| 2 | 参数、路径或请求 Schema 无效 |
| 3 | 需要用户决策、前置条件不足或流程已暂停 |
| 4 | `verify` 发现产物、状态、哈希或引用不一致 |
| 5 | 可重试的运行时错误或外部命令失败 |
| 6 | 依赖、运行环境或许可证预检失败 |

## Skills 分类与调用概览

### 项目功能

| Skill | 功能 | 调用时机 | CLI 示例 |
|---|---|---|---|
| `htd-ai-augmented-education` | 回答项目信息并将任务路由到确实能够实现需求的已注册 Skills | 询问本项目，或需要规范任务、确定 Skill 及调用顺序时 | `python skills/htd-ai-augmented-education/scripts/cli.py start --root . --input request.json` |
| `project-doc-audit` | 检查项目文档、文件架构、Skills 注册、版本、环境变量和 Python 依赖 | 需要审计项目文档、检查说明是否过期或核对项目状态时 | `runtime/.venv/Scripts/python.exe skills/project-doc-audit/scripts/cli.py start --root .` |

### AI 辅助学习

| Skill | 功能 | 调用时机 | CLI 示例 |
|---|---|---|---|
| `en-writing-master` | 创建作文工具，并按已有工具执行英语写作、批改和润色 | 用户提供写作规则、写作题目、作文或润色请求时 | `python skills/en-writing-master/scripts/cli.py list-tools --root .` |
| `render-handwritten-essay-card` | 将英语作文生成通用英语考试答题卡手写印刷体照片提示词，并在确认后调用 `/imagen` | 用户提供英语作文并要求生成答题卡照片时 | `python skills/render-handwritten-essay-card/scripts/cli.py start --root . --input request.json` |

### AI 辅助教学

暂无

### AI 辅助科研

暂无

### 常用工具（与 AI 辅助教育无关）

| Skill | 功能 | 调用时机 | CLI 示例 |
|---|---|---|---|
| `git-remote-diff` | 比较本地仓库、远端默认分支和工作区 | 需要检查 Git 同步状态时 | `python skills/git-remote-diff/scripts/cli.py start --root .` |
| `sensitive-commit-check` | 检查提交范围中的敏感信息 | 提交、推送或发布前 | `python skills/sensitive-commit-check/scripts/cli.py start --scope staged` |
| `format-conversion-master` | 执行可恢复的格式转换 | 用户要求转换文件格式时 | `python skills/format-conversion-master/scripts/cli.py start --input input.epub --to pdf` |

## 项目功能

### htd-ai-augmented-education

#### 具体场景示例

```yaml
scenario_examples:
  - id: route-project-task
    user_request: "我想知道应该调用哪些 Skill 来完成一个项目任务"
    when_to_call: "用户询问项目能力、Skill 选择或调用顺序时"
    invocation: "start → resume → deliver"
    expected_output: "根据权威项目文档生成可执行的 Skill 路由和调用提示"
```

入口为 `python skills/htd-ai-augmented-education/scripts/cli.py`，支持 `create-request`、`start`、`status`、`resume`、`deliver` 和 `verify`。请求分为 `project_info` 与 `task_routing`，也可使用 `auto` 由脚本分类。Skill 不保存项目事实副本，而是动态读取 `AGENTS.md`、`SOURCE_OF_TRUTH.md`、`CODE_OF_CONDUCT.md`、`docs/`、`.claude-plugin/plugin.json` 及已注册 Skills 的公开契约。

运行遵循 `prepared → validating_request → classifying_intent → resolving_authoritative_sources → building_evidence_packet → validating_evidence_packet → paused_agent_response → validating_agent_response → rendering_markdown → verifying_delivery → publishing → completed`。输入、意图、来源读取、响应或输出冲突进入对应暂停状态；来源之间的语义冲突由 Agent 按 `SOURCE_OF_TRUTH.md` 判定并记录依据或局限性。Agent 根据来源清单填写精简的结构化草稿；脚本负责 schema 校验和 Markdown 渲染。任务路由只能推荐插件清单中已注册且公开契约确实覆盖需求的 Skills；没有匹配能力时必须承认局限，保持调用序列为空，不得推荐外部 Skills。

正式结果位于 `outputs/htd-ai-augmented-education/runs/<run-id>/result.json` 和 `result.md`，状态、事件、来源清单及草稿位于 `logs/htd-ai-augmented-education/runs/<run-id>/`。默认使用 `deliver` 将通过验证的 `result.md` 作为 Markdown 正文直接返回对话。

## AI 辅助学习

### en-writing-master

#### 具体场景示例

```yaml
scenario_examples:
  - id: polish-english-essay
    user_request: "请按现有作文工具批改并润色这篇英语作文"
    when_to_call: "用户提供英语作文并要求批改或润色时"
    invocation: "start --mode grade|polish → resume → deliver"
    expected_output: "返回经过验证的批改或润色 Markdown 结果"
```

入口为 `python skills/en-writing-master/scripts/cli.py`，支持 `list-tools`、`create-tool`、`write`、`grade`、`polish`、`start`、`status`、`resume` 和 `verify`。创建工具可同时读取多个 `.txt`、`.md`、`.markdown`、`.epub` 文件及内联文本；仅 EPUB 来源通过 `format-conversion-master` 转为 Markdown。运行产物位于 `outputs/en-writing-master/runs/<run-id>/`，状态位于 `logs/en-writing-master/runs/<run-id>/`。

写作、批改和润色必须先找到并校验 `skills/en-writing-master/tools/<tool-id>/tool.json`。开源仓库不自带具体作文工具，用户须通过“新增工具”功能提供有权使用的资料；`tools/README.md` 说明本地工具和版权边界。工具目录由 `tool.json`、`TOOL.md`、`writing-rules.md`、`scoring-rubric.md`、`language-bank.md` 和 `source-notes.md` 组成；`tool.json` 保存各文件哈希、来源筛选审计和语言项目来源定位。`write` 首次运行由脚本生成 `generation_packet.json` 后进入 `paused_agent_generation`，Agent 提交 essay 后恢复；脚本不得根据话题硬编码作文。没有工具或用户给出具体目标分数但未提供满分时，状态机暂停并以退出码 3 表示需要用户输入。议论文目标分数默认使用 ±1 分区间；批改报告实际分数与目标差距；润色必须读取批改反馈并重新评分，若未至少提升 1 分必须说明原因。润色修改按优化要点分组，每个优化要点可包含多个句子位置，并在 `validating_polish_changes` 阶段由脚本校验；分数提升时不展示未提升原因，也不展示内部评分状态。工具创建支持从 EPUB 解析写作内容并按请求排除翻译内容，提取完整去重的词、词组和句型；Markdown 表格和 JSON/YAML 片段由脚本根据模板生成。

Skill 运行时产生的请求快照统一写入 `logs/en-writing-master/runs/<run-id>/request.json`，Agent 通过 `resume --input` 提交的内容归档为同一目录下的 `agent-response.json`，不写入项目根目录或 `runtime/`；用户提供的输入文件位置保持不变，仅生成运行目录内的归档副本。

对话交付必须使用 `deliver --mode write|grade|polish` 读取已验证的 `result.md`；`write`、`grade` 和 `polish` 默认 JSON 输出仅用于机器处理和状态恢复。`deliver` 会强制校验高级表达小节、固定表头及 `result.json`/`result.md` 一致性。

### render-handwritten-essay-card

#### 具体场景示例

```yaml
scenario_examples:
  - id: render-essay-card
    user_request: "把这篇英语作文生成通用考试答题卡上的手写照片"
    when_to_call: "用户要求生成作文答题卡照片或手写作文图片时"
    invocation: "start → deliver --mode preview → 用户确认 → resume"
    expected_output: "先展示完整生图提示词，确认后生成并验证图片"
```

入口为 `python skills/render-handwritten-essay-card/scripts/cli.py`，支持 `start`、`deliver --mode preview`、`status`、`resume` 和 `verify`。默认模型为 `image-2`，可切换为 `image-2.5`；答题卡为通用英语考试答题卡，字体为手写印刷体。

运行遵循 `prepared → validating_request → normalizing_essay → extracting_layout_requirements → composing_prompt → validating_prompt → publishing_prompt_preview → preview_ready → delivering_prompt_preview → paused_imagen_confirmation`。必须先调用 `deliver --mode preview`，原样在对话中展示一个完整提示词代码块，再询问是否调用 `/imagen`。接受后，Skill 直接发起项目内 `/imagen`；图片文件或返回地址保存到 `outputs/render-handwritten-essay-card/runs/<run-id>/`，并通过 `resume --image-path` 或 `resume --image-url` 完成验证和发布。拒绝调用时仅发布提示词并完成。状态与事件位于 `logs/render-handwritten-essay-card/runs/<run-id>/`。

每个 skill 的 `SKILL.md` 还应记录其专用参数、状态机、产物路径和恢复方式。

### project-doc-audit

#### 具体场景示例

```yaml
scenario_examples:
  - id: audit-project-docs
    user_request: "检查项目说明、Skill 分类和依赖是否与当前仓库一致"
    when_to_call: "用户要求审计项目文档、Skill 注册、文件结构或 Python 依赖时"
    invocation: "start → status/verify → deliver"
    expected_output: "生成只包含确定性差异和修改建议的审计报告，不自动修改文件"
```

入口为 `runtime/.venv/Scripts/python.exe skills/project-doc-audit/scripts/cli.py`，支持 `start`、`status`、`verify` 和 `deliver`。它递归检查核心文档、`docs/**/*.md`、活动子目录下的 `README.md`、文件架构、插件与 Skills 注册、`VERSION` 与 marketplace 版本、`.env*` 键名，以及 `skills/`、`utils/`、`runtime/`（排除 `runtime/.venv`）中的 Python 第三方依赖、requirements 版本约束和虚拟环境实际安装版本，并检查文档内明确引用的项目路径。

运行缓存位于 `logs/project-doc-audit/cache.json`，不提交 Git。报告位于 `outputs/project-doc-audit/runs/<run-id>/report.md`。状态机为 `prepared → discovering → loading_cache → comparing_snapshots → checking_structure → checking_documents → checking_skill_catalog → checking_skill_scenarios → checking_dependencies → checking_environment → validating_findings → rendering_report → verifying_report → completed`。Skill 还会校验各 `SKILL.md` front matter 的 `category` 与说明书分类是否一致，以及每个 Skill 详细章节是否包含结构化具体场景示例。Skill 只输出差异和修改建议，不自动修改文档；用户确认后再修改。

## 常用工具（与 AI 辅助教育无关）

### git-remote-diff

#### 具体场景示例

```yaml
scenario_examples:
  - id: compare-remote-state
    user_request: "检查本地分支和远端默认分支有哪些差异"
    when_to_call: "用户要求检查 Git 同步状态或本地与远端文件差异时"
    invocation: "start → verify"
    expected_output: "生成提交、工作区和文件差异报告，不执行 merge 或 reset"
```

### sensitive-commit-check

#### 具体场景示例

```yaml
scenario_examples:
  - id: scan-before-commit
    user_request: "提交前帮我检查变更里有没有密钥或个人信息"
    when_to_call: "用户准备提交、推送或要求提交前安全审查时"
    invocation: "start → review（如需）→ verify"
    expected_output: "给出脱敏风险报告，并在高风险或未决风险时阻止继续提交"
```

### format-conversion-master

#### 具体场景示例

```yaml
scenario_examples:
  - id: convert-epub-to-markdown
    user_request: "把这个 EPUB 转成 Markdown，保留目录、表格和链接"
    when_to_call: "用户要求转换 EPUB 或继续既有转换运行时"
    invocation: "start --to md → verify"
    expected_output: "生成经过验证的 Markdown 文件，不覆盖已有目标文件"
```
