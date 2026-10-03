# 运行环境

项目默认使用 `runtime/` 下的隔离环境。下列命令以 Windows PowerShell 为例，请从项目根目录执行。

## 环境变量配置

1. 将项目根目录下的 `.env.example` 复制为同目录的 `.env`。
2. 在 [MinerU API key 管理页面](https://mineru.net/apiManage/token) 获取 API key，填写 `.env` 中的 `MINERU_API_KEY`。
3. 使用 ASR 或选择火山引擎 TTS 时，在 [火山引擎 API key 管理页面](https://console.volcengine.com/speech/new/setting/apikeys) 获取 API key，填写 `.env` 中的 `VOLCENGINE_API_KEY`。

`.env` 含本地凭据，不应提交 Git。

## 文字转语音音色准备

`run-text-to-speech` 默认使用本地安装的 `edge-tts==7.2.8` 客户端，默认晓艺、语速 `+10%`；合成需要访问 Microsoft 在线服务，无需 API key 或 Edge 浏览器。本次运行可用 `start --backend volcengine --speaker 哆啦A梦`，也可在 skill 的 `config.yaml` 设置 `backend: volcengine`；本次参数优先，恢复使用配置快照。选择火山后，严格检查根目录 `.env` 的 `VOLCENGINE_API_KEY`，忽略同名进程环境变量。

火山引擎分支使用音色 ID。先在 [火山引擎音色克隆页面](https://console.volcengine.com/speech/new/experience/clone) 克隆音色，再到 [音色库](https://console.volcengine.com/speech/new/voices) 获取音色 ID，运行 skill 时通过 `--speaker <音色ID>` 指定。

完整参数说明见 [run-text-to-speech 使用说明](../skills/run-text-to-speech/SKILL.md)。

## Python 环境

当前使用 Python 3.11；测试版本为 Python 3.11.5。Python 依赖声明位于 `runtime/.venv/requirements.txt`，虚拟环境的实际文件不纳入 Git。

`runtime/` 只保存隔离运行环境、依赖声明和本说明。Skill 的请求快照、Agent 输入、状态机中间 JSON、日志和报告必须写入 `logs/<skill>/runs/<run-id>/` 或 `outputs/<skill>/runs/<run-id>/`，不得在此目录新增或更新运行产物。

```powershell
py -3.11 -m venv runtime/.venv
runtime/.venv/Scripts/python.exe -m pip install -r runtime/.venv/requirements.txt
runtime/.venv/Scripts/python.exe --version
```

依赖包括 `jsonschema`、`pytest`、`PyYAML` 和 `playwright`，分别用于 Schema 校验、测试、敏感信息策略解析和可见浏览器采集。`build-word-entry` 使用本机安装的 Chrome，不保存浏览器会话到正式产物。使用项目 Skills 时，优先调用 `runtime/.venv/Scripts/python.exe`。

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

## 语音 Skill 依赖

`websockets==15.0.1` 用于 ASR 流式分支，复用已有 PyYAML、jsonschema 和 python-dotenv。语音处理需要 FFmpeg/FFprobe，并支持 `FFMPEG_PATH`、`FFPROBE_PATH` 指定本机路径；否则从 PATH 查找。启动前执行对应 `verify_package.py --environment` 做本地预检。共享白噪音素材位于 `utils/assets/white-noise.wav`，由确定性脚本生成。真实 API 的资源与克隆音色权限由用户账号决定，Mock 测试不证明云端可用性。

Edge-TTS 依赖已纳入上述 requirements 安装步骤。启动及 `verify_package.py --environment` 检查当前 Python 环境可导入锁定版本及 WordBoundary 接口；缺失依赖退出码为 6。网络错误在运行时暂停重试；本地预检不发起合成。
