@echo off
setlocal EnableExtensions
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo Install Python 3.12 or newer, then run setup.bat again.
  pause
  exit /b 1
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 'Python 3.12 or newer is required, found ' + sys.version.split()[0])"
if errorlevel 1 (
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 (
    echo Could not create the virtual environment.
    pause
    exit /b 1
  )
)

set "PY=%~dp0.venv\Scripts\python.exe"
"%PY%" -m pip install --upgrade pip
if errorlevel 1 goto fail
"%PY%" -m pip install requests chardet pywebview
if errorlevel 1 goto fail

if exist ".env" (
  echo .env already exists, left it unchanged.
) else (
  > ".env" (
    echo TARGET_URL1=https://www.gmx.net/
    echo WEBDE_URL=https://www.web.de/
    echo.
    echo SERVICE_URL1=https://smscode.gg
    echo SMS_CODE_EMAIL=
    echo SMS_CODE_PASSWORD=
    echo SMS_CODE_ORDER_COUNT=3
    echo SMS_CODE_PORT=8780
    echo SMS_DEBUG_PORT=9333
    echo.
    echo SERVICE_URL2=https://hero-sms.com/
    echo HERO_SMS_EMAIL=
    echo HERO_SMS_PASSWORD=
    echo HERO_SMS_PORT=8781
    echo HERO_SMS_DEBUG_PORT=9334
    echo HERO_SMS_ORDER_COUNT=3
    echo HERO_SMS_COUNTRY=germany
    echo.
    echo SERVICE_URL3=https://anosim.net/
    echo ANOSIM_EMAIL=
    echo ANOSIM_PASSWORD=
    echo ANOSIM_PORT=8782
    echo ANOSIM_DEBUG_PORT=9335
    echo ANOSIM_ORDER_COUNT=3
    echo ANOSIM_COUNTRY=Germany
    echo.
    echo STUDIO_PORT=8790
  )
  echo Wrote .env. Fill in the three service logins before ordering numbers.
)

if not exist "dolphin\apikey.py" (
  "%PY%" -c "from pathlib import Path; Path('dolphin/apikey.py').write_text('api_key = \"\"\n', encoding='utf-8')"
  echo Wrote dolphin\apikey.py. Set api_key to the Dolphin cloud token.
)

if not exist "proxies.txt" (
  echo Create proxies.txt with one proxy per line: user:pass@host:port
)

echo Setup finished. Dolphin Anty must be running before you generate mailboxes.
echo Start the number services with start-service.bat
echo Open Mailbox with start-ui.bat
pause
exit /b 0

:fail
echo Setup failed.
pause
exit /b 1
