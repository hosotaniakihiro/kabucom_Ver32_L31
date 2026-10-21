# ============================================================
# ops/local_run/sync_back_to_w.ps1
# C:\autostock\src で行った変更を W: (NAS, git の正) へ書き戻す。
#
# 2026-09-17: 9/16 夜は W: が SMB 劣化 (EMFILE) で編集できず、その夜の実装は
# C: にしか無い。W: が復旧したらこれで書き戻し、W: で git diff を確認して
# コミットする。書き戻すのは「C: の方が新しいファイル」だけ (/XO)。
# W: を消したりはしない (/E、/MIR ではない)。
#
# 使い方:  powershell -ExecutionPolicy Bypass -File sync_back_to_w.ps1 [-DryRun]
#   1) -DryRun で対象一覧 (C:\autostock\sync_back_to_w.log) を確認
#   2) 本実行
#   3) W: で git status / git diff を見てコミット
#   4) その後 sync_to_local.ps1 -Mirror で C: を W: と完全一致に戻す
# ============================================================
param(
    [switch]$DryRun
)
$src = 'C:\autostock\src'
$dst = 'W:\BUSINESS\事業\04_株式自動売買システム\ソースコード'
$log = 'C:\autostock\sync_back_to_w.log'
if (-not (Test-Path "$dst\main.py")) { Write-Host "W: unreachable: $dst"; exit 1 }
$args = @(
    "`"$src`"", "`"$dst`"", '/E', '/XO',
    '/XD', '.venv', '__pycache__', '.git', 'logs', '.pytest_cache', 'runtime',
    '/XF', '*.pyc', '*.log', '*.db', '*.db-wal', '*.db-shm', '._*', 'settings.ini',
    '/R:2', '/W:2', '/NP', '/NDL', "/LOG:$log"
)
if ($DryRun) { $args += '/L' }
Write-Host ("sync C: -> W: dryrun={0}" -f $DryRun.IsPresent)
$p = Start-Process -FilePath 'robocopy.exe' -ArgumentList $args -Wait -PassThru -NoNewWindow
if ($p.ExitCode -ge 8) { Write-Host "sync FAILED rc=$($p.ExitCode) log=$log"; exit 1 }
Write-Host "sync ok rc=$($p.ExitCode) log=$log"
Get-Content $log | Select-String -Pattern '^\s+(New File|Newer|\*EXTRA)' | Select-Object -First 80
