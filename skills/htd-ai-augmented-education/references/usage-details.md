# htd-ai-augmented-education 使用与协议补充

入口为 `python skills/htd-ai-augmented-education/scripts/cli.py`，支持 `create-request`、`start`、`status`、`resume`、`deliver` 和 `verify`。请求分为 `project_info` 与 `task_routing`，也可使用 `auto` 由脚本分类。Skill 不保存项目事实副本，而是动态读取 `AGENTS.md`、`SOURCE_OF_TRUTH.md`、`CODE_OF_CONDUCT.md`、`docs/`、`.claude-plugin/plugin.json` 及已注册 Skills 的公开契约。

运行遵循 `prepared → validating_request → classifying_intent → resolving_authoritative_sources → building_evidence_packet → validating_evidence_packet → paused_agent_response → validating_agent_response → rendering_markdown → verifying_delivery → publishing → completed`。输入、意图、来源读取、响应或输出冲突进入对应暂停状态；来源之间的语义冲突由 Agent 按 `SOURCE_OF_TRUTH.md` 判定并记录依据或局限性。Agent 根据来源清单填写精简的结构化草稿；脚本负责 schema 校验和 Markdown 渲染。任务路由只能推荐插件清单中已注册且公开契约确实覆盖需求的 Skills；没有匹配能力时必须承认局限，保持调用序列为空，不得推荐外部 Skills。

正式结果位于 `outputs/htd-ai-augmented-education/runs/<run-id>/result.json` 和 `result.md`，状态、事件、来源清单及草稿位于 `logs/htd-ai-augmented-education/runs/<run-id>/`。默认使用 `deliver` 将通过验证的 `result.md` 作为 Markdown 正文直接返回对话。
