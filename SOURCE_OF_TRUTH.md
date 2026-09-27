# 权威来源

- 项目行为边界：`CODE_OF_CONDUCT.md`
- Agent 执行规则：`AGENTS.md`
- 即开即用学习应用：`applications/` 及各应用子目录中的 `README.md`
- skill 公开契约：各 `skills/<skill-name>/SKILL.md`
- 共享实现和 Schema：`utils/`
- 正式运行产物：`outputs/<skill-name>/runs/<run-id>/`
- 运行日志与状态：`logs/<skill-name>/runs/<run-id>/`，仅供本地审计且不提交 Git
- 项目目标和验收标准：`docs/PRDs/`
- 时间戳格式与生成实现：`utils/scripts/timestamp.py`；项目统一使用本地时间、不带时区的 `YYYY-MM-DDTHH:MM:SS`，文件名使用 `YYYYMMDDTHHMMSS` 及必要的 `_1`、`_2` 冲突后缀
