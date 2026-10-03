# convert-copy-to-transcript

将 UTF-8 Markdown、纯文本文件或直接文案确定性转换为可朗读逐字稿；脚本生成语义候选、预览、差异报告与确认回执，Agent 仅填写候选读法。

本 Skill 已集成本项目，共用根目录 `utils/` 与 `runtime/.venv/`，不再是独立目录包。完整命令与状态机见 [SKILL.md](SKILL.md)。

```text
runtime/.venv/Scripts/python.exe skills/convert-copy-to-transcript/scripts/cli.py --help
runtime/.venv/Scripts/python.exe skills/convert-copy-to-transcript/scripts/verify_package.py --environment
```

运行目录遵循 `outputs/<skill>/runs/<run-id>/` 与 `logs/<skill>/runs/<run-id>/`。导入来源和原始文件指纹保存在 `references/import-provenance.json`，仅作迁移来源记录，不作为当前文件校验清单。

零语义候选由 start 自动生成稿件预览并等待确认；有候选时只填写脚本生成的决策模板。resume 仅恢复 paused_error；错误状态命令返回失败但保持运行状态与产物。

回归测试：

```text
runtime/.venv/Scripts/python.exe -m pytest skills/convert-copy-to-transcript/tests -q
```

测试使用 mock 云端响应，不验证真实账号的资源或音色权限。
