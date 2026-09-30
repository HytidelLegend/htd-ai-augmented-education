[CmdletBinding()]
param(
  [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$pythonPath = Join-Path $repoRoot 'runtime/.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
  throw '缺少 runtime/.venv/Scripts/python.exe'
}
$argsList = @('-B', (Join-Path $PSScriptRoot 'scripts/start.py'))
if ($NoBrowser) { $argsList += '--no-browser' }
& $pythonPath @argsList
if ($LASTEXITCODE -ne 0) { throw "词汇星图启动失败，退出码 $LASTEXITCODE" }
