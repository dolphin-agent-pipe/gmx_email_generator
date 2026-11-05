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
echo Starting %NAME%
powershell -NoProfile -Command "$p = Start-Process -FilePath $env:PY -ArgumentList $env:SCRIPT -WorkingDirectory (Get-Location).Path -RedirectStandardOutput $env:LOG -RedirectStandardError $env:ERR -WindowStyle Hidden -PassThru; Set-Content -LiteralPath $env:PIDFILE -Value $p.Id -Encoding ascii"
if errorlevel 1 exit /b 1
if not exist "%PIDFILE%" exit /b 1
set /p SERVICE_PID=<"%PIDFILE%"
echo Waiting for %NAME%
powershell -NoProfile -Command "$deadline = (Get-Date).AddSeconds(120); $url = $env:HEALTH; $script = $env:SCRIPT; while ((Get-Date) -lt $deadline) { try { $r = Invoke-WebRequest -UseBasicParsing $url -TimeoutSec 3; if ($r.StatusCode -eq 200) { exit 0 } } catch {} ; $alive = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($script) }; if (-not $alive) { exit 2 }; Start-Sleep -Milliseconds 500 }; exit 1"
if errorlevel 1 (
  echo %NAME% did not stay running.
  if exist "%ERR%" type "%ERR%"
  if exist "%LOG%" type "%LOG%"
  exit /b 1
)
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
