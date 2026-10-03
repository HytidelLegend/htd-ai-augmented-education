# run-speech-to-text

通过火山引擎将音视频转为带词级与句级时间戳的逐字稿；支持共享热词、本次追加词表、纠错、确认、恢复和验证。

本 Skill 已集成本项目，共用根目录 `utils/` 与 `runtime/.venv/`，不再是独立目录包。完整命令与状态机见 [SKILL.md](SKILL.md)。

```text
runtime/.venv/Scripts/python.exe skills/run-speech-to-text/scripts/cli.py --help
runtime/.venv/Scripts/python.exe skills/run-speech-to-text/scripts/verify_package.py --environment
```

运行目录遵循 `outputs/<skill>/runs/<run-id>/` 与 `logs/<skill>/runs/<run-id>/`。导入来源和原始文件指纹保存在 `references/import-provenance.json`，仅作迁移来源记录，不作为当前文件校验清单。

回归测试：

```text
runtime/.venv/Scripts/python.exe -m pytest skills/run-speech-to-text/tests -q
```

测试使用 mock 云端响应，不验证真实账号的资源或音色权限。
