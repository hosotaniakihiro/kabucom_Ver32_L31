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
    $_.CommandLine -notmatch 'TaskSchedule'
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
    $_.CommandLine -notmatch 'TaskSchedule'
}
Write-Host ("left={0}" -f @($left).Count)
