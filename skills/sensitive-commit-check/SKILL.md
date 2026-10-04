---
name: sensitive-commit-check
category: project_function
description: 在 git add、git commit 或 git push 前检查本次变更是否包含 API key、账号密码、个人信息、私钥、商业机密、财务信息及其他敏感内容。当用户准备提交、推送或要求提交前安全审查时使用。
---

# Sensitive Commit Check

检查本次 Git 差异中的敏感信息，不自动执行 git add、commit 或 push。默认 worktree 范围检查已跟踪文件相对 HEAD 的最终新增行，以及未被 Git 忽略的新文件；staged 范围只检查实际暂存内容。新文件全部正文视为新增内容。

## 检查范围

- worktree：使用 git diff HEAD；无 HEAD 的新仓库使用空树作为基准。通过 git ls-files --others --exclude-standard 展开未跟踪的新文件，包含新目录内的文件。
- staged：使用 git diff --cached，直接读取 index 中的 blob，不能读取工作区内容代替暂存内容。未暂存的新文件不在此范围。
- 只扫描新增行；删除行、未变化的上下文、纯重命名和纯权限变化不参与敏感内容判断。重命名同时修改正文时只扫描新增行。
- 不遍历历史提交、logs、outputs 或 Skill 案例／回归目录；这些路径只有在实际进入所选 Git 差异时才作为候选。已跟踪文件即使后来被忽略，仍可能提交，其新增差异必须检查。
- 无扩展名或未知扩展名的 UTF-8 文本也扫描。新增或修改的二进制、办公文件、非 UTF-8 内容、超大新增文本和子模块只生成待确认项，不宣称正文已检查。符号链接检查链接文本，不跟随链接读取目标。

## 状态机

created → status_captured → commit_candidates_resolved → diff_captured → deterministic_scan_completed。

确定性扫描发现高风险时直接进入 verification_completed → blocked，不要求 Agent 先处理其他中风险线索。其余情况中，无待复核项时直接进入 verification_completed → approved；有待复核项时进入 semantic_review_required → archiving_agent_review → semantic_review_completed → verification_completed → 终态。

用户决策路径：needs_user_decision → archiving_user_decision → decision_applied → verification_completed → 终态。失败进入 failed，记录 resume_stage；resume 从扫描、审查归档、决策归档或终态判定的检查点恢复，不能跳过审查。缺失的审查小模板可以重新生成，已有填写内容不覆盖。检查点和归档采用原子写入，写入中断时保留原文件。旧运行沿用其原有证据和结果，不自动改判通过。

Git 基准、差异、候选内容、规则配置、扫描与取证脚本及审查 Schema 共同绑定脱敏指纹。扫描结束、review、submit-decision、resume 和 verify 重新核对；差异或规则变化后旧审查失效，必须重新 start。verify 还核对确定性发现、审查归档、终态和报告内容，且不改写检查点。日志中不保存完整差异或敏感正文。

## 排除规则

共享规则位于 utils/references/sensitive-scan-policy.yaml，由 utils/scripts/sensitive_content_scanner.py 执行：

- 仅在 README.md 的“申请授权请发送邮件至”上下文中，排除指定公开联系邮箱的 email 命中；其他位置、其他邮箱和同一行其他敏感匹配仍检查。
- 排除“八字段目录”“八字段模板”对个人运势关键词“八字”的误触发；不排除其他八字内容，也不跳过整份文件。
- SYNTHETIC_、TEST_、MOCK_、DUMMY_ 及示例域名保留原有合成样例判断。高风险凭据不能由 Agent 或用户确认覆盖。

## 工作流与命令

使用项目虚拟环境运行：

```text
runtime/.venv/Scripts/python.exe skills/sensitive-commit-check/scripts/cli.py start [--scope worktree|staged]
runtime/.venv/Scripts/python.exe skills/sensitive-commit-check/scripts/cli.py status --run-id <RUN_ID>
runtime/.venv/Scripts/python.exe skills/sensitive-commit-check/scripts/cli.py review --run-id <RUN_ID> --input <review.json>
runtime/.venv/Scripts/python.exe skills/sensitive-commit-check/scripts/cli.py submit-decision --run-id <RUN_ID> --input <decision.json>
runtime/.venv/Scripts/python.exe skills/sensitive-commit-check/scripts/cli.py verify --run-id <RUN_ID>
runtime/.venv/Scripts/python.exe skills/sensitive-commit-check/scripts/cli.py resume --run-id <RUN_ID>
```

create-request 保留为 start 的兼容别名。audit-skills、audit-history 及非 none 的 --supplemental 明确报错退出，不再启动补充扫描；--supplemental none 仅保留兼容。

脚本完成确定性扫描、准备清单和审查小模板，并渲染报告。无待复核项不需要 Agent。需要语义复核时，Agent 只处理 review_packet.json 中的待复核项及相关新增差异，核对模板预填清单，填写少量判断并设置 review_confirmed 为 true。不得读取旧内容作额外扫描或补充清单外文件。

review JSON 沿用 references/review.schema.json，包含 findings、reviewed_files、decisions 和 review_confirmed；每项判断包含 file、risk_level、category、evidence、recommendation、confidence。新运行必须引用本次扫描的 finding_id，脚本核对文件、运行编号、风险等级及处理建议，拒绝清单外文件、未知编号和无效值。review 先将输入归档为 agent-review.json，再推进状态；归档指纹在恢复和验证时复核。用户决策包含 decisions 和 decision_confirmed，每项使用 finding_id、allow 或 block 及非空 reason。高风险不能由用户确认放行。

运行日志、状态、审查清单及 report.md 位于 logs/sensitive-commit-check/runs/<run-id>/，不纳入 Git；时间戳使用共享 timestamp.py。报告沿用脱敏证据、路径、行号、哈希及既有模板。只有 approved 且 verify 返回 can_proceed: true 才能表示检查通过。退出码遵循 docs/Skills、应用说明书.md。

通用 Git 差异取证位于 utils/scripts/git_repository.py；文本规则和新增行扫描位于 utils/scripts/sensitive_content_scanner.py；状态机编排位于本 Skill 的 scripts/。回归测试位于 tests/test_sensitive_commit_check.py。

## 具体场景示例

```yaml
scenario_examples:
  - id: scan-before-commit
    user_request: "提交前帮我检查变更里有没有密钥或个人信息"
    when_to_call: "用户准备提交、推送或要求提交前安全审查时"
    invocation: "start → review（如需）→ verify"
    expected_output: "给出脱敏风险报告，并在高风险或未决风险时阻止继续提交"
```
