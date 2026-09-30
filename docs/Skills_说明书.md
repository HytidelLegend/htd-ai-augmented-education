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
| `render-handwritten-essay-card` | 将英语作文生成通用英语考试答题卡手写印刷体照片提示词，并在确认后调用 `/imagen` | 用户提供英语作文并要求生成答题卡照片时 | `python skills/render-handwritten-essay-card/scripts/cli.py start --root . --input request.json` |

| `build-word-entry` | 从单词或 UTF-8 词表生成可恢复、可校验的英语词条和批次关系 | 用户要建立词条、批量处理词表或恢复运行时 | `python skills/build-word-entry/scripts/cli.py start-word --root . --word bank` |
| `beta-build-curriculum-navigation` | 从一个或多个 Markdown 资料生成或增量维护课程学习顺序；本地测试版 | 用户要建立课程索引、安排跨资料学习顺序或插入新资料时 | `python skills/beta-build-curriculum-navigation/scripts/cli.py prepare --root . --request request.json` |
| `beta-interactive-tutor` | 按单份 Markdown 或课程导航生成有证据的讲义，收集答案并更新学习进度；本地测试版 | 用户要开始或继续交互式学习、作答或获取学习报告时 | `python skills/beta-interactive-tutor/scripts/cli.py start --root . --request request.json` |

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

个性化规划前确认学习者当前水平和学习目的；多个合法拓扑序出现时，Agent 逐题四选一、按二分思路估计知识边界，再在全部先修约束内选与目标和边界匹配的路线，并在排序理由中留痕。

这是本地测试版，暂不提交注册与说明改动。

#### 具体场景示例

```yaml
scenario_examples:
  - id: build-course-order
    user_request: "根据这些 Markdown 资料建立课程学习导航，并说明先学顺序"
    when_to_call: "用户提供项目内 Markdown 资料，要求建立或增量维护课程索引、学习顺序时"
    invocation: "init-request → prepare → resolve-source（逐份）→ resolve → resolve-ordering（如需）→ commit → verify"
    expected_output: "经批准的导航 JSON、确定性 Markdown 视图和来源片段"
```

入口为 `runtime/.venv/Scripts/python.exe skills/beta-build-curriculum-navigation/scripts/cli.py`。请求采用通用 v2 schema；`navigation_json` 可为空，默认正式产物位于 `outputs/beta-build-curriculum-navigation/runs/<run-id>/`，也可指定项目内任意 JSON 路径。状态、来源快照、决策模板、预览、差异和验证报告位于 `logs/beta-build-curriculum-navigation/runs/<run-id>/`。脚本生成候选与模板，Agent 只按候选 ID 补少量语义判断，正式 Markdown 由脚本渲染。

状态机按 `prepared → source_navigation_fragment_required（逐份）→ all_source_navigation_fragments_ready → ordering_decision_required（如需）／awaiting_loop_resolution（如需）→ awaiting_approval → completed` 推进；来源冲突、输入变化与运行错误进入 `paused_*`，使用 `status` 和 `resume` 从原运行恢复。提交前核对预览哈希与正式导航并发变化，`verify` 检查来源、顺序、JSON、Markdown 和单资料片段。本测试版目录由 `.gitignore` 忽略；注册与本节说明仅供本地测试，暂不提交。

### beta-interactive-tutor

首课前先用 `assessment-start` 确认当前水平和学习目的，再根据 `next_target` 一题一题调用 `assessment-question`、`assessment-answer` 完成最多五题的边界诊断。未完成时 `prepare-lesson` 返回待入学或待测评状态，不生成讲义。题目及答案只保存在运行日志中。

#### 具体场景示例

```yaml
scenario_examples:
  - id: learn-one-material
    user_request: "按这份 Markdown 资料逐课教我，并检查我的练习答案"
    when_to_call: "用户要求从一份 Markdown 或已完成的课程导航开始交互式学习时"
    invocation: "init-request → start → assessment-start → assessment-question/assessment-answer（逐题；或 assessment-import）→ prepare-lesson → publish-lesson → check-answers/submit-chat-answer → review-answers → report → verify"
    expected_output: "可追溯的讲义、学习路线图、摘要和学习报告"
```

入口为 `runtime/.venv/Scripts/python.exe skills/beta-interactive-tutor/scripts/cli.py`。多份资料须先由 `beta-build-curriculum-navigation` 生成 v2 导航 JSON、同名 Markdown 和 `.sources/`，再用 `supply-navigation` 恢复。脚本校验导航与来源，按当前单元生成证据包及紧凑决策模板；Agent 只补 Bloom 层级、少量讲解与题目、证据 ID 和必要的批改判断。脚本渲染讲义、答案栏、表格、摘要和报告。可选 v2 学习档案优先取请求路径，否则使用导航引用；双语术语表须显式提供，可用 `init-bilingual-glossary --file <terms.md>` 生成 `Source term`、`Target term`、`Note` 三列表格模板，项目单列术语表不能充当译法。批改模板中的得分、反馈和下一步动作必须由 Agent 填写。正式 Markdown 和最终报告 JSON 位于 `outputs/beta-interactive-tutor/runs/<run-id>/`，状态、快照及中间决策位于 `logs/beta-interactive-tutor/runs/<run-id>/`。

状态机按 `initialized → input_mode_detected → navigation_bundle_validated → student_profile_loaded → glossary_snapshot_ready → learning_queue_ready → unit_selected → evidence_bundle_ready → lesson_decision_required → awaiting_answer → review_decision_required → review_applied → completed` 推进；多资料、导航不一致、来源或档案变化以及用户暂停进入相应 `paused_*` 状态。使用 `status` 查看、`resume` 恢复、`verify` 检查，退出码遵循本说明书。旧项目的学习工程布局不在此测试版的恢复范围内。

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

入口为 `runtime/.venv/Scripts/python.exe skills/build-word-entry/scripts/cli.py`，支持 `start-word`、`start-list`、`resume`、`status`、`verify` 和 `deliver`。文本词表一行一词；CSV 指定单词列；JSON 接受对象数组或 words 数组，JSONL 每行一个对象，并读取 word 与可选提示字段，去重后保存全部原始行号。完整词条按 lemma 首字母写入 outputs/词汇星图/dicts/a.jsonl 至 z.jsonl；旧 entries 目录作为状态机兼容工作文件，同步后由应用读取 JSONL。当前不处理学龄段标签。单词与批次各有显式状态机；证据采集使用四站可见浏览器，脚本预填 `decision-template.json`，人工登录或验证码操作暂停；Agent 只校对并补足少量结构化义项判断，必要时按内容项给出证据索引。Cambridge 候选按词性保留英美 IPA 和音频来源 URL。AI 生成释义与例句通过结构检查后可入库，保留 `pending`、`confidence` 和生成方式，展示脚本追加 `（AI 生成，置信度 0.85）`。`gaps.json` 记录来源覆盖与逐字段 `fieldGaps`：确实有候选却未发布时标记待补，证据不足时标记待核验；选择器预览限量不算采集截断。形容词比较级／最高级经来源核对后存于 `inflections[]`，独立派生词关系存于 `derivatives[]`。新版词条的无法判断的同义／近义／反义候选按来源义项保存在 `pendingRelations[]`；旧版词条仍可验证。词族与直接派生词分开确认；已判断的表外关系可先以词面正式关系保存，目标义项核对后再链接。正式批次结果只含输入文件名，不泄漏本地绝对路径；原始网页快照和浏览器会话不保存。正式结果位于 `outputs/build-word-entry/runs/<run-id>/`，状态与中间证据位于 `logs/build-word-entry/runs/<run-id>/`。

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
```
