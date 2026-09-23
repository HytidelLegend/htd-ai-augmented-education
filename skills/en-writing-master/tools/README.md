# 本地作文工具

本开源仓库不自带具体的英语作文工具，以避免分发教材、真题解析、范文或其他可能受版权保护的资料。

请使用 `en-writing-master` 的“新增工具”功能，并传入你有权使用的写作规则、评分标准、题目示例和语言资料。工具会生成到本目录的本地子目录中；这些具体工具由 Git 忽略，不应提交到公共仓库。

```powershell
runtime/.venv/Scripts/python.exe `
  skills/en-writing-master/scripts/cli.py create-tool `
  --root . `
  --input request.json
```

创建后可用下面的命令确认本地工具是否通过校验：

```powershell
runtime/.venv/Scripts/python.exe skills/en-writing-master/scripts/cli.py list-tools --root .
```

不要把未经授权的教材原文、商业课程资料、真题解析或范文复制到仓库中。
