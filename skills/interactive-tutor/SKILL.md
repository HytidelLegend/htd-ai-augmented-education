---
name: interactive-tutor
category: ai_assisted_learning
description: 基于已完成学习导航开展可恢复教学、正式习题批改、课后答疑、双依赖图维护、笔记和错题总结。未指定项目时先列举本地有效导航供用户选择；确定项目后，开始或继续学习、提交答案或调整路线前必须先调用 build-curriculum-navigation。
---

# 交互式导师

入口为 `runtime/.venv/Scripts/python.exe skills/interactive-tutor/scripts/cli.py`。遵守项目规范；**确定项目后，每次教学调用前先调用导航技能校验/维护导航**。CLI 也会执行导航 `verify`；缺少新版有效诊断时阻止教学。单份 Markdown 也须先导航，禁止自行划分资料并重复入学诊断。

## 未指定项目时的选择入口

用户没有明确提供导航 JSON、导师项目目录或运行目录时，先执行 `start --root .`（也可用 `list-projects --root .`），不得猜测最近项目。已有对话中明确选定的项目可继续使用。v3 请求的 `input_paths: []` 同样进入发现流程，保留项目输出目录、交互模式、档案及术语表设置；`project_dir` 只是新项目的输出位置，不代表已选择导航。

共享 Python 模块 `utils/scripts/learning_project_selection.py` 结合本地导航 `outputs/` 和 `logs/` run 查找已完成且有效的导航，支持自定义导航路径、按实际路径去重。未完成或损坏导航不展示；只有一个候选也必须让用户选择。项目列表校验只读，不冻结配置、不修改教学进度。已有材料副本可用于导航有效性校验，恢复时继续使用副本；新建仍须原始材料有效。没有候选时提示先调用导航 skill，不创建教学项目。

对话原样展示脚本返回的 `markdown`，模板列为“序号｜项目名称｜run ID｜更新时间｜状态｜已有导师项目数”。Agent 只把用户序号或 `candidate_id` 传给 `select-project`，不手写表格或清单。选定导航后，如存在关联导师项目，展示脚本生成的已有项目表和“0. 新建导师项目”，用户可回复序号、项目 ID 或 `new`；不得自行恢复或新建。没有关联项目时进入现有新建流程，仍遵守材料备份批准要求。

入口状态为 `discovering_projects → awaiting_project_selection → validating_selection → awaiting_tutor_selection（有关联项目时）→ validating_selection → project_resolved`；无候选进入 `navigation_required`。合法迁移由脚本校验。等待选择返回退出码 3；清单、导航内容或项目修订变化时刷新并重新等待选择。`resume-selection` 在清单未变时保留当前选择阶段及已选导航；发现、校验中断或清单变化时重新发现项目。已解析结果直接返回，不重复创建。脚本异常保留选择检查点，修复后恢复；它不改变原教学状态机。状态和候选清单由共享 JSON 文件事务同时保存，写入失败回滚。

存在导航日志时先按 run-state Schema 校验检查点，同一路径以最新 run 为准，同秒冲突后缀按数值排序。最新 run 未完成或状态损坏时不能通过默认输出目录重新进入列表；仅当对应日志不存在时，才允许独立校验正式发布包。一个损坏候选不会阻止其他有效候选展示。

清单使用 `utils/references/learning-project-catalog-v1.schema.json`：`schema_version`、`catalog_sha256`、`entries`；每项为 `candidate_id`、`run_id`、`title`、`updated_at`、仓库相对 `navigation_path`、`navigation_status`、`availability`、`reason`、`tutor_projects`。关联导师项包含 `project_id`、`title`、相对 `project_dir`、`state`、`revision`。状态使用 `learning-project-selection-v1.schema.json`。清单、状态、请求选项和恢复结果写入 `logs/interactive-tutor/runs/<run-id>/project-catalog.json` 与 `selection-state.json`；时间由共享时间戳工具生成。

```text
start --root .
list-projects --root .
select-project --selection-dir <选择日志目录> --choice <序号或candidate_id> --catalog-sha256 <清单哈希>
select-tutor --selection-dir <选择日志目录> --choice <序号或项目ID或new> --catalog-sha256 <清单哈希>
resume-selection --selection-dir <选择日志目录>
```

## 配置与输入

`${SKILL_DIR}/config.yaml`：正式选择题默认最多 3 道、问答题最多 2 道、每课知识点最多 3 个；课内引导和补充追问不计入正式习题数量。掌握分类为良好 ≥80%、中等 ≥60%、其余一般，缺少证据单列。`podcast.enabled` 默认 false；开启时调用 `create-dialogue-podcast`，音频验证成功后才交付课程。

## 新建前确认名称

所有新建入口先运行名称状态机：`prepared → preparing_name_suggestion → awaiting_name_suggestion（多份材料）/awaiting_name_confirmation → validating_confirmed_name → creating_project → completed`。确认前只写 `logs/interactive-tutor/runs/<run-id>/creation/`，不创建学习项目。

单份材料建议名为材料文件名去掉扩展名。多份材料必须阅读清单中所有文件，填写脚本生成的 `name-suggestion.template.json`，仅提交 `title/rationale/reviewed_source_ids`，不能只读摘要或第一份材料。`suggest-project-name` 校验来源覆盖及哈希后等待用户确认；用户可接受建议或修改名称。没有材料的合成导航沿用导航标题，仍需确认。名称是项目展示字段，目录继续使用 run ID。

```text
creation-status --creation-dir <日志中的creation目录>
suggest-project-name --creation-dir <目录> --decision <小名称草稿JSON>
confirm-project-name --creation-dir <目录> --name <用户确认的名称> --proposal-sha256 <建议哈希>
```

名称确认不能替代材料备份路径批准。确认返回待备份计划时，继续一次性展示全部路径并等待批准。重复同一名称确认返回同一项目，不再创建；来源变化需重新准备建议。创建或恢复进入可学状态后，原样转交回执 `markdown`，明确项目绝对路径，提醒查看学习路线、当前课程、总结、学习报告、错题本和笔记本。

每次运行冻结配置到运行目录的 `config.yaml`。同项目继续执行时比较有效参数、保留历史并更新冻结版本，向用户转述 `config_message`。在下一个安全检查点使用新参数，不改写已发布课程及已完成批改；未发布课程按新上限校验。

`init-request --file <request.json> [--input <导航JSON>]` 生成请求。不提供输入时生成空输入列表，启动后进入项目选择；明确提供的输入必须是一个完整导航包。可指定项目内 `project_dir`、学习档案和双语术语表。双语术语表可用 `init-bilingual-glossary --file <terms.md>` 准备。档案与术语表冻结哈希，来源必须校验；变化时暂停核对。

## 项目布局与契约

默认项目根目录为 `outputs/interactive-tutor/runs/<run-id>/`，跨次调用恢复同一项目。根目录只放 `项目.json` 和中文 Markdown；其他 JSON 在 `artifacts/`，课程 JSON 在 `artifacts/lessons/`。`课程/` 只放 `课程_<chapter>-<section>.md`。状态、证据、未确认草稿和配置历史位于 `logs/interactive-tutor/runs/<run-id>/`。

学习材料采用 v2 清单：单份直接展示 `学习材料/<材料文件名>.md`，多份展示 `学习材料/<材料名>/<材料文件名>.md`，重名用来源 ID 区分。本地资源保留材料内相对结构，跨目录资源映射到 `_resources/`；Python 仅改写展示副本的本地引用，不修改源文件、不下载远程资源。逐字节原件放 `artifacts/material-originals/<source-id>/<完整版本哈希>/<原项目相对路径>`，导航哈希和教学证据校验继续读取原件归档，面向用户的图片指引使用浅层展示路径。

`artifacts/学习材料备份.json` 保存 `backup_path/sha256/size_bytes` 原件记录及 `display_path/display_sha256/display_size_bytes` 展示记录。旧展示版本归档到 `artifacts/material-history/`，历史清单仍位于 `artifacts/material-backup-history/`；当前展示、清单和学习文档同事务发布。v1 旧清单保持可读取，不自动搬移旧项目。本次修改不迁移已有运行。

共享 Python 备份子状态机为 `planning_backup → awaiting_backup_approval → copying_materials → verifying_backup → backup_ready`，错误进入 `paused_backup_error`，按计划哈希恢复。新建 `start` 先等待名称建议或名称确认；`confirm-project-name` 创建项目检查点后返回完整待创建路径和 `backup_plan_sha256`，项目停在 `backup_required`，不得准备课程。一次性展示全部路径并取得用户批准后，用 `resume --backup-plan-sha256 <批准的计划哈希>` 恢复；增量同步则重新执行 `supply-navigation --navigation <导航JSON> --backup-plan-sha256 <批准的计划哈希>`。名称确认和备份批准是两个独立门禁。新项目、旧项目补建和增量更新使用同一脚本，不使用任务专属复制脚本。

计划或材料改变导致哈希改变时需重新确认；失败重试复用同计划批准和已校验文件。不同内容的同名目标必须暂停，让用户选择保留、覆盖或更新，脚本不自行覆盖。每次调用校验当前清单及所有副本；后续教学证据和图片指引读取项目内副本，原文件变化或移走不影响教学。副本损坏须修复，不自动改用源文件。旧项目在下一次继续时走同一确认与补建流程；已有备份的项目必须显式 `supply-navigation` 才能切换导航及材料版本。

所有题目、答案、进度和学习文档先保存 JSON，再由共享 Python 脚本渲染 Markdown；对话只显示 Markdown。课程 JSON 保留参考答案和逐知识点评分点；课程 Markdown 不显示参考答案。不要直接手写最终表格或长结构化输出。模板由脚本预填，Agent 只补教学要点、题干、评分点和必要语义判断。

面向用户统一称“知识点”“课程”。知识点依赖图来自导航，课程依赖图来自课程规划；两者不得混用。一个课程可讲多个知识点，一个知识点可由多课讲解、复习或应用。未显式跳过的知识点至少有一个有效课程覆盖，单纯前置引用不算覆盖。

章节由整体学习板块划分。新增课程先按内容归入章节，再追加该章节小节编号，不重排既有编号；稳定 ID 与展示编号分离。课程实际顺序由依赖图决定。脚本校验课内知识点顺序、跨课提供者、覆盖和无环性。主线依赖的基础课程必须纳入主线；明确暂缓执行的要点保持支线，冲突时分离基础讲解与具体执行。支线可暂缓、明确跳过并恢复。

## 命令与状态机

备份状态迁移由共享脚本显式校验，非法迁移拒绝；`paused_backup_conflict` 表示需要处理整批目标冲突。批准清单包含材料副本、正式清单及本次新增历史清单。资源解析忽略代码示例和未使用的引用定义，解析实际使用的内联、引用式、HTML 和 Obsidian 引用，并递归复制关联 Markdown；绝对本地引用需要先确认可迁移方案。

初始化先保存 `backup_required` 检查点，发布失败后可继续同一项目。同步新版导航时，已校验的新清单、项目 JSON 与 Markdown 视图通过同一文件事务发布，失败回滚，旧版本仍可使用。原始材料若与脚本写入路径重合，拒绝写入；自定义项目目录不能位于 `runtime/` 或 `logs/`。

后续命令统一使用 `--project-dir <项目目录>` 或 `--run-dir <日志运行目录>`。

```text
start --root . --request <request.json>
status / resume / report / verify
supply-navigation --navigation <新版导航JSON>
prepare-lesson [--lesson <课程ID> --allow-missing]
publish-lesson --decision <decision.json> [--quality-review <复核JSON>]
check-answers [--lesson <课程ID>]
submit-chat-answer --answers <逐题答案JSON> [--lesson <课程ID>]
review-answers --review <review.json>
deliver-feedback --lesson <课程ID> --review-sha256 <批改指纹> --feedback-sha256 <反馈正文哈希>
questions --question <用户疑问> --answer <已完成回答> [--evidence-refs <来源> ...]
questions --no-questions（仅兼容旧版已发布课程）
plan --patch <计划补丁JSON>
confirm-plan --revision <计划修订号>
apply-adaptation --decision <反馈小决策JSON>
choose-route --choice <继续|支线课程ID或编号|later> --choice-sha256 <路线哈希>
skip / restore --target-type unit|lesson --target-id <ID>
note-prepare --draft <笔记小草稿JSON>
note-confirm --draft-hash <hash> --confirmed-by <用户>
```

教学启动可停在 `backup_required`，批准备份并校验完成后，新正文要点导航进入 `lesson_plan_required`，确认计划后进入 `ready`；之后为 `ready → lesson_decision_required → podcast_required（开启播客时）/awaiting_answer → review_decision_required → processing_learning_feedback → adaptation_decision_required/ready`。每个阶段校验合法状态；失败保留检查点，修正输入后继续。`resume` 不自动跨过反馈答疑、计划确认或选路。最终无待学且所有未跳过知识点均完成覆盖时进入完成状态。

## 一课的流程

1. `prepare-lesson` 选择可学主线，结合候选路线再安排支线；先检查脚本生成的 `plan-context.json` 或证据包 `course_grouping_review`，按主题、前置关系和学习负担判断组合机会，必要时用 `plan` 小补丁调整，不按字数合并；为本课知识点准备来源证据和精简决策模板。诊断与已批改结果只用于调整深度，不能据猜测直接完成知识点。
2. Agent 阅读证据，补概述、核心要点、公式、讲解与正式习题。每个本课要学习的知识点至少有一道正式习题；多知识点问答题必须有独立评分点。容量不足时拆课。事实引用当前证据，资料外解释显式标记，动态事实先查可靠资料；保持 Bloom 递进和必要支架，参考 `references/teaching-methods.md`。
3. `publish-lesson` 校验覆盖、数量及证据后保存课程 JSON、渲染 Markdown。标题下方在播客完成时引用 MP3；依次列前置知识、本课知识点、课程概述、课程讲解、思考与讨论、正式习题，保留双语术语和资料图片指引。开启播客后先停在 `podcast_required`，不写公开课程 Markdown、不接受作答；读取播客 `pending_decisions`，通过 `resume --podcast-response <小JSON>` 继续，完成验证后同事务发布课程和作答状态。
4. 用户在明显的逐题引用块中填写回答，或聊天回答后转成小答案 JSON。只允许编辑作答块。`submit-chat-answer` 保留用户原话并回填对应作答块；两个入口共用答案校验。选择题由脚本判分，问答由 Agent 填少量批改字段。每题反馈不可空白，逐题反馈放在对应作答块后，课末“本课批改总结”保存整体优点与不足（历史课程保留旧标题），均由脚本生成；不将答案、反馈写进学习报告。未答不能冒充已评分，作答检查后发生变化必须重新检查。
5. `review-answers` 自动维护学习路线、总结、学习报告和错题本；已学与掌握等级分别记录。**必须先原样展示返回的完整 `feedback_markdown`**（逐题作答、批改意见、完整参考答案、简略解释、差异及本课总结），展示后调用 `deliver-feedback` 留痕，再在同一轮继续后续流程。不得仅报总分或“批改已完成”，不得把文件写入当作已展示。问答与参考答案差异较大时，在脚本预填的小批改模板补充 `difference_notes`；`reference_explanation` 可补充简略解释。
6. 新发布课程在习题之后新增“学习反馈”：可选难度 A 过于简单、B 难度适中、C 过于困难（空白默认 B），及可选疑问/其他反馈（空白表示没有）。不计正式习题数量、不计分。批改后不再另问是否有疑问：空白评论自动处理难度信号；非空评论先分类，有疑问先回答，再保存下一课调整。历史已发布课程保留原答疑兼容流程，不自动重写讲义和批改。

## 文档、笔记和错题

- `学习路线.md`：双清单、双依赖表、知识点与课程双思维导图、主支线、状态及图谱页指引，每课后维护。知识点图紧接“知识点与前置关系”标题、位于表格上方；课程图紧接“课程与前置关系”标题、位于表格上方，不另设思维导图小节。双图采用 Markdown 的 Mermaid `flowchart LR` 代码块，不调用生图工具。知识点标签为“名称｜学习状态”，课程标签为“编号 标题｜主支线｜学习状态”；箭头统一表示“前置 → 后续”，保留多前置、孤立及已跳过节点，退出的历史课程不进入当前课程图。
- `总结.md`：累计每课内容概述、核心要点与重要公式，每课后生成/更新。
- `学习报告.md`：项目层面的学习评估。固定栏目为学习进展、知识点掌握与证据、阶段优势与薄弱点、未验证能力、下一步学习与复习建议。脚本生成统计和课程反馈链接，区分学习完成与掌握，限制结论范围；选择题不能证明自主解释和迁移能力。使用 `interactive-tutor-report-v3.schema.json`，不累计逐课作答反馈。
- `笔记本.md`：用户指定内容后，Agent 提取并补背景、整理描述。先 `note-prepare` 展示草稿，明确确认对应哈希后才 `note-confirm` 写入；未确认稿仅在日志。脚本验证草稿哈希与来源课程哈希，草稿或来源变化后必须重新展示并确认；确认状态单独保存。
- `错题本.md`：错误或不完整回答自动入本。Agent 在批改模板补薄弱点、普遍规律和纠正建议，脚本去重归并；标签加粗，每条归纳中的例题从 1 编号，并注明课程。每个评分不足的知识点必须有对应归纳，选择题例子包含全部选项。选择题判分直接根据已存作答及参考答案计算，不依赖可编辑批改模板。

## 课程播客

逐课子状态机为 `prepared → preparing_input → invoking_podcast → verifying_outputs → linking_lesson → publishing_audio → completed`；出错进入 `paused_error` 后恢复原播客 run。播客 skill 及转换、TTS 子运行仍使用自己的状态机和日志，产物目录通过 `utils/scripts/artifact_location.py` 固定绑定到 `artifacts/podcasts/<lesson-id>/<正文与配置指纹>/<podcast-run-id>/`，中间产物在其 `intermediate/` 下。恢复同一运行沿用该目录；重新发布课程使用新 run ID，避免同正文再次发布时与历史音频冲突。普通独立调用保持默认布局；公开播客 CLI 可通过 `start --artifact-dir` 指定项目内输出位置。

使用课程教学正文快照，绑定课程记录来源；前置知识、本课知识点清单、正式习题自动排除，保留教学概述和讲解。答案、评分和本课学习反馈不参与播客输入或指纹。问答转换仍按源块填写小模板；不确认逐字稿、不试听。成功后标题下生成 `[🎧 收听本课播客](../播客/课程_X-Y.mp3)`；验证 MP3、子运行、配置、引用哈希及链接。`publish-lesson --podcast-mock` 仅供离线接口测试，不用于真实课程交付。

## 动态路线和跳学

双思维导图由 `utils/scripts/mermaid_flowchart.py` 按 `utils/references/dependency-flowchart-v1.schema.json` 和 `utils/templates/dependency-flowchart.template.md` 确定性生成；输入为 `nodes[{id,label,status,track?}]`、`edges[{from,to}]`，由脚本直接从双依赖图构造，Agent 不编写图代码、不维护第二份图数据。稳定 ID 转换为安全节点 ID，脚本转义标签中的 Mermaid、HTML 和 Markdown 特殊字符。

统一保存入口运行文档子状态机 `prepared → validating_graphs → rendering_documents → verifying_documents → publishing → completed`，合法迁移由共享检查点实现校验；失败进入 `paused_error`，检查点保存在导师日志的 `document-render/state.json`。下一次 `resume` 读取并校验未完成检查点及迁移历史，从 `paused_error → validating_graphs` 重新校验权威 JSON；中断阶段先记录暂停再重新校验，不清空失败历史，检查点损坏时拒绝覆盖。教学与答疑门禁保持有效。JSON、依赖表、双思维导图、教学状态检查点和计划模板在同一事务发布；失败回滚，且不推进调用方内存模型的版本。已完成发布的下一次保存开启新文档生成周期。

`supply-navigation`、`plan`、`skip`、`restore`、课程发布和批改等所有经过统一保存入口的操作同步重生成双图；依赖、标题、编号、主支线和状态变化均不能保留旧图。`verify` 复核图中节点、边、标签、状态及整份 Markdown 的一致性；旧项目用 `resume` 补齐图示，单独 `verify` 不修改文档。

`plan` 接收 `base_revision` 和少量 `operations`：新增章节、增加/调整/退出课程。新增内容先固化资料，再调用导航技能更新知识点图，然后同步课程计划。已发布课程保留历史，需要替代时新增课程并退出旧课程；禁止删除唯一覆盖且没有替代的课程。未发布课程准备后仍可用 `plan` 退回规划阶段拆课，再重新准备；重新发布恢复的课程时保留此前讲义和作答版本。

调整参考当前仍合法的候选序列及已批改证据，保留旧候选版本，重新校验双图。新问题通常形成支线，但为主线提供必要基础时不能保持可选。

跳过课程不自动跳过其知识点，也不删除依赖边。用户明确要越过待学基础时才使用指定课程加 `--allow-missing`；后续课程的前置表必须指出待学基础来自哪个小节。恢复后可补学。网页右键保存待同步屏蔽/恢复指令，下次调用在安全阶段自动消费、去重并维护文档。暂缓支线不等于显式跳过，仍属于待学。用户明确屏蔽当前未批改课程（或它的全部知识点）时，允许退回可选课状态；已提交答案则先批改、再处理学习反馈，不用屏蔽绕过该门禁。课内疑问可随时通过 `questions` 留痕，只有旧版课后答疑状态才允许 `--no-questions`。

## 正文覆盖与新课程协议

课程规划必须覆盖每个正文要点，包括暂缓具体执行的要点（安排为支线）。学习路线以“材料覆盖”表展示要点、主支线、知识点、课程及教学块；只有 explained 块算讲解完成，前置引用、标题提及和 mentioned 不算。每个学习目标也须被讲解块覆盖。新正文要点导航初始化按同章同路线分组，进入 lesson_plan_required；Agent 检查并通过小计划补丁合并/拆分后，用 confirm-plan --revision 确认，不能机械采用一知识点一课。

五类教学块仅为填写建议：概念解释、操作步骤、具体示例、适用边界、迁移应用，按实际需要选填，不要求每课凑齐。lesson.reference_chars_min/max 默认 800/1500，软参考仅针对整课“课程讲解”小节的正文合计，不是整篇 Markdown，也不是每个知识点或教学块的字数。课程概述、思考与讨论、正式习题、作答及反馈等不计入。低于 800 字须补充讲解，或填写学习目标如何完整覆盖的“短而完整”理由；超过 1500 字不强制截断、拆课。简短材料不靠重复扩写；材料外说明显式标记。

共享 Python `utils/scripts/learning_teaching_quality.py` 按正文可见非空白字符计数，排除标题、Markdown 标记和链接地址；英文按字符，公式和代码内容计入并分别报告长度。只有单个知识点不是内容完整的证明，也不为凑字数组合课程；规划按主题、依赖和学习负担检查组合机会，旧导航新规划同样检查，历史已发布课程不自动重写。

教学模板按目标和原文要点预填候选块，默认 mentioned；Agent 填正文后判断实际 explained 覆盖，可按语义合并块，不能把关联字段齐全当作讲解充分。偏短正文、可见正文不足 80 字符的 explained 块或同块关联多个目标/要点进入复核；80 字符仅用于发现疑点，不是教学块的长度门禁。日志复核采用 `learning-teaching-review-v1.schema.json`：脚本预填课程 ID、指纹和待复核 block_id，Agent 只补 short_complete_reason 及 items[{block_id,judgment,reason}]，judgment 为 sufficient/needs_expansion。理由须说明目标及要点如何被讲解覆盖，不能只说明知识点数量。

讲解复核子状态机为 `prepared → measuring → review_required → validating_review → completed`；需补充进入 revision_required，归档本次判断及理由，必须修改教学正文后重新复核，不能仅把判断改为 sufficient，也不能通过调整配置或课程计划绕过正文补充。日志、模板和结果保存在导师日志的 `teaching-quality/<lesson-id>/<fingerprint>/`；正文、课程计划、导航或有效配置变化后指纹失效，旧复核拒绝复用。`publish-lesson --decision <JSON> [--quality-review <小复核JSON>]` 返回 teaching_review_required/teaching_revision_required 时退出码为 3，不发布、不进入播客、不接受作答；补充正文或提交复核后重新调用。历史 v5 讲义结构与 Schema 保留；新发布使用 v7 parts，统计与已接受判断随课程记录保存；冻结完整课程计划（含章节、前置提供课程及其他课程）、有效配置和教学依据并校验指纹。verify 复核 v5/v6/v7 教学块、派生正文、Markdown 正文及计数的一致性。重新发布保留旧复核快照并验证历史；计数规则分版本兼容已发布课程。重复调用不覆盖已填写的复核模板，非法输入不改变已有复核检查点。

新发布课程在正式习题后增加学习反馈：可选难度 A 过于简单、B 难度适中、C 过于困难（空白默认 B），以及可选疑问/其他反馈（空白表示没有）。两项不计习题数量、不计分。批改后不另问是否有疑问；空白评论直接处理难度信号，非空评论由 Agent 分类，有疑问先在 awaiting_feedback_questions 回答并留痕，再 apply-adaptation 保存下课调整。历史已发布课保持原答疑兼容流程，讲义和批改不自动重写。

下一步同时存在可学主线和支线时进入 awaiting_route_choice，展示候选并提示到 Web 图谱页查看详细路线。回复“继续/下一课”默认主线，沉默不推进；多条支线需明确选择。开启支线后先推进其后续支线，再回主线。主线结束且有未学支线时进入 awaiting_branch_continuation；暂缓后为 main_completed，支线仍待学且可恢复，不标记全项目完成。选择绑定路线快照哈希，过期后刷新。

v5/v6/v7 发布前，共享 Python 比较诊断与历史题干、选项（忽略排列）和近似度，命中后需改题或填写 reuse_review[{history_id,reason,purpose}]。完全重复仅允许明确复测；不同任务的语义辨析由 Agent 判断。报告保留复测的证据限制，不将复测答对当作新增迁移证据。脚本轮换四个正确选项位置。

导航默认 v3：Agent 必须读全文，Markdown 标题仅作参考，划分更细致要点。脚本生成正文片段和小模板，resolve-points 验证片段无遗漏及来源哈希，再进入来源导航决策；所有要点必须进入导航与课程。非知识内容才可注明忽略理由。

课程 v5 的 blocks 字段为 {block_id,kind,text,objective_indices,point_ids,coverage}，coverage 为 explained/mentioned；脚本扩充正文与覆盖表。聊天格式兼容逐题键值，新格式为 {answers:{题号:原话},learner_feedback:{difficulty,comment}}，文件有独立反馈标记块。Agent 仅填反馈小决策 feedback_kind/rationale/operations；下课 adaptation_applied{feedback_id,changes} 绑定调整依据。

通用逻辑固化到 utils/scripts/learning_content.py、learning_question_quality.py、learning_route.py，与既有项目、文档和播客状态机协作。新最终音频为 `<项目目录>/播客/课程_X-Y.mp3`，已验证原件和历史中间件仍保存在 artifacts/podcasts/；音频、引用和状态同事务发布，失败回滚。历史音频不自动移动。

Web 两图采用主线实线、支线虚线边框及指向支线的虚线入边，并加主/支角标和图例；API 节点显式返回 track。保留状态填充色、正在学习金色描边与关系外圈。主线已完成、支线暂缓的项目留在进行中。

新课程的公共 `publish-lesson` 入口要求 v7 parts，旧 v4/v5 讲义和批改保留读取兼容。教学块模板预填 ID、目标索引和要点引用，Agent 只补正文与必要判断。增量导航同步重新进入课程规划检查，清除过期选路；快照绑定候选、知识点进度、依赖和路线，屏蔽/恢复后刷新，默认主线采用当前推荐次序。新课答疑回执不再次询问是否有疑问。

播客已发布而末尾检查点中断时，`resume` 验证已发布讲义、音频及子运行后补齐检查点，不重复发布。音频与讲义事务包含二进制回滚测试。

## 课程讲解排版（v6 基础兼容）

v6 历史课程使用 `interactive-tutor-lesson-decision-v6.schema.json`。新课以文末 v7 契约为准。教学块仍关联稳定 ID、类型、学习目标、材料要点与 explained/mentioned 覆盖判断，增加结构化 `parts`；块级和知识点级 `text` 均由脚本派生。脚本预填 `parts` 的短段落模板；Agent 按语义补少量短正文，解释分段、并列用无序列表、步骤用有序列表、关键概念及结论局部加粗。长讲解使用多个 parts，不机械按字数切段、不要求每节凑齐类型。

| type | 字段 | 排版 |
|---|---|---|
| paragraph | text | 独立段落；支持局部 **加粗** |
| unordered_list | items | 无序列表；保留列表项内换行 |
| ordered_list | items | 脚本自动编号 |
| formula | latex、可选 caption | 独立 LaTeX 公式块 |
| code | code、可选 language/caption | 自动选择安全代码围栏 |
| quote | text、source、可选 url | 引用块与来源；链接限 HTTP/HTTPS |

有需要时加入公式、代码和引用。直接引文必须填写来源；材料内引用与资料外解释继续遵守证据规则，不能把无法核实的内容冒充原话。`utils/scripts/learning_teaching_layout.py` 负责结构校验、空行、缩进、编号、围栏和来源排版，`learning_content.py` 统一扩充知识点正文。

排版子状态机为 `prepared → validating → rendering → verifying → completed`，失败为 `paused_error`，中断或失败恢复时重新校验结构；检查点位于导师日志的 `teaching-layout/<输入指纹>/state.json`。随后进入既有覆盖、讲解质量、题目质量和播客门禁；Markdown、可见字数统计、复核指纹、播客输入及 verify 使用同一派生正文。公式和代码内容计入原有字数统计。v6 正文保留代码标识符、公式、引文及来源链接原文，不进行全局术语替换；历史 v4/v5 保留原有呈现规则。

只改善新发布课程，历史 v4/v5 讲义、作答与批改保留读取兼容，继续/批改不会自动转换成 parts；显式重新发布使用 v7 并保留旧版本。

## 项目详情、名称与可恢复归档

项目 block 显示 `创建时间：<时间>` 和 `最后一次学习时间：<时间>`，无学习记录显示“尚未学习”，无法可靠确定创建时间显示“未知”。新项目独立保存 created_at/last_learning_at；历史创建时间可从由 timestamp.py 生成的项目 ID 恢复，学习时间仅从可靠教学事件恢复，不使用文件修改时间。所有时间保持本地、不带时区的 YYYY-MM-DDTHH:MM:SS。

时间下方按钮依次为：第一行“详情｜删除”，第二行“打开项目目录”，第三行“跳转到图谱页”。点击卡片主体等价于点击详情；其他按钮不触发详情。主体与操作区采用独立按钮，支持键盘操作。

项目页右侧打开详情分屏：顶部为项目名称编辑框、保存、图谱页、删除及关闭；下方默认展开基础信息（ID、状态、时间、材料、目录）、当前课程、教学阶段、下一步事项和课程/知识点/主支线统计。知识点前置关系、课程前置关系和材料覆盖默认折叠。完成率为已学/全部节点，跳过另列；掌握度只统计已有评分证据。仅导航项目显示尚未开始教学、尚未规划课程，并保留导航中的材料要点覆盖记录。下一步展示具体操作建议，不重复教学阶段名称。窄屏上下排列。

详情由权威 JSON 派生，与学习路线.md 使用相同双图和覆盖逻辑，不解析 Markdown、不暴露题目参考答案。重命名仅修改展示名称，目录和稳定 ID 保持不变；导师和导航名称独立。导师名称同步项目 JSON 与学习文档，导航名称保存在共享展示登记、不修改共享导航内容；项目发现入口使用同一登记。

删除先明确提示可恢复，再执行归档。导师目录移入 outputs/interactive-tutor/archives/<项目ID>/，原始材料、共享导航和日志保留；归档后上游导航重新单独显示。仅导航项目归档展示登记并隐藏卡片，导航文件保留。项目页提供可恢复归档列表及恢复按钮；恢复导师目录到原位置，保留 ID、名称、课程、作答和进度，冲突拒绝覆盖。

共享实现为 utils/scripts/learning_project_management.py，登记位于 outputs/interactive-tutor/project-management.json；管理状态机为 prepared → validating → executing → verifying → completed，失败进入 paused_error，按绑定同一请求的操作 ID 重试。每次管理运行的目录名由 timestamp.py 生成，检查点保存在 logs/interactive-tutor/runs/<run-id>/project-management/。目录解析、版本检查、锁、归档恢复和幂等由 Python 执行；恢复检查点绑定项目 ID、目录、类型及操作前登记/教学修订；中断后出现其他管理或学习变更时报告版本冲突，拒绝旧操作覆盖新状态。已完成操作重复请求只返回原结果。管理操作不推进教学阶段、不更新最后学习时间。发布课程、提交答案、批改、答疑及反馈调整等教学记录成功提交才更新学习时间；浏览、轮询和设置修改不算学习；等待播客或重新发布但播客尚未验证完成时不计为新的学习活动。

新增接口 GET /api/archives、POST /api/projects/<id>/manage。管理请求字段为 actionId、expectedRevision、operation，operation 为 rename/archive/restore/open-directory，rename 额外携带 title。恢复版本来自归档列表，其他版本来自项目快照；重试必须复用同一操作 ID 和请求。浏览器仅提交登记的项目 ID，Python 服务解析目录并调用系统文件管理器。

## 批改反馈先于下一课

批改成功后，必须在对话中原样展示 `review-answers` 返回的 `feedback_markdown`，再执行 `deliver-feedback --lesson <课程ID> --review-sha256 <批改指纹> --feedback-sha256 <正文哈希>` 留痕。展示后同一轮继续现有学习反馈、答疑、选路及下一课流程，不另问是否确认批改或是否继续；原有需要用户选择的门禁仍有效。最后一课也必须展示反馈。交付回执由 Agent 在展示后提交，不代表脚本能独立验证聊天界面；禁止把文件生成成功当作已展示。

对话模板固定为“课程 X.Y｜习题批改”，按原题顺序逐题显示题干（选择题含全部选项）、你的作答原话、逐知识点评分、批改意见、完整参考答案、简略解释、与参考答案的差异，最后给出掌握优点和需要加强。参考答案保持完整；选择题额外显示正确选项及内容。问答题允许等价表达，不能仅凭字面不同判错。用户作答与参考答案差异较大时，Agent 必须在小批改模板的 `difference_notes` 明确指出缺漏、混淆或相反结论，可在 `reference_explanation` 补充简略解释；评分不足仍须填写已有逐知识点错题归纳。Python 预填参考解释候选；未补充时，解释复用评分点，差异复用脚本选项比对或已有错题归纳，不由 Agent 重写长反馈正文。

共享实现为 `utils/scripts/learning_review_delivery.py`，使用 `interactive-tutor-review-delivery-v1.schema.json`。子状态为 `pending → rendering → awaiting_display → delivered`，失败进入 `paused_error`，从 `paused_error → rendering` 恢复。记录绑定课程、题目、作答和批改内容指纹及反馈正文 SHA-256，合法迁移和历史由 Python 校验。批改与待交付记录、反馈 Markdown 随现有文档事务一起保存；发布失败回滚，按原批改输入重试，不留下已交付标记。检查点及反馈正文位于 `logs/interactive-tutor/runs/<run-id>/review-delivery/<批改指纹>/`，正式课程 JSON 保留对应权威记录。时间统一调用 `timestamp.py`。

`prepare-lesson`、`publish-lesson` 在待交付时返回 `feedback_delivery_required`（退出码 3）与完整反馈，不推进下一课；`status/resume/report` 同样返回待展示反馈，不自动交付。`review-answers` 保留原教学状态回执，新增 `delivery_status/lesson_id/review_sha256/feedback_sha256`。`deliver-feedback` 使用项目或运行目录入口，校验两个哈希后留痕，重复相同回执不修改版本或时间，过期回执拒绝。`verify` 复核记录、正文、日志和迁移历史的一致性。历史当前课有批改但没有交付记录时，只在恢复/下一课入口补建待展示记录，不重新评分、不修改原批改；已进入后续课的历史批改不追溯补交付。

验收覆盖：未交付不能开课、展示并留痕后同一轮继续、完整参考答案与差异说明、聊天/文件作答、最终课、反馈调整/答疑/选路兼容、非法或过期回执、重复调用、事务失败恢复及日志篡改检查。

新提交作答由脚本登记 `review_feedback_protocol: 1`；批改必须保留 `reference_explanation/difference_notes`，不得通过删去字段绕过校验。新模板的问答作答与参考答案文字不同时，`difference_notes` 必须非空，说明具体差异，或明确说明等价表达及依据；此校验不决定分数。`reference_explanation` 在新模板中必须非空。历史不含这两个字段的批改仍可读取，复用原评分点和错题归纳生成反馈。非法批改、反馈渲染失败不改变调用方模型；恢复时按原作答和批改输入重试。

反馈恢复除 `paused_error` 外，还处理日志中的 `pending/rendering` 中断，先验证 Schema、课程绑定和完整迁移历史，再重新渲染。每次项目保存前核对上次提交的反馈正文和检查点，损坏时拒绝覆盖，`status/resume/report` 不会通过重生成掩盖损坏。Schema 按状态约束正文哈希、错误信息与交付时间，时间字段限定项目统一格式。

<!-- teaching-context-v7:start -->
## 材料上下文与步骤复核（v7）

新课入口要求 `interactive-tutor-lesson-decision-v7.schema.json`；历史 v4/v5/v6 继续读取，不自动重写。默认学习者没有读过材料。解释材料中的观点、人物或案例前，交代必要背景，按上下文需要插入材料引用块，再解释其与学习目标的关系；不要求每课机械插入引用。引用本身不能单独算作 explained，讲解块的短正文疑点统计排除引文，整课正文计数仍包含引文。

`prepare-lesson` 在证据包预填 `material_quote_candidates`（原文、出处、定位及小 part 模板）与 `ordered_list_template`。Agent 选择候选，必要时只填少量删改文字、背景及解释。材料引用保留原意；引用讲者的历史举例不能冒充已核实事实。普通解释以清楚的短句表达材料性质与核实范围，不机械拼接分号，不全局替换原文标点。

| 字段 | 契约 |
|---|---|
| type | `material_quote` |
| evidence_id | 脚本预填的原材料证据 ID |
| edited | false：脚本提取原文；true：提供删改正文 |
| edited_text | 仅 edited=true 时填写；不手写标记或出处 |
| resolved | 脚本派生的正文、来源及证据快照；输入值不被信任 |

删改引用的正文开头由 Python 添加 `（有删改）`，出处自动展示材料名称、章节与行号。去掉原文时间戳、删除句子或改写用语均使用 edited=true。保留现有通用 quote，用于其他注明来源的引文。材料引用验证来源文件 SHA-256、定位切片、证据 ID 与本知识点的关联；发布与 verify 复核相同派生正文。

步骤使用 ordered_list，示例另起 paragraph；并列使用 unordered_list。脚本扫描段落与列表项中的材料指代、连续步骤和材料性质/核实范围的分号疑点；每个材料引用（含原文引用）复核背景是否足够、是否支持当前讲解，删改引用额外复核原意。Agent 仅填写逐块 sufficient/needs_expansion 与具体理由；合理不引用或不拆列表可说明依据。沿用 `learning-teaching-review-v1.schema.json`，short_complete_reason 在本复核中留空。

共享实现为 `utils/scripts/learning_teaching_context.py`，子状态机为 prepared → validating → checking_context_layout → review_required（有疑点）→ validating_review → completed；无疑点直接 completed，需修订进入 revision_required，异常进入 paused_error。需要修改时必须修改教学正文或引用再复核；改变块 ID、覆盖标签、题目或配置不能绕过修订。检查点、候选、模板及结果写入 `logs/interactive-tutor/runs/<run-id>/teaching-context/<lesson-id>/<输入指纹>/`；输入变化使旧判断失效。非法或过期复核不改检查点，重复调用保留已填模板，中断恢复重新校验来源，并重放已归档判断。JSON 原子写入；损坏的既有依据拒绝覆盖。新复核依据冻结 policy_version=2；已发布且未保存该字段的 v7 记录按原规则校验，历史课程不自动重写。

`publish-lesson --decision <JSON> [--layout-review <小复核JSON>] [--quality-review <讲解复核JSON>]` 返回 teaching_layout_review_required/teaching_layout_revision_required 时退出码 3，保持 lesson_decision_required，不发布、不启动播客、不接受作答。来源校验与确定性排版后执行上下文复核，再继续覆盖、讲解质量、习题质量和播客门禁。正式课程保存复核依据与判断；verify 检查来源、复核、正文、历史版本及独立归档的 review.json。

契约由 `runtime/.venv/Scripts/python.exe utils/scripts/render_learning_teaching_context_contract.py` 生成；状态机为 prepared → preparing → validating → publishing → verifying → completed，异常进入 paused_error。检查点写入 logs/interactive-tutor/runs/<run-id>/contract-generation/，时间与 run ID 由共享 utils/scripts/timestamp.py 生成；使用 --run-dir 恢复，同检查点绑定同一生成输入。Schema 与各文档通过共享文件事务同时更新，失败回滚。回归覆盖原文/删改引用、来源损坏、上下文与步骤复核、过期/重复/中断恢复、引用单独冒充讲解、CLI 发布/verify、历史 v6/v7 读取、元数据绕过拒绝及契约生成回滚。
<!-- teaching-context-v7:end -->

## 具体场景示例

```yaml
scenario_examples:
  - id: learn-one-material
    user_request: "继续课程并批改我的答案"
    when_to_call: "基于导航开始或继续教学、作答、答疑、记笔记或调整路线"
    invocation: "导航 verify → start/resume → prepare-lesson → publish-lesson → check-answers/submit-chat-answer → review-answers → 学习反馈处理/必要答疑 → 分叉选路（如有）→ prepare-lesson"
    expected_output: "中文课程、双图学习路线、逐课总结与报告、确认后的笔记和规律型错题本"
```

## 使用与协议补充

功能调用、配置和运行协议的补充说明见 [使用与协议补充](references/usage-details.md)。
