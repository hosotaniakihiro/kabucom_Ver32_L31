# ============================================================
# ops/local_run/restart_local.ps1
# 売買システムを止めて起動し直す (stop_all.ps1 -> 古い start_local の窓を閉じる -> start_local.bat)。
# 2026-10-22: watchdog.py が「NAS をつなぎ直しても本体が古い足のまま」を検知したときに
#   切り離して呼ぶ (stop_all.ps1 は watchdog 自身も止めるので、python から順に呼ぶと起動まで届かない)。
#   10/22 13:37 に手で行った再起動と同じ手順。
# 使い方: powershell -ExecutionPolicy Bypass -File restart_local.ps1 [-Reason text]
# ============================================================
param([string]$Reason = 'manual')
Set-Location C:\
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $env:LOCALAPPDATA 'autostock\runtime'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'restart_local.log'
function W($m) { Add-Content -Path $log -Value ("{0} {1}" -f (Get-Date -Format s), $m) -Encoding UTF8 }
W "restart begin reason=$Reason"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here 'stop_all.ps1') *>> $log
Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" | Where-Object { $_.CommandLine -match 'start_local\.bat' } | ForEach-Object {
    W ("close cmd {0}" -f $_.ProcessId)
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 3
W 'start'
Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', (Join-Path $here 'start_local.bat') -WorkingDirectory (Split-Path -Parent (Split-Path -Parent $here))
Start-Sleep -Seconds 120
# 2026-10-24: ソースは OneDrive 配下 (パスに「株式自動売買システム」を含む) に移った
$n = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'autostock\\src' -or $_.CommandLine -match '株式自動売買システム' }).Count
W ("restart done python_processes={0}" -f $n)