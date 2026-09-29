@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem ---- locate a Python interpreter ----
set "PY="
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY if exist "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe" set "PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not defined PY (
  echo [ERROR] Python not found. Install Python 3.8+ or set PY manually.
  exit /b 1
)

rem ---- run check-in (pass through any arguments) ----
"%PY%" "%~dp0glados_checkin.py" %*
set "CODE=%ERRORLEVEL%"

rem ---- keep the console open when double-clicked ----
echo %CMDCMDLINE% | find /i "/c" >nul
if not errorlevel 1 if not "%NO_PAUSE%"=="1" pause
exit /b %CODE%
