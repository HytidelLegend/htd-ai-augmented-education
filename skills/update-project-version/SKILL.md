---
name: update-project-version
category: project_function
description: 列出暂存差异及未被 Git 忽略的新文件，读取内容总结核心更新，预览下一版本并在用户确认后同步更新历史及版本号。当用户要求更新本项目版本、准备版本更新说明或同步版本文件时使用；Git 提交由用户执行。
---

# 更新项目版本

先列出已提交版本与暂存内容的差异，并读取未被 Git 忽略的新文件，再总结核心更新，供用户确认。确认后同步 `docs/更新历史.md`、`VERSION`、`README.md` 和 `.claude-plugin/marketplace.json`。只准备文件，不执行 Git add、commit 或 push；提交必须由用户进行。

调用前阅读 [Skills、应用说明书](../../docs/Skills、应用说明书.md) 和 [来源职责](../../SOURCE_OF_TRUTH.md)。版本按 HEAD 第一父链提交次数计算：首次提交 `0.1.0`，下一次提交末位加一；不按运行次数或语义版本递增。旧入口 `project-doc-audit history-*` 继续兼容历史迁移和旧运行。

## CLI

入口为 `runtime/.venv/Scripts/python.exe skills/update-project-version/scripts/cli.py`：

```text
runtime/.venv/Scripts/python.exe skills/update-project-version/scripts/cli.py start --root .
runtime/.venv/Scripts/python.exe skills/update-project-version/scripts/cli.py status --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/update-project-version/scripts/cli.py resume --root . --run-id <run-id> --input <review.json>
runtime/.venv/Scripts/python.exe skills/update-project-version/scripts/cli.py verify --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/update-project-version/scripts/cli.py deliver --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/update-project-version/scripts/cli.py confirm --root . --run-id <run-id> --input <confirmation.json>
runtime/.venv/Scripts/python.exe skills/update-project-version/scripts/cli.py publish --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/update-project-version/scripts/cli.py resume --root . --run-id <run-id>
```

`start` 冻结 HEAD → index 差异及 `git ls-files --others --exclude-standard -z` 列出的新文件，生成证据 JSON、小输入模板及 `diff.md`。新目录展开到文件；文本提供完整新增内容，空文件保留证据，二进制或非 UTF-8 文件提供类型、大小和指纹；符号链接仅检查链接文本，不跟随目标。已跟踪文件的未暂存修改不纳入摘要，版本号及更新历史仍作为派生文件排除。Agent 必须读取完整 patch，不能只看文件列表或提交标题。Agent 只填写核心更新单行短句、证据编号和跳过理由，再由 `resume --input` 校验并生成版本预览。空证据版本填写 `no_core_reason`，由脚本渲染“本次无核心更新”。

展示 `deliver` 的新版本和更新摘要，等待用户明确确认当前预览。未收到确认不得调用 `confirm` 或把模板中的 `confirmed` 改为 true。用户拒绝时保持待确认状态，不写版本文件；用户要求修改摘要时新建运行。`confirm` 只记录确认，不写目标文件；`publish` 才同步四文件。发布前遵守既有敏感信息检查要求。

退出码沿用说明书：待 Agent 审查、待用户确认或可恢复暂停为 3，查询状态为 0；`verify`、`deliver` 核验失败为 4，格式错误为 2。

## 状态机与恢复

`prepared → collecting_commits → preparing_review → awaiting_agent_review → validating_review → rendering_preview → preview_ready → awaiting_user_confirmation → validating_confirmation → confirmed → publishing → verifying → checking_recent_runs → completed`。

脚本校验迁移并保存检查点。失败进入 `paused_error` 并记录 `resume_stage`；无输入 `resume` 不越过审查或用户确认，已有预览不能替换摘要。误传输入后恢复原来的待确认或已确认状态，不重新生成已有预览，也不启动发布。确认写入中断可重试 `confirm`；无输入恢复仅在已经保存有效确认记录时恢复为 confirmed，否则回到待用户确认。只有明确尝试过 publish 或已经进入发布阶段时，resume 才继续发布。发布中断使用共享文件事务及跨运行锁恢复。已完成运行只核验，重复运行同一差异不会重复升版。

HEAD、暂存差异、新文件范围或内容、审查、预览或发布目标出现新增修改时，旧审查或确认不能用于写入。将新文件以相同 Git blob 和文件模式暂存时允许继续，保留冻结来源及证据编号，不重复收集；Git clean 过滤及换行转换由 Git blob 核对。README、marketplace 中非版本内容只读取 index；工作区存在未暂存非版本修改时暂停，避免覆盖编辑。旧历史沿用 HEAD，首次历史迁移沿用已经复核的明确例外。浅克隆或无首次提交时暂停，不猜测版本。

README、marketplace 的冻结证据保留原始 blob 编号，但重新暂存派生版本时不以该编号判定失效；非版本内容的规范化差异及文件模式仍须一致。新文件的可执行模式遵守 Git `core.filemode` 配置。

## 模板与产物

新运行的证据包使用 `schema_version: "1.1"`、`scope: "staged_and_untracked"`，保留 `mode: "staged"`。证据增加 `source`（`staged` 或 `untracked`）、`mode` 和 `blob_oid`；未跟踪证据另含 `file_type`（`text`、`binary` 或 `symlink`）、`size_bytes`、`content_sha256`。路径排序、证据编号及 Markdown 均由脚本生成。已有 1.0 运行和旧 history-* 入口仍只核验其原有暂存范围，不自动扩大取证；摘要及确认模板沿用原 Schema。

证据结构由共享 [version-candidate-packet-v1.schema.json](../../utils/references/version-candidate-packet-v1.schema.json) 校验；脚本同时校验路径排序、唯一性、连续证据编号及差异正文指纹。取证时和恢复、确认、发布、审计核验时均执行这些检查。

摘要复用 [history-review.schema.json](../project-doc-audit/references/history-review.schema.json)：脚本预填版本、差异指纹与证据编号；Agent 仅填写 `updates[].summary`、`evidence_ids`、`skipped[].reason` 和必要的 `no_core_reason`。全部证据须覆盖，更新短句最多 180 字。

确认模板按 [confirmation.schema.json](references/confirmation.schema.json) 生成：`run_id`、`packet_sha256`、`preview_sha256` 由脚本预填；Agent 依据用户回复设置 `confirmed: true`，`confirmed_at` 在确认入口通过 `utils/scripts/timestamp.py` 生成。预览指纹绑定四个目标文件的发布记录，用户可读预览另存内容指纹并核验，不能手工替换。

中间文件在 `logs/update-project-version/runs/<run-id>/`：`state.json`、`packet.json`、`review-template.json`、`review.json`、`confirmation-template.json`、`confirmation.json`、`publication.json`。正式产物在 `outputs/update-project-version/runs/<run-id>/`：`diff.md`、完整历史预览 `preview.md`、用户预览 `proposal.md`、核验记录 `bindings.json`、发布后提醒 `reminders.md`。日志和结果不纳入 Git，不写入 runtime。

`diff.md` 由冻结证据确定性渲染；提交摘要、确认、发布及结果核验时检查它与证据一致，staged 审计也复用共享渲染核对该文件。差异正文含 Markdown 围栏时自动选择更长围栏，避免截断显示；差异文件损坏后不继续使用已有摘要。

共享取证、模板校验和历史渲染在 `utils/scripts/commit_history.py`、`version_history.py`；共享生成编排在 `utils/scripts/version_workflow.py`，用户确认编排在本 Skill 的 `scripts/version_workflow.py`。新旧入口使用相同证据校验；project-doc-audit 能核验新 Skill 的已完成发布记录。

## 提交前提醒

`utils/scripts/recent_skill_runs.py` 在预览及交付时检查当前仓库最近 1h 内正式 `project-doc-audit`、`sensitive-commit-check` 的 run，按 `created_at` 启动时间计算，恰好 1h 计入。所有已启动状态均列出：没有运行则提醒先运行，尚未完成则提醒继续，失败/暂停则提醒恢复，结束但未通过核验则提醒处理问题。缺失或无效时间不冒充最近运行。

仅查询 status/verify 不创建新 run，历史生成子目录和测试目录不算正式审计。提醒只提示，不自动启动检查，不阻止版本预览；有最近运行也不证明当前暂存范围已通过审查。核验模块缺失、依赖不可用或记录损坏时标为未通过核验，不因提醒核验失败而中断版本流程。交付时重新计算时间，只展示最新提醒，冻结预览中的旧提醒不重复输出。

允许新文件尚未暂存时发布。发布后由用户暂存候选新文件和四个生成文件，与实际功能改动纳入同次提交；提交前运行 `project-doc-audit start --scope staged` 和敏感信息检查。staged 审计要求候选文件全部以已审查内容进入暂存区，缺失、内容不同或额外暂存改动均不认可旧发布记录。默认整仓审计仍按已提交 HEAD 校验；待提交候选应使用 staged 模式。

## 具体场景示例

```yaml
scenario_examples:
  - id: update-staged-version
    user_request: "读取暂存差异，给我新版本和更新摘要，确认后同步版本文件"
    when_to_call: "用户准备本项目的下一版本时"
    invocation: "start → Agent 读取 diff 并填写模板 → resume → verify/deliver → 用户确认 → confirm → publish → verify/deliver"
    expected_output: "列出差异和新版本摘要；确认后同步四文件并提示提交前检查，Git 提交由用户执行"
```

回归测试位于 `tests/test_version_workflow.py`，使用隔离环境及日志目录中的临时仓库；同时运行旧版本历史和目录审计回归。
