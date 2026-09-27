# 架构文档

系统边界、组件关系、状态机和恢复机制放在本目录。

面向用户的即开即用 AI 辅助学习小工具放在项目根目录的 [`applications/`](../../applications/)；每个应用的实现和使用说明由对应子目录维护。

## 时间戳约定

- JSON、日志、状态和 Manifest 字段使用本地时间、不带时区的 `YYYY-MM-DDTHH:MM:SS`。
- 文件名、目录名和运行 ID 中的时间部分使用 `YYYYMMDDTHHMMSS`。
- 同类对象的时间戳完全相同时，使用 `YYYYMMDDTHHMMSS_1`、`YYYYMMDDTHHMMSS_2` 等最小可用后缀。
- 所有生成逻辑统一调用 [`utils/scripts/timestamp.py`](../../utils/scripts/timestamp.py)，不在业务代码中直接生成时间戳。
