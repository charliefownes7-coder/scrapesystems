@echo off
REM ScrapeSystems — double-click this file every time.
REM
REM First time you run it: sets everything up (a few minutes), including
REM installing Python for you if this PC doesn't have it.
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

set "PYEXE="

REM CI-only test hook: pretend no Python is installed so the automatic
REM Python installer below gets exercised. Never set for real users.
if defined SCRAPESYSTEMS_SKIP_PYTHON_DETECT goto :install_python

call :find_python
if defined PYEXE goto :have_python

:install_python
echo Python isn't installed on this PC -- installing it now.
echo ^(This downloads about 25 MB and needs an internet connection.^)
echo.
set "PY_INSTALLER=%TEMP%\scrapesystems-python-installer.exe"
if exist "%PY_INSTALLER%" del "%PY_INSTALLER%" >nul 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile (Join-Path $env:TEMP 'scrapesystems-python-installer.exe') } catch { exit 1 }"
if errorlevel 1 goto :python_failed
if not exist "%PY_INSTALLER%" goto :python_failed
echo Running the Python installer ^(this takes a minute^)...
start /wait "" "%PY_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=0 Include_test=0 Include_doc=0 Shortcuts=0
if not exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" goto :python_failed
set PYEXE="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
del "%PY_INSTALLER%" >nul 2>nul

:have_python
echo Python found.
%PYEXE% --version

if exist "venv\Scripts\python.exe" goto :venv_ready
echo Creating a private Python environment for ScrapeSystems...
%PYEXE% -m venv venv
if errorlevel 1 goto :venv_failed
:venv_ready

echo Installing dependencies ^(this is the slow part, please wait^)...
REM pip can't safely upgrade itself when invoked directly as pip.exe on
REM Windows -- it can't replace its own running executable file. Running
REM it via python.exe -m pip instead avoids that entirely.
venv\Scripts\python.exe -m pip install --upgrade pip -q
if %errorlevel% neq 0 (
    echo Something went wrong installing pip. Check your internet connection and try again.
    pause
    exit /b 1
)
venv\Scripts\python.exe -m pip install -r requirements.txt -q
if %errorlevel% neq 0 (
    echo Something went wrong installing dependencies. Check your internet connection and try again.
    pause
    exit /b 1
)

echo Installing browser components for scraping...
venv\Scripts\python.exe -m playwright install firefox chromium
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
exit /b %errorlevel%

:python_failed
echo.
echo ScrapeSystems couldn't install Python automatically.
echo.
echo Please install it yourself:
echo 1. Download it from https://www.python.org/downloads/
echo 2. On the install screen, CHECK THE BOX that says
echo    "Add python.exe to PATH" before clicking Install
echo 3. Double-click this file again
echo.
pause
exit /b 1

:venv_failed
echo.
echo Something went wrong creating the Python environment.
echo Try again, or delete the "venv" folder next to this file first.
pause
exit /b 1

:find_python
REM Each candidate must actually run and be 3.9 or newer. This also
REM rejects the Microsoft Store "python" shortcut that exists on many
REM Windows PCs without a real Python behind it.
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=py -3"
    exit /b 0
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=python"
    exit /b 0
)
exit /b 1
