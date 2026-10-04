# build-word-entry 使用与协议补充

入口为 `runtime/.venv/Scripts/python.exe skills/build-word-entry/scripts/cli.py`，支持 `start-word`、`start-list`、`resume`、`status`、`verify` 和 `deliver`。文本词表一行一词；CSV 指定单词列；JSON 接受对象数组或 words 数组，JSONL 每行一个对象，并读取 word 与可选提示字段，去重后保存全部原始行号。完整词条按 lemma 首字母写入 outputs/词汇星图/dicts/a.jsonl 至 z.jsonl；词条状态机及审核直接读写唯一 JSONL 词库；旧 entries 经迁移验证后清理，不再作为长期工作副本。当前不处理学龄段标签。单词与批次各有显式状态机；证据采集使用四站可见浏览器，脚本预填 `decision-template.json`，人工登录或验证码操作暂停；Agent 只校对并补足少量结构化义项判断，必要时按内容项给出证据索引。Cambridge 候选按词性保留英美 IPA 和音频来源 URL。AI 生成释义与例句通过结构检查后可入库，保留 `pending`、`confidence` 和生成方式，展示脚本追加 `（AI 生成，置信度 0.85）`。`gaps.json` 记录来源覆盖与逐字段 `fieldGaps`：确实有候选却未发布时标记待补，证据不足时标记待核验；选择器预览限量不算采集截断。形容词比较级／最高级经来源核对后存于 `inflections[]`，独立派生词关系存于 `derivatives[]`。新版词条的无法判断的同义／近义／反义候选按来源义项保存在 `pendingRelations[]`；旧版词条仍可验证。词族与直接派生词分开确认；已判断的表外关系可先以词面正式关系保存，目标义项核对后再链接。正式批次结果只含输入文件名，不泄漏本地绝对路径；原始网页快照和浏览器会话不保存。正式结果位于 `outputs/build-word-entry/runs/<run-id>/`，状态与中间证据位于 `logs/build-word-entry/runs/<run-id>/`。

空词形和派生词字段的后续审核使用 `family_review.py start --project <id> → resume --run-id <id> --input <decision.json> → verify --run-id <id>`。状态机先生成逐字段候选包及精简决策模板，Agent 只判断候选是否具有直接关系；脚本校验来源、版本和判断后发布。审核结果保存于 `outputs/词汇星图/family-reviews/`，`automatic_passed` 表示审核判断已被脚本应用，`outcome` 分别记录已发布或现有来源无可证实内容；原采集缺口保留为历史记录。

当前 1.3 版还由脚本生成 `relation-review-template.json`：Agent 按候选 ID 完成关系与派生词判断，脚本校验全量覆盖后发布。已确认的表外关系进入正式词条，目标词条未建时使用 `lemma_only`；旧版词条继续可验证。

1.3 新词条的例句、搭配和短语均要求独立中译；例句用纯文本及双语字符区间由 Python 渲染加粗，搭配和短语只显示中译。Cambridge 成对例句与中译优先按页面结构采集；缺失时 `decision-template.json` 预留字段，Agent 在 `usageUpdates` 中补少量译文、置信度和中文区间。旧内容补译使用 `legacyUsageUpdates`，待补清单逐内容 ID 生成；新判断必须绑定本次 `evidenceDigest`。状态机在 `validating_entry` 后进入 `verifying_content`，通过当前页面定位及规则核验的读音和词形标记 `automatic_passed`，否则维持待核验并在 `gaps.json` 的 `verificationGaps` 记录具体原因。旧运行继续使用原版本状态机。

目标词条随后单独建成时，单词状态机生成 `incoming-link-review.json` 并暂停链接复核；`resume --input` 按 `link-decision.schema.json` 接收逐项链接或暂缓决定，脚本核对两端义项证据与修订状态后补入双向链接。

1.4 新词条增加可恢复的内容审查阶段。脚本从候选词条生成 `content-review-template.json`，Agent 按 `content-review-decision.schema.json` 对义项内的释义、用法及译文作分组判断并列出暂缓内容 ID。脚本扩展为逐内容项 `agent_passed` 与 `verificationRef`；暂缓项保持 `pending` 并进入 `contentGaps`。AI 生成内容通过后仍保留生成方式和置信度。旧运行继续按原版本状态机恢复。
