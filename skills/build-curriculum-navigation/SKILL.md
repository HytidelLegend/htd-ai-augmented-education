---
name: build-curriculum-navigation
category: ai_assisted_learning
description: 从项目内一个或多个 Markdown 资料生成知识点依赖图、背景诊断和评分后的候选学习顺序。开始或继续 interactive-tutor 前必须调用；已有有效导航用 verify 校验，资料或目标变化时增量维护。
---

# 学习导航

运行前读取项目规范和 `docs/Skills、应用说明书.md`。入口为 `runtime/.venv/Scripts/python.exe skills/build-curriculum-navigation/scripts/cli.py`。只读取资料，不修改原文。先保存权威 JSON，再由 Python 渲染 Markdown；对话只展示 Markdown，不展示题目参考答案。

## 职责和前置关系

本技能统一负责背景讨论、目标确认、入学诊断、知识点划分及其依赖关系。每次导师调用前先调用本技能：有效导航执行 `verify`，无导航或资料变化执行构建/增量流程。不得让导师重复开展入学诊断。

导师未指定项目时，可先通过共享 Python 项目发现流程只读校验并列举本技能已完成且有效的本地 run，用户选定项目后再进入教学前校验；列表查询不更新导航配置。默认发布目录与日志中登记的自定义导航路径均可发现，未完成或损坏导航不进入选择列表。

背景不足时，在对话中询问用户当前水平、学习目的、相关经历和必要约束；已有档案先核对，缺失信息只问必要问题。不要生成一节用于询问基本情况的课程。不能把“待确认”当作已确认背景。

## 配置和产物

`${SKILL_DIR}/config.yaml` 保存诊断上限（默认 5）、候选数量上限（默认 10）和五项评分权重。不要在业务代码或 schema 重复写死数量。

每次执行冻结配置到 `logs/build-curriculum-navigation/runs/<run-id>/config.yaml`。再次运行比较有效参数，变化时更新冻结版本、保留历史，并向用户转述命令的 `config_message`：本次运行以新参数为准。已答题保留；降低上限不补题，提高上限不自动重启完成的诊断。失败后保留尚未送达的参数变化提示，成功返回后才确认送达。

`init-request --file <request.json>` 生成 v3 请求模板（兼容读取 v2）。填写资料路径、标题，可选 v2 学习档案。默认正式导航位于 `outputs/build-curriculum-navigation/runs/<run-id>/navigation.json`、同名 Markdown 和 `.sources/`；自定义路径必须位于项目内。日志、快照、决策模板和诊断中间件位于运行目录。时间统一调用 `utils/scripts/timestamp.py`。

正式 JSON 的 `planning_profile` 包含 `route_context`、`assessment`、`ordering`；历史候选保存在 `ordering_history`。依赖图表示知识点关系，不是课程关系。

## 状态机和命令

```text
prepare --root . --request <request.json>
status --run-dir <run-dir>
resolve-points --run-dir <run-dir> --source-id <source-id> --decisions <point-division.json>
resolve-source --run-dir <run-dir> --source-id <source-id> --decisions <source-decisions.json>
resolve --run-dir <run-dir> --decisions <decisions.json>
diagnostic-start --run-dir <run-dir> --level <已确认背景> --goal <学习目标> [--goal-ids <目标知识点ID> ...]
diagnostic-status --run-dir <run-dir>
diagnostic-question --run-dir <run-dir> --question <question.json>
diagnostic-answer --run-dir <run-dir> --choice 0|1|2|3|4|unsure
resolve-ordering --run-dir <run-dir> --ordering <ordering.json>
apply-loop-resolution --run-dir <run-dir> --resolution <resolution.json>
commit --run-dir <run-dir> --confirmed-by <用户> --preview-sha256 <sha256>
resume --run-dir <run-dir>
verify --root . --navigation <navigation.json>
verify --root . --navigation <navigation.json> --material-project <学习项目目录>
```

`prepared → point_division_required（v3，逐份）→ source_navigation_fragment_required（逐份）→ all_source_navigation_fragments_ready → ordering_decision_required → awaiting_approval → completed`。有环进入 `awaiting_loop_resolution`，用户决定调整后回到排序阶段。唯一合法顺序也必须经过诊断。来源变化、并发冲突和错误进入对应暂停状态，按原检查点恢复。

诊断子状态为 `question_required → awaiting_answer → question_required/completed`，一次只问一道题。配置、题目、回答、停止原因和未测区域均持久化。尚未完成时不得提交排序结果。

## 学习材料备份与增量引入

学习项目使用 `utils/scripts/learning_material_backup.py` 管理原始 Markdown 及其引用的本地图片、附件和关联 Markdown。按原始字节复制，不改输入源文件；远程资源不下载。保留原始相对目录结构，越界路径、链接路径、缺失本地资源和目标冲突暂停处理。

独立构建导航时仍读取原资料，正式 `.sources/` 只保存导航片段；导师创建项目或补建旧项目备份时才复制材料。已有备份项目的每次教学前校验使用 `verify --material-project`：读取项目副本，同时沿用原来源 ID、章节和图片定位，不要求原材料仍存在或保持不变。备份损坏不能自动回退到原资料。

原材料变化仅通过原有导航构建/增量流程引入，仍完成来源决策、诊断及批准；之后导师用 `supply-navigation` 显式同步导航并追加材料版本。导航变化后禁止自动切换教学材料；此前副本保留，旧课引用继续有效。共享备份清单 schema 为 `utils/references/learning-material-backup-v1.schema.json`，不得手写最终清单。

## 来源决策填写

导师同步使用同一共享备份状态机，并在项目数据发布事务中同时更新材料清单；未成功发布的材料版本不能替换当前教学版本。实际图片来源必须与知识点来源一致，未登记的图片来源禁止回退到输入源路径。

先完整阅读 `source-work/<source-id>/reading-manifest.json` 指定章节。对每批填写 `covered_candidate_ids` 和少量 `items` 补丁，使用 `prepare_navigation_decisions.py collect-source`、`collect` 扩充完整决策。调用 `resolve-source` 后再提交总决策；不得根据截断预览猜测未阅读资料。Agent 仅判断纳入、目的、难度、目标相关知识点、先修关系和简短理由。

## 目标优先的自适应诊断

通过 `--goal-ids` 指定与学习需求直接对应的目标；脚本展开前置范围，优先探测共享基础和缺少证据的重要主题，每题后根据观察选择向后续或向前置调整，或转到缺少证据的主题；不通过传播观察替代对其他知识点的检验。无需在任意拓扑顺序上二分，也不要求入学时评估整个图；未测区域保持未知，后续正式习题继续提供证据。

读取 `next_target` 与来源证据后，Agent 填小题目 JSON：`unit_id`、`prompt`、`options`、`correct_index`。四个实质选项后必须追加第五项“没听过 / 不清楚 / 没把握”。直接考查定义、辨析、应用或推理，禁止“你是否听过某名词”。第五项不能是正确答案。脚本先落盘参考答案，再返回不含答案的公开视图及 `diagnostic-question.md`；对话展示题干和选项。

用户回答后调用 `diagnostic-answer`。正确、错误、不确定分别记录为观察，不把一次答对扩展为整个分支掌握，不把未知区域标记完成。到上限或相关点已取样时结束，说明诊断仅为初步线索。同图、同背景和目标的已存诊断直接复用；变化时保存旧诊断并重新评估必要部分。

## 候选排序和发布

诊断结束后脚本生成 `candidate-orders.json`，保留全部实际生成且去重的合法序列，最多配置数量；明确是否穷举及停止原因。按目标匹配 40%、背景匹配 30%、难度平缓 15%、主题连续 10%、后续解锁 5% 评分并非升序排列；最高同分可推荐多个，记录分项分数和理由。

`ordering-decisions.template.json` 自动带入诊断和推荐序列。Agent 只确认候选、补少量取舍理由；不能丢弃候选或违反先修约束。正式产物保存所有候选及历史版本，路线调整时复用仍合法的候选。Markdown 由脚本生成候选表，不手写长 JSON/表格。

`awaiting_approval` 时展示预览、差异和验证结果，取得用户对当前预览的批准后 `commit`。来源哈希、定位、先修顺序、概念首次讲解与视图一致性必须通过验证。面向学习者的 Markdown 使用“知识点”“课程”，不使用 unit/lesson 术语。

## 正文要点划分（新请求）

v3 在来源导航决策之前要求完整正文划分。Markdown 标题仅作参考；Agent 阅读 `body-fragments.json` 对应全文，填写 `point-division.template.json`：每个要点含 `summary/fragment_ids/track/reason`，非知识内容放入 `ignored_fragments` 并说明理由。脚本检查来源哈希、片段无遗漏及有效引用，分配稳定 `point_id`，固化正文行范围；再生成来源决策小模板。不可因目标无关而删除正文要点；暂缓具体执行的内容保留为支线。重复、目录、署名等非知识内容才可注明忽略理由。

契约为 `utils/references/learning-point-division-v1.schema.json`。导航新增 `material_points[{point_id,source_id,fragment_ids,summary,track,reason,unit_ids}]`，来源定位新增 `content_span`；每个要点必须有知识点，不能被来源决策排除。脚本校验结构覆盖，Agent 负责语义粒度与真实性；增量变更同样执行划分，不使用任务特化脚本补洞。

正文划分沿用既有 `learning-point-division-v1` Schema，随发布归档到 `.sources/<source-id>/point-division.json`，不增加用户填写字段。正式 `verify` 再验证全文片段、忽略理由、知识点/要点双向引用、来源范围与主支线；遗漏或伪造片段会拒绝通过。`resolve-points` 失败记录原步骤和输入，修正后 `resume` 恢复，重复失败保留原恢复步骤。主线不能依赖明确暂缓执行的支线，应分离基础讲解与具体执行。

公共导航 `prepare` 即使收到兼容的 v2 请求，也必须经过正文要点划分；恢复已存在的历史检查点不自动迁移。新建调用不能通过旧请求版本绕过全文覆盖。

## 具体场景示例

```yaml
scenario_examples:
  - id: build-course-order
    user_request: "根据这些资料建立学习导航"
    when_to_call: "规划、增量维护，或每次导师调用前校验导航"
    invocation: "prepare → resolve-points → resolve-source/resolve → diagnostic-start/question/answer → resolve-ordering → commit → verify"
    expected_output: "知识点依赖图、诊断、全部评分候选和确定性 Markdown"
```

## 使用与协议补充

功能调用、配置和运行协议的补充说明见 [使用与协议补充](references/usage-details.md)。
