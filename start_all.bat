@echo off
setlocal

REM ============================================================
REM File   : start_all.bat
REM Purpose:
REM   main_database.py と main.py を一括起動する
REM ============================================================

cd /d "%~dp0"

REM Anaconda Python を固定したい場合は下記を使う
REM set KABU_PYTHON_EXE=D:\Users\owner\anaconda3\python.exe

if not defined KABU_PYTHON_EXE (
    set KABU_PYTHON_EXE=python
)

echo [START_ALL_BAT] PROJECT_ROOT=%CD%
echo [START_ALL_BAT] PYTHON=%KABU_PYTHON_EXE%

"%KABU_PYTHON_EXE%" "%CD%\start_all.py" --delay 15

pause
endlocal
