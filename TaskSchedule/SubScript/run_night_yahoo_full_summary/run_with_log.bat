@echo off
setlocal

REM ============================================================
REM File   : TaskSchedule\SubScript\run_night_yahoo_full_summary\run_with_log.bat
REM Version: V1-NIGHT-YAHOO-RUN-WITH-LOG
REM ------------------------------------------------------------
REM 夜間Yahoo日足 + 1m/3m/5m summary バッチを実行し、ログをファイル保存する。
REM ============================================================

cd /d F:\Users\hosot\PycharmProjects\kabucom_Ver32_L31

if not exist logs mkdir logs

set LOG_FILE=F:\Users\hosot\PycharmProjects\kabucom_Ver32_L31\logs\night_yahoo_full_summary.log

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

C:\Users\hosot\AppData\Local\Microsoft\WindowsApps\python3.11.exe ^
F:\Users\hosot\PycharmProjects\kabucom_Ver32_L31\TaskSchedule\SubScript\run_night_yahoo_full_summary\main.py ^
>> "%LOG_FILE%" 2>&1

set RET=%ERRORLEVEL%

echo ============================================================>> "%LOG_FILE%"
echo END %date% %time% exit=%RET%>> "%LOG_FILE%"
echo ============================================================>> "%LOG_FILE%"

exit /b %RET%
