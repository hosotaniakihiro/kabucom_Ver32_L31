# ============================================================
# ops/local_run/stop_all.ps1
# 売買システムの python プロセスを止める (夜間バッチ TaskSchedule 配下は残す)。
# 2026-09-16: 前日のプロセスが翌朝まで残っていて二重起動・古いコードの残留が
# 起きたので、引けの手順として固定する。
# 使い方: powershell -ExecutionPolicy Bypass -File stop_all.ps1
# ============================================================
$procs = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -like 'python*' -and
    ($_.CommandLine -match '株式自動売買システム' -or $_.CommandLine -match 'autostock\\src') -and
    $_.CommandLine -notmatch 'TaskSchedule' -and
    $_.CommandLine -notmatch 'rss_collector'  # 2026-10-30: 楽天 RSS の記録 (タスク autostock_rakuten_rss) は止めない
}
Write-Host ("stopping {0} processes" -f @($procs).Count)
foreach ($p in $procs) {
    $tail = $p.CommandLine.Substring([Math]::Max(0, $p.CommandLine.Length - 60))
    Write-Host ("  pid={0} {1}" -f $p.ProcessId, $tail)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 3
$left = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -like 'python*' -and
    ($_.CommandLine -match '株式自動売買システム' -or $_.CommandLine -match 'autostock\\src') -and
    $_.CommandLine -notmatch 'TaskSchedule' -and
    $_.CommandLine -notmatch 'rss_collector'  # 2026-10-30: 楽天 RSS の記録 (タスク autostock_rakuten_rss) は止めない
}
Write-Host ("left={0}" -f @($left).Count)

# 2026-10-24: データ (summary 等) はこの PC のローカル root に書き、止めたあとで NAS へ写す
# (local_data_sync.py。ローカル root を使っていなければ何もしない)
$py = $env:KABU_PYTHON_EXE
if (-not $py -or -not (Test-Path $py)) { $py = 'C:\autostock\venv\Scripts\python.exe' }
if (Test-Path $py) {
    & $py (Join-Path $PSScriptRoot 'local_data_sync.py') to-nas --days 1
} else {
    Write-Host "[STOP_ALL] python not found; local_data_sync skipped"
}

# 2026-10-24: ログはこの PC に書き、止めたあとで NAS へ写す
& (Join-Path $PSScriptRoot 'copy_logs_to_nas.ps1')
