# 架构文档

系统边界、组件关系、状态机和恢复机制放在本目录。

面向用户的即开即用 AI 辅助学习小工具放在项目根目录的 [`applications/`](../../applications/)；每个应用的实现和使用说明由对应子目录维护。

## 时间戳约定

- JSON、日志、状态和 Manifest 字段使用本地时间、不带时区的 `YYYY-MM-DDTHH:MM:SS`。
- 文件名、目录名和运行 ID 中的时间部分使用 `YYYYMMDDTHHMMSS`。
- 同类对象的时间戳完全相同时，使用 `YYYYMMDDTHHMMSS_1`、`YYYYMMDDTHHMMSS_2` 等最小可用后缀。
- 所有生成逻辑统一调用 [`utils/scripts/timestamp.py`](../../utils/scripts/timestamp.py)，不在业务代码中直接生成时间戳。

## 语音工作流

三个语音 Skill 独立持久化状态与产物，通过 `utils/scripts/speech_handoff.py` 的九字段项目相对路径与 SHA-256 引用衔接；TTS 验证上游完成状态、确认回执并重新执行 producer verify。共享词表仍为一行一术语，ASR 权重仅保存在语音适配层及运行快照。

TTS 在现有状态机中通过 backend_resolved / checking_backend_environment 选择 Edge-TTS 或火山后端。共享 `utils/scripts/tts_backend.py` 负责配置、音色和参数预检；Edge 适配器只负责合成与格式转换，试听、停顿、白噪音和验证继续复用统一流程。参数变化使旧 batch 及试听确认失效，新运行严格核验合成指纹，旧运行按火山快照和原 schema 恢复。
