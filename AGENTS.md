# Agent 执行规则

来源职责、冲突处理和文档内容边界统一见 [SOURCE_OF_TRUTH.md](SOURCE_OF_TRUTH.md)。

## 简略文件架构

- `skills/`：各项 Skill 的使用说明和实现。
- `applications/`：学习应用及各应用的使用说明。
- `docs/`：项目文档；其中 `PRDs/` 和 `decisions/` 仅供本地使用。
- `utils/`：可复用的脚本和数据格式定义。
- `runtime/`：隔离运行环境；`outputs/`：正式运行结果；`logs/`：日志和运行状态。

详细文件架构、目录用途和说明入口见 [项目文件架构](docs/architecture/README.md)。

## 执行与阅读要求

- 需求和验收不清时阅读本地 `docs/PRDs/`（如存在）；缺失时依据公开 Skill 契约，并向用户澄清仍不明确的需求，不假定本地文档内容；来源冲突时阅读 `SOURCE_OF_TRUTH.md`；行为、安全或提交问题阅读 `CODE_OF_CONDUCT.md`；调用 skill 前阅读 `docs/Skills、应用说明书.md` 和对应 `SKILL.md`。
- 默认使用 `runtime/` 下的隔离环境；运行产物写入 `outputs/`，文本日志和状态机中间文件写入 `logs/<skill>/runs/<run-id>/`，日志不纳入 Git。不得把 Skill 运行产物写入 `runtime/`。
- 项目中所有时间戳统一使用本地时间、不带时区的 `YYYY-MM-DDTHH:MM:SS`；文件名或目录名中的时间戳去掉横杠和冒号，使用 `YYYYMMDDTHHMMSS`。同类对象发生完全相同的时间戳时，依次追加 `_1`、`_2` 等后缀。时间戳必须通过 `utils/scripts/timestamp.py` 生成，不得在业务代码中自行生成其他格式。

## 实现要求

- 所有 Skill 必须采用显式状态机，脚本校验状态迁移并保存检查点；修改流程时同步公开说明与测试。
- 能确定的处理优先固化为 Skill 自有 Python 脚本；跨 Skill 或应用共用的部分放入 `utils/`。Agent 仅做必要的语义判断，不用单次任务的临时脚本代替正式实现。
- 表格、YAML、JSON 和长篇格式化结果优先由脚本生成；复用现有 Schema 与模板，让 Agent 只补少量内容。新增或改变输出格式前与用户讨论 Schema/template。

## 文档职责与维护

- 用户文档使用通俗语言，功能简介直接说明能做什么；必要术语作简短解释，工具名称、命令、参数和路径保持准确。具体内容边界遵守 SOURCE_OF_TRUTH.md。
- `docs/architecture/README.md` 只维护项目文件结构、目录用途和说明入口；修改目录时核对它与简略介绍，迁出有效实现规则后再去重。
- `README.md` 的功能表按 Skills、应用分别展示，分类和顺序与 `docs/Skills、应用说明书.md` 一致；各功能描述为 1～3 句话。
- `docs/Skills、应用说明书.md` 只介绍功能、调用时机、CLI 示例和应用启动方式，附详细文档链接；保留简短统一退出码、时间戳约定，不放技术路线、实现细节、状态机、详细协议或 YAML 场景示例。
- 说明书只允许四个二级标题：统一退出码、时间戳、Skills 分类与调用、应用分类与使用。Skills 下为三级标题“概览、项目功能、通用工具、AI 辅助学习、AI 辅助教学、AI 辅助科研”；AI 辅助学习下为四级标题“背诵与记忆、交互式学习、英语、绘图”。应用下为三级标题“概览、背诵与记忆、交互式学习、英语”；教学、科研保留“暂无”。
- 各 Skill 的配置、实现、状态机、Schema、详细协议和 YAML 场景示例放在各自 `SKILL.md` 或其明确链接的文档；应用专属内容放在各自目录，README 提供入口。迁移内容先核对已有契约并去重，不丢失有效规则或历史兼容说明。
- 调用应用前阅读本说明书及对应应用 README；调用 Skill 前阅读本说明书及对应 SKILL.md。
- 总览简短目录维护在 `utils/references/capability-catalog.json`；用 `runtime/.venv/Scripts/python.exe utils/scripts/capability_catalog.py` 生成 README 和说明书表格，`--check` 只校验，`--run-dir` 恢复生成。README 不写入内部生成注释，生成范围按实际 Markdown 标题和表格结构定位；不要手工维护多份表格；新 schema/template 先与用户讨论。公共目录解析、渲染和检查固化到 `utils/`，Python 优先，Agent 只填写短描述和必要语义判断。

- 已确认的分类归属和成员顺序由共享目录生成器校验，并与各 SKILL.md 的 category 交叉核对；不能通过同时修改目录数据和派生表格绕过分类约束。

## 版本与提交维护

- 按 SOURCE_OF_TRUTH.md 的版本与更新历史职责执行；历史回填只读取 Git 第一父链提交差异，不将当前工作区变化追加到旧版本。
- 提交前先核对实际暂存范围，使用 update-project-version 的 `start` 准备证据和小模板（兼容 project-doc-audit 的 `history-start --mode staged`）；Agent 只填核心更新短句、证据编号及跳过理由，由脚本渲染更新历史并同步 VERSION、README 和 marketplace。
- 生成预览后用 `verify` 核验，展示新版本和核心更新；用户确认后用 `confirm` 绑定当前预览，再用 `publish` 明确写入（旧入口沿用 `history-verify`、`history-publish`）；将生成文件纳入同次提交后运行 project-doc-audit 的 `start --scope staged`、报告验证及敏感信息检查。不另建一次“提交后升版”的提交；未获提交授权时只交付改动。
- 第一次迁移使用 `history-start --mode history` 补齐既有提交映射；历史迁移例外仅覆盖来源说明中的确定边界。生成中间记录放 logs，预览与核验结果放 outputs，均不纳入 Git。
- 版本由提交序号计算，不能按脚本运行次数递增；历史或暂存区指纹变化后旧审查失效。不得绕过生成状态机手写长历史、伪造证据或覆盖预览后新增的修改。
- 待提交预览的 README 和 marketplace 同样依据暂存区；存在未暂存的非版本内容时先确定提交范围。暂存生成文件后核验本次审查与差异绑定，不能仅凭版本标题正确就判定可提交。
