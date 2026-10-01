---
name: schedule-ebbinghaus-plan
category: ai_assisted_learning
description: 按分批首次背诵和递增复习间隔，为编号 1 至 n 的记忆项目生成 d 天内完成目标的逐日计划。当用户要求按艾宾浩斯规则安排背诵、复习或求每批 item 数时使用。
---

# 艾宾浩斯背诵计划

输入编号 1 至 `n` 的 item 数量，以及每天首次背诵的 `daily_items` 或首遍完成期限 `d`（二选一），生成可验证的 JSON，并由脚本渲染 Markdown。所有排程与格式化由 Python 完成，不需要 Agent 填写逐日列表。

## 输入与规则

- 请求为符合 `references/request.schema.json` 的 JSON。`n` 必填；`daily_items` 与 `d` 必须恰好提供一个，且为正整数。脚本计算未输入的每日数量或实际首遍天数。
- `completion_mode` 可为 `first_pass`（默认，所有 item 在第 `d` 天前首次背诵）或 `one_review`（所有 item 在第 `d` 天前还至少复习一次）。
- `review_offsets` 默认 `[1, 2, 4, 7, 15]`，表示相对于各 batch 首次背诵日的复习日；可提供严格递增的正整数数组覆盖。该间隔列表是本项目的排程约定，不声称是实验唯一规定的时间表。
- 每天依次引入一个新 batch，直到所有 item 首次背诵完成；同一天到期的复习 batch 数量不限。`d` 是完成期限，允许提前完成。
- 给定 `d`：`first_pass` 时必须有 `d <= n`，`b = ceil(n/d)`，并把 item 均匀分为恰好 `d` 批，每天首次背诵一批；`one_review` 时设最早间隔为 `r`，`b = ceil(n/(d-r))`。若期限不可行，状态机暂停并报告原因。给定 `daily_items`：`b = daily_items`，`d` 由实际批次数（`one_review` 再加 `r`）确定。最后一批可以少于 `b` 个 item。
- 两种模式都输出所有预定复习日，包含第 `d` 天以后的复习，并连续列出中间的空闲日。

## CLI

```powershell
runtime/.venv/Scripts/python.exe skills/schedule-ebbinghaus-plan/scripts/cli.py start --root . --input request.json
runtime/.venv/Scripts/python.exe skills/schedule-ebbinghaus-plan/scripts/cli.py status --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/schedule-ebbinghaus-plan/scripts/cli.py resume --root . --run-id <run-id> [--input revised-request.json]
runtime/.venv/Scripts/python.exe skills/schedule-ebbinghaus-plan/scripts/cli.py verify --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/schedule-ebbinghaus-plan/scripts/cli.py deliver --root . --run-id <run-id>
```

`start` 和 `resume` 输出包含运行 ID 与状态的 JSON 回执。`resume --input` 用于修订暂停运行的请求；无 `--input` 时重试。`deliver` 在重新验证后输出用户可读 Markdown。退出码遵循 `docs/Skills_说明书.md`。

## 状态机与产物

`prepared → validating_request → selecting_batch_size → building_schedule → validating_schedule → rendering_outputs → verifying_outputs → completed`。输入无效进入 `paused_invalid_request`，期限不足进入 `paused_infeasible`，可重试错误进入 `paused_runtime`；`resume` 从请求验证重新计算。

正式结果位于 `outputs/schedule-ebbinghaus-plan/runs/<run-id>/result.json` 和 `result.md`；请求快照、状态与事件位于 `logs/schedule-ebbinghaus-plan/runs/<run-id>/`。运行 ID 与状态时间戳统一由 `utils/scripts/timestamp.py` 生成。`verify` 重新计算计划并检查 JSON Schema、请求摘要以及 Markdown 一致性。

## 资源

- `references/request.schema.json`：请求结构。
- `references/result.schema.json`：正式 JSON 结构。
- `utils/scripts/spaced_repetition.py`：可供其他 Skill 复用的 batch 排程和 Markdown 渲染函数。
