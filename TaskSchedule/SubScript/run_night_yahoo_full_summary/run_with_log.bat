@echo off
setlocal

REM ============================================================
REM File   : TaskSchedule\SubScript\run_night_yahoo_full_summary\run_with_log.bat
REM Version: V1-NIGHT-YAHOO-RUN-WITH-LOG
REM ------------------------------------------------------------
REM 夜間Yahoo日足 + 1m/3m/5m summary バッチを実行し、ログをファイル保存する。
REM ============================================================

REM 2026-08-06: 旧パス F:\Users\hosot\PycharmProjects\kabucom_Ver32_L31 は存在せず
REM バッチが起動できない状態だったため、現行のリポジトリ配置に修正。
set PROJECT_ROOT=W:\BUSINESS\事業\04_株式自動売買システム\ソースコード

cd /d "%PROJECT_ROOT%"

if not exist logs mkdir logs

set LOG_FILE=%PROJECT_ROOT%\logs\night_yahoo_full_summary.log

echo.>> "%LOG_FILE%"
echo ============================================================>> "%LOG_FILE%"
echo START %date% %time%>> "%LOG_FILE%"
echo ============================================================>> "%LOG_FILE%"

REM ---- Daily DB build/update options ----
REM 前日までのDBを作る場合はEND_DATEを指定する。
REM 必要に応じてここを書き換える。
set NIGHT_YAHOO_DAILY_FORCE_FULL=1
set NIGHT_YAHOO_DAILY_END_DATE=2026-06-24
set NIGHT_YAHOO_DIRECT_ONLY=1
set NIGHT_YAHOO_DIRECT_TIMEOUT=10
set NIGHT_YAHOO_DAILY_SYMBOL_TIMEOUT_SEC=30
set NIGHT_YAHOO_DAILY_FULLCOL_REPAIR=1

REM summaryまで実行したくない場合は 1 にする。
REM set NIGHT_YAHOO_SKIP_SUMMARY=1

REM 稼働中プロセスと同じ .venv を使う (旧: WindowsApps\python3.11.exe)
"%PROJECT_ROOT%\.venv\Scripts\python.exe" ^
"%PROJECT_ROOT%\TaskSchedule\SubScript\run_night_yahoo_full_summary\main.py" ^
>> "%LOG_FILE%" 2>&1

set RET=%ERRORLEVEL%

echo ============================================================>> "%LOG_FILE%"
echo END %date% %time% exit=%RET%>> "%LOG_FILE%"
echo ============================================================>> "%LOG_FILE%"

exit /b %RET%
