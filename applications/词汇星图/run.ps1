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

$serviceScript = Join-Path $PSScriptRoot 'scripts/service.py'
$viteScript = Join-Path $PSScriptRoot 'node_modules/vite/bin/vite.js'
$owners = @(
  foreach ($port in @(5185, 5186)) {
    Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
      Select-Object -ExpandProperty OwningProcess
  }
) | Sort-Object -Unique

# Only stop processes started for this application; another service may use these ports.
foreach ($ownerPid in $owners) {
  $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerPid"
  if (-not $process) { continue }
  $commandLine = [string]$process.CommandLine
  if ($commandLine.IndexOf($serviceScript, [StringComparison]::OrdinalIgnoreCase) -lt 0 -and
      $commandLine.IndexOf($viteScript, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
    throw "端口 5185 或 5186 被其他进程 $ownerPid 占用：$commandLine"
  }
}

foreach ($ownerPid in $owners) {
  Write-Host "正在停止旧的词汇星图进程 $ownerPid..."
  Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
}

if ($owners.Count -gt 0) {
  $deadline = [DateTime]::UtcNow.AddSeconds(5)
  do {
    Start-Sleep -Milliseconds 100
    $remaining = @(
      foreach ($port in @(5185, 5186)) {
        Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
      }
    )
  } while ($remaining.Count -gt 0 -and [DateTime]::UtcNow -lt $deadline)
  if ($remaining.Count -gt 0) {
    throw '旧的词汇星图进程已停止，但端口 5185 或 5186 仍未释放'
  }
}

$argsList = @('-B', (Join-Path $PSScriptRoot 'scripts/start.py'))
if ($NoBrowser) { $argsList += '--no-browser' }
& $pythonPath @argsList
if ($LASTEXITCODE -ne 0) { throw "词汇星图启动失败，退出码 $LASTEXITCODE" }
