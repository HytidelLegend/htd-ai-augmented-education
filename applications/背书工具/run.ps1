[CmdletBinding()]
param(
  [int]$Port = 5175,
  [int]$ServicePort = 5176,
  [switch]$Preview
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$pythonPath = Join-Path $repoRoot 'runtime/.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
  throw '缺少 runtime/.venv/Scripts/python.exe'
}
$argsList = @('-B', (Join-Path $PSScriptRoot 'scripts/start.py'), '--port', "$Port", '--service-port', "$ServicePort")
if ($Preview) { $argsList += '--preview' }
& $pythonPath @argsList
if ($LASTEXITCODE -ne 0) { throw "背书工具启动失败，退出码 $LASTEXITCODE" }
