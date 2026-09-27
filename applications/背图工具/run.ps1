[CmdletBinding()]
param(
  [int]$Port = 5173,
  [switch]$Preview,
  [int]$ServicePort = 5174,
  [string]$WorkspacePath = ''
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$pythonPath = Join-Path $repoRoot 'runtime/.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
  throw 'Project runtime is missing: runtime/.venv/Scripts/python.exe'
}
if ($Port -lt 1 -or $Port -gt 65535 -or $ServicePort -lt 1 -or $ServicePort -gt 65535) {
  throw '端口必须在 1 到 65535 之间'
}
if ($Port -eq $ServicePort) {
  throw 'GUI 和服务端口不能相同'
}

function Stop-ProcessTree([int]$RootPid) {
  $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $RootPid" -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty ProcessId)
  foreach ($childPid in $children) {
    Stop-ProcessTree $childPid
  }
  Stop-Process -Id $RootPid -Force -ErrorAction SilentlyContinue
}

foreach ($listenPort in @($Port, $ServicePort)) {
  $owners = @(Get-NetTCPConnection -State Listen -LocalPort $listenPort -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique)
  foreach ($ownerPid in $owners) {
    if ($ownerPid -eq $PID) {
      throw "端口 $listenPort 被当前 PowerShell 进程占用，无法停止自身"
    }
    Write-Host "端口 $listenPort 已被进程 $ownerPid 占用，正在停止旧进程树..."
    Stop-ProcessTree $ownerPid
  }
  if ($owners.Count -gt 0) {
    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
      Start-Sleep -Milliseconds 100
      $remaining = @(Get-NetTCPConnection -State Listen -LocalPort $listenPort -ErrorAction SilentlyContinue)
    } while ($remaining.Count -gt 0 -and [DateTime]::UtcNow -lt $deadline)
    if ($remaining.Count -gt 0) {
      throw "端口 $listenPort 的旧进程已停止，但端口仍未释放"
    }
  }
}

$launcher = Join-Path $repoRoot 'utils/scripts/beitu_runtime.py'
$launchArgs = @('-B', $launcher, '--port', "$Port", '--service-port', "$ServicePort")
if ($Preview) { $launchArgs += '--preview' }
if (-not [string]::IsNullOrWhiteSpace($WorkspacePath)) {
  $launchArgs += @('--workspace', [System.IO.Path]::GetFullPath($WorkspacePath))
}
& $pythonPath @launchArgs
if ($LASTEXITCODE -ne 0) { throw "Editor launcher failed with exit code $LASTEXITCODE" }
