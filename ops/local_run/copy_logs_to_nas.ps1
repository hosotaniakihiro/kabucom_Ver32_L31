# ============================================================
# ops/local_run/copy_logs_to_nas.ps1
# 2026-10-24: logs are written on this PC (%LOCALAPPDATA%\autostock\logs) and
# copied to the NAS afterwards. Writing logs directly to the NAS lost them
# whenever the NAS/SMB hung (Errno 24, Mup 139).
# Called from stop_all.ps1 (stop is Stop-Process -Force, so log_mirror's final
# flush never runs) and from the night Yahoo batch (run_with_log.bat).
# The local file is always the original; the NAS copy is overwritten.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File copy_logs_to_nas.ps1 [-Days 2]
# ============================================================
param([int]$Days = 2)

$local = Join-Path $env:LOCALAPPDATA 'autostock\logs'
# UNC first: mapped drives (X:) do not exist in a scheduled-task session.
$nas = '\\192.168.0.22\AutoStockBuyAndSell\logs'
if (-not (Test-Path $nas)) { $nas = 'X:\logs' }
if (-not (Test-Path $nas)) { Write-Host "[COPY LOGS] NAS not reachable; skipped"; exit 0 }

# local sub dir -> NAS sub dir
$pairs = @(
    @('console', 'console'),
    @('boot_evidence', 'boot_evidence'),
    @('night_yahoo', 'night_yahoo')
)
foreach ($p in $pairs) {
    $src = Join-Path $local $p[0]
    if (-not (Test-Path $src)) { continue }
    $dst = Join-Path $nas $p[1]
    & robocopy $src $dst /MAXAGE:$Days /R:2 /W:2 /NP /NDL /NJH /NJS | Out-Null
    # robocopy: 0-7 = ok, 8+ = failure
    Write-Host ("[COPY LOGS] {0} -> {1} rc={2}" -f $src, $dst, $LASTEXITCODE)
}
exit 0
