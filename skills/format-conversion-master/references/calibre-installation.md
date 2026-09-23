# Calibre 安装与检查

从 [Calibre 官方 Windows 下载页](https://www.calibre-ebook.com/download_windows64) 安装，或在 PowerShell 执行：

```powershell
winget install --id calibre.calibre --exact --source winget --accept-package-agreements --accept-source-agreements
```

推荐保留默认安装位置：

```text
C:\Program Files\Calibre2\
```

Windows 的 `PATH` 必须加入目录，而不是 `.exe` 文件本身。加入后重新打开终端并检查：

```powershell
ebook-convert --version
```

必须能输出版本号且退出码为 0。检查失败时，格式转换 Skill 会进入 `paused_configuration`，不会自动下载或切换后端。
