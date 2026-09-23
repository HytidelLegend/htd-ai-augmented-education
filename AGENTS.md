# Agent 执行规则

- `CODE_OF_CONDUCT.md` 规定贡献者和 Agent 的行为边界。
- `SOURCE_OF_TRUTH.md` 规定权威来源、冲突优先级和派生关系。
- `docs/` 保存 PRD、架构、贡献、安全、更新历史和 skill 说明等详细文档。
- 需求和验收不清时阅读 `docs/PRDs/`；来源冲突时阅读 `SOURCE_OF_TRUTH.md`；行为、安全或提交问题阅读 `CODE_OF_CONDUCT.md`；调用 skill 前阅读 `docs/Skills_说明书.md` 和对应 `SKILL.md`。
- 默认使用 `runtime/` 下的隔离环境；运行产物写入 `outputs/`，文本日志和状态机中间文件写入 `logs/<skill>/runs/<run-id>/`，日志不纳入 Git。不得把 Skill 运行产物写入 `runtime/`。
