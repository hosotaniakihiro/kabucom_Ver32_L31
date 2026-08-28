@echo off
setlocal

REM ============================================================
REM File   : TaskSchedule\SubScript\run_night_yahoo_full_summary\run_with_log.bat
REM Version: V1-NIGHT-YAHOO-RUN-WITH-LOG
REM ------------------------------------------------------------
REM 夜間Yahoo日足 + 1m/3m/5m summary バッチを実行し、ログをファイル保存する。
REM ============================================================

REM 2026-08-06: old path F:\Users\hosot\PycharmProjects\kabucom_Ver32_L31 does
REM             not exist, so this bat could not start at all. Fixed to use
REM             the current repository layout.
REM NOTE: keep lines in this file ASCII-only, and never write the repository
REM       path literally. This file is UTF-8 but cmd.exe reads it as CP932,
REM       so Japanese bytes break line parsing and a literal Japanese path
REM       makes "cd" fail with
REM       "The filename, directory name, or volume label syntax is incorrect".
REM       This bat lives in
REM         <root>\TaskSchedule\SubScript\run_night_yahoo_full_summary\
REM       so the repository root is three levels up.
REM 2026-08-25: the task ran nightly with LastResult=1 but wrote nothing to
REM   the log, so the failure could not be diagnosed at all. Two changes:
REM   1) pushd instead of cd /d, so a UNC action path also works.
REM      cd /d cannot make a UNC path the current directory; pushd maps a
REM      temporary drive letter for it. W: is a mapped drive and does not
REM      exist in a non-interactive (S4U) task logon session.
REM   2) if the repository root cannot be reached, write the reason to a
REM      local fallback log instead of dying silently.
set "FALLBACK_LOG=%TEMP%\night_yahoo_full_summary_bootstrap.log"

pushd "%~dp0..\..\.." 2>nul
if errorlevel 1 goto no_root
if not exist "%CD%\.venv\Scripts\python.exe" goto no_root
set "PROJECT_ROOT=%CD%"
goto root_ok

:no_root
echo.>> "%FALLBACK_LOG%"
echo ============================================================>> "%FALLBACK_LOG%"
echo START %date% %time% BOOTSTRAP FAILED>> "%FALLBACK_LOG%"
echo dp0=%~dp0>> "%FALLBACK_LOG%"
echo CD=%CD%>> "%FALLBACK_LOG%"
echo repository root or .venv is not reachable from this task session.>> "%FALLBACK_LOG%"
echo drives visible to this session:>> "%FALLBACK_LOG%"
net use >> "%FALLBACK_LOG%" 2>&1
echo ============================================================>> "%FALLBACK_LOG%"
exit /b 1

:root_ok

REM Log messages contain emoji (e.g. the INIT ENGINES line). With stdout
REM redirected to a file, Python defaults to CP932 here and the logging
REM handler raises UnicodeEncodeError for every such line. It does not stop
REM the batch but floods the log, so force UTF-8 for stdout/stderr.
set "PYTHONIOENCODING=utf-8"

if not exist logs mkdir logs

set LOG_FILE=%PROJECT_ROOT%\logs\night_yahoo_full_summary.log

echo.>> "%LOG_FILE%"
echo ============================================================>> "%LOG_FILE%"
echo START %date% %time%>> "%LOG_FILE%"
echo ============================================================>> "%LOG_FILE%"

REM ---- Daily DB build/update options ----
REM Set END_DATE only when you want to build the DB up to a specific date.
REM 2026-08-06: 2026-06-24 was left pinned here, so the batch could never
REM             produce current data. Unset it; without END_DATE the batch
REM             updates up to the latest market date.
REM set NIGHT_YAHOO_DAILY_END_DATE=2026-06-24
REM FORCE_FULL=1 rebuilds the whole 5.1GB DB every run. Back to the
REM default incremental update (0). Set 1 only for a full rebuild.
set NIGHT_YAHOO_DAILY_FORCE_FULL=0
set NIGHT_YAHOO_DIRECT_ONLY=1
set NIGHT_YAHOO_DIRECT_TIMEOUT=10
set NIGHT_YAHOO_DAILY_SYMBOL_TIMEOUT_SEC=30
set NIGHT_YAHOO_DAILY_FULLCOL_REPAIR=1

REM ---- Minute (summary) catch-up options ----
REM 2026-08-06: the batch now looks at what is already stored in the summary
REM             DBs and re-fetches minute bars from the oldest incomplete day
REM             up to the latest market date.
REM   - auto-detect by default (a day whose stored volume is below 50 percent
REM     of the reference is treated as incomplete)
REM   - Yahoo serves 1m data for about 30 days only; older days are skipped
REM   - one request covers at most 8 days, so it is split into 7-day windows
REM To change the threshold:
REM set NIGHT_YAHOO_CATCHUP_MIN_RATIO=0.5
REM To re-fetch from a specific date:
REM set NIGHT_YAHOO_START_DATE=2026-08-05
REM To keep the old behaviour (latest market date only):
REM set NIGHT_YAHOO_DISABLE_CATCHUP=1

REM Set 1 to skip the summary stage.
REM set NIGHT_YAHOO_SKIP_SUMMARY=1

REM Use the same .venv as the running processes (was: WindowsApps python3.11)
REM -u (unbuffered) is required. With stdout redirected to a file Python
REM block-buffers, so a multi-hour run shows nothing in the log until it
REM ends and progress cannot be checked.
"%PROJECT_ROOT%\.venv\Scripts\python.exe" -u ^
"%PROJECT_ROOT%\TaskSchedule\SubScript\run_night_yahoo_full_summary\main.py" ^
>> "%LOG_FILE%" 2>&1

set RET=%ERRORLEVEL%
popd

echo ============================================================>> "%LOG_FILE%"
echo END %date% %time% exit=%RET%>> "%LOG_FILE%"
echo ============================================================>> "%LOG_FILE%"

exit /b %RET%
