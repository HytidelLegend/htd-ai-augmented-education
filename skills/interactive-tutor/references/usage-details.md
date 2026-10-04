# interactive-tutor 使用与协议补充

未指定导航或已有导师项目时，执行 `start --root .` 或 `list-projects --root .`，共享 `utils/scripts/learning_project_selection.py` 只读发现本地导航 run，仅展示已完成且有效的导航；单候选也等待用户选择。Python 按 `learning-project-catalog-v1.schema.json` 生成“序号｜项目名称｜run ID｜更新时间｜状态｜已有导师项目数”，Agent 仅转交序号或候选 ID。有关联导师项目时再列出已有项目与“新建”选项，保留现有材料备份、作答及答疑门禁。

入口状态机为 `discovering_projects → awaiting_project_selection → validating_selection → awaiting_tutor_selection（可选）→ validating_selection → project_resolved`；无候选为 `navigation_required`。等待返回退出码 3；清单变化则刷新重选。新增 `select-project`、`select-tutor` 与 `resume-selection`，清单和选择状态保存在导师 `logs/` run，支持默认发布目录及日志登记的自定义导航路径。完整字段、命令参数及恢复规则见导师 Skill。

导航 run 检查点须通过 Schema 校验，同路径按最新 run 状态筛选，未完成或损坏状态不能经默认输出目录绕过筛选；同秒 run 后缀按数值排序。选择状态与清单同事务保存并支持失败回滚；恢复时清单未变则保留当前阶段与已选导航，避免重新询问第一阶段。

两个学习 skill 共用 `utils/scripts/learning_material_backup.py` 和 `learning-material-backup-v1.schema.json`（兼容清单版本 1.0/2.0）。新项目的 `学习材料/` 采用浅层可读副本；逐字节原件归档到 `artifacts/material-originals/`，历史展示到 `artifacts/material-history/`，清单在 `artifacts/学习材料备份.json`，原文件只读。备份状态机保持规划、路径确认、复制、验证及完成。当前展示、清单和学习文档同事务发布；导航及教学证据验证原件，图片指引指向展示副本，旧运行不自动迁移。原资料变化仍由导航增量及 `supply-navigation` 显式引入。

新建导师项目先经 `utils/scripts/learning_project_creation.py` 确认名称：单份建议材料名，多份阅读全部材料后提交小主题草稿；确认前仅写日志。命令新增 `creation-status/suggest-project-name/confirm-project-name`。启动和恢复回执提供项目绝对路径及相关文档链接。聊天作答回填逐题作答区，反馈放每题答案后及课末；学习报告使用 v3 Schema，记录项目进展、掌握证据、阶段优势和薄弱点、未验证能力及后续建议。学习路线双图位于对应前置关系标题下、表格上，错题本标签加粗、例题编号。

启用导师 `podcast.enabled` 后，`publish-lesson` 进入 `podcast_required`，通过 `resume --podcast-response` 驱动 `create-dialogue-podcast` 及其子状态机。音频验证成功后才发布课程与 MP3 引用。`utils/scripts/artifact_location.py` 固定跨 skill 产物目录，最终音频同课程文件名发布到学习项目 `播客/`，原文、逐字稿、历史原件及中间产物位于 `artifacts/podcasts/`；状态、日志和模板仍位于各自 `logs/`。默认独立播客调用继续使用原布局。

必须使用已完成新版导航，删除导师独立入学诊断及直接资料入口。每次调用先执行导航校验；课程与知识点是多对多关系，两张依赖图分别维护。未显式跳过知识点必须有有效课程覆盖，课程内外顺序都满足前置约束，主线基础不能留作可选支线。

备份子状态迁移由脚本校验，冲突停在 `paused_backup_conflict`；复制忽略代码示例和未使用引用定义，批准路径包括正式及历史清单。新清单与项目 JSON/Markdown 同事务发布，失败回滚；初始化失败保留待备份检查点。原始输入与写入路径重合时拒绝执行，材料产物不得进入 `runtime/` 或 `logs/`。

默认每课最多 3 个知识点、3 道正式选择题、2 道问答题，每个本课知识点均需正式习题检验。引导和追问不计入。良好 ≥80%、中等 ≥60%、其余一般，无评分证据单列。配置与课程播客开关位于 `skills/interactive-tutor/config.yaml`，冻结、更新及通知规则同导航。

状态为 `ready → lesson_decision_required → podcast_required（开启播客时）/awaiting_answer → review_decision_required → processing_learning_feedback → adaptation_decision_required/ready`。新课程批改后处理学习反馈并调整下一课，有疑问先回答；历史课程保持兼容。笔记先展示整理稿，确认后写入；错题自动归并薄弱点、规律、纠正与例题。

项目根目录只留 `项目.json`、`学习路线.md`、`总结.md`、`学习报告.md`、`笔记本.md`、`错题本.md`；课程写入学习项目的 `课程/` 子目录，文件名格式为 `课程_X-Y.md`（X、Y 为课程编号；这是运行产物命名示例），其余 JSON 在 `artifacts/` 和 `artifacts/lessons/`。先落盘 JSON（含参考答案）再渲染 Markdown，题目视图不显示答案。运行状态与中间模板继续位于日志目录。支持 `plan`、`skip/restore`、`supply-navigation` 维护计划、消费网页指令和同步双图。完整 CLI 及模板契约见 `skills/interactive-tutor/SKILL.md`。 未发布课程可退回规划拆分，恢复后重新发布保留旧版本；显式跳过当前未批改课程允许选择后续课程，已经作答仍先批改并处理学习反馈，历史已发布课兼容原答疑状态。笔记确认同时校验草稿与来源哈希，每个薄弱知识点都有错题归纳。

导师 `学习路线.md` 新增知识点、课程双 Mermaid `flowchart LR` 思维导图，不调用生图工具。Python 从现有双图按 `utils/references/dependency-flowchart-v1.schema.json` 和 `utils/templates/dependency-flowchart.template.md` 自动构造节点与边；知识点显示名称和状态，课程显示编号、标题、主支线和状态，完整保留多前置、孤立及已跳过节点，退出课程不展示。导航、计划或状态变化时，双图随 JSON 和依赖表同事务发布；`verify` 检查图示一致性，旧项目用 `resume` 补齐。

文档生成子状态机为 `prepared → validating_graphs → rendering_documents → verifying_documents → publishing → completed`；失败进入 `paused_error`，检查点在导师日志的 `document-render/state.json`。恢复读取并校验未完成检查点，保留失败和中断历史后重新校验权威 JSON；损坏检查点拒绝覆盖，不绕过教学及答疑门禁。教学状态检查点和计划模板随项目文档同事务保存，失败不推进内存模型版本。共享实现位于 `utils/scripts/mermaid_flowchart.py`，恢复能力复用 `utils/scripts/workflow_checkpoint.py`。

## 迁移协议补充

通用逻辑固化到 utils/scripts/learning_content.py、utils/scripts/learning_question_quality.py、utils/scripts/learning_route.py，与既有项目、文档和播客状态机协作。新最终音频为 `<项目目录>/播客/课程_X-Y.mp3`，已验证原件和历史中间件仍保存在 artifacts/podcasts/；音频、引用和状态同事务发布，失败回滚。历史音频不自动移动。

有需要时加入公式、代码和引用。直接引文必须填写来源；材料内引用与资料外解释继续遵守证据规则，不能把无法核实的内容冒充原话。`utils/scripts/learning_teaching_layout.py` 负责结构校验、空行、缩进、编号、围栏和来源排版，`utils/scripts/learning_content.py` 统一扩充知识点正文。

项目 block 显示 `创建时间：<时间>` 和 `最后一次学习时间：<时间>`，无学习记录显示“尚未学习”，无法可靠确定创建时间显示“未知”。新项目独立保存 created_at/last_learning_at；历史创建时间可从由 utils/scripts/timestamp.py 生成的项目 ID 恢复，学习时间仅从可靠教学事件恢复，不使用文件修改时间。所有时间保持本地、不带时区的 YYYY-MM-DDTHH:MM:SS。

共享实现为 utils/scripts/learning_project_management.py，登记位于 outputs/interactive-tutor/project-management.json；管理状态机为 prepared → validating → executing → verifying → completed，失败进入 paused_error，按绑定同一请求的操作 ID 重试。每次管理运行的目录名由 utils/scripts/timestamp.py 生成，检查点保存在 logs/interactive-tutor/runs/<run-id>/project-management/。目录解析、版本检查、锁、归档恢复和幂等由 Python 执行；恢复检查点绑定项目 ID、目录、类型及操作前登记/教学修订；中断后出现其他管理或学习变更时报告版本冲突，拒绝旧操作覆盖新状态。已完成操作重复请求只返回原结果。管理操作不推进教学阶段、不更新最后学习时间。发布课程、提交答案、批改、答疑及反馈调整等教学记录成功提交才更新学习时间；浏览、轮询和设置修改不算学习；等待播客或重新发布但播客尚未验证完成时不计为新的学习活动。

共享实现为 `utils/scripts/learning_review_delivery.py`，使用 `interactive-tutor-review-delivery-v1.schema.json`。子状态为 `pending → rendering → awaiting_display → delivered`，失败进入 `paused_error`，从 `paused_error → rendering` 恢复。记录绑定课程、题目、作答和批改内容指纹及反馈正文 SHA-256，合法迁移和历史由 Python 校验。批改与待交付记录、反馈 Markdown 随现有文档事务一起保存；发布失败回滚，按原批改输入重试，不留下已交付标记。检查点及反馈正文位于 `logs/interactive-tutor/runs/<run-id>/review-delivery/<批改指纹>/`，正式课程 JSON 保留对应权威记录。时间统一调用 `utils/scripts/timestamp.py`。

## 共享实现补充

课程讲解深度由 `utils/scripts/learning_teaching_quality.py` 子状态机复核：Python 统计整课讲解正文并生成与内容指纹绑定的日志模板，Agent 只补偏短完整性理由和有疑点教学块的判断。正文、规划或配置改变须重新复核；通过后才允许播客和课程发布，统计与复核随课程记录保存并由 verify 校验。`utils/scripts/learning_content.py` 生成按目标与要点分开的候选块及课程组合候选，不因字数自动合并。

课程播客由 `utils/scripts/learning_lesson_podcast.py` 驱动；独立日志路径由 `utils/scripts/tts_workspace.py` 保存。课程答案、逐题反馈、课末反馈和学习文档通过共享文件事务同步；`utils/scripts/learning_report.py` 按项目级证据渲染学习报告。材料展示副本和逐字节原件分别保存、分别校验，兼容旧版清单；具体保存位置遵守本文件前述材料备份与播客约定。
