# render-handwritten-essay-card 使用与协议补充

入口为 `python skills/render-handwritten-essay-card/scripts/cli.py`，支持 `start`、`deliver --mode preview`、`status`、`resume` 和 `verify`。默认模型为 `image-2`，可切换为 `image-2.5`；答题卡为通用英语考试答题卡，字体为手写印刷体。

运行遵循 `prepared → validating_request → normalizing_essay → extracting_layout_requirements → composing_prompt → validating_prompt → publishing_prompt_preview → preview_ready → delivering_prompt_preview → paused_imagen_confirmation`。必须先调用 `deliver --mode preview`，原样在对话中展示一个完整提示词代码块，再询问是否调用 `/imagen`。接受后，Skill 直接发起项目内 `/imagen`；图片文件或返回地址保存到 `outputs/render-handwritten-essay-card/runs/<run-id>/`，并通过 `resume --image-path` 或 `resume --image-url` 完成验证和发布。拒绝调用时仅发布提示词并完成。状态与事件位于 `logs/render-handwritten-essay-card/runs/<run-id>/`。

每个 skill 的 `SKILL.md` 还应记录其专用参数、状态机、产物路径和恢复方式。
