# ============================================================
# File   : TaskSchedule/SubScript/run_night_yahoo_full_summary/register_task.ps1
# ------------------------------------------------------------
# Register the nightly Yahoo daily batch as a Windows scheduled task.
#
# NOTE: keep this file ASCII-only. PowerShell reads it as CP932 here, so
#       Japanese bytes break string parsing (same rule as run_with_log.bat).
#
# Background (2026-08-18):
#   stock_analysis.db had not been updated since 2026-07-17, so the daily
#   MTF signals were judged on month-old data. Two causes:
#     1. Norton TLS interception blocked Yahoo. Fixed in 79d3337 by calling
#        scripts/yahoo_ca_bundle.install() from the batch itself.
#     2. The batch was never registered in Task Scheduler (manual runs only).
#        This script registers it.
#
# Usage (PowerShell):
#   .\register_task.ps1                 # weekdays 20:00 (default)
#   .\register_task.ps1 -Time "21:30"
#   .\register_task.ps1 -WhatIf         # show settings only, do not register
# ============================================================
param(
    [string]$TaskName = "NightYahooDailyFullSummary",
    [string]$Time     = "16:00",
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

$bat = Join-Path $PSScriptRoot "run_with_log.bat"
if (-not (Test-Path $bat)) { throw "bat not found: $bat" }

$action    = New-ScheduledTaskAction -Execute $bat
$trigger   = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $Time
$settings  = New-ScheduledTaskSettingsSet `
                -StartWhenAvailable `
                -DontStopOnIdleEnd `
                -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
                -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited

Write-Host "TaskName  : $TaskName"
Write-Host "Trigger   : Mon-Fri at $Time"
Write-Host "Action    : $bat"
Write-Host "Settings  : 3h limit / no parallel runs / run after missed start"

if ($WhatIf) { Write-Host "" ; Write-Host "-WhatIf: not registered." ; return }

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

$info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
Write-Host ""
Write-Host "Registered. NextRunTime: $($info.NextRunTime)"
