@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if exist "runtime\python\pythonw.exe" (
  "runtime\python\pythonw.exe" "app\tools\launch_desktop.py"
) else if exist "runtime\python\python.exe" (
  "runtime\python\python.exe" "app\tools\launch_desktop.py"
) else (
  echo [ERROR] Bundled Python is missing. Extract the complete RPTS archive.
  pause
  exit /b 1
)
if errorlevel 1 (
  echo [ERROR] GUI startup failed. See data\reports\RenPyStartupReport.zip
  echo Run RPTS-Debug.bat for detailed diagnostics.
  pause
)
