#!/bin/bash
# ScrapeSystems — double-click this file every time.
#
# First time you run it: sets everything up (a few minutes).
# Every time after: just starts the agent (a few seconds).
# You never need to run anything else, and you never need Terminal.

cd "$(dirname "$0")"

# A completed setup leaves this marker behind. Its absence means
# either this is the very first run, or a previous setup attempt
# didn't finish — either way, (re)run setup below.
SETUP_DONE=".setup_complete"

if [ ! -f "$SETUP_DONE" ]; then
    echo "== ScrapeSystems: first-time setup =="
    echo "This only happens once — grab a coffee, it takes a few minutes."
    echo ""

    # macOS's built-in "python3" is a shim that only works once Apple's
    # Command Line Developer Tools are installed. On a Mac that doesn't
    # have them yet (most non-developer Macs), running python3 pops up
    # a system dialog asking to install them — and everything below
    # will fail in confusing ways if we push forward before that's done.
    # Check for this up front and explain it clearly instead.
    if ! xcode-select -p &> /dev/null; then
        echo "Apple's Command Line Tools aren't installed yet — ScrapeSystems needs"
        echo "these to run Python. This is a one-time system install (a few minutes),"
        echo "separate from ScrapeSystems itself."
        echo ""
        echo "A system window is about to pop up asking to install them:"
        echo "  1. Click \"Install\" in that window"
        echo "  2. Wait for it to finish (a progress bar will show)"
        echo "  3. Come back here and double-click this file again"
        echo ""
        xcode-select --install &> /dev/null
        read -p "Press Enter to close..."
        exit 1
    fi

    if ! command -v python3 &> /dev/null; then
        echo "❌ Python 3 isn't installed on this Mac."
        echo ""
        echo "1. Download it from https://www.python.org/downloads/"
        echo "2. Run the installer"
        echo "3. Double-click this file again"
        echo ""
        read -p "Press Enter to close..."
        exit 1
    fi
    echo "✅ Python 3 found: $(python3 --version)"

    if [ ! -d "venv" ]; then
        echo "Creating a private Python environment for ScrapeSystems..."
        python3 -m venv venv
    fi

    echo "Installing dependencies (this is the slow part, please wait)..."
    # On older Intel Macs, pip can try to compile a dependency (greenlet,
    # used by Playwright) for the wrong target architecture and fail with
    # a cryptic "architecture not supported" compiler error. Being
    # explicit about the architecture avoids that.
    export ARCHFLAGS="-arch $(uname -m)"
    if ! ./venv/bin/pip install --upgrade pip -q; then
        echo "❌ Something went wrong installing pip. Check your internet connection and try again."
        read -p "Press Enter to close..."
        exit 1
    fi
    if ! ./venv/bin/pip install -r requirements.txt -q; then
        echo "❌ Something went wrong installing dependencies. Check your internet connection and try again."
        read -p "Press Enter to close..."
        exit 1
    fi

    echo "Installing browser components for scraping..."
    if ! ./venv/bin/playwright install firefox chromium; then
        echo "❌ Something went wrong installing browser components. Try again, or check your internet connection."
        read -p "Press Enter to close..."
        exit 1
    fi

    touch "$SETUP_DONE"
    echo ""
    echo "✅ Setup complete! Starting ScrapeSystems now..."
    echo "(From now on, double-clicking this same file just starts the agent directly.)"
    echo ""
fi

# --- Every run, first time and every time after, lands here ---
echo "Starting ScrapeSystems agent..."
./venv/bin/python3 main.py
