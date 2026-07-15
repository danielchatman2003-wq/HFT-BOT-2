#!/usr/bin/env bash
# One-command setup + run for Mac/Linux:   bash start.sh
# First run: installs everything and asks for your Kalshi API key details.
# Later runs: starts the bot immediately (paper mode unless .env says otherwise).
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found -- install Python 3.10+ from python.org first."
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Creating virtual environment (one-time)..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "First-time setup -- you need a free Kalshi API key:"
  echo "  1. Sign in at kalshi.com (or demo.kalshi.co for play money)."
  echo "  2. Account -> API keys -> Create. Copy the Key ID and save the"
  echo "     downloaded private key (.pem) file somewhere."
  echo ""
  read -r -p "Paste your Kalshi API key ID: " KEY_ID
  read -r -p "Path to the downloaded .pem file (e.g. ~/Downloads/kalshi-key.pem): " PEM_PATH
  PEM_PATH="${PEM_PATH/#\~/$HOME}"
  python3 - "$KEY_ID" "$PEM_PATH" <<'PY'
import pathlib
import re
import sys

key_id, pem = sys.argv[1].strip(), sys.argv[2].strip()
p = pathlib.Path(".env")
text = p.read_text()
text = re.sub(r"(?m)^KALSHI_API_KEY_ID=.*$", f"KALSHI_API_KEY_ID={key_id}", text)
text = re.sub(r"(?m)^KALSHI_PRIVATE_KEY_PATH=.*$", f"KALSHI_PRIVATE_KEY_PATH={pem}", text)
p.write_text(text)
print(".env written -- paper mode against the live market is the default.")
PY
fi

echo ""
echo "Starting bot (Ctrl-C to stop). PnL summary afterwards: .venv/bin/python -m src.report"
exec python -m src.bot
