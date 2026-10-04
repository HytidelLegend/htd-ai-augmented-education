# build-mnemonic-keywords 使用与协议补充

原句命中术语表时，候选与源关键词保留完整术语；联想口诀可使用代表字词、缩写或谐音，但须通过映射回忆完整术语。脚本校验源关键词边界及口诀片段覆盖，运行词表快照存于本次日志目录。

入口为 `runtime/.venv/Scripts/python.exe skills/build-mnemonic-keywords/scripts/cli.py`，支持 `start`、`resume`、`deliver --mode preview`、`status` 和 `verify`。输入仅允许一句，可为长难句；共享规范 `utils/references/mnemonic-association-principles.md` 统一定义附件原则、四步编句流程、关键词覆盖、常见性、逻辑通顺、易记性、长词压缩、抽象词具体化以及场景增强方法。流程读取并记录共享规范哈希，先提取完整关键词或代表字词，再编句并校验每个所选词的原词或完整谐音是否实际出现。`start` 生成候选关键词和 `generation_packet.json` 后暂停，Agent 以 `references/agent-response.schema.json` 提交少量结构化内容。脚本负责状态迁移、schema 校验、原则检查、口诀覆盖校验、Markdown 渲染、提示词预览和图片归档；覆盖、原则检查或自然度不通过时只发布关键词和说明，不发布候选口诀或生图提示词。

状态机为 `prepared → validating_request → normalizing_sentence → loading_principles_reference → extracting_keywords → building_generation_packet → paused_agent_generation → validating_agent_response → composing_result → self_checking → publishing_prompt_preview → preview_ready → paused_image_confirmation`；覆盖、原则检查或自然度不通过时从 `self_checking` 进入 `paused_quality_review`，只发布关键词和说明。图片确认后进入 `invoking_imagegen → verifying_image_result → publishing → completed`。若联想牵强、生硬、增加负担或不适合该知识，结果必须标记 `weak`/`bad` 并给出不使用联想法或改用其他方法的建议。参考资料保留用户和“单易之”提供的全部案例；附件和通用记忆原则统一维护在 `utils/references/mnemonic-association-principles.md`。
