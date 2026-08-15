@echo off
REM ScrapeSystems — one-time setup (Windows)
REM
REM Run this ONCE after downloading the ScrapeSystems folder (double-click
REM this file). After it finishes, you'll have a "Start ScrapeSystems"
REM shortcut you can just double-click from then on — no command prompt
REM needed again.

echo == ScrapeSystems setup ==
echo.

REM --- Check Python is installed ---
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python isn't installed.
    echo Download it from https://www.python.org/downloads/
    echo IMPORTANT: on the install screen, check the box that says
    echo "Add python.exe to PATH" before clicking Install.
    echo Then run this setup script again.
    pause
    exit /b 1
)
echo Python found.
python --version

REM --- Create a virtual environment ---
if not exist venv (
    echo Creating a virtual environment...
    python -m venv venv
)

REM --- Install everything ScrapeSystems needs ---
echo Installing dependencies (this can take a few minutes the first time)...
venv\Scripts\pip install --upgrade pip -q
venv\Scripts\pip install -r requirements.txt -q

REM --- Install Playwright's browser binaries ---
echo Installing browser components for scraping...
venv\Scripts\playwright install firefox chromium

REM --- Create a double-clickable launcher ---
echo @echo off > "Start ScrapeSystems.bat"
echo cd /d "%%~dp0" >> "Start ScrapeSystems.bat"
echo venv\Scripts\python.exe main.py >> "Start ScrapeSystems.bat"

echo.
echo Setup complete!
echo From now on, just double-click "Start ScrapeSystems.bat" in this folder
echo to run the agent — no command prompt needed.
pause
