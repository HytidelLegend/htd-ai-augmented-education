---
name: mark-memory-spans
category: ai_assisted_learning
description: 从纯文本中提取可用于填空题的语义记忆要点，保存为不重叠、可相邻的文本 span，并用「」输出标记版本；支持新增、删除和调整 span。当用户要求标记背诵重点、提取记忆要点、制作挖空文本或修改已有记忆 span 时使用。
---

# 记忆要点 Span 标记

输入一段纯文本。Agent 判断语义记忆要点，脚本保存 JSON、生成用「」标记的 Markdown，并支持后续编辑。
Agent 在提交 span 前阅读 `references/span-examples.md` 的正反案例；脚本负责可确定的区间、挖空和主干校验，学科语义边界由 Agent 判断。

Agent 先根据输入语义判断：要背诵的古诗文默认使用 `classical_recitation`，其他内容使用 `key_points`。脚本按逗号、分号、句号、问号、叹号和换行列出古诗文分句；顿号留在分句内部。Agent 只选择需要练习的分句编号，脚本将编号扩展为整个分句正文的 span，分隔标点和分句边缘的引号留在原位。古诗文模式豁免下述普通模式的单句密度和剩余主干要求，但不允许只挖空分句中的个别字词。

## 规则

- span 使用零基、左闭右开区间；必须对应规范化源文本中的连续字符。
- span 不允许重叠，但允许相邻。
- 专业名词、固定术语和不可拆分短语不得拆开。
- `utils/references/术语表.txt` 是两个记忆 Skill 共用的术语表，每行一个中文或英文术语。普通模式中，脚本先给出完整术语候选，再校验 Agent 选择、`edit` 与 `verify` 的区间边界；术语是否作为考点仍按语义判断。运行时的词表快照保存在本次 `logs/mark-memory-spans/runs/<run-id>/`，恢复和验证使用快照。
- 连续文本可以包含多个相邻记忆要点，例如 `用「分液漏斗」「萃取」……`。
- 先保留句子的主语、谓语、判断关系和逻辑连接，再选择记忆要点；不要把几乎每个名词都标记。
- 只选择最关键、最容易考查、能区分答案的核心概念、行动要求、价值判断和固定并列短语。
- 已能表达完整意思的句子主干、解释性补充、背景范围和普通名词默认不标记。
- 每个 span 应是最小完整语义单元；不要因为语法连续就把后续说明词一并吸收，例如标记“创新驱动发展”时不自动扩展为“创新驱动发展战略”。
- 一个长句可以拆成多个独立记忆要点，例如分别标记“科技创新”和“经济社会发展”，不必把整段说明合并成一个 span。
- 用户明确修订的 span 边界优先于 Agent 的泛化判断，脚本不得自动扩展用户删除的词语。
- 记忆要点挖空后，剩余文本必须仍能分辨句子主干；Agent 必须提交通过判断和理由，脚本还会执行密度和标题检查。
- “这就是……”等命名结论承担判断句的落点时，保留结论名称，例如“这就是牛顿第一定律”；“这种现象叫作……”同理。不能仅因名称是专业术语便将它挖空。定义主体足够明确时，名称仍可独立考查，例如“物体保持原来运动状态不变的性质叫作「惯性」”。
- 已选 span 的答案若在紧接的比较或解释中直接出现，要检查是否需要连同后续完整要点一起标记，避免提前泄露答案；不能机械地把全文所有重复术语都标记。
- 已选方法、方向或结论若能由邻近的原因、条件或比较句唯一推出，也属于答案泄露；只追加标记直接决定该答案的最小判据，保留因果、比较关系和其他不会单独给出答案的解释。例如标记「向上排空气法」时，后文“密度比空气大”中的「大」也要标记。
- 在“某量随某因素变化”的关系中，若变化因素是可独立考查的决定对象，应单独标记该因素，同时保留“随……增大而增大”等变化判断主干；例如“浮力随「排开液体体积」的增大而增大”。
- 多个彼此独立的短 span 可以在同一句中占较高比例，但覆盖率超过 85% 或挖空后失去主干时仍必须拒绝；单个 span 默认不超过句子 40%。若完整核心关系稍长，但明确保留了主语、情态词和命名结论，可放宽至 50%，仍须逐项判断主干是否可读。
- 每个 `「span」` 必须在挖空结果中对应一个 `____` 占位符；挖空文本必须由脚本依据已验证 spans 重新生成。
- 句子的谓语、时间关系、因果关系和转折连接优先保留为挖空后的主干；当“动作 + 对象”可以拆分时，优先保留动作、标记对象。
- 并列宾语或并列概念应分别标记；固定制度名称、历史对象和专业术语整体标记，不拆成普通词语。
- 并列属性在各自都能独立作答时分别标记，例如“偏南风”“温暖湿润”“偏北风”“寒冷干燥”；季节、因果和判断关系保留在主干中。
- 动作与对象可以拆分时，动作谓语保留在主干中、对象单独标记，例如保留“开展”并标记“水土保持”；不要把“开展水土保持”整体挖空。
- 若动作与对象共同构成需背诵的核心关系，不能强行拆开；例如“磁场总要「阻碍引起感应电流的磁通量变化」”，保留“磁场总要”和“这就是楞次定律”作为主干。
- 多个并列措施或步骤如果能分别考查，应分别标记；不要把整串措施合并成一个 span。
- 两个名词共同构成一个不可替代的技术对象时，应整体标记，例如“地表覆被和下垫面性质”；不得只按连接词机械拆分。
- 描述生物过程时，场所、能量来源、参与物和产物可分别作为记忆要点；保留“利用”“合成”“释放”等动作及输入到产出的关系，不把整段转化过程合为一个 span。
- 描述反射弧等有顺序的传导路径时，各环节可分别标记；保留“接受”“经”“到达”“传到”等连接与动作，使挖空后仍能辨认先后顺序。
- 复制等机制中的固定原则和专业术语应作为完整语义单元，不拆开“碱基互补配对原则”等名称；“这种方式称为……”后的命名结论仍按主干规则保留。
- 同一术语再次出现时，按它在各句中的语义作用分别判断，不机械要求全部挖空；承担结果说明或句子主干的再次出现可以保留。
- “的统治”“的影响”“的作用”等连接性成分默认保留；结果或影响短语只有在本身是独立考点时才标记。
- “因此”“因而”“决定了”等因果连接和必要解释默认保留，挖空后仍须能还原因果主干。
- 脚本必须校验每个 `「span」` 与一个且仅一个按顺序生成的 `____` 占位符对应。
- 源文本先统一换行，再计算 UTF-8 SHA-256。文本变化后，原 span 必须视为需要重新校验。
- 本节的主干保留、密度和语义要点边界规则适用于 `key_points`；古诗文整分句边界规则优先。

### 数学材料的选点

- 公式等号右侧含运算或乘方的表达式应作为完整候选要点；选中公式时不要只挖空其中一项或留下决定答案的运算部分。保留等号、公式名称和变量所指对象，使挖空后仍知道需要回忆哪条关系。
- 位置、数量、比例等数学关系若可分别考查，就分别标记最小结论，保留比较对象和“关系是”“等于”等判断主干。
- 大于零、等于零、小于零等互斥条件分别对应的结论应单独标记；保留每个条件及其与结论的对应顺序，不把整条条件判断一起挖空。
- 解题步骤中可独立考查的关键对象可以标记；“先找到……再检查……”等动作和先后关系保留。公式是否是考点、条件与结论的语义边界仍由 Agent 判断，脚本只提出完整候选并拒绝明显拆断公式的 span。

### 文学常识与名著的选点

- 作品名作为句子主题时通常保留，用它定位正在复习的作品；优先从其后选择可独立回忆的作者、原名、篇目、典型情节、重要人物和场所。若标题或邻近原文已经直接给出某个答案，不再机械地挖空同一名称。
- 保留“是……的作品”“原名……”“经历了……”“以……为背景”“围绕……展开”“既是……也是……”等叙述与判断关系；只标记关系中的最小事实单元，不把作者和文体、背景对象和“为背景”等一并吸收。
- 可分别作答的并列人物、篇目分别标记；作为整体考查的固定组合可合成一个 span，例如四大家族的姓氏顺序。不要仅因顿号或“和”就机械合并或拆分。
- 篇名和引语内部的记忆内容可以标记，外围的《》或引号保留；说明性内容和句末判断默认保留，除非它本身是明确的独立考点。
- 脚本优先提供完整篇名、短引语和枚举组合等候选区间，并在总量上限内轮流从各段取候选，避免前段耗尽预算；候选不是最终答案。Agent 仅判断哪些事实值得挖空及是否保留可读主干，区间、密度、渲染和对齐仍由脚本校验。

## CLI

```powershell
runtime/.venv/Scripts/python.exe skills/mark-memory-spans/scripts/cli.py start --root . --input request.json
runtime/.venv/Scripts/python.exe skills/mark-memory-spans/scripts/cli.py resume --root . --run-id <run-id> --input agent-response.json
runtime/.venv/Scripts/python.exe skills/mark-memory-spans/scripts/cli.py edit --root . --input edit-request.json
runtime/.venv/Scripts/python.exe skills/mark-memory-spans/scripts/cli.py deliver --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/mark-memory-spans/scripts/cli.py status --root . --run-id <run-id>
runtime/.venv/Scripts/python.exe skills/mark-memory-spans/scripts/cli.py verify --root . --run-id <run-id>
```

`start` 和 `resume` 使用可恢复状态机；`edit` 按当前 span 集合顺序应用新增、删除和调整操作。编辑古诗文时须在编辑请求中传入 `mode: "classical_recitation"`，脚本会拒绝半句区间。调整后区间长度为 0 时自动删除。`deliver` 读取已验证的 `result.md` 并将人类可读版本输出到对话。输出沿用现有 JSON 字段、`「」` 和 `____`；古诗文的“主干保留”在 Markdown 中显示“不适用”。

## 状态机

`prepared → validating_request → normalizing_text → extracting_candidates → building_generation_packet → paused_agent_selection → validating_agent_response → validating_mode → [validating_clause_selection] → validating_cloze_quality → validating_cloze_alignment → rendering_outputs → verifying_outputs → completed`。方括号阶段仅用于古诗文。普通模式继续检查标题误标、单句密度和剩余主干；古诗文模式检查完整分句区间。`validating_cloze_alignment` 仍逐项检查标记 span 与纯文本 `____` 占位符；检查失败进入 `paused_quality_review`，补交 Agent 响应后可恢复到 `validating_agent_response`。

输入、Agent 响应、挖空质量或源文本哈希不一致时进入对应暂停或错误路径。日志写入 `logs/mark-memory-spans/runs/<run-id>/`，产物写入 `outputs/mark-memory-spans/runs/<run-id>/`。

## 资源

- `references/request.schema.json`：提取请求。
- `references/agent-response.schema.json`：Agent 判断模式；普通模式提交 span 与挖空判断，古诗文模式只提交选中的分句编号。
- `references/edit-request.schema.json`：编辑请求和源文本哈希。
- `references/output.schema.json`：正式结果。
- `references/span-examples.md`：按主题分表的正反案例，每行包含学段、主题、原文、正面、挖空、正面原因、负面和负面原因。
- `references/general-span-examples.json`：古诗文、相邻 span、固定短语、政治和历史案例的结构化源数据。
- `references/geography-span-examples.json`：地理正面/负面案例的结构化源数据。
- `references/physics-span-examples.json`：物理正面/负面案例的结构化源数据。
- `references/math-span-examples.json`：数学公式、条件结论和解题步骤的正面/负面案例源数据。
- `references/chemistry-span-examples.json`：化学正面/负面案例的结构化源数据。
- `references/biology-span-examples.json`：生物正面/负面案例的结构化源数据。
- `references/literature-span-examples.json`：文学常识与名著正面/负面案例的结构化源数据。
- `utils/references/cloze-inference-rules.json`：可确定的“答案—原因判据”映射，由共用挖空质量脚本检查。
- `utils/references/术语表.txt`：每行一个中文或英文术语，原文命中时作为不可从中间切开的完整单元。

参考案例 Markdown 由共享 Python 脚本根据结构化数据生成。`masked` 不需要在案例 JSON 中重复保存，由脚本从正面 span 推导；古诗文案例使用 `mode: "classical_recitation"` 校验整分句边界。追加案例时在对应 JSON 的 `examples` 中增加一项，再运行以下命令重建全部表格：

```powershell
runtime/.venv/Scripts/python.exe utils/scripts/render_span_examples.py `
  --source skills/mark-memory-spans/references/general-span-examples.json `
  --source skills/mark-memory-spans/references/geography-span-examples.json `
  --source skills/mark-memory-spans/references/physics-span-examples.json `
  --source skills/mark-memory-spans/references/chemistry-span-examples.json `
  --source skills/mark-memory-spans/references/biology-span-examples.json `
  --source skills/mark-memory-spans/references/literature-span-examples.json `
  --source skills/mark-memory-spans/references/math-span-examples.json `
  --target skills/mark-memory-spans/references/span-examples.md
```
