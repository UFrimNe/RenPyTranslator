@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"runtime\python\python.exe" "app\tools\launch_desktop.py" --diagnostic --make-report
echo.
echo Report: data\reports\RenPyStartupReport.zip
pause
