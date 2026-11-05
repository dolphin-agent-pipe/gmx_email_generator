@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "logs" mkdir "logs"

set "SMS_CODE_PORT=8780"
set "HERO_SMS_PORT=8781"
set "ANOSIM_PORT=8782"
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    if /I "%%A"=="SMS_CODE_PORT" set "SMS_CODE_PORT=%%B"
    if /I "%%A"=="HERO_SMS_PORT" set "HERO_SMS_PORT=%%B"
    if /I "%%A"=="ANOSIM_PORT" set "ANOSIM_PORT=%%B"
  )
)

set "MODE=%~1"
if "%MODE%"=="" set "MODE=all"

if /I "%MODE%"=="all" goto mode_all
if /I "%MODE%"=="sms" goto mode_sms
if /I "%MODE%"=="hero" goto mode_hero
if /I "%MODE%"=="anosim" goto mode_anosim

echo Usage: start-service.bat [all^|sms^|hero^|anosim]
pause
exit /b 1

:mode_all
echo Closing previous number services
call :stop_services sms_code_service.py hero_sms_service.py anosim_service.py
call :stop_browsers sms_browser kka_browser anosim_browser
call :launch sms sms_code_service.py !SMS_CODE_PORT!
if errorlevel 1 goto bg_fail
call :launch hero hero_sms_service.py !HERO_SMS_PORT!
if errorlevel 1 goto bg_fail
call :launch anosim anosim_service.py !ANOSIM_PORT!
if errorlevel 1 goto bg_fail
echo Number services are running. Open the window with start-ui.bat
pause
exit /b 0

:mode_sms
call :stop_services sms_code_service.py
call :stop_browsers sms_browser
"%PY%" sms_code_service.py
exit /b !ERRORLEVEL!

:mode_hero
call :stop_services hero_sms_service.py
call :stop_browsers kka_browser
"%PY%" hero_sms_service.py
exit /b !ERRORLEVEL!

:mode_anosim
call :stop_services anosim_service.py
call :stop_browsers anosim_browser
"%PY%" anosim_service.py
exit /b !ERRORLEVEL!

:bg_fail
echo A number service failed. See the logs folder.
call :stop_services sms_code_service.py hero_sms_service.py anosim_service.py
call :stop_browsers sms_browser kka_browser anosim_browser
for %%N in (sms hero anosim) do if exist "logs\%%N.pid" del "logs\%%N.pid" >nul 2>&1
pause
exit /b 1

:launch
set "NAME=%~1"
set "SCRIPT=%~2"
set "PORT=%~3"
set "LOG=%CD%\logs\%NAME%.log"
set "ERR=%CD%\logs\%NAME%.err.log"
set "PIDFILE=%CD%\logs\%NAME%.pid"
set "HEALTH=http://127.0.0.1:%PORT%/health"
echo Starting %NAME%. Its log stays in that command window.
start "%NAME%" /D "%CD%" cmd /k "%PY% %SCRIPT%"
echo Waiting for %NAME%
set /a WAITED=0

:wait_ready
curl.exe -fsS --max-time 3 -o NUL "%HEALTH%" >nul 2>&1
if not errorlevel 1 goto service_ready
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and $_.CommandLine -and $_.CommandLine.Contains($env:SCRIPT) }) { exit 0 } else { exit 2 }"
if errorlevel 2 (
  echo %NAME% did not stay running. See its command window.
  exit /b 1
)
set /a WAITED+=1
if %WAITED% GEQ 120 (
  echo %NAME% did not become ready. See its command window.
  exit /b 1
)
timeout /t 1 /nobreak >nul
goto wait_ready

:service_ready
echo %NAME% is ready
exit /b 0

:stop_services
set "STOP_SCRIPTS=%*"
powershell -NoProfile -Command "$names = @($env:STOP_SCRIPTS -split ' ' | Where-Object { $_ }); Get-CimInstance Win32_Process | ForEach-Object { $proc = $_; if ($proc.CommandLine) { foreach ($name in $names) { if ($proc.CommandLine.Contains($name)) { Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue; break } } } }"
exit /b 0

:stop_browsers
set "STOP_DIRS=%*"
powershell -NoProfile -Command "$dirs = @($env:STOP_DIRS -split ' ' | Where-Object { $_ }); Get-CimInstance Win32_Process | ForEach-Object { $proc = $_; if ($proc.Name -eq 'chrome.exe' -and $proc.CommandLine) { foreach ($dir in $dirs) { if ($proc.CommandLine.Contains($dir)) { Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue; break } } } }"
timeout /t 2 /nobreak >nul
exit /b 0
