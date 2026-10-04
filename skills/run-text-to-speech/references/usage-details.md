# run-text-to-speech 使用与协议补充

本次可用 `start --backend volcengine --speaker 哆啦A梦` 覆盖后端和音色，无需改写配置；恢复使用有效配置快照。配置 `skills/run-text-to-speech/config.yaml` 的 `backend`，默认 `edge-tts`（晓艺、语速 `+10%`），需联网。火山分支严格检查根目录 `.env` 中的 `VOLCENGINE_API_KEY`；Edge 分支检查隔离环境中的 `edge-tts==7.2.8`。后端预检纳入状态机，缺失依赖或配置暂停，修复后 resume；音色映射表由 Python 从 YAML 生成，见 Skill 文档。

```yaml
scenario_examples:
- id: run-text-to-speech
  user_request: 请用默认音色朗读这篇文案，先给我试听
  when_to_call: 用户要求文字转语音、生成朗读音频、试听或修订已有合成语音时
  invocation: start → 上游转换确认 → resume → 必要时 approve-preview → verify → deliver
  expected_output: 最终音频、clean 母版和完整时间戳；达到目标时先提供试听并等待确认
```

TTS 在分批前按语境将结构助词“地”改为请求文本中的“的”，两个后端共用 Python 实现；明确词汇用法保留，不确定项进入 `awaiting_context_decisions`。脚本生成候选与模板，Agent 只填写 ID 和判断，使用 `apply-context-decisions --input` 提交；对话及播客通过 `resume --input` 转交。批准稿和公开时间戳保留原文，请求快照、规则与指纹在日志中冻结并由 verify 复核。决策归档和已决候选使用不可变哈希快照；停顿重定时沿用基准判断，非法决策不改变父子运行。词、句时间戳均验证还原结果，对齐失败在发布前进入可恢复验证暂停。

统一入口为 `runtime/.venv/Scripts/python.exe skills/run-text-to-speech/scripts/cli.py`。完整状态机、专用参数、输出和恢复约定见该 Skill 契约。

三个语音 Skill 分别使用自己的 `outputs/<skill>/runs/<run-id>/` 与 `logs/<skill>/runs/<run-id>/`，通过九字段 `speech-handoff-v1.schema.json` 衔接。ASR 默认复用 `utils/references/术语表.txt`，本次词表只追加到运行快照；语义决策仍只填候选 ID 与 replacement，全文、差异与确认回执由脚本生成。

语音流程补充：转换零语义候选自动生成稿件预览并等待确认；正常等待状态不能用 resume，错误命令不改变状态。TTS 的实际音频总时长（含停顿）严格低于试听目标时不生成试听，直接合并并验证最终音频；达到目标则保留试听确认。

## 后端与历史运行兼容

共享 `utils/scripts/tts_backend.py` 负责配置、音色和参数预检；Edge 适配器只负责合成与格式转换，试听、停顿、白噪音和验证复用统一流程。后端选择使用既有 `backend_resolved` 与 `checking_backend_environment` 阶段。参数变化使旧 batch 和试听确认失效，新运行严格核验合成指纹，旧运行按火山快照和原 Schema 恢复。

TTS 验证上游完成状态、确认回执，并重新执行 producer verify。共享词表仍为一行一术语，ASR 权重只保存在语音适配层及运行快照。
