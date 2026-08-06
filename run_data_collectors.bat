@echo off
setlocal

REM ============================================================
REM data_collectors サブプロジェクト起動BAT
REM ============================================================

REM 2026-08-06: PROJECT_ROOT pointed to F:\script\python\kabu\kabucom_Ver32_L31
REM             which does not exist, and PYTHON_EXE pointed to Anaconda.
REM             Use this bat's own location and the repo .venv instead.
REM NOTE: keep lines in this file ASCII-only. cmd.exe reads it as CP932
REM       while the file is UTF-8, and some Japanese byte sequences break
REM       line parsing (comments end up executed as commands).
cd /d "%~dp0"
set "PROJECT_ROOT=%CD%"

if not defined PYTHON_EXE if exist "%PROJECT_ROOT%\.venv\Scripts\python.exe" set "PYTHON_EXE=%PROJECT_ROOT%\.venv\Scripts\python.exe"
if not defined PYTHON_EXE set "PYTHON_EXE=python"

echo [RUN_DATA_COLLECTORS] PROJECT_ROOT=%PROJECT_ROOT%
echo [RUN_DATA_COLLECTORS] PYTHON=%PYTHON_EXE%

"%PYTHON_EXE%" "%PROJECT_ROOT%\scripts\data_collectors_runner.py"

endlocal
