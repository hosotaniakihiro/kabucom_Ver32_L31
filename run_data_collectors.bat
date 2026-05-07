@echo off
setlocal

REM ============================================================
REM data_collectors サブプロジェクト起動BAT
REM ============================================================

set PROJECT_ROOT=F:\script\python\kabu\kabucom_Ver32_L31
set PYTHON_EXE=D:\Users\owner\anaconda3\python.exe

cd /d %PROJECT_ROOT%

%PYTHON_EXE% %PROJECT_ROOT%\scripts\data_collectors_runner.py

endlocal
