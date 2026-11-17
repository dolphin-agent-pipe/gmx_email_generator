@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)

set "STUDIO_PORT=8790"
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    if /I "%%A"=="STUDIO_PORT" set "STUDIO_PORT=%%B"
  )
)

echo Closing a previous Mailbox window if one is still open
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-ui.ps1" -Port %STUDIO_PORT%
timeout /t 1 /nobreak >nul

echo Opening Mailbox
"%~dp0.venv\Scripts\python.exe" studio.py
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" pause
exit /b %CODE%
