---
name: init
description: 初始化和验证 AI 辅助教育项目的学习者档案，包括正在学习的内容、目标考试、考试时间、可复习时间、每日投入时长和学习风格。
---

# 学习者初始化

## 用途与边界

`init` 只负责把用户提供的学习者信息校验并固化为版本化 JSON 档案；它不生成课程内容、不会替用户决定考试目标，也不会读取聊天记录猜测缺失字段。

## CLI

```powershell
runtime/.venv/Scripts/python.exe skills/init/scripts/cli.py create-request --input profile.json --request-file request.json
runtime/.venv/Scripts/python.exe skills/init/scripts/cli.py start --root . --input profile.json
runtime/.venv/Scripts/python.exe skills/init/scripts/cli.py status --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/init/scripts/cli.py resume --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/init/scripts/cli.py verify --root . --run-id <run-id>
```

状态机为 `initialized → running → verifying → completed`；输入不完整或 Schema 不通过时进入 `paused_input_required`，可补充原始请求后用 `resume` 继续。正式档案写入 `outputs/init/runs/<run-id>/learner-profile.json`，状态和事件写入 `logs/init/runs/<run-id>/`。

完成门禁：Schema 通过、输出哈希与状态一致、没有待决策项，且 `verify` 独立复核通过。
