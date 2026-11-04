@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "app\tools\build-release.ps1" %*
set "RPTS_BUILD_EXIT=%ERRORLEVEL%"
if not "%RPTS_BUILD_EXIT%"=="0" echo [ERROR] Build failed. See the message above and docs/BUILD_WINDOWS.md.
if not defined CI pause
exit /b %RPTS_BUILD_EXIT%
