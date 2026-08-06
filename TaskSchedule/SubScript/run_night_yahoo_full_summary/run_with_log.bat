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
REM 特定日までのDBを作りたい場合だけ END_DATE を指定する。
REM 2026-08-06: 2026-06-24 が固定されたままだったため解除。
REM             指定しなければ最新営業日まで更新される。
REM set NIGHT_YAHOO_DAILY_END_DATE=2026-06-24
REM FORCE_FULL=1 は 5.1GB のDBを毎回作り直すため既定の差分更新(0)に戻す。
REM 全銘柄を作り直したいときだけ 1 にする。
set NIGHT_YAHOO_DAILY_FORCE_FULL=0
set NIGHT_YAHOO_DIRECT_ONLY=1
set NIGHT_YAHOO_DIRECT_TIMEOUT=10
set NIGHT_YAHOO_DAILY_SYMBOL_TIMEOUT_SEC=30
set NIGHT_YAHOO_DAILY_FULLCOL_REPAIR=1

REM ---- Minute (summary) catch-up options ----
REM 2026-08-06: summary DB の格納状況を見て、欠損している日から
REM             最新営業日までの分足をまとめて取り直すようにした。
REM   - 未指定なら自動判定 (格納量が基準の50%未満の日を未完了とみなす)
REM   - Yahoo の 1m は約30日前までしか取得できないため、それ以前は対象外
REM   - 1リクエスト最大8日のため7日窓に分割して取得する
REM 判定のしきい値を変えたい場合:
REM set NIGHT_YAHOO_CATCHUP_MIN_RATIO=0.5
REM 特定日から取り直したい場合:
REM set NIGHT_YAHOO_START_DATE=2026-08-05
REM 従来どおり最新営業日1日だけにしたい場合:
REM set NIGHT_YAHOO_DISABLE_CATCHUP=1

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
