@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem Note: delayed expansion is required below, so a folder path containing "!" will break this file.
set "SCRIPT=%~dp0ghost_typer.py"
if not exist "%SCRIPT%" (
  echo [ERROR] ghost_typer.py not found. Keep run.bat and ghost_typer.py in the same folder.
  pause
  exit /b 1
)

set "GOOD="
set "P1="
set "P2="
for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "P1=%%~i"
for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "P2=%%~i"

for %%C in ("!P1!" "!P2!" "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" "C:\Python314\python.exe" "C:\Python313\python.exe" "C:\Python312\python.exe") do (
  if "!GOOD!"=="" (
    if exist "%%~C" (
      "%%~C" -c "import tkinter;tkinter.Tcl()" >nul 2>&1
      if not errorlevel 1 set "GOOD=%%~C"
    )
  )
)

if "!GOOD!"=="" (
  echo [ERROR] No Python with a working tkinter/Tcl found.
  echo Please install Python 3.8+ with the "tcl/tk and IDLE" option enabled, then run again.
  pause
  exit /b 1
)

rem pythonw.exe sits next to python.exe; derive the folder instead of string-replacing the name.
for %%P in ("!GOOD!") do set "PYDIR=%%~dpP"
set "PYW=!PYDIR!pythonw.exe"
if not exist "!PYW!" set "PYW=!GOOD!"

rem With extra arguments (e.g. --selftest) keep the console interpreter so output is visible.
if not "%~1"=="" set "PYW=!GOOD!"

start "" "!PYW!" "!SCRIPT!" %*
exit /b 0