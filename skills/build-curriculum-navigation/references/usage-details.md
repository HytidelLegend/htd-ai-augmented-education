# build-curriculum-navigation 使用与协议补充

所有导师调用前必须先调用本 Skill：有效导航用 `verify`，新资料或目标变化用构建/增量流程。背景不足在对话中讨论，不生成询问背景的课程。导航图是知识点图。脚本采用目标优先自适应探测，默认最多 5 道诊断选择题（四个实质选项＋末尾不确定选项），未测区域保持未知；配置位于 `skills/build-curriculum-navigation/config.yaml`。

脚本生成最多 10 条候选序列，保留全部实际生成候选及历史，按权重计算分项和总分并非升序排列。默认权重为目标 40%、背景 30%、难度 15%、主题 10%、解锁 5%。参数冻结在日志运行目录，每次继续比较、更新并提示变化。

状态为 `prepared → point_division_required（新 v3 请求）→ source_navigation_fragment_required → all_source_navigation_fragments_ready → ordering_decision_required → awaiting_approval → completed`；诊断子状态为 `question_required → awaiting_answer → question_required/completed`，环或错误进入相应暂停。Agent 只填写小补丁、出题及必要语义判断，Python 扩充 JSON 和 Markdown。正式导航位于 `outputs/build-curriculum-navigation/runs/<run-id>/`，日志位于对应 `logs/`。完整契约见 `skills/build-curriculum-navigation/SKILL.md`。
