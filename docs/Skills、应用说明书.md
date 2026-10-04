# Skills、应用说明书

本说明书介绍 Skills 和应用的功能与调用方式。详细配置和技术说明见各自文档。

所有 Skill 都可以通过命令行调用：

```text
python skills/<skill-name>/scripts/cli.py <command> ...
```

## 统一退出码

| 退出码 | 含义 |
|---:|---|
| 0 | 命令成功完成 |
| 2 | 参数、路径或输入内容格式不正确 |
| 3 | 需要用户决策、前置条件不足或流程已暂停 |
| 4 | `verify` 检查发现结果文件、运行记录或引用不一致 |
| 5 | 可重试的运行时错误或外部命令失败 |
| 6 | 依赖、运行环境或许可证预检失败 |

## 时间戳

所有 Skill 的时间字段使用本地时间、不带时区的 `YYYY-MM-DDTHH:MM:SS`；文件名和运行目录中的时间部分使用 `YYYYMMDDTHHMMSS`，同类对象冲突时追加 `_1`、`_2` 等后缀。

## Skills 分类与调用

### 概览

- [项目功能](#项目功能)：`htd-ai-augmented-education`、`project-doc-audit`、`update-project-version`、`sensitive-commit-check`、`git-remote-diff`。
- [通用工具](#通用工具)：`convert-copy-to-transcript`、`create-dialogue-podcast`、`format-conversion-master`、`run-speech-to-text`、`run-text-to-speech`。
- [AI 辅助学习](#ai-辅助学习)：`build-mnemonic-keywords`、`mark-memory-spans`、`schedule-ebbinghaus-plan`、`build-curriculum-navigation`、`interactive-tutor`、`build-word-entry`、`en-writing-master`、`render-handwritten-essay-card`。
- [AI 辅助教学](#ai-辅助教学)：暂无。
- [AI 辅助科研](#ai-辅助科研)：暂无。

### 项目功能

| Skill | 功能 | 调用时机 | CLI 示例 |
| --- | --- | --- | --- |
| [`htd-ai-augmented-education`](../skills/htd-ai-augmented-education/SKILL.md) | 回答项目信息，并推荐能够完成任务的 Skills | 询问本项目，或需要规范任务、确定 Skill 及调用顺序时 | `python skills/htd-ai-augmented-education/scripts/cli.py start --root . --input request.json` |
| [`project-doc-audit`](../skills/project-doc-audit/SKILL.md) | 检查项目文档、文件结构、工具清单、版本和运行环境 | 需要审计项目文档、检查说明是否过期或核对项目状态时 | `runtime/.venv/Scripts/python.exe skills/project-doc-audit/scripts/cli.py start --root .` |
| [`update-project-version`](../skills/update-project-version/SKILL.md) | 读取暂存差异和未被 Git 忽略的新文件并总结更新，用户确认后同步版本号与更新历史 | 准备本项目新版本或同步版本文件时 | `runtime/.venv/Scripts/python.exe skills/update-project-version/scripts/cli.py start --root .` |
| [`sensitive-commit-check`](../skills/sensitive-commit-check/SKILL.md) | 检查提交范围中的敏感信息 | 提交、推送或发布前 | `runtime/.venv/Scripts/python.exe skills/sensitive-commit-check/scripts/cli.py start --scope staged` |
| [`git-remote-diff`](../skills/git-remote-diff/SKILL.md) | 比较本地仓库、远端默认分支和工作区 | 需要检查 Git 同步状态时 | `python skills/git-remote-diff/scripts/cli.py start --root .` |

### 通用工具

| Skill | 功能 | 调用时机 | CLI 示例 |
| --- | --- | --- | --- |
| [`convert-copy-to-transcript`](../skills/convert-copy-to-transcript/SKILL.md) | 文案转逐字稿 | 用户要求把文案转换为口播稿、逐字稿或 TTS 输入时 | `runtime/.venv/Scripts/python.exe skills/convert-copy-to-transcript/scripts/cli.py start --root . --input-file copy.txt` |
| [`create-dialogue-podcast`](../skills/create-dialogue-podcast/SKILL.md) | 书面内容转两人一问一答播客 | 文章或课程讲义转双音色音频时 | `runtime/.venv/Scripts/python.exe skills/create-dialogue-podcast/scripts/cli.py start --input-file article.txt` |
| [`format-conversion-master`](../skills/format-conversion-master/SKILL.md) | 格式转换 | 用户要求转换文件格式时 | `python skills/format-conversion-master/scripts/cli.py start --input input.epub --to pdf` |
| [`run-speech-to-text`](../skills/run-speech-to-text/SKILL.md) | 语音转文字 | 用户要求语音转文字、提取时间戳或批量生成逐字稿时 | `runtime/.venv/Scripts/python.exe skills/run-speech-to-text/scripts/cli.py start --root . --input audio.mp3` |
| [`run-text-to-speech`](../skills/run-text-to-speech/SKILL.md) | 文字转语音 | 用户要求文字转语音、生成朗读音频、试听或修订已有合成语音时 | `runtime/.venv/Scripts/python.exe skills/run-text-to-speech/scripts/cli.py start --input-file transcript.txt` |

### AI 辅助学习

#### 背诵与记忆

| Skill | 功能 | 调用时机 | CLI 示例 |
| --- | --- | --- | --- |
| [`build-mnemonic-keywords`](../skills/build-mnemonic-keywords/SKILL.md) | 将一句背诵内容转为关键词、联想场景和生图提示词 | 用户要求联想记忆、记忆口诀、场景化背诵或将记忆法生成图片时 | `runtime/.venv/Scripts/python.exe skills/build-mnemonic-keywords/scripts/cli.py start --root . --input request.json` |
| [`mark-memory-spans`](../skills/mark-memory-spans/SKILL.md) | 提取并标记文本记忆要点，支持挖空练习和要点编辑 | 用户要求标记背诵重点、提取记忆要点、生成挖空文本或修改已有记忆要点 时 | `runtime/.venv/Scripts/python.exe skills/mark-memory-spans/scripts/cli.py start --root . --input request.json` |
| [`schedule-ebbinghaus-plan`](../skills/schedule-ebbinghaus-plan/SKILL.md) | 规划符合艾宾浩斯遗忘曲线的学习和复习 | 用户指定 n，以及每天 item 数或首遍天数，并要求按艾宾浩斯规则生成逐日背诵计划时 | `runtime/.venv/Scripts/python.exe skills/schedule-ebbinghaus-plan/scripts/cli.py start --root . --input request.json` |

#### 交互式学习

| Skill | 功能 | 调用时机 | CLI 示例 |
| --- | --- | --- | --- |
| [`build-curriculum-navigation`](../skills/build-curriculum-navigation/SKILL.md) | 整理学习资料和知识点之间的先后关系，规划学习顺序 | 用户要建立课程索引、安排跨资料学习顺序或插入新资料时 | `python skills/build-curriculum-navigation/scripts/cli.py prepare --root . --request request.json` |
| [`interactive-tutor`](../skills/interactive-tutor/SKILL.md) | 依据学习导航开展课程讲解、批改和答疑，维护学习进度 | 用户要开始或继续交互式学习、作答或获取学习报告时 | `python skills/interactive-tutor/scripts/cli.py start --root . --request request.json` |

#### 英语

| Skill | 功能 | 调用时机 | CLI 示例 |
| --- | --- | --- | --- |
| [`build-word-entry`](../skills/build-word-entry/SKILL.md) | 生成英语词条 | 用户要建立词条、批量处理词表或恢复运行时 | `python skills/build-word-entry/scripts/cli.py start-word --root . --word bank` |
| [`en-writing-master`](../skills/en-writing-master/SKILL.md) | 创建作文工具，并按已有工具执行英语写作、批改和润色 | 用户提供写作规则、写作题目、作文或润色请求时 | `python skills/en-writing-master/scripts/cli.py list-tools --root .` |

#### 绘图

| Skill | 功能 | 调用时机 | CLI 示例 |
| --- | --- | --- | --- |
| [`render-handwritten-essay-card`](../skills/render-handwritten-essay-card/SKILL.md) | 将英语作文生成通用英语考试答题卡手写印刷体照片提示词 | 用户提供英语作文并要求生成答题卡照片时 | `python skills/render-handwritten-essay-card/scripts/cli.py start --root . --input request.json` |

### AI 辅助教学

暂无

### AI 辅助科研

暂无

## 应用分类与使用

### 概览

- [背诵与记忆](#背诵与记忆-1)：`背书工具`、`背图工具`。
- [交互式学习](#交互式学习-1)：`交互式学习`。
- [英语](#英语-1)：`词汇星图`。

### 背诵与记忆

| 应用 | 功能 | 启动方式 | 文档 |
| --- | --- | --- | --- |
| 背书工具 | 标记文本中的记忆要点，通过挖空练习进行背诵 | `在 applications/背书工具/ 中运行 ./run.ps1（首次启动先按应用说明安装依赖）` | [使用说明](../applications/背书工具/README.md) |
| 背图工具 | 遮住图片中的部分内容，通过隐藏和显示进行回忆练习 | `在 applications/背图工具/ 中运行 ./run.ps1 -Preview（首次启动先按应用说明安装依赖）` | [使用说明](../applications/背图工具/README.md) |

### 交互式学习

| 应用 | 功能 | 启动方式 | 文档 |
| --- | --- | --- | --- |
| 交互式学习 | 查看学习项目、课程图和知识点图，管理项目与学习设置 | `在 applications/交互式学习/ 中运行 ./run.ps1（首次启动先按应用说明安装依赖）` | [使用说明](../applications/交互式学习/README.md) |

### 英语

| 应用 | 功能 | 启动方式 | 文档 |
| --- | --- | --- | --- |
| 词汇星图 | 浏览词条和词汇关系图，管理词表、收藏与背诵计划 | `在 applications/词汇星图/ 中运行 ./run.ps1（首次启动先按应用说明安装依赖）` | [使用说明](../applications/词汇星图/README.md) |
