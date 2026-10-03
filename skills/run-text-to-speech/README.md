# run-text-to-speech

将本项目已完成且确认的逐字稿通过 Edge-TTS（默认）或火山引擎合成为语音；支持分批、试听确认、静音白噪音、时间戳、恢复和局部修订。

本 Skill 已集成本项目，共用根目录 `utils/` 与 `runtime/.venv/`，不再是独立目录包。完整命令与状态机见 [SKILL.md](SKILL.md)。

```text
runtime/.venv/Scripts/python.exe skills/run-text-to-speech/scripts/cli.py --help
runtime/.venv/Scripts/python.exe skills/run-text-to-speech/scripts/verify_package.py --environment
```

运行目录遵循 `outputs/<skill>/runs/<run-id>/` 与 `logs/<skill>/runs/<run-id>/`。导入来源和原始文件指纹保存在 `references/import-provenance.json`，仅作迁移来源记录，不作为当前文件校验清单。

本次后端通过 `start --backend volcengine --speaker 哆啦A梦` 指定；不改写配置文件，恢复沿用快照。实际总时长严格低于试听目标时直接生成最终音频；等于或超过目标时需要试听确认，重试也按当前音频时长判断。

回归测试：

```text
runtime/.venv/Scripts/python.exe -m pytest skills/run-text-to-speech/tests -q
```

测试使用 mock 云端响应，不验证真实账号的资源或音色权限；Edge 测试使用确定性音频与边界事件，覆盖完整适配、试听确认、修订、参数变更及历史运行兼容。

音色配置由 `scripts/render_voice_docs.py` 从 `references/voices.yaml` 生成。

<!-- voice-mapping:start -->
| 后端 | 名称 | 音色 ID | 资源 ID |
|---|---|---|---|
| volcengine | Hytidel（正常说话）（默认） | S_IBxS5KRZ1 | seed-icl-2.0 |
| volcengine | Hytidel | S_IBxS5KRZ1 | seed-icl-2.0 |
| volcengine | 大雄 | S_NBxS5KRZ1 | seed-icl-2.0 |
| volcengine | 哆啦A梦 | S_QBxS5KRZ1 | seed-icl-2.0 |
| edge-tts | 晓艺（默认） | zh-CN-XiaoyiNeural | 不适用 |
| edge-tts | 云扬 | zh-CN-YunyangNeural | 不适用 |
| edge-tts | 晓晓 | zh-CN-XiaoxiaoNeural | 不适用 |
| edge-tts | 云希 | zh-CN-YunxiNeural | 不适用 |
<!-- voice-mapping:end -->
