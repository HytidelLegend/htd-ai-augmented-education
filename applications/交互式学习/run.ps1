[CmdletBinding()]
param([int]$Port = 5177, [int]$ServicePort = 5178, [switch]$NoPause)
$ErrorActionPreference = 'Stop'
try {
    $repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $pythonPath = Join-Path $repoRoot 'runtime/.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) { throw '缺少 runtime/.venv/Scripts/python.exe' }
    & $pythonPath -B -X utf8 (Join-Path $PSScriptRoot 'scripts/start.py') --port "$Port" --service-port "$ServicePort"
    if ($LASTEXITCODE -ne 0) { throw "交互式学习启动失败，退出码 $LASTEXITCODE；请查看上方错误和日志路径。" }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    if (-not $NoPause -and [Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
        Read-Host '按 Enter 关闭窗口' | Out-Null
    }
    exit 1
}
