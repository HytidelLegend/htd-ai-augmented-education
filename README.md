# 【Hytidel】AI 辅助教育工具箱

简体中文 | 繁體中文（待更新） | English（待更新）

> 面向学生、教师和学术工作者的中文 AI Skills 工具箱，汇集我探索 AI 辅助学习、教学与科研的方法和工具。

**Version：** [0.1.0](VERSION) · **Skills：** 7 · **License：** [CC BY-NC 4.0](LICENSE)

你可以在国内的软件豆包（豆包工作）、WorkBuddy、千问办公、DeepSeek Harness、Kimi Code、Minimax Code、Z Code，或者国外的软件 Codex、Claude Code、Cursor、Grok Build、Grok Bot、Gemini CLI 等支持 Skills 的 Agent 的桌面端/命令行中使用这个工具箱。

[解决什么问题](#解决什么问题) · [功能表](#功能表) · [安装与更新](#安装与更新) · [快速开始](#快速开始) · [文件架构](#文件架构) · [相关文档](#相关文档) · [作者信息](#作者信息) · [许可证](#许可证)

## 解决什么问题

用 AI 提升学习、教学、科研的效率，让人类从繁琐、机械的重复性劳动中解放出来，进而专注于知识的学习和创造。

这个工具箱将我探索的方法整理为可调用的 Agent Skills，当前已实现的教育场景以辅助学习为主，辅助教学和科研方向仍在建设中。

## 功能表

| 分类 | Skill | 当前功能 |
| --- | --- | --- |
| 项目入口 | `htd-ai-augmented-education` | 根据任务说明已注册 Skills 的选择、调用顺序和示例 |
| AI 辅助学习 | `en-writing-master` | 基于用户创建的本地作文工具进行英语写作、批改和润色 |
| AI 辅助学习 | `render-handwritten-essay-card` | 为英语作文生成答题卡手写照片提示词，并在确认后生成图片 |
| 项目维护 | `project-doc-audit` | 检查文档、Skills 注册、版本和 Python 依赖的差异 |
| 项目维护 | `git-remote-diff` | 比较本地仓库、远端默认分支和工作区 |
| 项目维护 | `sensitive-commit-check` | 在提交或发布前检查敏感信息 |
| 文件处理 | `format-conversion-master` | 执行可恢复的 EPUB 格式转换 |
| AI 辅助教学 | — | 待更新 |
| AI 辅助科研 | — | 待更新 |

各 Skill 的适用时机、调用入口和产物说明见 [Skills 说明书](docs/Skills_说明书.md)。

## 安装与更新

在你的 Agent 中运行：

```go
克隆开源仓库 `HytidelLegend/htd-ai-augmented-education`，并按 `README.md` 的要求配置环境
```

## 快速开始

1. 按照 [runtime/README.md](runtime/README.md) 配置运行环境。
2. 在支持 Skills 的 Agent 中调用项目路由 Skill，直接描述你需要的功能。例如，批改英语作文时输入：

   ```text
   /htd-ai-augmented-education 告诉我批改这篇英语作文需要调用哪些 skills，调用顺序是什么，并给出具体调用示例。
   ```

直接调用 `htd-ai-augmented-education` 并说明你要完成的功能，它会依据项目文档判断哪些已注册 Skills 能完成任务，告诉你调用顺序，并给出具体调用示例。实际批改英语作文前，需要按 [本地作文工具说明](skills/en-writing-master/tools/README.md) 创建或配置有权使用的作文工具；开源仓库不附带具体教材或评分工具。

## 文件架构

```text
.
├── .claude-plugin/       # 插件清单与版本配置
├── docs/                 # PRD、架构、贡献、安全和 Skills 文档
├── runtime/              # 隔离运行环境及配置说明
├── skills/               # 各项 Skill 的契约与实现
├── utils/                # 共享脚本与 Schema
├── outputs/              # 正式运行产物（不纳入 Git）
├── logs/                 # 运行日志和状态（不纳入 Git）
├── AGENTS.md             # Agent 执行规则
├── SOURCE_OF_TRUTH.md    # 权威来源与冲突优先级
├── CODE_OF_CONDUCT.md    # 行为规范
├── LICENSE               # 许可证
└── VERSION               # 项目版本
```

## 相关文档

文档入口及阅读规则详见 [AGENTS.md](AGENTS.md)。

- [SOURCE_OF_TRUTH.md](SOURCE_OF_TRUTH.md)：权威来源与冲突优先级
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)：贡献者和 Agent 的行为边界
- [项目 PRD](docs/PRDs/AI辅助教育项目.md)：目标、范围与验收
- [架构文档](docs/architecture/README.md)：架构文档入口
- [Skills 说明书](docs/Skills_说明书.md)：能力、调用入口与运行约束
- [贡献指南](docs/contributing/贡献指南.md)：贡献要求
- [安全说明](docs/security/安全说明.md)：安全文档入口

## 作者信息

作者：Hytidel（海底桃）

公众号/抖音/小红书/B站/视频号：[Hytidel聊AI](https://www.xiaohongshu.com/user/profile/5eaae4c30000000001000143)、[Hytidel聊商业](https://www.xiaohongshu.com/user/profile/65699378000000003d028a34)

## 许可证

本项目采用 [CC BY-NC 4.0](LICENSE) 许可证。

- 个人使用、学习、研究与非商业项目可以在遵守许可证条款的前提下使用。
- 公开发布衍生作品时，请注明来源，并说明是否作过修改。
- 商业用途需要单独授权。

申请授权请发送邮件至：hytidel333@gmail.com。
