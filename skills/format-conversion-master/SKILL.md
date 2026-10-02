---
name: format-conversion-master
category: common_tool
description: 通过状态机执行可恢复的格式转换；支持 EPUB 转 PDF 或 Markdown，以及本地 DOC、DOCX、PDF 通过 MinerU 转 Markdown 并交付完整解析包。当用户要求转换这些文件格式，或继续、检查既有转换运行时使用。
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
- `DOC/DOCX/PDF → Markdown`：调用 MinerU v4 精准解析 API；仅接受本地文件，支持扫描 PDF 的识别，默认模型 `vlm`。
- 暂不支持 `EPUB → PDF → Markdown`。EPUB 分支不执行 OCR。
- EPUB → Markdown 默认写入 `outputs/format-conversion-master/runs/<run-id>/`；请求中的 `output_file` 可指定外部目标路径，但仍禁止覆盖已有文件。
- 本 Skill 不创建或修改业务归档对象。

## MinerU 分支

先在 `${PROJECT_DIR}/.env` 填写 `MINERU_API_KEY`；进程环境变量优先于根目录 `.env`。不读取 `settings/.env`，不把密钥、鉴权头或签名地址写入状态、日志和产物。缺失、空值或占位密钥暂停，服务端鉴权失败也暂停。

```text
runtime/.venv/Scripts/python.exe skills/format-conversion-master/scripts/cli.py start --root . --input document.pdf --to md
runtime/.venv/Scripts/python.exe skills/format-conversion-master/scripts/cli.py start --root . --input document.docx --to md --language ch --model-version vlm --page-ranges 1-10
```

单文件上限为 200 MB、200 页；本地检查文件大小及格式头，页数超限由服务端拒绝，不隐式截断。参数 `model_version`、`is_ocr`、`enable_formula`、`enable_table`、`language`、`page_ranges` 可通过请求中的 `mineru` 对象配置；默认模型 `vlm`、公式和表格开启、语言 `ch`。网络请求默认 60 秒，轮询默认 300 秒、间隔 5 秒。`request_timeout` 和 `poll_interval` 上限 60 秒；超时暂停后查询同一批次。

脚本 `create-request` 生成请求 JSON；可使用 `--model-version`、`--language`、`--page-ranges`、`--is-ocr`、`--request-timeout`、`--poll-timeout`、`--poll-interval`，也可在 `start` 直接指定这些参数。已有请求文件与 CLI 的 MinerU 配置不得同时提供。额外字段及错误类型由 Schema 拒绝。

状态机为 `prepared → validating_input → staging_input → checking_configuration → requesting_upload → uploading → polling → downloading → extracting → preparing_markdown → verifying → publishing → completed`。配置、输入、网络、超时、远端失败、解压、验证和发布冲突进入对应 `paused_*` 状态；恢复仅读取检查点 `resume_stage`。远端任务按本次 `data_id` 匹配，禁止取批次首项代替匹配。

提交前保存意图，成功后立即保存 `batch_id`；提交响应不明确进入 `paused_submission_unknown`，不自动重提。核对 MinerU 中同一 `data_id` 后用 `resume --run-id <id> --batch-id <batch_id>` 恢复。上传签名地址仅存在进程内存；中断恢复先查询原任务，已经上传则继续；仍等待上传且地址不再可用时进入 `paused_upload_expired`，核对远端状态后新建运行。远端失败后恢复仍查询原任务，不自动创建新任务。

正式输出固定为 `outputs/format-conversion-master/runs/<run-id>/mineru/`，完整保留服务返回的 `full.md`、图片目录及全部布局／内容／模型 JSON 和其他文件，不重写 Markdown、不添加 front matter、不将表格转换成另一种模板。此分支不接受 `--output`；EPUB 的外部目标接口保持原有行为。输入快照、原始 ZIP、解压暂存、状态、验证与发布回执写入对应 `logs/` 运行目录。

下载与解压设大小上限，拒绝路径穿越、绝对路径、符号链接、重复路径及不安全 Windows 路径。发布前检查非空 UTF-8 `full.md`、本地资源引用和全包文件哈希；验证后以目录重命名发布，拒绝覆盖已有目标。`verify` 复核全包文件集合及哈希；新增、删除或修改任一文件都会失败。结构验证不保证 OCR 语义准确。

共享配置、API 和 ZIP 实现分别位于 `utils/scripts/project_env.py`、`mineru_client.py` 和 `safe_archive.py`；状态、哈希、Schema、时间戳和发布机制复用项目工具。Python 执行全部转换阶段，无需 Agent 生成正文或长 JSON。API 细节见 [references/mineru-api.md](references/mineru-api.md)。

## 执行约束

1. EPUB → PDF 分支开始前必须执行 `ebook-convert --version`；命令不可用或版本号无法解析时暂停。Markdown 分支不依赖 Calibre。
2. 因 Calibre 缺失暂停时提示用户从官方渠道安装，并将包含 `ebook-convert.exe` 的 `Calibre2` 目录加入 `PATH`；不得自动安装或改用 Pandoc。
3. 输出写入 `outputs/format-conversion-master/runs/<run-id>/`，验证通过后原子发布；已存在的目标文件不得覆盖。
4. 输入、输出、Calibre 版本、哈希、阶段和错误均写入运行检查点；恢复只依赖 `state.json` 的 `resume_stage`。

详细安装说明见 [references/calibre-installation.md](references/calibre-installation.md)。

## 完成门禁

只有状态为 `completed`、转换产物通过对应格式验证、发布回执和哈希闭合时，才可以报告完成。EPUB → Markdown 遇到无法解析的 XHTML 或疑似扫描内容必须暂停并说明原因。
# CLI

入口：`python skills/format-conversion-master/scripts/cli.py`。支持 `create-request`（别名 `init-request`）、`start`、`status`、`resume`、`verify`、`list-formats`；状态和文本日志写入 `logs/format-conversion-master/runs/<run-id>/`，正式转换产物写入 `outputs/format-conversion-master/runs/<run-id>/`。退出码遵循 `docs/Skills_说明书.md`；暂停返回 3，网络和运行错误返回 5，参数错误返回 2，验证失败返回 4，依赖缺失返回 6。

新运行目录直接使用共享工具生成的 `YYYYMMDDTHHMMSS`，同秒冲突追加 `_1`、`_2`；旧运行 ID 仍可恢复。请求快照保存哈希，恢复和完成验证时检查；修改配置应新建运行，`.env` 密钥更新不影响快照。轮询超时从开始轮询时计算，不包含上传耗时。缺少 `python-dotenv` 时仅 MinerU 分支进入 `paused_dependency`，EPUB 无需此依赖。API 鉴权错误和上传签名地址过期分别处理；鉴权请求禁止重定向，存储重定向只允许 HTTPS。完成验证同时核对产物清单状态、磁盘回执和状态中的回执。共享 `utils/scripts/file_publish.py` 验证目录副本后发布；已完成运行再次 `resume` 也重新验证产物。
