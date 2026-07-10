@echo off
REM ============================================================
REM  charts3dprint - one-click launcher (Windows)
REM  Double-click this file. It installs what's needed and opens
REM  the app in your browser.
REM ============================================================
setlocal
cd /d "%~dp0"

REM find Python
set PY=python
where python >nul 2>nul || set PY=py
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python 3.9+ is required but was not found.
  echo   Install it from https://www.python.org/downloads/
  echo   ^(tick "Add Python to PATH" during install^), then run this again.
  echo.
  pause
  exit /b 1
)

echo Installing dependencies ^(first run only, ~1-2 min^)...
%PY% -m pip install -r requirements.txt --quiet --disable-pip-version-check

echo.
echo Starting charts3dprint - your browser will open at http://127.0.0.1:5000
echo Keep this window open while using the app. Close it to quit.
echo.
%PY% -m charts3dprint --gui

pause
