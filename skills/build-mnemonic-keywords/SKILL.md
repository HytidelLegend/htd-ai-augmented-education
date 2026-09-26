---
name: build-mnemonic-keywords
category: ai_assisted_learning
description: 为初高中及其他需要背诵的一句知识生成关键词、联想记忆场景和 Image 2、Image 2.5 或豆包可用的生图提示词，并在用户确认后调用 /imagegen。用户要求背诵、记忆口诀、联想记忆、场景化记忆或把记忆法画成图时使用。
---

# 关键词联想记忆构建

本 Skill 只处理一句输入，可为长难句。关键词、句子、故事和记忆场景必须遵循共享规范 [`utils/references/mnemonic-association-principles.md`](../../utils/references/mnemonic-association-principles.md)。编句固定经过四步：提取关键词或代表词、适当调整顺序、编句子或故事、自检关键词覆盖。保留“简洁”就必须出现“简洁”或其谐音；保留“简”就必须出现“简”或其谐音。性联想默认允许使用；夸张、猎奇、搞笑或荒诞表达也可以使用。用户给出的原句和附件才是记忆对象；Skill 不主动替学科编写知识例题。

联想法的适用边界：一句话中应包含多个要记忆的要点，且每个要点最好是一个关键词、词组或简短短语。联想句必须覆盖所有所选关键词，不能只覆盖其中一个字；它必须常见、逻辑通顺、容易记忆和复述。太长的词可换成简短词，抽象名词可换成具体事物。不得把关键词粗暴并列成生硬、陌生、增加记忆负担的句子；这种情况必须判为 `weak` 或 `bad`。如果找不到符合条件的句子，只展示提取出的关键词，不展示候选口诀或生图提示词。

## 命名候选

- `build-mnemonic-keywords`（采用）
- `memory-cue`
- `recall-scene`

三个候选均为小写、横杠分隔，且不使用 `ing` 形式。

## CLI

```powershell
runtime/.venv/Scripts/python.exe skills/build-mnemonic-keywords/scripts/cli.py start --root . --input request.json
runtime/.venv/Scripts/python.exe skills/build-mnemonic-keywords/scripts/cli.py resume --root . --run-id <run-id> --input agent-response.json
runtime/.venv/Scripts/python.exe skills/build-mnemonic-keywords/scripts/cli.py deliver --root . --run-id <run-id> --mode preview
runtime/.venv/Scripts/python.exe skills/build-mnemonic-keywords/scripts/cli.py resume --root . --run-id <run-id> --decision accept --image-path <path>
runtime/.venv/Scripts/python.exe skills/build-mnemonic-keywords/scripts/cli.py resume --root . --run-id <run-id> --decision accept --image-url <url>
runtime/.venv/Scripts/python.exe skills/build-mnemonic-keywords/scripts/cli.py resume --root . --run-id <run-id> --decision decline
runtime/.venv/Scripts/python.exe skills/build-mnemonic-keywords/scripts/cli.py status --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/build-mnemonic-keywords/scripts/cli.py verify --root . --run-id <run-id>
```

输入 JSON 至少包含 `sentence`，可选 `subject`、`model`（`image-2`、`image-2.5`、`doubao`、`other`）和 `aspect_ratio`。运行产物位于 `outputs/build-mnemonic-keywords/runs/<run-id>/`，请求快照、生成包、状态和事件位于 `logs/build-mnemonic-keywords/runs/<run-id>/`。

## 状态机

`prepared → validating_request → normalizing_sentence → loading_principles_reference → extracting_keywords → building_generation_packet → paused_agent_generation → validating_agent_response → composing_result → self_checking → publishing_prompt_preview → preview_ready → paused_image_confirmation`。若覆盖校验、原则检查或自然度判断不通过，则从 `self_checking` 进入 `paused_quality_review`，只发布关键词和失败说明，不发布口诀或生图提示词。

`paused_image_confirmation` 后，用户拒绝生图则进入 `publishing → completed`；用户确认后，对话层调用 `/imagegen`，把图片路径或地址传给 `resume`，进入 `invoking_imagegen → verifying_image_result → publishing → completed`。输入、Agent 草稿、质量判断或图片结果不足时进入相应 `paused_*` 状态。退出码 3 表示需要用户决策或 Agent 结构化草稿，2 表示输入或 schema 错误，4 表示验证失败。

## Agent 结构化草稿

`start` 生成 `generation_packet.json` 并暂停。Agent 只需按 `references/agent-response.schema.json` 提交：

- `selected_keywords`：第一步从候选中筛出真正需要记忆的完整词或代表字词，按需要保留原顺序；
- `keyword_mappings`：逐项提供“关键词、关键词作用、熟悉物象、场景中的位置或动作、口诀中实际出现的片段、片段类型”；`mnemonic_cue` 必须原样出现在 `mnemonic` 中，`cue_type` 为 `exact` 或 `homophone`；
- `mnemonic`：一句可复述的记忆法，关键词与物象的对应要清楚；
- `scene`：包含主体、动作、夸张或幽默视觉关系的场景；
- `sensory_hooks`：至少两种感官线索；
- `order_note`：说明顺序是否必须保留以及场景如何固定；
- `image_prompt`：完整、可直接交给生图工具的提示词，避免依赖本次对话的省略指代；
- `self_check.fit`：`good`、`weak` 或 `bad`；当联想牵强、增加新负担、无法想象、只是解释知识而不能帮助回忆原句，或不如直接背诵时必须选择 `weak`/`bad`，并在 `reason` 和 `recommendation` 中明确告诉用户这个例子不适合用联想法或需要改用其他方法。
- `self_check.principle_checks`：逐项填写共享规范中的原则是否适用、是否通过和简短原因；不适用的原则标记为 `applicable: false`，不应因此判为失败。

可使用搞笑、猎奇或成人双关，但应服从用户适龄偏好；默认优先清晰、可视化、可复述的表达。参考案例见 `references/mnemonic-examples.md`，其中用户和“单易之”的全部案例必须保留并用于质量校准；生物、化学、物理、英语的生硬示例属于负面样例，不得作为推荐输出。

## 对话交付规则

1. 读取 `generation_packet.json`，根据候选关键词和参考案例提交结构化 Agent 草稿；不要自行改写 JSON schema。
2. 只有覆盖校验和自然度自检均通过时，`resume --input` 才进入图片预览；再执行 `deliver --mode preview`，原样发送完整生图提示词代码块和确认问题。提示词中应明确主体、动作、构图、风格、感官线索、文字可读性要求和负面提示词。
3. 用户确认后调用 `/imagegen`。生成结果必须通过 `resume --image-path` 或 `resume --image-url` 归档；未取得结果时保持暂停，不得伪造图片成功。
4. 最终交付读取 `outputs/build-mnemonic-keywords/runs/<run-id>/result.md`。若自检为 `weak` 或 `bad`，结果中必须显眼说明“这个例子不适合直接使用联想法”或具体替代建议。

## 参考资料

- `references/mnemonic-examples.md`：用户提供案例、“单易之”医学案例和质量校准样例。
- `../../utils/references/mnemonic-association-principles.md`：所有记忆相关 Skill 共用的编句、联想和场景原则。
- `references/request.schema.json`：请求 schema。
- `references/agent-response.schema.json`：Agent 草稿 schema。
- `references/output.schema.json`：正式结果 schema。
