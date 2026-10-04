# create-dialogue-podcast 使用与协议补充

入口为 `runtime/.venv/Scripts/python.exe skills/create-dialogue-podcast/scripts/cli.py`，支持 `start/status/resume/verify/deliver`。输入副本、TXT 和最终 MP3 位于 `outputs/create-dialogue-podcast/runs/<run-id>/`；状态、筛选判断、配置及子运行引用位于对应 `logs/` 目录。

内容检查仅让 Agent 提交少量区间判断；普通候选须询问用户，已验证的 tutor 讲义自动跳过前置知识、本课知识点列表和正式习题。共享编排位于 `utils/scripts/dialogue_pipeline.py`，内容区间筛选位于 `utils/scripts/podcast_content.py`。Agent 提交区间判断、少量问答和候选读法，Python 处理持久化、校验、全文渲染与音频。问答按源块生成，依次覆盖正文，保持原文语言，不新增事实。调用转换的 `qa-dialogue` 模式与 TTS 的 `dialogue` 模式；免确认策略明确记为跳过审阅，直接合成，不伪造用户确认。子 TTS 显式关闭试听；普通语音模式保留原有确认流程。父运行只归档原文、TXT 和 MP3，子运行独立保存，失败后按检查点恢复。

| 后端 | 提问人 | 回答人 |
|---|---|---|
| volcengine | 大雄（S_NBxS5KRZ1） | 哆啦 A 梦（S_QBxS5KRZ1） |
| edge-tts | 云扬（zh-CN-YunyangNeural） | 晓艺（zh-CN-XiaoyiNeural） |

```yaml
schema_version: 1
tts:
  backend: edge-tts
  voices:
    volcengine:
      questioner: S_NBxS5KRZ1
      answerer: S_QBxS5KRZ1
    edge-tts:
      questioner: zh-CN-YunyangNeural
      answerer: zh-CN-XiaoyiNeural
  question_to_answer_gap_ms: 0
  answer_to_question_gap_ms: 0
```

两个间隔参数分别控制提问→回答、回答→下一次提问，默认 0 ms；只作用于角色切换。Edge-TTS 合成需联网。恢复沿用配置快照与已验证音频，最终输出 clean MP3。

跨 Skill 对话使用 `dialogue-handoff-v2.schema.json`，每个发言保留现有九字段 handoff。完整职责、状态、退出码及验收见 Skill 契约；生成内容及未确定的“地”字语境需 Agent 少量判断，其余处理由 Python 完成；TTS 等待时通过父运行 `resume --input` 转交现有决策模板，不增加逐字稿或试听确认。

## 契约文档生成

`runtime/.venv/Scripts/python.exe skills/create-dialogue-podcast/scripts/render_contracts.py` 使用共享 `utils/scripts/capability_catalog.py` 的文档发布流程：prepared → loading → validating → publishing → verifying → completed；错误进入 paused_error，可通过 `--run-dir` 恢复。Schema、模板、Skill 契约、插件清单和派生总览通过共享文件事务同时发布，失败回滚；输入变化时拒绝复用旧检查点。检查点位于 logs/create-dialogue-podcast/runs/<run-id>/contract-generation/，时间戳由共享 timestamp 工具生成。

`--check` 只读校验，不创建日志或修改文件；差异返回退出码 4，参数错误返回 2，运行时文件错误返回 5。详细播客协议只放在本 Skill 文档中，README 和总说明书仅由共享目录生成简短表格。
