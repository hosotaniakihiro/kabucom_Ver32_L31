@echo off
setlocal

REM ============================================================
REM File   : scripts/archive_dbs_to_onedrive.bat
REM Purpose:
REM   大引け後に当日の push / ranking DB を OneDrive へ保管する。
REM   タスクスケジューラから 1 日 1 回呼ぶ。
REM ------------------------------------------------------------
REM NOTE: keep lines in this file ASCII-only where it matters.
REM       cmd.exe reads it as CP932 while the file is UTF-8.
REM ============================================================

cd /d "%~dp0.."

if not defined KABU_PYTHON_EXE if exist "%CD%\.venv\Scripts\python.exe" set "KABU_PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
if not defined KABU_PYTHON_EXE set "KABU_PYTHON_EXE=python"

echo [ARCHIVE_BAT] PROJECT_ROOT=%CD%
echo [ARCHIVE_BAT] PYTHON=%KABU_PYTHON_EXE%

"%KABU_PYTHON_EXE%" "%CD%\scripts\archive_dbs_to_onedrive.py" %*

endlocal
