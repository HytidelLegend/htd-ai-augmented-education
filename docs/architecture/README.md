# 项目文件架构

本文说明项目的主要文件和目录、各自用途及详细说明入口。来源职责与冲突处理见 [SOURCE_OF_TRUTH.md](../../SOURCE_OF_TRUTH.md)，执行要求见 [AGENTS.md](../../AGENTS.md)。

## 文件与目录

- `.claude-plugin/`：插件清单、Skill 注册及版本配置。
- `skills/`：各项 Skill 的使用说明、脚本和参考资料；每项 Skill 的入口是其 `SKILL.md`，详细实现说明由它链接。
- `applications/`：即开即用的学习应用；应用目录与入口见 [应用说明](../../applications/README.md)，各应用使用说明由其子目录维护。
- `docs/`：项目文档，包括本文件、[Skills、应用说明书](../Skills、应用说明书.md)、[贡献指南](../contributing/贡献指南.md)、[安全说明](../security/安全说明.md)和[更新历史](../更新历史.md)。
- `docs/PRDs/`：仅在本地保存项目需求和验收标准；存在时可参考，缺失时依据公开 Skill 说明并澄清未确定的需求。
- `docs/decisions/`：仅在本地保存项目决策记录。
- `utils/`：各 Skill 或应用共用的脚本、数据格式定义和模板；`scripts/` 保存实现，`references/` 保存共用规则与数据格式，`templates/` 保存共用模板。
- `runtime/`：隔离运行环境和配置说明，配置入口见 [运行环境说明](../../runtime/README.md)。
- `outputs/`：正式运行结果；各 Skill 通常保存到 `outputs/<skill-name>/runs/<run-id>/`，学习应用和学习项目按各自说明保存。
- `logs/`：文本日志、运行状态与中间文件，通常保存到 `logs/<skill-name>/runs/<run-id>/`。
- `examples/`、`templates/`：项目示例与模板；Skill 自有示例及模板由各 Skill 子目录维护。
- `tests/`：项目级检查；Skill 自有回归测试位于对应 Skill 子目录。
- `tmp/`：本地临时文件或历史迁移暂存内容，不作为活动功能入口。

## 根目录文件

- [README.md](../../README.md)：项目概览、功能表、安装与使用入口。
- [AGENTS.md](../../AGENTS.md)：Agent 的阅读顺序、执行和文档维护要求。
- [SOURCE_OF_TRUTH.md](../../SOURCE_OF_TRUTH.md)：来源职责、冲突优先级及文档内容边界。
- [CODE_OF_CONDUCT.md](../../CODE_OF_CONDUCT.md)：贡献者和 Agent 的行为要求。
- [VERSION](../../VERSION)：项目版本号。
- [LICENSE](../../LICENSE)：许可证。
- `.env.example`：环境变量填写示例；本地 `.env` 保存实际配置。
- `.gitignore`：不纳入 Git 的文件与目录规则。
- `pytest.ini`：Python 测试配置。
- `git-push.ps1`：Git 推送辅助脚本，使用时遵守提交与发布规则。

## Git 跟踪与本地内容

源码、公开说明、示例配置和各 Skill 的测试纳入 Git，具体以 `.gitignore` 为准。根目录的 `examples/`、`tests/`、`tmp/` 和 `git-push.ps1` 仅供本地使用；`docs/PRDs/` 和 `docs/decisions/` 不纳入 Git，不同步到远端，新克隆仓库不包含这两个目录。

正式运行结果、日志、隔离环境和实际环境配置仅在本地保存；运行环境说明和 `runtime/.venv/requirements.txt` 依赖清单纳入 Git。不得将 Skill 运行结果写入 `runtime/`；各 Skill 或应用的特殊保存位置见其详细说明。
