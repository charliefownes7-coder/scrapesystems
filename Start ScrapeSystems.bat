@echo off
REM ScrapeSystems — double-click this file every time.
REM
REM First time you run it: sets everything up (a few minutes).
REM Every time after: just starts the agent (a few seconds).
REM You never need to run anything else, and you never need a command prompt.

cd /d "%~dp0"

REM A completed setup leaves this marker behind. Its absence means
REM either this is the very first run, or a previous setup attempt
REM didn't finish -- either way, (re)run setup below.
if exist ".setup_complete" goto :launch

echo == ScrapeSystems: first-time setup ==
echo This only happens once -- grab a coffee, it takes a few minutes.
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python isn't installed on this PC.
    echo.
    echo 1. Download it from https://www.python.org/downloads/
    echo 2. On the install screen, CHECK THE BOX that says
    echo    "Add python.exe to PATH" before clicking Install
    echo 3. Double-click this file again
    echo.
    pause
    exit /b 1
)
echo Python found.
python --version

if not exist venv (
    echo Creating a private Python environment for ScrapeSystems...
    python -m venv venv
)

echo Installing dependencies ^(this is the slow part, please wait^)...
venv\Scripts\pip install --upgrade pip -q
if %errorlevel% neq 0 (
    echo Something went wrong installing pip. Check your internet connection and try again.
    pause
    exit /b 1
)
venv\Scripts\pip install -r requirements.txt -q
if %errorlevel% neq 0 (
    echo Something went wrong installing dependencies. Check your internet connection and try again.
    pause
    exit /b 1
)

echo Installing browser components for scraping...
venv\Scripts\playwright install firefox chromium
if %errorlevel% neq 0 (
    echo Something went wrong installing browser components. Try again, or check your internet connection.
    pause
    exit /b 1
)

echo. > .setup_complete
echo.
echo Setup complete! Starting ScrapeSystems now...
echo ^(From now on, double-clicking this same file just starts the agent directly.^)
echo.

:launch
echo Starting ScrapeSystems agent...
venv\Scripts\python.exe main.py
