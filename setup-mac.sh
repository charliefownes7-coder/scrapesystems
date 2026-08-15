#!/bin/bash
# ScrapeSystems — one-time setup (Mac)
#
# Run this ONCE after downloading the ScrapeSystems folder. After this
# finishes, you'll have a "Start ScrapeSystems" file you can just
# double-click from then on — no terminal needed again.

set -e  # stop immediately if anything fails, rather than limping on

echo "== ScrapeSystems setup =="
echo ""

# --- Check Python is installed ---
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 isn't installed."
    echo "Download it from https://www.python.org/downloads/ , install it,"
    echo "then run this setup script again."
    exit 1
fi
echo "✅ Python 3 found: $(python3 --version)"

# --- Create a virtual environment (isolated, won't conflict with anything else) ---
if [ ! -d "venv" ]; then
    echo "Creating a virtual environment..."
    python3 -m venv venv
fi

# --- Install everything ScrapeSystems needs ---
echo "Installing dependencies (this can take a few minutes the first time)..."
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q

# --- Install Playwright's browser binaries (needed for scraping) ---
echo "Installing browser components for scraping..."
./venv/bin/playwright install firefox chromium

# --- Create a double-clickable launcher ---
cat > "Start ScrapeSystems.command" << 'EOF'
#!/bin/bash
cd "$(dirname "$0")"
./venv/bin/python3 main.py
EOF
chmod +x "Start ScrapeSystems.command"

echo ""
echo "✅ Setup complete!"
echo "From now on, just double-click 'Start ScrapeSystems.command' in this folder"
echo "to run the agent — no terminal needed."
