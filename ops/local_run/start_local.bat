@echo off
setlocal
REM ============================================================
REM ops/local_run/start_local.bat
REM   Launch the trading system from the LOCAL copy:
REM     source : C:\autostock\src   (synced from W: by sync_to_local.ps1)
REM     python : C:\autostock\venv
REM   Since 2026-09-16 production runs from here. Do NOT use W:\...\start_all.bat
REM   (python startup over SMB took 30-40 min and hung; local start takes ~2 min).
REM   NOTE: keep this file ASCII-only (cmd.exe reads it as CP932).
REM ============================================================
set "KABU_PYTHON_EXE=C:\autostock\venv\Scripts\python.exe"
set "SRC=C:\autostock\src"
if not exist "%KABU_PYTHON_EXE%" (
  echo [START_LOCAL] venv not found: %KABU_PYTHON_EXE%
  pause
  exit /b 1
)
if not exist "%SRC%\start_all.py" (
  echo [START_LOCAL] source not found: %SRC%  ^(run sync_to_local.ps1 first^)
  pause
  exit /b 1
)
cd /d "%SRC%"
echo [START_LOCAL] SRC=%SRC%
echo [START_LOCAL] PYTHON=%KABU_PYTHON_EXE%
REM 2026-09-17: watchdog (notify only; add --restart to auto-restart) in its own window
start "autostock watchdog" "%KABU_PYTHON_EXE%" "%SRC%\ops\local_run\watchdog.py"
"%KABU_PYTHON_EXE%" "%SRC%\start_all.py" --delay 15
pause
endlocal
