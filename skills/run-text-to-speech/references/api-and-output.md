# API 与输出约定

## 目录

- API 请求
- 输入副本
- 版本化试听与人工确认
- 时间戳
- 音频拼接
- 白噪音铺底

## API 请求

- 端点：`https://openspeech.bytedance.com/api/v3/tts/unidirectional`
- API key：由共享 `utils/scripts/tts_backend.py` 严格读取项目根目录 `.env` 中的 `VOLCENGINE_API_KEY`，忽略进程环境变量，不写入日志或输出。
- 默认资源 ID：`seed-tts-2.0`。
- 默认 `speech_rate` 为 `10`，即约 1.1 倍速；允许范围为 `[-50, 100]`。
- 请求开启 `enable_timestamp` 与 `enable_subtitle`。
- 真实请求按 batch 顺序执行，失败按配置重试。Mock 模式不会读取 API key，也不会访问网络。

## 输入副本

每次运行使用独立 `logs/run-text-to-speech/runs/<run-id>/inputs/`；上游逐字稿通过 handoff 引用，不共写运行目录：

- 文件输入：只接受 UTF-8 `.txt` 或 `.md`，按原始字节复制，不改动源文件。
- 文本输入：保存为 UTF-8、无 BOM、LF 换行的 `inputs/transcript.txt`。
- TTS 只读取上游确认的 `approved/transcript.txt`，并在本次日志的 `generated/synthesis.txt` 生成合成文本。段落前已有句末标点时删除换行，否则补 `。` 后连接下一段。
- 逐字稿中的 `{{pause:1500ms}}` 表示插入 `1500 ms` 静音；只接受 `1～60000` 的整数毫秒。指令保留在批准逐字稿中，但不会进入 `synthesis.txt` 或 TTS API 请求。
- 本次日志的 `generated/synthesis-map.json` 记录 LF 标准化逐字稿中的段落字符区间、合成文本区间、自动补入的标点、关联 batch 及 batch 内局部区间。

## 局部修订

- `locate` 在已完成运行的标准化原文中做字面量匹配，返回稳定 `match_id`、上下文、段落和 batch。
- `revise` 创建新的运行和输出目录，继承音色、资源、音频参数与配置快照，不修改基准运行。
- 第一版每处修改必须完整位于一个 batch 内，不能包含换行；修改后各原 batch 文本拼接结果必须等于新的合成文本。
- 未修改 batch 的音频和原始时间戳按 SHA-256 校验后复制到新输出；只有修改 batch 请求 TTS。
- 完整音频、完整时间戳、诊断和 manifest 始终重新生成。
- manifest 的 `revision` 保存基准运行、基准 manifest 指纹、修改详情、重新生成和复用的 batch ID；每个 batch 用 `origin` 标记 `generated` 或 `reused`。

## 版本化试听

所有 batch 已验证且实际合并音频总时长严格低于目标时，直接生成最终音频，不生成试听或批准回执；测量包含首尾静音、显式停顿、批次间停顿及容器时长。等于目标仍需试听。重试后变短也采用此规则，保留已拒绝版本供审计，并将 current_version 置为 0、approval 置为 null；跳过原因保存在已有状态事件中。

- `config.yaml` 的 `preview.duration_seconds` 是试听目标时长；runner 根据真实时间戳找到达到目标后的首个完整句末，并裁切试听音频。
- 试听产物写入 `speech/preview/vNNN/`。clean 试听保留为母版；启用白噪音时，人工试听入口为同版本的 `preview_with_white_noise.<format>`。
- 当前版本以试听音频 SHA-256 确认。普通 `resume` 不能越过 `paused_preview_approval`。
- `retry-preview` 在同一 run ID 内保留旧版本、拒绝原因和参数快照，清理当前 batch 工作副本后重新合成；批准版本生成结构化 `approval-receipt.json`。
- 全量阶段复用批准试听阶段已经验证的 batch，不重复调用 TTS API。

## 时间戳

每个 batch 保存 API 原始时间戳。`full.timestamps.json` 使用毫秒和半开区间
`[start_ms, end_ms)`，包含三层：

- `items`：API 返回的字／词粒度时间戳。
- `sentences`：由逐字稿句末标点与 items 对齐形成的句子范围。
- `batches`：每个 batch 在完整音频中的范围。
- `batches` 同时记录合成文本 span、原文段落 ID、段落片段及 `origin`。
- `diagnostics`：有效发音时长、首尾静音、字词间隔、静音占比及超阈值长停顿。长停顿只产生告警，不影响完成状态。
- `pauses`：逐字稿显式停顿，包含原始指令、逐字稿 span、合成文本锚点及完整音频中的半开区间；显式停顿不计入非显式长停顿告警。

拼接时如按 `config.yaml` 插入边界静音，总时间戳会累计静音偏移。默认停顿均为 `0`。

## 音频拼接

- WAV 使用 Python 标准库拼接。
- MP3、OGG Opus 和 PCM 以外的容器处理使用系统 `ffmpeg`／`ffprobe`；缺失时明确失败，不静默降级。
- 拼接结果保存为 `speech/full_clean.<format>`，不得由铺底步骤覆盖。
- 完整音频、时间戳和 `manifest.json` 仅在所有 batch 成功且验证通过后交付。

## 白噪音铺底

- 共享素材固定为 `${PROJECT_DIR}/utils/assets/white-noise.wav`，由 `utils/scripts/audio_noise_bed.py` 确定性生成并记录 SHA-256。
- `config.yaml` 的 `white_noise.enabled` 默认是 `true`；`white_noise.volume_dbfs` 默认是 `-70.0`，表示白噪音自身的目标 RMS，而不是人声音量增益。
- runner 按 clean 音频信号检测静音：默认阈值为 `-60 dBFS`、检测窗口为 `10 ms`、最短持续时间为 `120 ms`，只在通过检测的区间循环素材，并以 `15 ms` 淡入淡出避免点击声。有人声的区间不混入白噪音。
- 铺底版 MP3 使用 `96 kbps` 编码，降低再次编码对人声的损伤；PCM 混音阶段的人声样本保持不变。生成 `speech/full_with_white_noise.<format>` 后，时间戳不发生偏移。
- manifest schema 3/4 同时记录 `clean_audio`、`white_noise` 和下游默认入口 `full_audio`。启用时 `full_audio` 指向铺底版，禁用时指向 clean 母版。
- schema 2 历史运行继续按原契约验证和交接，不原地迁移 manifest。

## Edge-TTS 分支

默认后端为 `edge-tts`，音色晓艺、语速 `+10%`。客户端流式收集 MP3 和 WordBoundary；100 ns 转毫秒后沿用 items，再按原文句末聚合 sentences。音频与时间戳在 staging 成对验证后发布，失败恢复不复用半成品。支持现有音频格式，非原生格式由 FFmpeg 转换。不支持 SSML、火山资源 ID 或情绪参数。manifest schema 4 保存后端、版本、时间戳粒度；读取兼容 schema 2/3。

重新试听先校验所选后端支持的参数；Edge 拒绝 resource-id、emotion 及显式 emotion-scale。合成参数变化时全部 batch 标为 generated，同步更新修订清单，不能复用旧音色或旧语速音频。新运行的每个原始时间戳必须携带合成指纹；删除该字段同样使 verify 失败。历史 schema 3 运行及其修订兼容旧原始时间戳格式。适配器版本冻结于运行状态和原始时间戳中。

独立 verify 对本项目 schema 4 运行要求状态文件存在，并核对配置快照指纹与 manifest 试听记录；缺失或不一致时拒绝验证。短音频仍须通过上游批准稿、batch 指纹、音频、停顿与时间戳检查。
