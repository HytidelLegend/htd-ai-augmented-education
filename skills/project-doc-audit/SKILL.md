---
name: project-doc-audit
category: project_function
description: 检查项目文档、文件架构、Skills 注册、版本、环境变量和 Python 依赖是否符合当前项目状态，保存缓存快照并生成差异建议。当用户要求审计项目文档、检查文档是否过期或核对项目说明与实际结构时使用。
---

# 项目文档审计

运行：

```text
runtime/.venv/Scripts/python.exe skills/project-doc-audit/scripts/cli.py start --root .
```

查看状态、验证和交付：

```text
runtime/.venv/Scripts/python.exe skills/project-doc-audit/scripts/cli.py status --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/project-doc-audit/scripts/cli.py verify --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/project-doc-audit/scripts/cli.py deliver --root . --run-id <run-id>
```

## 状态机

`prepared → discovering → checking_documents → checking_structure → checking_skill_catalog → checking_document_scope → checking_application_catalog → checking_catalog_consistency → checking_skill_scenarios → checking_environment → checking_dependencies → checking_applications → checking_commit_history → checking_versions → checking_changelog → validating_findings → rendering_report → verifying_report → completed`。

脚本实际按所列状态逐步推进，检查确定性事实并保存 `logs/project-doc-audit/cache.json`。文档哈希用于统计内容未变化的文档，所有文档仍执行检查，避免引用目标变化被遗漏；缓存不提交 Git。报告位于 `outputs/project-doc-audit/runs/<run-id>/report.md`。Skill 只提出差异和修改建议，不自动修改文档；用户同意后再执行修改。

路径检查只报告明确静态引用的缺失：支持仓库路径、相对文档目录的显式路径、应用契约声明的文档上下文、Skill 章节上下文和通配符。运行产物、未限定目录的数据或通用 Markdown 文件名、历史迁移输入和格式示例不要求已存在；代码围栏不作为内联引用或章节扫描。历史输入豁免要求引用紧邻历史标记且后文明确说明迁移、清理或归档，不因同一句中出现“历史”就跳过当前引用。应用状态从实现的 `Phase` 类型提取，支持单引号和双引号并排除注释，逐项核对 PRD 中的独立状态名，兼容链式描述及中文相邻文本。

`verify` 校验 findings Schema、日志与输出 JSON 一致性、差异计数和 Markdown 渲染一致性；`deliver` 同样校验，失败返回退出码 4 且不交付报告。旧运行沿用原报告格式进行一致性校验。回归测试位于 `skills/project-doc-audit/scripts/test_document_audit.py`，使用隔离环境运行 pytest，临时文件放在本次运行的日志目录中。

检查范围包括核心文档、`docs/**/*.md`、所有活动子目录中的 `README.md`、插件与 Skills 一致性、各 `SKILL.md` front matter 的 `category`、`docs/Skills、应用说明书.md` 中的 Skills/应用分类、标题层级和总览职责，以及各 SKILL.md 中的具体场景示例、`VERSION` 与 marketplace 版本、`.env*` 键名和 `skills/`、`utils/`、`runtime/`（排除 `runtime/.venv`）中的 Python 第三方依赖及虚拟环境安装版本。

`applications/*/application-audit.json` 声明每个应用的 README、PRD、版本来源、状态机和数据模型。脚本读取 `package.json`、实现源码、应用 README 与 PRD，确定性比较应用版本、文档路径、状态机、项目 JSON 字段和产物目录说明。应用审查不自动修改文件；应用快照缓存与报告仍分别写入 `logs/` 和 `outputs/`。应用契约 schema 位于 `utils/references/application-audit.schema.json`。

`category` 必须是 `project_function`、`ai_assisted_learning`、`ai_assisted_teaching`、`ai_assisted_research` 或 `common_tool` 之一。具体场景示例读取各 Skill 的 SKILL.md 中的 YAML `scenario_examples`，每项必须包含 `id`、`user_request`、`when_to_call`、`invocation` 和 `expected_output`；单独的 CLI 命令不算场景示例。


## 具体场景示例

```yaml
scenario_examples:
  - id: audit-project-docs
    user_request: "检查项目说明、Skill 分类和依赖是否与当前仓库一致"
    when_to_call: "用户要求审计项目文档、Skill 注册、文件结构或 Python 依赖时"
    invocation: "start → status/verify → deliver"
    expected_output: "生成确定性差异、修改建议及少量待复核线索的审计报告，不自动修改文件"
```

## 使用与协议补充

功能调用、配置和运行协议的补充说明见 [使用与协议补充](references/usage-details.md)。

## 总览文档职责审计

检查 README 功能表与 Skills、应用说明书的分类、顺序、成员和生成内容一致；检查四个二级标题及三级/四级分类的规定层级，教学、科研保留“暂无”。共享目录的八字段模板为 kind、name、category、subcategory、function、when、entry、document，功能描述为 1～3 句话，调用和启动入口应链接到现有公开契约。

README 使用“分类｜Skill/应用｜当前功能”；说明书分别使用“Skill｜功能｜调用时机｜CLI 示例”和“应用｜功能｜启动方式｜文档”。README、说明书只作功能与调用/使用入口；详细状态机、Schema、协议及 YAML 场景示例归入所属 Skill 或应用文档，退出码和时间戳保留简短公共约定。检查旧路径残留、重复归类、缺失能力及生成器内容职责；语义疑点只提出复核建议，不以关键词扫描代替完整语义判断。

共享检查实现在 utils/scripts/catalog_audit.py；目录解析、模板渲染与生成状态机在 utils/scripts/capability_catalog.py。生成采用 prepared → loading → validating → publishing → verifying → completed，错误进入 paused_error 后恢复 loading；写入通过共享文件事务回滚，生成检查点位于 logs/project-doc-audit/runs/<run-id>/catalog-generation/。不自动修复审计发现；`--check` 为只读一致性检查。

审计完整标题树（含子分类所属父级），并检查目录数据中的已确认成员归属、顺序及 Skill category，防止两份派生总览一起错误仍通过检查。README 功能表按实际标题和表格定位，检查额外的重复表格和详细协议；缺失 README、标题或表格以及定位不唯一时生成差异，不崩溃。检查阶段在实际执行对应检查前推进。

## 文件职责与用户语言审计

来源职责集中在 SOURCE_OF_TRUTH.md；AGENTS.md 保留简略文件架构、执行与阅读要求，并链接来源说明和 docs/architecture/README.md；CODE_OF_CONDUCT.md 维护行为边界。共享 `utils/scripts/catalog_audit.py` 检查缺失引用、完整来源清单残留和长规则逐行重复，并复用现有差异类型和报告字段。

文件架构文档须包含文件与目录说明，只介绍结构、用途、Git 跟踪边界和说明入口；检查重复时间戳章节、语音工作流及明确状态机或协议残留。Skill 实现内容迁出前核对所属 Skill 已链接的详细文档，已有内容去重，缺失的有效规则及历史兼容说明补入原文档。脚本能确定的结构、路径、生成结果与重复内容在既有 checking_document_scope 阶段检查，不新增状态或报告格式。

README 不出现功能表内部生成注释。共享生成器按真实 Markdown 标题及连续表格定位，两张表格与已确认目录数据一致；旧标记只作迁移输入，生成时移除。标题、表格缺失或不唯一时报告差异，写入前停止；保留表格后的简短使用入口及其他章节。表格生成复用现有可恢复发布状态机和文件事务，不由 Agent 手工维护。

README、Skills 与应用说明书、应用使用说明采用通俗语言：功能简介直接说明能做什么，操作说明解释怎么用；必要术语简短解释，名称、命令、参数和路径准确保留。Agent 只复核少量语义疑点及迁移等价性，关键词出现不直接判定违规；审计报告继续由 Python 从现有 findings Schema 渲染，不引入新的语义判断模板。确认后的文档边界规则必须落实到共享脚本，不能仅靠本次任务的临时脚本。

本地来源路径检查允许 `docs/PRDs/` 和 `docs/decisions/` 在新克隆仓库中缺失；目录存在时仍检查其中明确引用的文件，不把普通公开文档当作可选来源。

## 版本与提交历史审计及生成

来源和内容边界见 [SOURCE_OF_TRUTH.md](../../SOURCE_OF_TRUTH.md) 的“版本与提交更新历史”。`start` 默认核对工作区版本与已提交 HEAD 的第一父链序号；`start --scope staged` 从 index 核对下一个版本、版本三处同步、连续更新历史，以及与当前差异匹配的已完成摘要审查和发布记录。版本标题正确但审查缺失、差异指纹过期或生成文件未正确暂存，均产生 `changelog_review_missing`。历史提交的版本文件只在明确迁移边界之后执行递增校验，浅克隆或无法读取提交历史时报告前置条件缺失，不猜测提交数量。

更新历史只能包含版本标题与核心更新列表，最新在前，无核心更新时保留固定说明。检查重复、缺失、乱序版本以及工作区、日志、待办和日期章节；README 只保留当前版本和更新历史入口。结构检查不能证明摘要语义正确，Agent 必须逐次读取差异，不能只读提交标题或把未提交的新功能回填到旧版本。

生成是独立的明确入口，不由 `start` 审计自动修复：

```text
runtime/.venv/Scripts/python.exe skills/project-doc-audit/scripts/cli.py history-start --root . --mode history
runtime/.venv/Scripts/python.exe skills/project-doc-audit/scripts/cli.py history-start --root . --mode staged
runtime/.venv/Scripts/python.exe skills/project-doc-audit/scripts/cli.py history-status --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/project-doc-audit/scripts/cli.py history-resume --root . --run-id <run-id> --input <review.json>
runtime/.venv/Scripts/python.exe skills/project-doc-audit/scripts/cli.py history-verify --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/project-doc-audit/scripts/cli.py history-deliver --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/project-doc-audit/scripts/cli.py history-publish --root . --run-id <run-id>
```

实际生成状态机为 `prepared → collecting_commits → preparing_review → awaiting_agent_review → validating_review → rendering_preview → preview_ready → publishing → verifying → completed`。失败进入 `paused_error`，保存 `resume_stage`；`history-resume` 无输入不会越过待审查阶段，已完成运行只复核、不再次升版。`history-publish` 明确写入四个目标文件，不执行 Git add、commit 或 push；提交仍须获授权。

共用取证在 `utils/scripts/commit_history.py`，版本、历史渲染、审查校验与证据绑定在 `utils/scripts/version_history.py`；生成和审计复用相同的证据门禁。共享编排在 `utils/scripts/version_workflow.py`；`scripts/history_workflow.py` 保留兼容入口。请求、完整差异包、小模板、审查归档、状态及发布恢复记录保存到 `logs/project-doc-audit/runs/<run-id>/history/`，预览 `preview.md` 与去除差异正文的核验记录 `bindings.json` 保存到对应 outputs 目录。时间戳复用共享工具；发布复用文件事务、每运行锁及跨运行共用发布锁。写入中断后根据原始及目标指纹继续发布，出现第三种内容时拒绝覆盖。

`references/history-review.schema.json` 约束小输入：脚本预填 `schema_version`、`packet_sha256`、每次提交的 `version` 和证据编号；Agent 填 `updates[]` 的单行 `summary`、`evidence_ids`，以及 `skipped[]` 的 `evidence_id`、短 `reason`。不引用的证据须说明跳过理由，全部证据必须覆盖；空证据版本填写 `no_core_reason`。脚本分配版本并渲染 Markdown，不让 Agent 编写完整历史文件或哈希。

待提交差异只读取 index。VERSION 与更新历史的同步变动不进入摘要证据；README 和 marketplace 只忽略本项目版本值的变化，其他内容及文件模式变化照常取证。生成两份版本展示文件时同样读取 index；工作区存在未暂存的非版本内容时暂停，避免混入预览或覆盖用户编辑。历史条目从 HEAD 保留，明确的首次历史迁移例外沿用已经补齐的工作区历史；同次提交已有候选标题时重建该候选，不重复新增版本。工作区旧条目被改写时须先复核，不能静默丢弃。

HEAD 或实际暂存内容差异变化后旧审查失效。发布后的版本文件须重新暂存，再用 staged 审计检查；该检查核对差异包、审查输入、绑定记录、发布指纹及暂存中的四个生成文件。旧运行核验记录只对应其冻结输入，不冒充新暂存区的审查。缺少本地记录的已提交历史只作确定性结构核验，不能宣称摘要语义已复核；待提交审计须先通过本 Skill 建立本次审查记录。

`history-status` 返回完整状态，包括恢复阶段和模板路径；查询待审查运行也返回 0。恢复成功后清除旧错误与恢复阶段，输入格式错误不会被当成成功；预览尚未完成时 `verify/deliver` 返回 4，不交付部分结果。

回归测试为 `scripts/test_version_history.py`，覆盖首次提交、合并第一父链、浅克隆、历史迁移例外、接口、指纹失效、同次提交重复准备、未暂存版本展示内容隔离、过期摘要检出、跨运行发布锁及中断恢复；复用既有 findings 报告结构，只扩展差异类型枚举，旧报告保持可验证。

共享脚本对 README 全文检查专用实现章节和详细状态或结构化协议，不局限于功能表。用户语言检查只采集简介与功能、启动、操作说明中的少量专业表达线索，排除代码、内联标识符和专用技术章节；沿用现有报告字段，confidence=needs_review 表示待复核，不表示已判定违规。名称、命令与路径保持原样，是否需要解释由 Agent 核对上下文。每份文档最多展示三条线索，不增加输出 Schema 或新模板。

下一版本可通过 [update-project-version](../update-project-version/SKILL.md) 准备：新增用户确认门禁及最近一小时检查提醒。staged 审计同时识别该 Skill 的发布记录，核验差异、摘要、预览、用户确认及四个生成文件；对包含未跟踪新文件的候选，还要求全部候选已按审查内容暂存，拒绝缺失、内容不同或额外暂存的改动。旧 history-* 命令、日志路径、Schema 和恢复协议继续兼容。
