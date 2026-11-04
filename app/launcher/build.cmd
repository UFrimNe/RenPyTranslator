@echo off
setlocal EnableExtensions
set "RPTS_VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
where cl.exe >nul 2>nul
if errorlevel 1 (
  if not exist "%RPTS_VSWHERE%" (
    echo [ERROR] Install Visual Studio Build Tools: Desktop development with C++.
    exit /b 1
  )
  for /f "usebackq tokens=*" %%i in (`"%RPTS_VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "RPTS_VS=%%i"
  if not defined RPTS_VS (
    echo [ERROR] Visual C++ x64 build tools are missing.
    exit /b 1
  )
)
if defined RPTS_VS call "%RPTS_VS%\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
pushd "%~dp0"
if not exist "..\..\build\launcher" mkdir "..\..\build\launcher"
rc.exe /nologo /fo "..\..\build\launcher\launcher.res" launcher.rc
if errorlevel 1 goto failed
cl.exe /nologo /utf-8 /std:c++17 /O2 /MT /EHsc /W4 /DUNICODE /D_UNICODE launcher.cpp /Fo"..\..\build\launcher\launcher.obj" /Fe"..\..\RPTS.exe" /link /SUBSYSTEM:WINDOWS /MANIFEST:NO "..\..\build\launcher\launcher.res" user32.lib
if errorlevel 1 goto failed
popd
exit /b 0
:failed
popd
exit /b 1
