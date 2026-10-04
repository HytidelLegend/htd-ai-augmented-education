# mark-memory-spans 使用与协议补充

两个记忆 Skill 共用 `utils/references/术语表.txt`，每行一个中文或英文术语。`mark-memory-spans` 将命中的完整术语列为候选，并在提交、编辑和验证时拒绝从术语中间切开的区间；是否挖空仍由语义决定。运行词表快照存于对应的 `logs/<skill>/runs/<run-id>/`。

古诗文背诵新增整分句模式：Agent 根据输入语义判断是否为要背诵的古诗文；是则默认按整分句挖空，否则沿用下述记忆要点模式。脚本按逗号、分号、句号、问号、叹号和换行列出候选分句，顿号留在分句内部；Agent 仅挑选需要练习的分句编号，脚本扩展完整区间并保留分隔标点及分句边缘的引号。古诗文豁免普通模式的标记密度及剩余主干要求；`edit`、`verify`、`deliver` 都复核分句边界。输出字段及 `「」`、`____` 不变，Markdown 的古诗文主干检查显示“不适用”。

入口为 `runtime/.venv/Scripts/python.exe skills/mark-memory-spans/scripts/cli.py`，支持 `start`、`resume`、`deliver`、`edit`、`status` 和 `verify`。文本先统一换行，再计算 UTF-8 SHA-256；每个 span 保存该整段文本的哈希。span 使用零基左闭右开区间，不允许重叠但允许相邻；专业名词和固定术语不得拆分。记忆要点遵循“主干优先、最小充分”原则，只标记最关键的核心概念、行动要求、价值判断和固定并列短语，不把几乎每个名词都列为要点。多个独立短 span 同句覆盖率不超过 85% 时可保留，但挖空后必须仍有可读主干；单个 span 默认不超过句子 40%，保留主语、情态词和命名结论的完整核心关系可放宽至 50%。Markdown 使用 `「span」` 标记，连续的独立要点显示为相邻的 `「span1」「span2」`。

`start` 生成候选 span 和 `generation_packet.json` 后进入 `paused_agent_selection`；Agent 只提交选定区间、语义角色和挖空主干判断，脚本负责原文切片、哈希、重叠校验、主干与过度标记检查、标记渲染、每个 span 与纯文本 `____` 的逐项对应校验和 Schema 校验。命名结论需要保留，邻近比较不得直接泄露已挖空答案；若邻近原因、条件或比较句足以唯一推出挖空答案，还须标记最小决定判据并保留因果主干，例如挖空「向上排空气法」时同步挖空“密度比空气「大」”中的「大」。变化关系中的决定因素可独立标记，动作和对象共同构成核心关系时允许完整标记。`verify` 和 `deliver` 会重新计算源文本、span、标记文本、挖空文本和对齐信息，防止产物被修改后仍通过检查。质量检查失败会进入 `paused_quality_review`，可补交 Agent 响应后恢复。`deliver` 读取已验证的 `result.md` 并输出到对话。`edit` 按顺序应用 `add`、`delete` 和 `adjust` 操作；调整为零长度时删除。正式产物位于 `outputs/mark-memory-spans/runs/<run-id>/result.json` 和 `result.md`，日志及状态位于 `logs/mark-memory-spans/runs/<run-id>/`。

错误拆分案例必须保留：`科学立法、严格执法、公正司法、全民守法` 不应拆成“科学、立法、严格、执法、公正、司法、全民、守法”；正确的四个记忆要点是四个完整并列短语。相邻 span 的正例为 `用「分液漏斗」「萃取」……`。政治/思想品德、地理、数学、物理、化学、生物和语文的主干保留、动作与对象边界、传导顺序、联合技术对象、原因判据泄露及过度标记正反样例见 `skills/mark-memory-spans/references/span-examples.md`；该文档按主题分表，每行统一列出原文、正面、挖空、正面原因、负面和负面原因，由 `utils/scripts/render_span_examples.py` 从 `skills/mark-memory-spans/references/` 下的案例 JSON 生成。数学材料的公式右侧表达式整体候选和明显拆断检查由共用脚本处理，条件与结论的语义选点仍由 Agent 判断；语文材料保留作品主题和叙述关系，分别标记可独立考查的作者、篇目、情节、人物和场所；脚本优先提供完整篇名、引语及组合候选，并在上限内轮流覆盖各段，Agent 负责少量语义选择。
