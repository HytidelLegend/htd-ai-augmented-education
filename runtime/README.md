# 运行环境

项目默认使用 `runtime/` 下的隔离环境。下列命令以 Windows PowerShell 为例，请从项目根目录执行。

## Python 环境

当前使用 Python 3.11；测试版本为 Python 3.11.5。Python 依赖声明位于 `runtime/.venv/requirements.txt`，虚拟环境的实际文件不纳入 Git。

`runtime/` 只保存隔离运行环境、依赖声明和本说明。Skill 的请求快照、Agent 输入、状态机中间 JSON、日志和报告必须写入 `logs/<skill>/runs/<run-id>/` 或 `outputs/<skill>/runs/<run-id>/`，不得在此目录新增或更新运行产物。

```powershell
py -3.11 -m venv runtime/.venv
runtime/.venv/Scripts/python.exe -m pip install -r runtime/.venv/requirements.txt
runtime/.venv/Scripts/python.exe --version
```

依赖包括 `jsonschema`、`pytest` 和 `PyYAML`，分别用于 Schema 校验、测试和敏感信息策略解析。使用项目 Skills 时，优先调用 `runtime/.venv/Scripts/python.exe`。

## Node.js 环境

当前仓库没有 Node.js 依赖声明，也没有需要执行的 Node.js 安装步骤；Node.js 的版本要求及依赖安装方式：**待更新**。

当前测试机器安装了 Node.js 20.17.0 和 npm 10.8.2。可用以下命令查看本机版本：

```powershell
node --version
npm --version
```

## 按功能安装的额外软件

EPUB 转 PDF 功能需要 Calibre 的 `ebook-convert`。安装与检查方法见 [Calibre 安装说明](../skills/format-conversion-master/references/calibre-installation.md)；其他功能无需为此安装 Calibre。

## 测试环境

以下为本项目当前使用的本机环境记录，并非其他系统的兼容性承诺：

| 项目 | 版本或配置 |
| --- | --- |
| 操作系统 | Windows 11 家庭版中文版 |
| 系统架构 | x64（64 位） |
| CPU | 13th Gen Intel(R) Core(TM) i7-13700H |
| Python | 3.11.5 |
| Node.js | 20.17.0 |
| npm | 10.8.2 |
