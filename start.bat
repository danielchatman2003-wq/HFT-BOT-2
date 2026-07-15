@echo off
rem One-click setup + run for Windows: double-click start.bat
rem First run: installs everything and asks for your Kalshi API key details.
cd /d %~dp0

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ required -- install it from python.org, tick "Add to PATH".
  pause
  exit /b 1
)

if not exist .venv (
  echo Creating virtual environment ^(one-time^)...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt

if exist .env goto run
copy .env.example .env >nul
echo.
echo First-time setup -- you need a free Kalshi API key:
echo   1. Sign in at kalshi.com ^(or demo.kalshi.co for play money^).
echo   2. Account -^> API keys -^> Create. Copy the Key ID and save the
echo      downloaded private key ^(.pem^) file somewhere.
echo.
set /p KEY_ID="Paste your Kalshi API key ID: "
set /p PEM_PATH="Path to the downloaded .pem file: "
python -c "import os,pathlib,re; p=pathlib.Path('.env'); t=p.read_text(); t=re.sub(r'(?m)^KALSHI_API_KEY_ID=.*$','KALSHI_API_KEY_ID='+os.environ['KEY_ID'].strip(),t); t=re.sub(r'(?m)^KALSHI_PRIVATE_KEY_PATH=.*$','KALSHI_PRIVATE_KEY_PATH='+os.environ['PEM_PATH'].strip().strip('\"'),t); p.write_text(t); print('.env written -- paper mode against the live market is the default.')"

:run
echo.
echo Starting bot (close this window or press Ctrl-C to stop).
echo PnL summary afterwards: .venv\Scripts\python -m src.report
python -m src.bot
pause
