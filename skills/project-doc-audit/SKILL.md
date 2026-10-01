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

`prepared → discovering → discovering_applications → loading_cache → loading_application_contracts → comparing_snapshots → checking_structure → checking_documents → checking_application_documents → checking_application_versions → checking_application_capabilities → checking_application_state_machines → checking_application_data_contracts → checking_skill_catalog → checking_skill_scenarios → checking_dependencies → checking_environment → validating_findings → rendering_report → verifying_report → completed`。

脚本实际按所列状态逐步推进，检查确定性事实并保存 `logs/project-doc-audit/cache.json`。文档哈希用于统计内容未变化的文档，所有文档仍执行检查，避免引用目标变化被遗漏；缓存不提交 Git。报告位于 `outputs/project-doc-audit/runs/<run-id>/report.md`。Skill 只提出差异和修改建议，不自动修改文档；用户同意后再执行修改。

路径检查只报告明确静态引用的缺失：支持仓库路径、相对文档目录的显式路径、应用契约声明的文档上下文、Skill 章节上下文和通配符。运行产物、未限定目录的数据或通用 Markdown 文件名、历史迁移输入和格式示例不要求已存在；代码围栏不作为内联引用或章节扫描。历史输入豁免要求引用紧邻历史标记且后文明确说明迁移、清理或归档，不因同一句中出现“历史”就跳过当前引用。应用状态从实现的 `Phase` 类型提取，支持单引号和双引号并排除注释，逐项核对 PRD 中的独立状态名，兼容链式描述及中文相邻文本。

`verify` 校验 findings Schema、日志与输出 JSON 一致性、差异计数和 Markdown 渲染一致性；`deliver` 同样校验，失败返回退出码 4 且不交付报告。旧运行沿用原报告格式进行一致性校验。回归测试位于 `skills/project-doc-audit/scripts/test_document_audit.py`，使用隔离环境运行 pytest，临时文件放在本次运行的日志目录中。

检查范围包括核心文档、`docs/**/*.md`、所有活动子目录中的 `README.md`、插件与 Skills 一致性、各 `SKILL.md` front matter 的 `category`、`docs/Skills_说明书.md` 中的 Skill 分类与具体场景示例、`VERSION` 与 marketplace 版本、`.env*` 键名和 `skills/`、`utils/`、`runtime/`（排除 `runtime/.venv`）中的 Python 第三方依赖及虚拟环境安装版本。

`applications/*/application-audit.json` 声明每个应用的 README、PRD、版本来源、状态机和数据模型。脚本读取 `package.json`、实现源码、应用 README 与 PRD，确定性比较应用版本、文档路径、状态机、项目 JSON 字段和产物目录说明。应用审查不自动修改文件；应用快照缓存与报告仍分别写入 `logs/` 和 `outputs/`。应用契约 schema 位于 `utils/references/application-audit.schema.json`。

`category` 必须是 `project_function`、`ai_assisted_learning`、`ai_assisted_teaching`、`ai_assisted_research` 或 `common_tool` 之一。具体场景示例只认说明书各 Skill 详细章节中的 YAML `scenario_examples`，每项必须包含 `id`、`user_request`、`when_to_call`、`invocation` 和 `expected_output`；单独的 CLI 命令不算场景示例。

