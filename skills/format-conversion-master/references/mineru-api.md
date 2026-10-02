# MinerU 接入约定

权威接口文档：https://mineru.net/apiManage/docs 。本项目参考 `tmp/external-skills/everything-to-markdown` 的调用流程，以官方 v4 精准解析 API 为准，不依赖该临时目录。

仅实现本地 DOC、DOCX、PDF：POST `/api/v4/file-urls/batch` 获取上传地址，PUT 原始文件上传，GET `/api/v4/extract-results/batch/{batch_id}` 按 `data_id` 查询，完成后下载 `full_zip_url`。鉴权头只用于官方 API，不发给上传／下载存储服务。上传及下载地址必须为 HTTPS。

请求中 `model_version`、`enable_formula`、`enable_table`、`language` 在顶层；`name`、`data_id`、`is_ocr`、`page_ranges` 在 `files` 项内。本项目使用单文件批次；不启用回调、不额外申请其他导出格式。默认导出的完整包直接交付。

当前文档限制：单文件 200 MB、200 页。上传请求超时不代表远端未创建任务，禁止盲目重试 POST。轮询 GET、下载和本地处理可按检查点恢复；不记录原始错误响应，避免其中包含签名地址或用户文档内容。

状态处理：`waiting-file` 表示等待上传或等待提交解析；上传成功后短暂出现此状态可继续轮询。`pending`、`running`、`converting` 继续查询，`done` 下载，`failed` 暂停。Token 错误或过期暂停更新根目录 `.env`；上传签名地址未持久化，恢复时仍未上传需核对后新建运行。

安全门禁：原始包下载到 `logs/`，安全解压后验证非空 Markdown、本地图片／链接和全量文件哈希，再原子发布到 run 的 `mineru/` 子目录。原始文件名及层级保持不变，内部生成 JSON 统一由共享 `write_json` 写入；时间字段调用 `utils/scripts/timestamp.py`。
