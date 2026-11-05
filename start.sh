#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -x .venv/Scripts/python.exe ]]; then
  PY=".venv/Scripts/python.exe"
elif [[ -x .venv/bin/python ]]; then
  PY=".venv/bin/python"
else
  echo "Run ./setup.sh first." >&2
  exit 1
fi

mkdir -p logs
pids=()

cleanup() {
  local pid
  if ((${#pids[@]})); then
    for pid in "${pids[@]}"; do
      kill "$pid" 2>/dev/null || true
    done
  fi
}
trap cleanup EXIT INT TERM

start_bg() {
  local name="$1"
  local script="$2"
  echo "Starting ${name} (logs/${name}.log)"
  "$PY" "$script" > "logs/${name}.log" 2>&1 &
  pids+=("$!")
}

mode="${1:-all}"
case "$mode" in
  all)
    start_bg sms sms_code_service.py
    start_bg hero hero_sms_service.py
    start_bg anosim anosim_service.py
    echo "Opening Mailbox"
    "$PY" studio.py
    ;;
  studio)
    echo "Opening Mailbox"
    "$PY" studio.py
    ;;
  sms)
    exec "$PY" sms_code_service.py
    ;;
  hero)
    exec "$PY" hero_sms_service.py
    ;;
  anosim)
    exec "$PY" anosim_service.py
    ;;
  *)
    echo "Usage: ./start.sh [all|studio|sms|hero|anosim]" >&2
    exit 1
    ;;
esac
