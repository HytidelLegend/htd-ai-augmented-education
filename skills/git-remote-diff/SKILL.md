---
name: git-remote-diff
category: common_tool
description: 检查当前项目是否为 Git 仓库，刷新远端引用并比较本地仓库、远端默认分支和工作区状态，生成包含文件差异的 JSON 与用户可读 Markdown 报告。当用户要求检查 Git 信息、判断项目是否与云端最新版一致或查看本地与远端差异时使用。
---

# Git 远端差异检查

这是一个独立的项目级 Skill，不接入 `/hdt`。只执行读取、fetch 和报告生成，不执行 push、merge、reset 或 checkout。

## 执行

从项目根目录运行：

```powershell
runtime/.venv/Scripts/python.exe skills/git-remote-diff/scripts/cli.py start --root .
```

恢复暂停或失败运行：

```powershell
runtime/.venv/Scripts/python.exe skills/git-remote-diff/scripts/cli.py resume --root . --run-id <run-id>
```

查看已有运行状态：

```powershell
runtime/.venv/Scripts/python.exe skills/git-remote-diff/scripts/cli.py status --root . --run-id <run-id>
```

## 状态机

`INIT → REPO_CHECK → REMOTE_DISCOVERY → DEFAULT_BRANCH_RESOLUTION → FETCH_REMOTE → COMPARE_COMMITS → COMPARE_FILES → COMPARE_WORKTREE → WRITE_JSON → RENDER_MARKDOWN → VALIDATE_REPORT → COMPLETE`。

非 Git 仓库、无远端、无法确定默认分支或 fetch 失败时进入对应 `PAUSED_*` 状态，保留 `logs/git-remote-diff/runs/<run-id>/state.json`，不得猜测或伪造一致性结论。

## 输出

每次运行生成 `state.json`、`diff.json`、`diff.md` 和 `events.jsonl`。`diff.json` 是唯一报告数据源，`diff.md` 由脚本渲染，包含提交差异、已暂存、未暂存和未跟踪文件的 Markdown 表格。

完成门禁：状态为 `complete`，JSON 结构有效，Markdown 包含 JSON 中的全部差异文件，路径为仓库相对路径，且报告不泄露认证信息。
# CLI

入口：`python skills/git-remote-diff/scripts/cli.py`。支持 `create-request`、`start`、`status`、`resume`、`verify`；默认状态写入 `logs/git-remote-diff/runs/<run-id>/`，日志仅为文本且不纳入 Git。退出码遵循 `docs/Skills_说明书.md`。
