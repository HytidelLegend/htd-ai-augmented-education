---
name: run-speech-to-text
category: common_tool
description: 通过火山引擎将音视频转为带词级与句级时间戳的逐字稿；支持共享热词、本次追加词表、纠错、确认、恢复和验证。
---

# 语音转文字

通过火山引擎将音视频转为带词级与句级时间戳的逐字稿；支持共享热词、本次追加词表、纠错、确认、恢复和验证。

## 具体场景示例

```yaml
scenario_examples:
- id: run-speech-to-text
  user_request: 请把这段音频转为带时间戳的逐字稿
  when_to_call: 用户要求语音转文字、提取时间戳或批量生成逐字稿时
  invocation: start → submit-corrections（可选）→ approve-transcript → verify → deliver
  expected_output: 确认后的逐字稿、毫秒时间戳及已验证的 handoff
```

## 项目运行契约

- `--root` 指向项目根目录，使用 `runtime/.venv/Scripts/python.exe`。
- 正式产物位于 `outputs/<skill>/runs/<run-id>/`，输入快照、状态、事件、API 回执和中间文件位于 `logs/<skill>/runs/<run-id>/`；不写入 `runtime/`。
- 所有时间字段及 run ID 均调用 `utils/scripts/timestamp.py`；同秒冲突追加数字后缀。
- `start`、`status`、`resume`、`verify`、`deliver` 为统一入口；原有专用命令仍可调用。`deliver` 仅交付 completed 运行，并重新验证，不隐式批准草稿。
- CLI 退出码遵循说明书：成功 0、输入无效 2、等待决策 3、验证失败 4、运行错误 5、依赖或配置缺失 6。`status` 查询成功返回 0。
- 确认只能依据当前稿件或试听的精确 SHA-256；用户未明确确认时不能填写确认回执。普通恢复不得越过确认门禁。
- 文件输入只读；同名目标冲突不覆盖。Mock 回归不读取密钥，不访问云端。

## 跨 Skill 契约

`handoff.json` 由共享 `utils/scripts/speech_handoff.py` 生成，按 `speech-handoff-v1.schema.json` 校验。字段为 `schema_version`、`producer_skill`、`producer_run_id`，以及 `manifest`、`approved_transcript`、`approval_receipt` 各自的 `_path` 和 `_sha256`。路径相对项目根目录。

TTS 保存上游引用快照，重新读取上游状态、执行上游 `verify` 并复核所有哈希。批准稿与确认回执必须一致；上游发生变化后拒绝继续合成。不共写上游运行目录，不自动选择最新 run。

## 输入、热词与命令

支持 `.mp4`、`.mov`、`.mkv`、`.mp3`、`.wav`、`.m4a`、`.flac`；必须有有效音轨。默认 `streaming`，可用 `--implementation recording_file` 切换。`config.yaml` 保存提供方与音频参数，每次运行冻结配置。

`VOLCENGINE_API_KEY` 统一由 `project_env.py` 读取，进程环境变量优先于根目录 `.env`。FFmpeg/FFprobe 可通过 `FFMPEG_PATH`、`FFPROBE_PATH` 指定，否则使用 PATH。

热词默认复用 `utils/references/术语表.txt`；`--hotwords-file` 追加本次词表，格式为一行一词，可选 `词语|权重`（1～10，默认 4）。相同词忽略英文大小写，本次权重优先。原包词表在 `references/optional-hotwords.txt`，仅显式指定时使用，不自动写回共享表。最终词表、来源与哈希冻结在本次日志中；非法词或超限明确报错，不静默截断。

```text
runtime/.venv/Scripts/python.exe skills/run-speech-to-text/scripts/cli.py start --root . --input input.mp3
runtime/.venv/Scripts/python.exe skills/run-speech-to-text/scripts/cli.py start --root . --input input.mp3 --hotwords-file skills/run-speech-to-text/references/optional-hotwords.txt
runtime/.venv/Scripts/python.exe skills/run-speech-to-text/scripts/cli.py submit-corrections --root . --run-id <run-id> --corrections <json>
runtime/.venv/Scripts/python.exe skills/run-speech-to-text/scripts/cli.py approve-transcript --root . --run-id <run-id> --confirmed-by <确认来源> --transcript-sha256 <hash> --timestamps-sha256 <hash>
runtime/.venv/Scripts/python.exe skills/run-speech-to-text/scripts/cli.py resume --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/run-speech-to-text/scripts/cli.py verify --root . --run-id <run-id>
```

原有 `run`、`init-request`、`--run-dir` 接口仍可调用。`scripts/export_transcript_markdown.py` 从验证后的 run 导出已有句级时间标记 Markdown 模板，并区分“待人工确认”与“已确认”；导出目录必须位于项目 `outputs/` 内，支持上述音视频格式；相同内容可重复导出，内容不同的同名目标拒绝覆盖。

## 状态机

流式主路径：`initialized → input_validated → audio_extracted → asr_connecting → asr_initialized → asr_streaming → asr_receiving → asr_stream_completed → asr_responded → transcript_normalized → awaiting_transcript_approval → transcript_approved → verifying → completed`。

录音文件分支将流式阶段替换为 `asr_submitting → asr_submitted → asr_polling → asr_responded`。共享状态存储校验合法迁移；失败进入 `paused_error`，从持久化检查点恢复。流式中断重新建立会话；录音提交前保存 task ID，提交结果不明确时查询已有任务，不重复提交。输入、提取音频、热词正文与来源快照均登记指纹；变化时暂停。纠错与批准前先复核现有产物，避免重新登记被篡改的内容。并行初始化通过日志目录中的项目锁保护，同秒冲突由统一时间戳函数追加后缀。

批量入口为 `scripts/batch_transcripts.py start --root . --input-dir <目录>`，支持 `resume/status/verify/deliver --run-id <batch-run-id>`。批量状态为 `prepared → processing → awaiting_transcript_approval → processing → completed`，错误进入 `paused_error`；支持上述音视频格式，每个文件的 child run 在调用云端前持久化。Markdown 导出按批准状态和正文哈希分别放入 child 的 `generated/markdown/<sha256>/` 或 `approved/markdown/<sha256>/`，纠错后保留原导出。`verify/deliver` 是只读校验，不调用云端、不推进状态、不重新导出；只有 completed 批次可成功交付。批量完成需要所有 child run 均完成且验证通过。

## 产物与完成门禁

正式目录保存 `generated/transcript.txt`、`generated/full.timestamps.json`、`approved/transcript.txt`、`approved/full.timestamps.json`、`approved/transcript-approval.json`、`manifest.json` 和 `handoff.json`。纠错模板与补丁、原始响应、媒体处理日志、热词和输入副本保存在日志目录。

时间戳沿用 schema `1.1`，毫秒、`[start_ms, end_ms)`，非空 `items` 和 `sentences`。脚本复核正文与时间戳对齐、批准回执和 manifest 中所有文件哈希；确认完成前不能向下游发布已批准状态。
