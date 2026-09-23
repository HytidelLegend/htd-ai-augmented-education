---
name: project-doc-audit
category: project_function
description: 检查项目文档、文件架构、Skills 注册、版本、环境变量和 Python 依赖是否符合当前项目状态，使用增量缓存生成差异建议。当用户要求审计项目文档、检查文档是否过期或核对项目说明与实际结构时使用。
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

`prepared → discovering → loading_cache → comparing_snapshots → checking_structure → checking_documents → checking_skill_catalog → checking_skill_scenarios → checking_dependencies → checking_environment → validating_findings → rendering_report → verifying_report → completed`。

脚本实际按所列状态逐步推进，检查确定性事实并保存 `logs/project-doc-audit/cache.json`。未变化的文档按哈希跳过；缓存不提交 Git。报告位于 `outputs/project-doc-audit/runs/<run-id>/report.md`。Skill 只提出差异和修改建议，不自动修改文档；用户同意后再执行修改。

检查范围包括核心文档、`docs/**/*.md`、所有活动子目录中的 `README.md`、插件与 Skills 一致性、各 `SKILL.md` front matter 的 `category`、`docs/Skills_说明书.md` 中的 Skill 分类与具体场景示例、`VERSION` 与 marketplace 版本、`.env*` 键名和 `skills/`、`utils/`、`runtime/`（排除 `runtime/.venv`）中的 Python 第三方依赖及虚拟环境安装版本。

`category` 必须是 `project_function`、`ai_assisted_learning`、`ai_assisted_teaching`、`ai_assisted_research` 或 `common_tool` 之一。具体场景示例只认说明书各 Skill 详细章节中的 YAML `scenario_examples`，每项必须包含 `id`、`user_request`、`when_to_call`、`invocation` 和 `expected_output`；单独的 CLI 命令不算场景示例。

