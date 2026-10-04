# 背图工具

在浏览器中遮住图片上的内容，通过隐藏和显示进行回忆练习；图片和项目按目录保存在本机。

## 运行

```bash
npm install
```

推荐使用启动脚本。脚本会检查保存目录和端口，准备最新页面，再启动应用及配套的本地保存服务；不需要单独运行 `npm run build`：

```powershell
.\run.ps1 -Preview
```

默认原生写入服务使用仓库中的 `outputs/背图工具/`。如需指定其他目录，使用 `-WorkspacePath`。

如需单独构建产物：

```bash
npm run build
```

启动应用页面 请使用 `run.ps1 -Preview`，它会同时启动本地写入服务。

首次使用点击“选择项目目录”，选择仓库中的 `outputs/背图工具/`。也可以直接点击“新建项目”或“导入 ZIP”，应用会先请求目录授权；目录连接成功后需要再次点击对应按钮选择图片或 ZIP。Chrome/Edge 可在获得授权后直接读写该目录；不支持目录授权的浏览器不能直接读写目录，但仍可使用 ZIP 导入/导出。

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

## 开发说明

页面使用 React（页面组件工具）、Vite（页面构建工具）和 TypeScript（源码语言），图片练习画布使用 SVG 图形格式；开发依赖见 `package.json`。
