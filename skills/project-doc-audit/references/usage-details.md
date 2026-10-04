# project-doc-audit 使用与协议补充

入口为 `runtime/.venv/Scripts/python.exe skills/project-doc-audit/scripts/cli.py`，支持 `start`、`status`、`verify` 和 `deliver`。它递归检查核心文档、`docs/**/*.md`、活动子目录下的 `README.md`、文件架构、插件与 Skills 注册、`VERSION` 与 marketplace 版本、`.env*` 键名，以及 `skills/`、`utils/`、`runtime/`（排除 `runtime/.venv`）中的 Python 第三方依赖、requirements 版本约束和虚拟环境实际安装版本，并检查文档内明确引用的项目路径。

对 `applications/*/application-audit.json` 声明的应用，脚本还读取 `package.json`、README、PRD、状态机和数据模型，比较应用版本、文档路径、实现状态、项目 JSON 字段和产物目录说明。每个应用的审计契约使用 `utils/references/application-audit.schema.json` 校验。

运行缓存位于 `logs/project-doc-audit/cache.json`，不提交 Git。报告位于 `outputs/project-doc-audit/runs/<run-id>/report.md`。状态机为 `prepared → discovering → checking_documents → checking_structure → checking_skill_catalog → checking_document_scope → checking_application_catalog → checking_catalog_consistency → checking_skill_scenarios → checking_environment → checking_dependencies → checking_applications → checking_commit_history → checking_versions → checking_changelog → validating_findings → rendering_report → verifying_report → completed`。Skill 还会校验各 `SKILL.md` front matter 的 `category` 与说明书分类是否一致，以及各 SKILL.md 是否包含结构化具体场景示例。Skill 只输出差异和修改建议，不自动修改文档；用户确认后再修改。

## 文件职责与用户语言审计

来源职责集中在 SOURCE_OF_TRUTH.md；AGENTS.md 保留简略文件架构、执行与阅读要求，并链接来源说明和 docs/architecture/README.md；CODE_OF_CONDUCT.md 维护行为边界。共享 `utils/scripts/catalog_audit.py` 检查缺失引用、完整来源清单残留和长规则逐行重复，并复用现有差异类型和报告字段。

文件架构文档须包含文件与目录说明，只介绍结构、用途、Git 跟踪边界和说明入口；检查重复时间戳章节、语音工作流及明确状态机或协议残留。Skill 实现内容迁出前核对所属 Skill 已链接的详细文档，已有内容去重，缺失的有效规则及历史兼容说明补入原文档。脚本能确定的结构、路径、生成结果与重复内容在既有 checking_document_scope 阶段检查，不新增状态或报告格式。

README 不出现功能表内部生成注释。共享生成器按真实 Markdown 标题及连续表格定位，两张表格与已确认目录数据一致；旧标记只作迁移输入，生成时移除。标题、表格缺失或不唯一时报告差异，写入前停止；保留表格后的简短使用入口及其他章节。表格生成复用现有可恢复发布状态机和文件事务，不由 Agent 手工维护。

README、Skills 与应用说明书、应用使用说明采用通俗语言：功能简介直接说明能做什么，操作说明解释怎么用；必要术语简短解释，名称、命令、参数和路径准确保留。Agent 只复核少量语义疑点及迁移等价性，关键词出现不直接判定违规；审计报告继续由 Python 从现有 findings Schema 渲染，不引入新的语义判断模板。确认后的文档边界规则必须落实到共享脚本，不能仅靠本次任务的临时脚本。

本地来源路径检查允许 `docs/PRDs/` 和 `docs/decisions/` 在新克隆仓库中缺失；目录存在时仍检查其中明确引用的文件，不把普通公开文档当作可选来源。

共享脚本对 README 全文检查专用实现章节和详细状态或结构化协议，不局限于功能表。用户语言检查只采集简介与功能、启动、操作说明中的少量专业表达线索，排除代码、内联标识符和专用技术章节；沿用现有报告字段，confidence=needs_review 表示待复核，不表示已判定违规。名称、命令与路径保持原样，是否需要解释由 Agent 核对上下文。每份文档最多展示三条线索，不增加输出 Schema 或新模板。

版本审计与历史生成的完整状态机、调用方式及小模板见 [Skill 版本与提交历史说明](../SKILL.md#版本与提交历史审计及生成)。start --scope staged 从暂存区核对待提交版本；审计不自动修复。
