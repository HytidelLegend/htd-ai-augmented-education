# schedule-ebbinghaus-plan 使用与协议补充

入口为 `runtime/.venv/Scripts/python.exe skills/schedule-ebbinghaus-plan/scripts/cli.py`，支持 `start`、`status`、`resume`、`verify` 和 `deliver`。请求必填正整数 `n`，并在每天首次背诵 item 数 `daily_items` 与首遍天数 `d` 中恰选一个；脚本计算另一个值。完成标准可选 `first_pass`（默认）或 `one_review`，后者要求各 item 在期限内至少复习一次。复习间隔默认在首次背诵后的第 1、2、4、7、15 天，可用严格递增的正整数数组覆盖。每天依次引入一个新 batch，到期复习 batch 不限量。给定 `d` 时默认模式要求 `d <= n`，batch 大小为 `ceil(n/d)`，item 均匀分到恰好 `d` 天；至少复习一轮时设最早复习间隔为 `r`，大小为 `ceil(n/(d-r))`，期限不可行时暂停报告原因。给定 `daily_items` 时直接用它作为 batch 上限，期限由实际批次数计算。排程结果会继续列出第 `d` 天之后的复习，空闲日也保留。

状态机为 `prepared → validating_request → selecting_batch_size → building_schedule → validating_schedule → rendering_outputs → verifying_outputs → completed`；输入错误、期限不足和运行错误分别进入可恢复暂停状态。Python 脚本完成计算、Schema 校验和 Markdown 渲染，Agent 不填写逐日列表。正式结果位于 `outputs/schedule-ebbinghaus-plan/runs/<run-id>/result.json` 与 `result.md`；请求快照、状态与事件位于 `logs/schedule-ebbinghaus-plan/runs/<run-id>/`。
