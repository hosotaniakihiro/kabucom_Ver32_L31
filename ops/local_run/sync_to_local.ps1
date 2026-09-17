# ============================================================
# ops/local_run/sync_to_local.ps1
# W: (NAS, git の正) のソースを C:\autostock\src へ同期する。
#
# 2026-09-16: W: 上で Python を起動すると数千ファイルの import が SMB を通り、
# セッションが飽和すると起動に 30〜40 分かかって途中でハングした (この日 3 回)。
# ローカルにコピーした状態では 2 分で 1 分 tick まで到達した。
# 以後、本番は C:\autostock\src + C:\autostock\venv で起動する。
#
# 2026-09-17: 9/16 夜は W: が EMFILE で書けず、その夜の実装 (C9/B6/A5/B7/A3 ほか) は
# C: 側にしか無い。/MIR だと C: の新規ファイルが消えるので、
#   - 既定は /E /XO (W: の方が新しいファイルだけ持ってくる、C: の独自ファイルは残す)
#   - C: → W: へ書き戻して W: でコミットし終えた後だけ -Mirror を付けて完全同期する
# 書き戻しは sync_back_to_w.ps1。
#
# 使い方:  powershell -ExecutionPolicy Bypass -File sync_to_local.ps1 [-Mirror] [-DryRun]
# ============================================================
param(
    [switch]$Mirror,
    [switch]$DryRun
)
$src = 'W:\BUSINESS\事業\04_株式自動売買システム\ソースコード'
$dst = 'C:\autostock\src'
$log = 'C:\autostock\sync_to_local.log'
New-Item -ItemType Directory -Force $dst | Out-Null
$mode = if ($Mirror) { @('/MIR') } else { @('/E', '/XO') }
$args = @("`"$src`"", "`"$dst`"") + $mode + @(
    '/XD', '.venv', '__pycache__', '.git', '_backup_20260902_fix', '_backup_20260903_push_fix',
           'logs', 'ai_fix_worktree', '.playwright-mcp', '.pytest_cache',
    '/XF', '*.pyc', '*.log', '*.db', '*.db-wal', '*.db-shm', '._*',
    '/R:2', '/W:2', '/NP', '/NFL', '/NDL', "/LOG:$log"
)
if ($DryRun) { $args += '/L' }
Write-Host ("sync W: -> C: mode={0} dryrun={1}" -f ($mode -join ' '), $DryRun.IsPresent)
$p = Start-Process -FilePath 'robocopy.exe' -ArgumentList $args -Wait -PassThru -NoNewWindow
# robocopy: 0-7 は成功 (1=コピーあり, 2=余分削除あり ...)、8 以上が失敗
if ($p.ExitCode -ge 8) { Write-Host "sync FAILED rc=$($p.ExitCode) log=$log"; exit 1 }
Write-Host "sync ok rc=$($p.ExitCode) log=$log"
