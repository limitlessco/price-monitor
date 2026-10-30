@echo off
rem One-click local run for Windows. Double-click this file, or run it from a terminal.
rem It checks prices and shows matches on screen. Nothing is sent and nothing is saved:
rem the alerts themselves come from the hourly GitHub Actions run.
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"

set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% --version >nul 2>nul || goto :nopython

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating a private Python environment in .venv ...
    %PY% -m venv .venv || goto :fail
) else (
    echo [1/3] Python environment found.
)

echo [2/3] Installing or updating the required packages ...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt || goto :fail

if not exist config.toml copy config.example.toml config.toml >nul

echo [3/3] Checking prices - nothing is sent and nothing is saved ...
".venv\Scripts\python.exe" -m price_monitor --config config.toml --state state\local-state.json --dry-run
if errorlevel 1 goto :fail
echo.
echo Done. Lines starting with "DRY RUN alert" are products that match your rules.
goto :end

:nopython
echo Python 3.11 or newer is not installed.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo then run this file again.
goto :end

:fail
echo.
echo Something went wrong - see the messages above, and the Troubleshooting part of README.md.

:end
echo.
pause
