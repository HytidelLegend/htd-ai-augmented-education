---
name: sensitive-commit-check
category: common_tool
description: 在 git add、git commit 或 git push 前检查本次变更是否包含 API key、账号密码、个人信息、私钥、商业机密、财务信息及其他敏感内容。当用户准备提交、推送或要求提交前安全审查时使用。
---

# Sensitive Commit Check

在提交或推送前运行安全门禁。默认检查 `git status` 中全部可能进入本次提交的变更文件、各 Skills 的案例／回归文件，以及项目内过去运行产物；也支持只检查 staged 文件或关闭补充范围。本 Skill 只检查和生成报告，不自动执行 `git add`、`git commit` 或 `git push`。

## 状态机

`created → status_captured → commit_candidates_resolved → supplemental_scope_resolved → deterministic_scan_completed → historical_fingerprint_indexed → cross_scope_matches_completed → semantic_review_required → archiving_agent_review → semantic_review_completed → verification_completed → approved | blocked | needs_user_decision`

用户确认恢复路径为 `needs_user_decision → archiving_user_decision → decision_applied → verification_completed → approved | blocked | needs_user_decision`。

发现高风险进入 `blocked`；无法确认的中风险进入 `needs_user_decision`。必须从检查点恢复，不得猜测状态。

## 工作流

1. 使用项目虚拟环境运行 `scripts/sensitive_commit_check.py start`。默认补充范围为 `all`。
2. 脚本保存 Git 原始状态并解析 worktree 或 staged 候选文件。
3. 脚本按 `utils/references/sensitive-scan-policy.yaml` 发现各 Skills 的 `tests`、`fixtures`、`examples`、`samples`、`assets`、`prompts`、`references`，以及项目 `logs/` 和各 Skill 的 `artifacts`、`attempts`、`requests`。
4. 共享扫描器 `utils/scripts/sensitive_content_scanner.py` 执行确定性扫描。二进制、办公文件、过大文本和非 UTF-8 文件只生成待确认项，不读取其正文。
   策略定义文件和本 Skill 说明可通过精确路径跳过语义关键词自扫描，但仍执行密钥、邮箱、凭据等确定性正则扫描。
5. 脚本从历史运行发现中构建只含 SHA-256 的脱敏指纹索引，再与 Skills 的案例／回归文件交叉比对；不得把历史敏感原文写入索引、报告或审查包。
6. Agent 只处理 `review_packet.json` 中需要语义判断的内容，例如商业战略、产品设计、财务资料、个人星盘、个人运势预测及合成 fixture 判断，不默认遍历整个项目。
7. 脚本预填 `reviewed_files`；Agent 核对完整清单、补充少量结构化判断，并将 `review_confirmed` 设置为 `true`。`review` 会先将外部输入归档为当前 run 目录下的 `agent-review.json`，再推进状态机和 `verify` 校验结果；不得将审核结果写入 `runtime/`。缺少文件或混入清单外文件都会拒绝推进。
8. 只有 `approved` 且 `verify` 返回 `can_proceed: true`，才能告诉用户可以继续 Git 操作。
9. `needs_user_decision` 状态必须通过 `submit-decision` 归档用户选择后恢复。用户确认只能解决中风险项，不能覆盖高风险、`block` 或 `historical_match`。

## 补充检查范围

- `none`：只检查 Git 候选文件。
- `skills_regression`：额外检查各 Skills 的案例、提示词、参考资料和回归测试。
- `historical_runs`：额外扫描项目内历史运行产物并生成脱敏指纹索引。
- `all`：同时启用 `skills_regression` 和 `historical_runs`；这是默认值。

历史运行产物本身若被 `.gitignore` 排除，不作为本次提交文件阻断；它们用于发现真实敏感值是否被复制进可提交的 Skill 案例或回归测试。历史值与 Skill 文件的指纹一致时，以 `historical_match` 高风险阻断。

## 敏感类别

API key、token、密码、Cookie、数据库连接串、SSH/TLS/云服务私钥、个人信息（含邮箱、手机号、身份证、个人星盘和个人运势预测）、客户资料、商业战略、未公开产品设计、路线图、定价、合同、法务材料、资产负债表、现金流表、银行和税务资料、内部基础设施配置、生产配置、数据库快照及其他未公开信息。

## 命令

```text
python scripts/sensitive_commit_check.py start [--scope worktree|staged] [--supplemental none|skills_regression|historical_runs|all]
python scripts/sensitive_commit_check.py audit-skills [--scope worktree|staged]
python scripts/sensitive_commit_check.py audit-history [--scope worktree|staged]
python scripts/sensitive_commit_check.py status --run-id <RUN_ID>
python scripts/sensitive_commit_check.py review --run-id <RUN_ID> --input <review.json>
python scripts/sensitive_commit_check.py submit-decision --run-id <RUN_ID> --input <decision.json>
python scripts/sensitive_commit_check.py verify --run-id <RUN_ID>
python scripts/sensitive_commit_check.py resume --run-id <RUN_ID>
```

日志、状态、审查清单、历史指纹索引和 `report.md` 位于 `logs/sensitive-commit-check/runs/<run_id>/`。报告只保留脱敏证据、路径、行号和哈希，不复制完整 secret。`resume` 会读取最近检查点并输出下一步动作，不会猜测或重建状态。

Agent 审查 JSON 至少包含 `findings`、`reviewed_files`、`decisions` 和 `review_confirmed: true`；每个发现包含 `file`、`risk_level`、`category`、`evidence`、`recommendation`、`confidence`，引用确定性发现时应附带其 `finding_id`，脚本会合并语义判断而不是重复追加。用户决策 JSON 包含非空 `decisions` 和 `decision_confirmed: true`；每项使用 `finding_id`、`decision: allow|block` 和非空 `reason`。高风险阻断，中风险请求确认，低风险允许并告警。测试中的凭据优先改用 `SYNTHETIC_`、`TEST_`、`MOCK_`、`DUMMY_` 前缀或 `.invalid` 域名；这些标记只降低确定性规则风险，不能覆盖历史指纹匹配。
# CLI

入口：`python skills/sensitive-commit-check/scripts/cli.py`。支持 `create-request`、`start`、`status`、`resume`、`verify`、`submit-decision`；报告和状态写入 `logs/sensitive-commit-check/runs/<run-id>/`，日志仅为文本且不纳入 Git。退出码遵循 `docs/Skills_说明书.md`。
