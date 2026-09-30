# 词汇星图

本机单用户词典应用。项目页、词汇星图、字典页、日历页、收藏夹、单词页和占位词页使用同一份总词库；背单词页暂为占位。每项目独立保存词表与熟悉度，收藏夹跨项目共用。当前学龄段只有动态生成的“所有单词（测试）”；正式词表由用户以后提供。

## 启动

在本目录运行 `npm ci`，然后运行：

```powershell
.\run.ps1
```

`run.ps1` 同时启动本地 API 与 Vite 前端并打开浏览器；不要直接打开 `index.html`。前端地址为 `http://127.0.0.1:5185/`，本地 API 为 `http://127.0.0.1:5186/`。Ctrl+C 停止。可用 `-NoBrowser` 只启动服务而不自动打开浏览器。

## 旧词库迁移

首次使用前运行 `runtime/.venv/Scripts/python.exe applications/词汇星图/scripts/service.py migrate`。该命令校验旧 `outputs/英文词典/entries/`，导入 `outputs/词汇星图/dicts/a.jsonl` 至 `z.jsonl`，并按修订号对账 `outputs/词汇星图/entries/` 工作文件：较新的修订同步到另一侧，同修订但内容不同则停止，不删除原文件。旧目录中的历史副本若低于已发布修订，会直接跳过。

## 数据与恢复

权威词条保存在 `outputs/词汇星图/dicts/a.jsonl` 至 `z.jsonl`；项目及其熟悉度分别保存在 `projects/<projectId>/project.json` 与 `learning-state.json`。收藏与界面状态保存在根目录的 `favorites-state.json`、`ui-state.json`；旧根目录 `learning-state.json` 只迁入首次建立的默认项目。正式学龄段清单将保存在 `stages/`，每条引用记录源词条修订号；词条修改后需显式复核阶段引用。词条数据使用现有 `build-word-entry` Schema 1.4，并兼容已发布的 1.3 词条；其余文件的 Schema 在 `utils/references/dictionary-*.schema.json`。`config.yaml` 是界面颜色与图谱引力、斥力、阻尼及理想边长的单一配置来源，改动后重启应用生效。状态更新检查修订号并原子写入；写入失败会显示错误和重试入口。

状态文件与阶段文件的空模板可由 `runtime/.venv/Scripts/python.exe -m utils.scripts.dictionary_contract template --kind stage --stage-id <阶段ID> --label <名称> --output <目标路径>` 生成；`--kind` 也可取 `learning`、`favorites`、`ui`、`project`；项目空模板需指定 `--project-id` 与 `--label`。项目词表经导入接口规范化和校验后填充。阶段词汇及义项引用需在提供正式词表后填写并校验，不从空模板推断词表。

占位词只展示候选关联，不展示猜测释义。点击“新建词条”调用 `build-word-entry` 的单词状态机；同一词已有未完成运行时复用运行 ID。登录、验证码及质量复核暂停时，在页面查看状态并从检查点继续。需要语义审查时，根据 skill 审查包填写结构化 JSON；应用不会跳过审查。

图谱默认只显示当前项目词表中的单词及表内关系，选中单词后才展开其直接同族词和关联词；项目词表中的未建词条显示为待建节点，待核验连接使用虚线。首屏最多显示 500 个词条及占位节点，搜索和聚焦可以访问其余词。英美发音使用有道接口的对应口音；只有接口返回可播放音频时才显示播放按钮。可播放音频缓存到 `outputs/词汇星图/cache/audio/`；音频是播放来源，不作为词条证据。

图谱响应遵循 `utils/references/dictionary-graph-v1.schema.json`。聚焦词条时先展示词族和分组抽取的直接邻居，“显示其他节点”可展开剩余关联；待核验同义／近义候选仅在界面归为“近义词 · 按规则分类”。词条 `familyId` 的确定性合并在 `build-word-entry` 发布状态机中处理。

## 验证

运行 `npm run build`、`runtime/.venv/Scripts/python.exe applications/词汇星图/scripts/smoke.py`、`runtime/.venv/Scripts/python.exe applications/词汇星图/scripts/smoke_ui.py`，以及现有 `runtime/.venv/Scripts/python.exe skills/build-word-entry/scripts/smoke.py`。应用测试使用临时目录，不修改真实学习状态；浏览器测试将页面预览写入 `outputs/词汇星图/preview-word-page.png`。
