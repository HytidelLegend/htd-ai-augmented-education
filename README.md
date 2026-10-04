# 【Hytidel】AI 辅助教育工具箱

简体中文 | 繁體中文（待更新） | English（待更新）

> 面向学生、教师和学术工作者的中文 AI Skills 与学习应用工具箱，汇集我探索 AI 辅助学习、教学与科研的方法和工具。

**Version：** [0.1.11](VERSION) · **Skills：** 18 · **License：** [CC BY-NC 4.0](LICENSE)

你可以在国内的软件豆包（豆包工作）、WorkBuddy、千问办公、DeepSeek Harness、Kimi Code、Minimax Code、Z Code，或者国外的软件 Codex、Claude Code、Cursor、Grok Build、Grok Bot、Gemini CLI 等支持 Skills 的 Agent 的桌面端/命令行中使用这个工具箱。

[解决什么问题](#解决什么问题) · [功能表](#功能表) · [安装与更新](#安装与更新) · [快速开始](#快速开始) · [文件架构](#文件架构) · [相关文档](#相关文档) · [作者信息](#作者信息) · [许可证](#许可证)

## 解决什么问题

用 AI 提升学习、教学、科研的效率，让人类从繁琐、机械的重复性劳动中解放出来，进而专注于知识的学习和创造。

这个工具箱将我探索的方法整理为可调用的 Agent Skills 和即开即用的学习应用，当前已实现的教育场景以辅助学习为主，辅助教学和科研方向仍在建设中。

## 功能表

### Skills

| 分类 | Skill | 当前功能 |
| --- | --- | --- |
| 项目功能 | [`htd-ai-augmented-education`](skills/htd-ai-augmented-education/SKILL.md) | 回答项目信息，并推荐能够完成任务的 Skills |
| 项目功能 | [`project-doc-audit`](skills/project-doc-audit/SKILL.md) | 检查项目文档、文件结构、工具清单、版本和运行环境 |
| 项目功能 | [`update-project-version`](skills/update-project-version/SKILL.md) | 读取暂存差异和未被 Git 忽略的新文件并总结更新，用户确认后同步版本号与更新历史 |
| 项目功能 | [`sensitive-commit-check`](skills/sensitive-commit-check/SKILL.md) | 检查提交范围中的敏感信息 |
| 项目功能 | [`git-remote-diff`](skills/git-remote-diff/SKILL.md) | 比较本地仓库、远端默认分支和工作区 |
| 通用工具 | [`convert-copy-to-transcript`](skills/convert-copy-to-transcript/SKILL.md) | 文案转逐字稿 |
| 通用工具 | [`create-dialogue-podcast`](skills/create-dialogue-podcast/SKILL.md) | 书面内容转两人一问一答播客 |
| 通用工具 | [`format-conversion-master`](skills/format-conversion-master/SKILL.md) | 格式转换 |
| 通用工具 | [`run-speech-to-text`](skills/run-speech-to-text/SKILL.md) | 语音转文字 |
| 通用工具 | [`run-text-to-speech`](skills/run-text-to-speech/SKILL.md) | 文字转语音 |
| AI 辅助学习 / 背诵与记忆 | [`build-mnemonic-keywords`](skills/build-mnemonic-keywords/SKILL.md) | 将一句背诵内容转为关键词、联想场景和生图提示词 |
| AI 辅助学习 / 背诵与记忆 | [`mark-memory-spans`](skills/mark-memory-spans/SKILL.md) | 提取并标记文本记忆要点，支持挖空练习和要点编辑 |
| AI 辅助学习 / 背诵与记忆 | [`schedule-ebbinghaus-plan`](skills/schedule-ebbinghaus-plan/SKILL.md) | 规划符合艾宾浩斯遗忘曲线的学习和复习 |
| AI 辅助学习 / 交互式学习 | [`build-curriculum-navigation`](skills/build-curriculum-navigation/SKILL.md) | 整理学习资料和知识点之间的先后关系，规划学习顺序 |
| AI 辅助学习 / 交互式学习 | [`interactive-tutor`](skills/interactive-tutor/SKILL.md) | 依据学习导航开展课程讲解、批改和答疑，维护学习进度 |
| AI 辅助学习 / 英语 | [`build-word-entry`](skills/build-word-entry/SKILL.md) | 生成英语词条 |
| AI 辅助学习 / 英语 | [`en-writing-master`](skills/en-writing-master/SKILL.md) | 创建作文工具，并按已有工具执行英语写作、批改和润色 |
| AI 辅助学习 / 绘图 | [`render-handwritten-essay-card`](skills/render-handwritten-essay-card/SKILL.md) | 将英语作文生成通用英语考试答题卡手写印刷体照片提示词 |
| AI 辅助教学 | — | 暂无 |
| AI 辅助科研 | — | 暂无 |

### 应用

| 分类 | 应用 | 当前功能 |
| --- | --- | --- |
| 背诵与记忆 | [背书工具](applications/背书工具/README.md) | 标记文本中的记忆要点，通过挖空练习进行背诵 |
| 背诵与记忆 | [背图工具](applications/背图工具/README.md) | 遮住图片中的部分内容，通过隐藏和显示进行回忆练习 |
| 交互式学习 | [交互式学习](applications/交互式学习/README.md) | 查看学习项目、课程图和知识点图，管理项目与学习设置 |
| 英语 | [词汇星图](applications/词汇星图/README.md) | 浏览词条和词汇关系图，管理词表、收藏与背诵计划 |

各 Skill 的适用时机、调用入口和应用启动方式见 [Skills、应用说明书](docs/Skills、应用说明书.md)。

## 安装与更新

在你的 Agent 中运行：

```text
克隆开源仓库 `HytidelLegend/htd-ai-augmented-education`，并按 `README.md` 的要求配置环境
```

## 快速开始

1. 按照 [runtime/README.md](runtime/README.md) 配置运行环境。
2. 在支持 Skills 的 Agent 中调用项目入口 Skill，直接描述你需要的功能。例如，批改英语作文时输入：

   ```text
   /htd-ai-augmented-education 告诉我批改这篇英语作文需要调用哪些 skills，调用顺序是什么，并给出具体调用示例。
   ```

直接调用 `htd-ai-augmented-education` 并说明你要完成的功能，它会依据项目文档判断项目中哪些 Skills 能完成任务，告诉你调用顺序，并给出具体调用示例。

## 文件架构

```text
.
├── .claude-plugin/       # 插件清单与版本配置
├── applications/       # 即开即用的 AI 辅助学习小工具
├── docs/                 # 文件结构、贡献、安全和使用说明
├── runtime/              # 隔离运行环境及配置说明
├── skills/               # 各项 Skill 的使用说明和实现
├── utils/                # 共享脚本和数据格式定义
├── outputs/              # 正式运行产物（不纳入 Git）
├── logs/                 # 运行日志和状态（不纳入 Git）
├── AGENTS.md             # Agent 执行规则
├── SOURCE_OF_TRUTH.md    # 权威来源与冲突优先级
├── CODE_OF_CONDUCT.md    # 行为规范
├── LICENSE               # 许可证
└── VERSION               # 项目版本
```

详细目录用途与说明入口见 [项目文件架构](docs/architecture/README.md)。

## 相关文档

* 文档入口及阅读规则详见 [AGENTS.md](AGENTS.md)。

- [SOURCE_OF_TRUTH.md](SOURCE_OF_TRUTH.md)：权威来源与冲突优先级
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)：贡献者和 Agent 的行为边界
- 本地项目 PRD：`docs/PRDs/AI辅助教育项目.md`（如存在，不同步到远端仓库）
- [架构文档](docs/architecture/README.md)：项目文件与目录说明
- [Skills、应用说明书](docs/Skills、应用说明书.md)：Skills 与应用的功能、调用及使用入口
- [贡献指南](docs/contributing/贡献指南.md)：贡献要求
- [更新历史](docs/更新历史.md)：每次提交的核心更新，按版本倒序展示。
- [安全说明](docs/security/安全说明.md)：安全文档入口
- [AI 辅助学习应用](applications/README.md)：即开即用小工具目录及添加约定

## 作者信息

作者：Hytidel（海底桃）

公众号/抖音/小红书/B站/视频号：[Hytidel聊AI](https://www.xiaohongshu.com/user/profile/5eaae4c30000000001000143)、[Hytidel聊商业](https://www.xiaohongshu.com/user/profile/65699378000000003d028a34)

## 许可证

本项目采用 [CC BY-NC 4.0](LICENSE) 许可证。

- 个人使用、学习、研究与非商业项目可以在遵守许可证条款的前提下使用。
- 公开发布衍生作品时，请注明来源，并说明是否作过修改。
- 商业用途需要单独授权。

申请授权请发送邮件至：hytidel333@gmail.com。
