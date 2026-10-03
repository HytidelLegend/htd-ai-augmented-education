---
name: run-text-to-speech
category: common_tool
description: 将本项目已完成且确认的逐字稿通过 Edge-TTS（默认）或火山引擎合成为语音；支持分批、试听确认、静音白噪音、时间戳、恢复和局部修订。
---

# 文字转语音

将本项目已完成且确认的逐字稿通过 Edge-TTS（默认）或火山引擎合成为语音；支持分批、试听确认、静音白噪音、时间戳、恢复和局部修订。

## 具体场景示例

```yaml
scenario_examples:
- id: run-text-to-speech
  user_request: 请用默认音色朗读这篇文案，先给我试听
  when_to_call: 用户要求文字转语音、生成朗读音频、试听或修订已有合成语音时
  invocation: start → 上游转换确认 → resume →（达到试听目标时 approve-preview）→ verify → deliver
  expected_output: 最终音频、clean 母版和完整时间戳；达到目标时提供试听并等待确认
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

## 输入与命令

直接输入 `--text` 或 UTF-8 `--input-file`（恰选一个）时，脚本调用项目的 `convert-copy-to-transcript`，为两个 skill 分别建立独立 run，暂停等待上游确认。使用 `--transcript-run-id` 引用已有批准稿；默认 producer 为 `convert-copy-to-transcript`，可用 `--producer-skill run-speech-to-text` 消费已经批准的 ASR 逐字稿。

`--workspace-dir` 或指向现有生产者目录的 `--output-root` 作为明确上游引用，不再共写该目录；新合成产物固定为 `outputs/run-text-to-speech/runs/<run-id>/speech/`。输入与上游文案比较仅忽略 BOM 与换行差异，不一致进入 `paused_input_mismatch`。

```text
runtime/.venv/Scripts/python.exe skills/run-text-to-speech/scripts/cli.py start --root . --input-file copy.md --backend volcengine --speaker 哆啦A梦
runtime/.venv/Scripts/python.exe skills/run-text-to-speech/scripts/cli.py start --root . --transcript-run-id <转换-run-id>
runtime/.venv/Scripts/python.exe skills/run-text-to-speech/scripts/cli.py start --root . --producer-skill run-speech-to-text --transcript-run-id <ASR-run-id>
runtime/.venv/Scripts/python.exe skills/run-text-to-speech/scripts/cli.py resume --root . --run-id <TTS-run-id>
runtime/.venv/Scripts/python.exe skills/run-text-to-speech/scripts/cli.py approve-preview --root . --run-id <TTS-run-id> --confirmed-by <确认来源> --preview-sha256 <hash>
runtime/.venv/Scripts/python.exe skills/run-text-to-speech/scripts/cli.py retry-preview --root . --run-id <TTS-run-id> --reason <原因> --speech-rate <新语速>
runtime/.venv/Scripts/python.exe skills/run-text-to-speech/scripts/cli.py verify --root . --run-id <TTS-run-id>
```

`run/start` 等价。原有 `locate`、`revise`、`retime-pauses`、`supersede` 保留。局部修订创建新的转换与 TTS run，重新等待批准，仅按完整指纹复用未修改 batch。

通过 `start --backend edge-tts|volcengine` 覆盖本次后端；优先级为本次参数、配置文件，保存有效配置快照，恢复不接受后端覆盖。配置 `backend: edge-tts | volcengine`，默认 `edge-tts`。Edge 默认音色为晓艺（`zh-CN-XiaoyiNeural`），语速 `10` 映射为 `+10%`；火山默认音色为 `Hytidel（正常说话）`、资源 `seed-icl-2.0`。映射位于 `references/voices.yaml`。保留配置中的试听目标、音频参数与静音白噪音。脚本预检音色映射、资源匹配、素材、音频工具；账号对该音色的可用性须由实际服务响应确认，配置失败暂停，不擅自换音色。

若所有 batch 已验证且音频总长度严格低于试听目标时长（包括显式停顿与批次间停顿），不生成试听及确认回执，直接合并、铺底并验证最终音频；等于目标时长仍需试听。重试后变短也跳过新试听，保留已拒绝版本，current_version 归零、approval 清空；已有批准版本仍按其回执验证。通过状态机 `preview_synthesizing → batches_ready` 推进，保留跳过事件，不伪造批准。默认试听在达到 `config.yaml` 的目标时长后首个完整句末结束。试听确认以当前版本音频哈希为准，参数改变使旧确认失效；试听通过后复用已验证 batch。`--mock` 强制 WAV，不读取 API key、不访问网络。

不支持 SSML。批准稿中的 `{{pause:1500ms}}` 原样保留，在 API payload 中移除并转为静音事件；非法指令拒绝合成。白噪音仅在持续静音区间铺底，默认 -70 dBFS，保留 clean 母版，不混入人声区间。

## 状态机与完成门禁

`initialized → workspace_resolved → input_validated → backend_resolved → checking_backend_environment → awaiting_transcript → transcript_validated → input_staged → batches_planned → preview_synthesizing → preview_rendering → preview_ready → paused_preview_approval → synthesizing → batches_ready → merging_audio → timestamps_merged → adding_white_noise → final_audio_ready → verifying → completed`。

暂停状态为 `paused_input_mismatch`、`paused_transcript_not_ready`、`paused_preview_approval`、`paused_retryable`、`paused_configuration`、`paused_verification`。迁移由共享状态存储校验；普通 `resume` 不得越过试听确认。上游 handoff 在合成、发布及独立 `verify` 时重新校验，`--run-id` 与 `--output-dir` 均执行同一检查；白噪音素材在铺底前复核预检指纹。

正式 `speech/` 保存 `batches/` 音频、版本化 `preview/`、`full_clean.<format>`、启用时的 `full_with_white_noise.<format>`、`full.timestamps.json` 和 `manifest.json`。输入副本、`synthesis.txt`、`synthesis-map.json`、batch 原始时间戳和 API 中间文件保存在 TTS 日志 run。

完整时间戳沿用 schema `1.1`，含 `items/sentences/batches/diagnostics/pauses`，单位为毫秒。完成要求有效上游批准稿、需要试听时的当前试听确认、batch 指纹、音频时长与时间戳、停顿事件、静音铺底及全部 manifest 检查通过。API 与音频细节见 `references/api-and-output.md`。

## 后端配置与兼容

火山分支严格从项目根目录 `.env` 读取 `VOLCENGINE_API_KEY`，忽略进程环境变量；Edge 分支只检查当前隔离环境中的 `edge-tts==7.2.8` 及词级边界接口，不读取密钥。Edge 是本地客户端，合成使用 Microsoft 在线服务，需要联网，无需安装 Edge 浏览器。预检不代表在线服务可用。

Edge 使用词级边界（100 ns 转整数毫秒），聚合现有句级时间戳；输出时间戳 schema 仍为 `1.1`，manifest 为 `4`，新增 `backend`、`backend_version`、`timestamp_granularity`。不支持自定义 SSML、火山资源 ID 和情绪参数（包括显式 `--emotion-scale`）；指定不支持的参数会报错。Edge 原生输出 24 kHz MP3，其他已有格式和采样率通过 FFmpeg 转换。

恢复使用运行快照，不读取最新后端配置；旧运行缺少 backend 时按火山引擎恢复。修订继承基准运行后端和参数，合成指纹绑定后端、依赖及适配器版本、音色、资源、参数与文本。新运行缺少合成指纹时验证失败；旧运行及其修订维持原 schema 3 验证，兼容没有该字段的历史 batch。重新试听先校验候选参数再修改文件；音色、资源或音频参数改变后，全部 batch 重新合成并撤销旧试听确认。配置缺失进入 `paused_configuration`（6），网络故障进入 `paused_retryable`（5）；resume 重检环境。Mock 不读取密钥、不联网。

## 音色映射

音色表由 `scripts/render_voice_docs.py` 从 YAML 生成，使用 `--check` 校验。

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

独立 verify 要求本项目 schema 4 运行状态存在，并核对配置快照指纹及 manifest 与状态中的试听记录；不能通过删除状态或试听记录跳过门禁。

本地预检与启动共用 `utils/scripts/tts_backend.py` 的配置、参数和音色映射检查；仅检查所选后端，不要求另一后端的音色映射可用。锁定版本与运行快照中的适配器版本变化时暂停，防止错误复用。

上游尚未完成时，回执沿用 `next_action` 字段指明上游命令，`error.message` 给出上游 Skill 和 run ID。先完成该上游操作与 verify，再 resume 本次 TTS；不能在正常等待语义决策的转换运行上调用 resume。
