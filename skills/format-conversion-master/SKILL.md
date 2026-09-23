---
name: format-conversion-master
category: common_tool
description: 将文件确定性转换为指定格式，并通过状态机保存检查点、验证结果和恢复信息；首版支持 EPUB 转 PDF。当用户要求转换文件格式、将 EPUB 导出为 PDF，或继续、检查既有格式转换运行时使用。
---

# 格式转换

## 固定入口

使用项目虚拟环境执行：

```powershell
runtime/.venv/Scripts/python.exe skills/format-conversion-master/scripts/cli.py start --root <项目根目录> --input <文件> --to pdf
```

EPUB 直接解析为 Markdown：

```powershell
runtime/.venv/Scripts/python.exe skills/format-conversion-master/scripts/cli.py start --root <项目根目录> --input <文件> --to md
```

使用 `create-request` 生成请求文件，再用 `start` 执行；也可以直接使用 `start`。

继续、查询和验证分别使用 `resume`、`status`、`verify`。不要由 Agent 手写转换命令、请求 JSON 或状态文件。

需要生成请求模板时使用 `init-request --request-file <JSON 文件> --input <文件> --to pdf|md`。

## 当前支持

- `EPUB → PDF`：使用 Calibre，目标 PDF 使用 A4 纸张和默认页边距。
- `EPUB → Markdown`：直接读取 EPUB package，按 spine 顺序解析 XHTML；保留图片、表格、脚注、目录和超链接，不做内容清洗。
- 暂不支持 `EPUB → PDF → Markdown`，也不执行 OCR。
- Markdown 默认写入 `outputs/format-conversion-master/runs/<run-id>/`；请求中的 `output_file` 可指定外部目标路径，但仍禁止覆盖已有文件。
- 本 Skill 不创建或修改业务归档对象。

## 执行约束

1. PDF 分支开始前必须执行 `ebook-convert --version`；命令不可用或版本号无法解析时暂停。Markdown 分支不依赖 Calibre。
2. 暂停时提示用户从 Calibre 官方渠道安装，并将包含 `ebook-convert.exe` 的 `Calibre2` 目录加入 `PATH`；不得自动安装或改用 Pandoc。
3. 输出写入 `outputs/format-conversion-master/runs/<run-id>/`，验证通过后原子发布；已存在的目标文件不得覆盖。
4. 输入、输出、Calibre 版本、哈希、阶段和错误均写入运行检查点；恢复只依赖 `state.json` 的 `resume_stage`。

详细安装说明见 [references/calibre-installation.md](references/calibre-installation.md)。

## 完成门禁

只有状态为 `completed`、转换产物通过对应格式验证、发布回执和哈希闭合时，才可以报告完成。Markdown 分支遇到无法解析的 XHTML 或疑似扫描内容必须暂停并说明原因。
# CLI

入口：`python skills/format-conversion-master/scripts/cli.py`。支持 `create-request`、`start`、`status`、`resume`、`verify`；状态和文本日志写入 `logs/format-conversion-master/runs/<run-id>/`，正式转换产物写入 `outputs/format-conversion-master/runs/<run-id>/`。退出码遵循 `docs/Skills_说明书.md`。
