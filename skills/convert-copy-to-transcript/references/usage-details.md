# convert-copy-to-transcript 使用与协议补充

```yaml
scenario_examples:
- id: convert-copy-to-transcript
  user_request: 请把这篇 Markdown 转成朗读逐字稿
  when_to_call: 用户要求把文案转换为口播稿、逐字稿或 TTS 输入时
  invocation: start → apply-decisions → approve → verify → deliver
  expected_output: UTF-8 逐字稿预览、差异报告、批准稿与 handoff
```

统一入口为 `runtime/.venv/Scripts/python.exe skills/convert-copy-to-transcript/scripts/cli.py`。完整状态机、专用参数、输出和恢复约定见该 Skill 契约。
