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

## 时间戳

所有 Skill 的时间字段使用本地时间、不带时区的 `YYYY-MM-DDTHH:MM:SS`；文件名和运行目录中的时间部分使用 `YYYYMMDDTHHMMSS`，同类对象冲突时追加 `_1`、`_2` 等后缀。实现统一调用 `utils/scripts/timestamp.py`。

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
| `build-mnemonic-keywords` | 将一句背诵内容转为关键词、联想场景和生图提示词，并在确认后调用 `/imagegen` | 用户要求联想记忆、记忆口诀、场景化背诵或将记忆法生成图片时 | `start → resume → deliver --mode preview → 用户确认 → resume` |
| `mark-memory-spans` | 从纯文本提取可挖空的语义记忆要点，保存不重叠 span，并用「」标记；支持新增、删除和调整 span | 用户要求标记背诵重点、提取记忆要点、生成挖空文本或修改已有记忆 span 时 | `start → resume`；编辑使用 `edit` |
| `schedule-ebbinghaus-plan` | 将编号 1 至 n 的 item 分批安排首次背诵及间隔复习 | 用户指定 n，以及每天 item 数或首遍天数，并要求按艾宾浩斯规则生成逐日背诵计划时 | `start → verify → deliver`；暂停后用 `resume` |
| `render-handwritten-essay-card` | 将英语作文生成通用英语考试答题卡手写印刷体照片提示词，并在确认后调用 `/imagen` | 用户提供英语作文并要求生成答题卡照片时 | `python skills/render-handwritten-essay-card/scripts/cli.py start --root . --input request.json` |

| `build-word-entry` | 从单词或 UTF-8 词表生成可恢复、可校验的英语词条和批次关系 | 用户要建立词条、批量处理词表或恢复运行时 | `python skills/build-word-entry/scripts/cli.py start-word --root . --word bank` |
| `beta-build-curriculum-navigation` | 从 Markdown 资料维护知识点依赖图、自适应诊断及评分候选顺序；本地测试版 | 用户要建立课程索引、安排跨资料学习顺序或插入新资料时 | `python skills/beta-build-curriculum-navigation/scripts/cli.py prepare --root . --request request.json` |
| `beta-interactive-tutor` | 先校验学习导航，再生成课程、批改、答疑并维护双图与学习文档；本地测试版 | 用户要开始或继续交互式学习、作答或获取学习报告时 | `python skills/beta-interactive-tutor/scripts/cli.py start --root . --request request.json` |

### AI 辅助教学

暂无

### AI 辅助科研

暂无

### mark-memory-spans

两个记忆 Skill 共用 `utils/references/术语表.txt`，每行一个中文或英文术语。`mark-memory-spans` 将命中的完整术语列为候选，并在提交、编辑和验证时拒绝从术语中间切开的区间；是否挖空仍由语义决定。运行词表快照存于对应的 `logs/<skill>/runs/<run-id>/`。

古诗文背诵新增整分句模式：Agent 根据输入语义判断是否为要背诵的古诗文；是则默认按整分句挖空，否则沿用下述记忆要点模式。脚本按逗号、分号、句号、问号、叹号和换行列出候选分句，顿号留在分句内部；Agent 仅挑选需要练习的分句编号，脚本扩展完整区间并保留分隔标点及分句边缘的引号。古诗文豁免普通模式的标记密度及剩余主干要求；`edit`、`verify`、`deliver` 都复核分句边界。输出字段及 `「」`、`____` 不变，Markdown 的古诗文主干检查显示“不适用”。

#### 具体场景示例

```yaml
scenario_examples:
  - id: mark-memory-points
    user_request: "请标出这段知识中适合挖空记忆的要点"
    when_to_call: "用户提供纯文本并要求提取、标记或修改记忆要点时"
    invocation: "start → resume → deliver → verify"
    expected_output: "返回带「」标记的 Markdown、包含源文本 SHA-256 的 span JSON，以及挖空后的主干检查结果"
```

入口为 `runtime/.venv/Scripts/python.exe skills/mark-memory-spans/scripts/cli.py`，支持 `start`、`resume`、`deliver`、`edit`、`status` 和 `verify`。文本先统一换行，再计算 UTF-8 SHA-256；每个 span 保存该整段文本的哈希。span 使用零基左闭右开区间，不允许重叠但允许相邻；专业名词和固定术语不得拆分。记忆要点遵循“主干优先、最小充分”原则，只标记最关键的核心概念、行动要求、价值判断和固定并列短语，不把几乎每个名词都列为要点。多个独立短 span 同句覆盖率不超过 85% 时可保留，但挖空后必须仍有可读主干；单个 span 默认不超过句子 40%，保留主语、情态词和命名结论的完整核心关系可放宽至 50%。Markdown 使用 `「span」` 标记，连续的独立要点显示为相邻的 `「span1」「span2」`。

`start` 生成候选 span 和 `generation_packet.json` 后进入 `paused_agent_selection`；Agent 只提交选定区间、语义角色和挖空主干判断，脚本负责原文切片、哈希、重叠校验、主干与过度标记检查、标记渲染、每个 span 与纯文本 `____` 的逐项对应校验和 Schema 校验。命名结论需要保留，邻近比较不得直接泄露已挖空答案；若邻近原因、条件或比较句足以唯一推出挖空答案，还须标记最小决定判据并保留因果主干，例如挖空「向上排空气法」时同步挖空“密度比空气「大」”中的「大」。变化关系中的决定因素可独立标记，动作和对象共同构成核心关系时允许完整标记。`verify` 和 `deliver` 会重新计算源文本、span、标记文本、挖空文本和对齐信息，防止产物被修改后仍通过检查。质量检查失败会进入 `paused_quality_review`，可补交 Agent 响应后恢复。`deliver` 读取已验证的 `result.md` 并输出到对话。`edit` 按顺序应用 `add`、`delete` 和 `adjust` 操作；调整为零长度时删除。正式产物位于 `outputs/mark-memory-spans/runs/<run-id>/result.json` 和 `result.md`，日志及状态位于 `logs/mark-memory-spans/runs/<run-id>/`。

错误拆分案例必须保留：`科学立法、严格执法、公正司法、全民守法` 不应拆成“科学、立法、严格、执法、公正、司法、全民、守法”；正确的四个记忆要点是四个完整并列短语。相邻 span 的正例为 `用「分液漏斗」「萃取」……`。政治/思想品德、地理、数学、物理、化学、生物和语文的主干保留、动作与对象边界、传导顺序、联合技术对象、原因判据泄露及过度标记正反样例见 `skills/mark-memory-spans/references/span-examples.md`；该文档按主题分表，每行统一列出原文、正面、挖空、正面原因、负面和负面原因，由 `utils/scripts/render_span_examples.py` 从 `skills/mark-memory-spans/references/` 下的案例 JSON 生成。数学材料的公式右侧表达式整体候选和明显拆断检查由共用脚本处理，条件与结论的语义选点仍由 Agent 判断；语文材料保留作品主题和叙述关系，分别标记可独立考查的作者、篇目、情节、人物和场所；脚本优先提供完整篇名、引语及组合候选，并在上限内轮流覆盖各段，Agent 负责少量语义选择。

### 常用工具（与 AI 辅助教育无关）

| Skill | 功能 | 调用时机 | CLI 示例 |
|---|---|---|---|
| `git-remote-diff` | 比较本地仓库、远端默认分支和工作区 | 需要检查 Git 同步状态时 | `python skills/git-remote-diff/scripts/cli.py start --root .` |
| `sensitive-commit-check` | 检查提交范围中的敏感信息 | 提交、推送或发布前 | `runtime/.venv/Scripts/python.exe skills/sensitive-commit-check/scripts/cli.py start --scope staged --supplemental all` |
| `format-conversion-master` | 执行可恢复的格式转换 | 用户要求转换文件格式时 | `python skills/format-conversion-master/scripts/cli.py start --input input.epub --to pdf` |
| `run-speech-to-text` | 语音转文字 | 用户要求语音转文字、提取时间戳或批量生成逐字稿时 | `start → submit-corrections（可选）→ approve-transcript → verify → deliver` |
| `convert-copy-to-transcript` | 文案转逐字稿 | 用户要求把文案转换为口播稿、逐字稿或 TTS 输入时 | `start → apply-decisions → approve → verify → deliver` |
| `run-text-to-speech` | 文字转语音 | 用户要求文字转语音、生成朗读音频、试听或修订已有合成语音时 | `start → 上游转换确认 → resume → 必要时 approve-preview → verify → deliver` |

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

### beta-build-curriculum-navigation

所有导师调用前必须先调用本 Skill：有效导航用 `verify`，新资料或目标变化用构建/增量流程。背景不足在对话中讨论，不生成询问背景的课程。导航图是知识点图。脚本采用目标优先自适应探测，默认最多 5 道诊断选择题（四个实质选项＋末尾不确定选项），未测区域保持未知；配置位于 Skill 的 `config.yaml`。

脚本生成最多 10 条候选序列，保留全部实际生成候选及历史，按权重计算分项和总分并非升序排列。默认权重为目标 40%、背景 30%、难度 15%、主题 10%、解锁 5%。参数冻结在日志运行目录，每次继续比较、更新并提示变化。

```yaml
scenario_examples:
  - id: build-course-order
    user_request: "根据这些资料建立学习导航"
    when_to_call: "规划、增量维护，或每次导师调用前校验导航"
    invocation: "prepare → resolve-source/resolve → diagnostic-start/question/answer → resolve-ordering → commit → verify"
    expected_output: "知识点依赖图、诊断、全部评分候选和确定性 Markdown"
```

状态为 `prepared → source_navigation_fragment_required → all_source_navigation_fragments_ready → ordering_decision_required → awaiting_approval → completed`；诊断子状态为 `question_required → awaiting_answer → question_required/completed`，环或错误进入相应暂停。Agent 只填写小补丁、出题及必要语义判断，Python 扩充 JSON 和 Markdown。正式导航位于 `outputs/beta-build-curriculum-navigation/runs/<run-id>/`，日志位于对应 `logs/`。完整契约见 `skills/beta-build-curriculum-navigation/SKILL.md`。

### beta-interactive-tutor

未指定导航或已有导师项目时，执行 `start --root .` 或 `list-projects --root .`，共享 `utils/scripts/learning_project_selection.py` 只读发现本地导航 run，仅展示已完成且有效的导航；单候选也等待用户选择。Python 按 `learning-project-catalog-v1.schema.json` 生成“序号｜项目名称｜run ID｜更新时间｜状态｜已有导师项目数”，Agent 仅转交序号或候选 ID。有关联导师项目时再列出已有项目与“新建”选项，保留现有材料备份、作答及答疑门禁。

入口状态机为 `discovering_projects → awaiting_project_selection → validating_selection → awaiting_tutor_selection（可选）→ validating_selection → project_resolved`；无候选为 `navigation_required`。等待返回退出码 3；清单变化则刷新重选。新增 `select-project`、`select-tutor` 与 `resume-selection`，清单和选择状态保存在导师 `logs/` run，支持默认发布目录及日志登记的自定义导航路径。完整字段、命令参数及恢复规则见导师 Skill。

导航 run 检查点须通过 Schema 校验，同路径按最新 run 状态筛选，未完成或损坏状态不能经默认输出目录绕过筛选；同秒 run 后缀按数值排序。选择状态与清单同事务保存并支持失败回滚；恢复时清单未变则保留当前阶段与已选导航，避免重新询问第一阶段。

两个学习 skill 共用 `utils/scripts/learning_material_backup.py` 和 `learning-material-backup-v1.schema.json`：导师项目在 `学习材料/` 逐字节备份 Markdown 及引用的本地资源，清单在 `artifacts/学习材料备份.json`，原文件只读。备份子状态机为 `planning_backup → awaiting_backup_approval → copying_materials → verifying_backup → backup_ready`；错误暂停并保留检查点。初次创建或旧项目补建需一次性批准路径清单，以 `resume --backup-plan-sha256 <hash>` 恢复；教学状态从 `backup_required` 转为 `ready`。已有副本的每次导航校验使用 `verify --material-project <学习项目目录>`；教学和图片指引使用副本，原文件变化通过导航增量及 `supply-navigation` 显式引入，保留材料历史版本。新资料同步同样确认计划哈希，副本损坏或目标冲突暂停处理，不自动回退源文件。

必须使用已完成新版导航，删除导师独立入学诊断及直接资料入口。每次调用先执行导航校验；课程与知识点是多对多关系，两张依赖图分别维护。未显式跳过知识点必须有有效课程覆盖，课程内外顺序都满足前置约束，主线基础不能留作可选支线。

备份子状态迁移由脚本校验，冲突停在 `paused_backup_conflict`；复制忽略代码示例和未使用引用定义，批准路径包括正式及历史清单。新清单与项目 JSON/Markdown 同事务发布，失败回滚；初始化失败保留待备份检查点。原始输入与写入路径重合时拒绝执行，材料产物不得进入 `runtime/` 或 `logs/`。

```yaml
scenario_examples:
  - id: learn-one-material
    user_request: "继续课程并批改我的答案"
    when_to_call: "基于导航开始或继续教学、作答、答疑、记笔记或调整路线"
    invocation: "导航 verify → start/resume → prepare-lesson → publish-lesson → check-answers/submit-chat-answer → review-answers → questions → 用户明确无疑问 → prepare-lesson"
    expected_output: "中文课程、双图学习路线、逐课总结与报告、确认后的笔记和规律型错题本"
```

默认每课最多 3 个知识点、3 道正式选择题、2 道问答题，每个本课知识点均需正式习题检验。引导和追问不计入。良好 ≥80%、中等 ≥60%、其余一般，无评分证据单列。配置与播客占位开关位于 Skill 的 `config.yaml`，冻结、更新及通知规则同导航。

状态为 `ready → lesson_decision_required → awaiting_answer → review_decision_required → awaiting_questions → ready/completed`。批改后先反馈掌握优缺点并更新文档，询问疑问；用户明确没有疑问前，不准备下一课。笔记先展示整理稿，确认后写入；错题自动归并薄弱点、规律、纠正与例题。

项目根目录只留 `项目.json`、`学习路线.md`、`总结.md`、`学习报告.md`、`笔记本.md`、`错题本.md`；课程在 `课程/课程_X-Y.md`，其余 JSON 在 `artifacts/` 和 `artifacts/lessons/`。先落盘 JSON（含参考答案）再渲染 Markdown，题目视图不显示答案。运行状态与中间模板继续位于日志目录。支持 `plan`、`skip/restore`、`supply-navigation` 维护计划、消费网页指令和同步双图。完整 CLI 及模板契约见 `skills/beta-interactive-tutor/SKILL.md`。 未发布课程可退回规划拆分，恢复后重新发布保留旧版本；显式跳过当前未批改课程允许选择后续课程，已经作答仍先批改并答疑。笔记确认同时校验草稿与来源哈希，每个薄弱知识点都有错题归纳。

导师 `学习路线.md` 新增知识点、课程双 Mermaid `flowchart LR` 思维导图，不调用生图工具。Python 从现有双图按 `utils/references/dependency-flowchart-v1.schema.json` 和 `utils/templates/dependency-flowchart.template.md` 自动构造节点与边；知识点显示名称和状态，课程显示编号、标题、主支线和状态，完整保留多前置、孤立及已跳过节点，退出课程不展示。导航、计划或状态变化时，双图随 JSON 和依赖表同事务发布；`verify` 检查图示一致性，旧项目用 `resume` 补齐。

文档生成子状态机为 `prepared → validating_graphs → rendering_documents → verifying_documents → publishing → completed`；失败进入 `paused_error`，检查点在导师日志的 `document-render/state.json`。恢复读取并校验未完成检查点，保留失败和中断历史后重新校验权威 JSON；损坏检查点拒绝覆盖，不绕过教学及答疑门禁。教学状态检查点和计划模板随项目文档同事务保存，失败不推进内存模型版本。共享实现位于 `utils/scripts/mermaid_flowchart.py`，恢复能力复用 `utils/scripts/workflow_checkpoint.py`。

### build-word-entry

#### 具体场景示例

```yaml
scenario_examples:
  - id: build-dictionary-entry
    user_request: "请从这份词表建立可追溯的英语词条"
    when_to_call: "用户提供单词或 UTF-8 词表并要求生成、更新词条时"
    invocation: "start-word/start-list → status → resume → verify → deliver"
    expected_output: "生成词条 JSON、带 AI 置信度标注的可读结果及可恢复的批次报告"
```

入口为 `runtime/.venv/Scripts/python.exe skills/build-word-entry/scripts/cli.py`，支持 `start-word`、`start-list`、`resume`、`status`、`verify` 和 `deliver`。文本词表一行一词；CSV 指定单词列；JSON 接受对象数组或 words 数组，JSONL 每行一个对象，并读取 word 与可选提示字段，去重后保存全部原始行号。完整词条按 lemma 首字母写入 outputs/词汇星图/dicts/a.jsonl 至 z.jsonl；词条状态机及审核直接读写唯一 JSONL 词库；旧 entries 经迁移验证后清理，不再作为长期工作副本。当前不处理学龄段标签。单词与批次各有显式状态机；证据采集使用四站可见浏览器，脚本预填 `decision-template.json`，人工登录或验证码操作暂停；Agent 只校对并补足少量结构化义项判断，必要时按内容项给出证据索引。Cambridge 候选按词性保留英美 IPA 和音频来源 URL。AI 生成释义与例句通过结构检查后可入库，保留 `pending`、`confidence` 和生成方式，展示脚本追加 `（AI 生成，置信度 0.85）`。`gaps.json` 记录来源覆盖与逐字段 `fieldGaps`：确实有候选却未发布时标记待补，证据不足时标记待核验；选择器预览限量不算采集截断。形容词比较级／最高级经来源核对后存于 `inflections[]`，独立派生词关系存于 `derivatives[]`。新版词条的无法判断的同义／近义／反义候选按来源义项保存在 `pendingRelations[]`；旧版词条仍可验证。词族与直接派生词分开确认；已判断的表外关系可先以词面正式关系保存，目标义项核对后再链接。正式批次结果只含输入文件名，不泄漏本地绝对路径；原始网页快照和浏览器会话不保存。正式结果位于 `outputs/build-word-entry/runs/<run-id>/`，状态与中间证据位于 `logs/build-word-entry/runs/<run-id>/`。

空词形和派生词字段的后续审核使用 `family_review.py start --project <id> → resume --run-id <id> --input <decision.json> → verify --run-id <id>`。状态机先生成逐字段候选包及精简决策模板，Agent 只判断候选是否具有直接关系；脚本校验来源、版本和判断后发布。审核结果保存于 `outputs/词汇星图/family-reviews/`，`automatic_passed` 表示审核判断已被脚本应用，`outcome` 分别记录已发布或现有来源无可证实内容；原采集缺口保留为历史记录。

当前 1.3 版还由脚本生成 `relation-review-template.json`：Agent 按候选 ID 完成关系与派生词判断，脚本校验全量覆盖后发布。已确认的表外关系进入正式词条，目标词条未建时使用 `lemma_only`；旧版词条继续可验证。

1.3 新词条的例句、搭配和短语均要求独立中译；例句用纯文本及双语字符区间由 Python 渲染加粗，搭配和短语只显示中译。Cambridge 成对例句与中译优先按页面结构采集；缺失时 `decision-template.json` 预留字段，Agent 在 `usageUpdates` 中补少量译文、置信度和中文区间。旧内容补译使用 `legacyUsageUpdates`，待补清单逐内容 ID 生成；新判断必须绑定本次 `evidenceDigest`。状态机在 `validating_entry` 后进入 `verifying_content`，通过当前页面定位及规则核验的读音和词形标记 `automatic_passed`，否则维持待核验并在 `gaps.json` 的 `verificationGaps` 记录具体原因。旧运行继续使用原版本状态机。

目标词条随后单独建成时，单词状态机生成 `incoming-link-review.json` 并暂停链接复核；`resume --input` 按 `link-decision.schema.json` 接收逐项链接或暂缓决定，脚本核对两端义项证据与修订状态后补入双向链接。

1.4 新词条增加可恢复的内容审查阶段。脚本从候选词条生成 `content-review-template.json`，Agent 按 `content-review-decision.schema.json` 对义项内的释义、用法及译文作分组判断并列出暂缓内容 ID。脚本扩展为逐内容项 `agent_passed` 与 `verificationRef`；暂缓项保持 `pending` 并进入 `contentGaps`。AI 生成内容通过后仍保留生成方式和置信度。旧运行继续按原版本状态机恢复。

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

### schedule-ebbinghaus-plan

#### 具体场景示例

```yaml
scenario_examples:
  - id: plan-numbered-items
    user_request: "将编号 1 到 30 的内容安排在 10 天内首次背完，并列出后续复习"
    when_to_call: "用户给出 item 数量和完成天数，要求生成分批背诵与复习日程时"
    invocation: "start → verify → deliver；暂停后使用 resume"
    expected_output: "返回经验证的逐日 item 编号 JSON，并由脚本渲染对应 Markdown"
```

入口为 `runtime/.venv/Scripts/python.exe skills/schedule-ebbinghaus-plan/scripts/cli.py`，支持 `start`、`status`、`resume`、`verify` 和 `deliver`。请求必填正整数 `n`，并在每天首次背诵 item 数 `daily_items` 与首遍天数 `d` 中恰选一个；脚本计算另一个值。完成标准可选 `first_pass`（默认）或 `one_review`，后者要求各 item 在期限内至少复习一次。复习间隔默认在首次背诵后的第 1、2、4、7、15 天，可用严格递增的正整数数组覆盖。每天依次引入一个新 batch，到期复习 batch 不限量。给定 `d` 时默认模式要求 `d <= n`，batch 大小为 `ceil(n/d)`，item 均匀分到恰好 `d` 天；至少复习一轮时设最早复习间隔为 `r`，大小为 `ceil(n/(d-r))`，期限不可行时暂停报告原因。给定 `daily_items` 时直接用它作为 batch 上限，期限由实际批次数计算。排程结果会继续列出第 `d` 天之后的复习，空闲日也保留。

状态机为 `prepared → validating_request → selecting_batch_size → building_schedule → validating_schedule → rendering_outputs → verifying_outputs → completed`；输入错误、期限不足和运行错误分别进入可恢复暂停状态。Python 脚本完成计算、Schema 校验和 Markdown 渲染，Agent 不填写逐日列表。正式结果位于 `outputs/schedule-ebbinghaus-plan/runs/<run-id>/result.json` 与 `result.md`；请求快照、状态与事件位于 `logs/schedule-ebbinghaus-plan/runs/<run-id>/`。

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


### build-mnemonic-keywords

原句命中术语表时，候选与源关键词保留完整术语；联想口诀可使用代表字词、缩写或谐音，但须通过映射回忆完整术语。脚本校验源关键词边界及口诀片段覆盖，运行词表快照存于本次日志目录。

#### 具体场景示例

```yaml
scenario_examples:
  - id: memorize-one-sentence
    user_request: "请帮我记住我国温度带从南到北的顺序，并给一个能生成图片的联想场景"
    when_to_call: "用户提供一句需要背诵的知识，要求提取关键词、联想记忆或视觉化图片时"
    invocation: "start → resume → deliver --mode preview → 用户确认 → resume"
    expected_output: "返回关键词、联想记忆、自检结论、生图提示词；确认后归档 /imagegen 图片"
```

入口为 `runtime/.venv/Scripts/python.exe skills/build-mnemonic-keywords/scripts/cli.py`，支持 `start`、`resume`、`deliver --mode preview`、`status` 和 `verify`。输入仅允许一句，可为长难句；共享规范 `utils/references/mnemonic-association-principles.md` 统一定义附件原则、四步编句流程、关键词覆盖、常见性、逻辑通顺、易记性、长词压缩、抽象词具体化以及场景增强方法。流程读取并记录共享规范哈希，先提取完整关键词或代表字词，再编句并校验每个所选词的原词或完整谐音是否实际出现。`start` 生成候选关键词和 `generation_packet.json` 后暂停，Agent 以 `references/agent-response.schema.json` 提交少量结构化内容。脚本负责状态迁移、schema 校验、原则检查、口诀覆盖校验、Markdown 渲染、提示词预览和图片归档；覆盖、原则检查或自然度不通过时只发布关键词和说明，不发布候选口诀或生图提示词。

状态机为 `prepared → validating_request → normalizing_sentence → loading_principles_reference → extracting_keywords → building_generation_packet → paused_agent_generation → validating_agent_response → composing_result → self_checking → publishing_prompt_preview → preview_ready → paused_image_confirmation`；覆盖、原则检查或自然度不通过时从 `self_checking` 进入 `paused_quality_review`，只发布关键词和说明。图片确认后进入 `invoking_imagegen → verifying_image_result → publishing → completed`。若联想牵强、生硬、增加负担或不适合该知识，结果必须标记 `weak`/`bad` 并给出不使用联想法或改用其他方法的建议。参考资料保留用户和“单易之”提供的全部案例；附件和通用记忆原则统一维护在 `utils/references/mnemonic-association-principles.md`。

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

对 `applications/*/application-audit.json` 声明的应用，脚本还读取 `package.json`、README、PRD、状态机和数据模型，比较应用版本、文档路径、实现状态、项目 JSON 字段和产物目录说明。每个应用的审计契约使用 `utils/references/application-audit.schema.json` 校验。

运行缓存位于 `logs/project-doc-audit/cache.json`，不提交 Git。报告位于 `outputs/project-doc-audit/runs/<run-id>/report.md`。状态机为 `prepared → discovering → discovering_applications → loading_cache → loading_application_contracts → comparing_snapshots → checking_structure → checking_documents → checking_application_documents → checking_application_versions → checking_application_capabilities → checking_application_state_machines → checking_application_data_contracts → checking_skill_catalog → checking_skill_scenarios → checking_dependencies → checking_environment → validating_findings → rendering_report → verifying_report → completed`。Skill 还会校验各 `SKILL.md` front matter 的 `category` 与说明书分类是否一致，以及每个 Skill 详细章节是否包含结构化具体场景示例。Skill 只输出差异和修改建议，不自动修改文档；用户确认后再修改。

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
  - id: convert-document-to-markdown
    user_request: "把这个本地 PDF 或 Word 文档转成 Markdown"
    when_to_call: "用户提供本地 DOC、DOCX、PDF 并要求转 Markdown 时"
    invocation: "start --to md → status/resume → verify"
    expected_output: "在对应 run 的 mineru/ 子目录交付完整 MinerU 解析包"
```

DOC/DOCX/PDF → Markdown 使用 MinerU v4 精准解析 API，根目录 `.env` 配置 `MINERU_API_KEY`，进程环境变量优先。仅支持本地文件；此分支固定发布到 `outputs/format-conversion-master/runs/<run-id>/mineru/`，完整保留 `full.md`、图片及所有解析 JSON，不接受外部 `--output`。EPUB 接口保持兼容。

MinerU 状态机为 `prepared → validating_input → staging_input → checking_configuration → requesting_upload → uploading → polling → downloading → extracting → preparing_markdown → verifying → publishing → completed`。脚本负责上传、轮询、下载、安全解压及全包校验；暂停后依据 `resume_stage` 继续，查询已有批次，提交结果不明确时不重提。完整参数、签名地址中断处理及退出码见 Skill 契约。运行状态、中间包和回执均保存于对应 `logs/` 目录；转换无需 Agent 生成正文。

词汇星图的葫芦排程改由共享 `utils/scripts/hulu_schedule.py` 应用状态机负责，旧独立 skill 移至 `tmp/schedule-hulu-plan/`，不注册、不参与运行链。拼写关系维护使用 `skills/build-word-entry/scripts/spelling_relations.py start --full`，支持增量 `start`、`status`、`resume`、`verify`；通用实现和 Schema 位于 `utils/`，全量／增量均无需 Agent 判断。

### run-speech-to-text

通过火山引擎将音视频转为带词级与句级时间戳的逐字稿；支持共享热词、本次追加词表、纠错、确认、恢复和验证。

```yaml
scenario_examples:
- id: run-speech-to-text
  user_request: 请把这段音频转为带时间戳的逐字稿
  when_to_call: 用户要求语音转文字、提取时间戳或批量生成逐字稿时
  invocation: start → submit-corrections（可选）→ approve-transcript → verify → deliver
  expected_output: 确认后的逐字稿、毫秒时间戳及已验证的 handoff
```

统一入口为 `runtime/.venv/Scripts/python.exe skills/run-speech-to-text/scripts/cli.py`。完整状态机、专用参数、输出和恢复约定见该 Skill 契约。

### convert-copy-to-transcript

将 UTF-8 Markdown、纯文本文件或直接文案确定性转换为可朗读逐字稿；脚本生成语义候选、预览、差异报告与确认回执，Agent 仅填写候选读法。

```yaml
scenario_examples:
- id: convert-copy-to-transcript
  user_request: 请把这篇 Markdown 转成朗读逐字稿
  when_to_call: 用户要求把文案转换为口播稿、逐字稿或 TTS 输入时
  invocation: start → apply-decisions → approve → verify → deliver
  expected_output: UTF-8 逐字稿预览、差异报告、批准稿与 handoff
```

统一入口为 `runtime/.venv/Scripts/python.exe skills/convert-copy-to-transcript/scripts/cli.py`。完整状态机、专用参数、输出和恢复约定见该 Skill 契约。

### run-text-to-speech

将本项目已完成且确认的逐字稿通过 Edge-TTS（默认）或火山引擎合成为语音；支持分批、试听确认、静音白噪音、时间戳、恢复和局部修订。

本次可用 `start --backend volcengine --speaker 哆啦A梦` 覆盖后端和音色，无需改写配置；恢复使用有效配置快照。配置 `skills/run-text-to-speech/config.yaml` 的 `backend`，默认 `edge-tts`（晓艺、语速 `+10%`），需联网。火山分支严格检查根目录 `.env` 中的 `VOLCENGINE_API_KEY`；Edge 分支检查隔离环境中的 `edge-tts==7.2.8`。后端预检纳入状态机，缺失依赖或配置暂停，修复后 resume；音色映射表由 Python 从 YAML 生成，见 Skill 文档。

```yaml
scenario_examples:
- id: run-text-to-speech
  user_request: 请用默认音色朗读这篇文案，先给我试听
  when_to_call: 用户要求文字转语音、生成朗读音频、试听或修订已有合成语音时
  invocation: start → 上游转换确认 → resume → 必要时 approve-preview → verify → deliver
  expected_output: 最终音频、clean 母版和完整时间戳；达到目标时先提供试听并等待确认
```

统一入口为 `runtime/.venv/Scripts/python.exe skills/run-text-to-speech/scripts/cli.py`。完整状态机、专用参数、输出和恢复约定见该 Skill 契约。

三个语音 Skill 分别使用自己的 `outputs/<skill>/runs/<run-id>/` 与 `logs/<skill>/runs/<run-id>/`，通过九字段 `speech-handoff-v1.schema.json` 衔接。ASR 默认复用 `utils/references/术语表.txt`，本次词表只追加到运行快照；语义决策仍只填候选 ID 与 replacement，全文、差异与确认回执由脚本生成。

语音流程补充：转换零语义候选自动生成稿件预览并等待确认；正常等待状态不能用 resume，错误命令不改变状态。TTS 的实际音频总时长（含停顿）严格低于试听目标时不生成试听，直接合并并验证最终音频；达到目标则保留试听确认。
