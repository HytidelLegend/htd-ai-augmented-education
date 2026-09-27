# 背图工具

离线优先的 Web 背图练习编辑器。页面以 React + Vite + TypeScript 构建，画布使用 SVG，项目资源按目录保存。

## 运行

```bash
npm install
```

推荐使用启动脚本。脚本会以状态机启动与当前 GUI 匹配的本地原生写入服务，校验工作区和端口，执行最新生产构建，再启动 GUI；不需要单独运行 `npm run build`：

```powershell
.\run.ps1 -Preview
```

默认原生写入服务使用仓库中的 `outputs/背图工具/`。如需指定其他目录，使用 `-WorkspacePath`。

如需单独构建产物：

```bash
npm run build
```

启动生产 GUI 请使用 `run.ps1 -Preview`，它会同时启动本地写入服务。

首次使用点击“选择项目目录”，选择仓库中的 `outputs/背图工具/`。也可以直接点击“新建项目”或“导入 ZIP”，应用会先请求目录授权；目录连接成功后需要再次点击对应按钮选择图片或 ZIP。Chrome/Edge 会通过 File System Access API 写入该目录；不支持该 API 的浏览器不能执行目录读写，但仍可使用 ZIP 导入/导出。

## 功能

- PNG、JPEG、WebP 图片导入；画布尺寸读取原图像素尺寸。
- 编辑模式下矩形移动、四角缩放、旋转、颜色、描边、文本、字号和模式编辑。
- 练习模式下普通两态和提示三态点击循环。
- Alt+滚轮缩放、Ctrl+S 保存、Ctrl+Z / Ctrl+Y 撤销重做。
- 编辑中的未保存状态自动写入工作区临时草稿；下次启动时可选择恢复。
- 项目目录包含 `project.json`、`assets/original.*` 和 `assets/thumbnail.png`。
- `project.json` 保存 `revision` 和 `lastWriterId`；浏览器发送当前基准修订号，原生服务在同一事务中校验并递增修订号。旧页面或旧构建覆盖新保存时会报冲突而不会静默清空矩形；界面会停止重复提交，并提供保留草稿或重新打开。
- 启动状态和服务日志写入 `logs/背图工具/runs/<run-id>/`；端口已占用、服务工作区不匹配或服务提前退出时，启动脚本直接失败，不复用未知实例。
- 支持 ZIP 导入导出、项目重命名、二次确认删除和打开目录。

诊断记录写入工作区的 `.__beitu_diagnostics__/runs/<run-id>/events.jsonl`，可用脚本复盘保存后切换是否读到空矩形：

```bash
python utils/scripts/beitu_diagnostics.py analyze --workspace outputs/背图工具
```

## 目录约定

```text
outputs/背图工具/<projectId>/
├── project.json
└── assets/
    ├── original.png|jpg|webp
    └── thumbnail.png
```
