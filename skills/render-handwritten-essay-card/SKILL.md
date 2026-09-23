---
name: render-handwritten-essay-card
category: ai_assisted_learning
description: 将英语作文转换为适用于 Image 2 或 Image 2.5 的通用英语考试答题卡手写印刷体照片提示词，并在用户确认后发起项目内 /imagen 生图。用户提供英语作文、要求生成答题卡照片或手写作文图片时使用。
---

# 手写英语作文答题卡照片

本 Skill 使用脚本状态机保存可恢复运行。输入可以是内联英语作文或 UTF-8 文本文件；默认模型为 `image-2`，可切换为 `image-2.5`。答题卡固定为通用英语考试答题卡，字体固定为清晰的手写印刷体。

## CLI

```powershell
runtime/.venv/Scripts/python.exe skills/render-handwritten-essay-card/scripts/cli.py start --root . --input request.json
runtime/.venv/Scripts/python.exe skills/render-handwritten-essay-card/scripts/cli.py deliver --root . --run-id <run-id> --mode preview
runtime/.venv/Scripts/python.exe skills/render-handwritten-essay-card/scripts/cli.py status --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/render-handwritten-essay-card/scripts/cli.py resume --root . --run-id <run-id> --decision accept
runtime/.venv/Scripts/python.exe skills/render-handwritten-essay-card/scripts/cli.py resume --root . --run-id <run-id> --decision decline
runtime/.venv/Scripts/python.exe skills/render-handwritten-essay-card/scripts/cli.py resume --root . --run-id <run-id> --decision accept --image-path <path>
runtime/.venv/Scripts/python.exe skills/render-handwritten-essay-card/scripts/cli.py verify --root . --run-id <run-id>
```

`start` 会生成并校验预览交付载荷，然后进入 `paused_imagen_confirmation`，返回退出码 3。对话层必须立即调用 `deliver --mode preview`，原样发送其 stdout；stdout 固定为“一个完整提示词代码块 + 后置确认问题”，不得自行改写、概括或只发送确认句。用户同意后，直接发起 `/imagen`，并将返回图片路径或地址通过 `resume --image-path` 或 `resume --image-url` 传回脚本；若暂时无法提供结果，保持暂停，不得伪造成功。

## 状态机

`prepared → validating_request → normalizing_essay → extracting_layout_requirements → composing_prompt → validating_prompt → publishing_prompt_preview → preview_ready → delivering_prompt_preview → paused_imagen_confirmation`。

接受后进入 `invoking_imagen → verifying_image_result → publishing → completed`；拒绝则直接进入 `publishing → completed`。输入、输出冲突、验证失败和无法调用 `/imagen` 时使用对应 `paused_*` 状态。

## 产物

正式产物位于 `outputs/render-handwritten-essay-card/runs/<run-id>/`，包括 `result.json`、`result.md`，以及生图成功时的图片文件或返回地址记录。状态和事件位于 `logs/render-handwritten-essay-card/runs/<run-id>/`。

## 对话交付

必须通过 `deliver --mode preview` 读取并原样发送 `delivery.message_markdown`：一个完整提示词代码块，随后是确认问题。不得只发送确认问题。提示词必须保留作文原文、段落顺序和标点意图，并明确要求通用英语考试答题卡、手写印刷体、真实拍摄效果；负面提示词必须禁止电脑字体、连笔书法、装饰字体、增删改写正文和不可读文字。
