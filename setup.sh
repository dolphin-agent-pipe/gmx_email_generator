#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  BASE=python3
elif command -v python >/dev/null 2>&1; then
  BASE=python
else
  echo "Install Python 3.12 or newer, then run ./setup.sh again." >&2
  exit 1
fi

"$BASE" - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit(
        f"Python 3.12 or newer is required, found {sys.version.split()[0]}"
    )
PY

if [[ ! -d .venv ]]; then
  "$BASE" -m venv .venv
fi

if [[ -x .venv/Scripts/python.exe ]]; then
  PY=".venv/Scripts/python.exe"
elif [[ -x .venv/bin/python ]]; then
  PY=".venv/bin/python"
else
  echo "The virtual environment is missing its Python executable." >&2
  exit 1
fi

"$PY" -m pip install --upgrade pip
"$PY" -m pip install requests chardet pywebview

if [[ ! -f .env ]]; then
  cat > .env <<'EOF'
TARGET_URL1=https://www.gmx.net/
WEBDE_URL=https://www.web.de/

SERVICE_URL1=https://smscode.gg
SMS_CODE_EMAIL=
SMS_CODE_PASSWORD=
SMS_CODE_ORDER_COUNT=3
SMS_CODE_PORT=8780
SMS_DEBUG_PORT=9333

SERVICE_URL2=https://hero-sms.com/
HERO_SMS_EMAIL=
HERO_SMS_PASSWORD=
HERO_SMS_PORT=8781
HERO_SMS_DEBUG_PORT=9334
HERO_SMS_ORDER_COUNT=3
HERO_SMS_COUNTRY=germany

SERVICE_URL3=https://anosim.net/
ANOSIM_EMAIL=
ANOSIM_PASSWORD=
ANOSIM_PORT=8782
ANOSIM_DEBUG_PORT=9335
ANOSIM_ORDER_COUNT=3
ANOSIM_COUNTRY=Germany

STUDIO_PORT=8790
EOF
  echo "Wrote .env. Fill in the three service logins before ordering numbers."
else
  echo ".env already exists, left it unchanged."
fi

if [[ ! -f dolphin/apikey.py ]]; then
  printf 'api_key = ""\n' > dolphin/apikey.py
  echo "Wrote dolphin/apikey.py. Set api_key to the Dolphin cloud token."
fi

if [[ ! -f proxies.txt ]]; then
  echo "Create proxies.txt with one proxy per line: user:pass@host:port"
fi

echo "Setup finished. Dolphin Anty must be running before you generate mailboxes."
echo "Start the app with ./start.sh"
