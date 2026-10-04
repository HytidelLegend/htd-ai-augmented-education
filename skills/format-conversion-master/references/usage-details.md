# format-conversion-master 使用与协议补充

DOC/DOCX/PDF → Markdown 使用 MinerU v4 精准解析 API，根目录 `.env` 配置 `MINERU_API_KEY`，进程环境变量优先。仅支持本地文件；此分支固定发布到 `outputs/format-conversion-master/runs/<run-id>/mineru/`，完整保留 `full.md`、图片及所有解析 JSON，不接受外部 `--output`。EPUB 接口保持兼容。

MinerU 状态机为 `prepared → validating_input → staging_input → checking_configuration → requesting_upload → uploading → polling → downloading → extracting → preparing_markdown → verifying → publishing → completed`。脚本负责上传、轮询、下载、安全解压及全包校验；暂停后依据 `resume_stage` 继续，查询已有批次，提交结果不明确时不重提。完整参数、签名地址中断处理及退出码见 Skill 契约。运行状态、中间包和回执均保存于对应 `logs/` 目录；转换无需 Agent 生成正文。

词汇星图的葫芦排程改由共享 `utils/scripts/hulu_schedule.py` 应用状态机负责，旧独立 skill 移至 `tmp/schedule-hulu-plan/`，不注册、不参与运行链。拼写关系维护使用 `skills/build-word-entry/scripts/spelling_relations.py start --full`，支持增量 `start`、`status`、`resume`、`verify`；通用实现和 Schema 位于 `utils/`，全量／增量均无需 Agent 判断。
