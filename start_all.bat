@echo off
setlocal

REM ============================================================
REM File   : start_all.bat
REM Purpose:
REM   main_database.py と main.py を一括起動する
REM ============================================================

cd /d "%~dp0"

REM 2026-08-06: default was system "python", which differs from the .venv
REM             used by the running processes. Prefer the repo .venv.
REM NOTE: keep lines in this file ASCII-only. cmd.exe reads it as CP932
REM       while the file is UTF-8, and some Japanese byte sequences break
REM       line parsing (comments end up executed as commands).
REM To use another Python, set KABU_PYTHON_EXE before running this bat.
REM   example: set KABU_PYTHON_EXE=D:\Users\owner\anaconda3\python.exe

if not defined KABU_PYTHON_EXE if exist "%CD%\.venv\Scripts\python.exe" set "KABU_PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
if not defined KABU_PYTHON_EXE set "KABU_PYTHON_EXE=python"

echo [START_ALL_BAT] PROJECT_ROOT=%CD%
echo [START_ALL_BAT] PYTHON=%KABU_PYTHON_EXE%

"%KABU_PYTHON_EXE%" "%CD%\start_all.py" --delay 15

pause
endlocal
