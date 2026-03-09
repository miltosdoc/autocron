#!/bin/bash
set -e

# AutoCron Installer
# Usage: curl -fsSL https://raw.githubusercontent.com/miltosdoc/autocron/main/install.sh | bash

echo ""
echo "🤖 Installing AutoCron..."
echo ""

VENV_DIR="$HOME/.autocron-env"
REPO="https://github.com/miltosdoc/autocron.git"

# 1. Create venv
if [ ! -d "$VENV_DIR" ]; then
    echo "📦 Creating Python environment..."
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

# 2. Install packages
echo "📥 Installing CoPaw + AutoCron (this may take a minute)..."
pip install --upgrade pip -q
pip install "autocron-agent @ git+$REPO"

# 3. Register CoPaw skill
echo ""
autocron install

# 4. Add to PATH permanently
AUTOCRON_BIN="$VENV_DIR/bin"
SHELL_RC=""
if [ -f "$HOME/.zshrc" ]; then
    SHELL_RC="$HOME/.zshrc"
elif [ -f "$HOME/.bashrc" ]; then
    SHELL_RC="$HOME/.bashrc"
fi

if [ -n "$SHELL_RC" ]; then
    if ! grep -q "autocron-env/bin" "$SHELL_RC" 2>/dev/null; then
        echo "" >> "$SHELL_RC"
        echo "# AutoCron" >> "$SHELL_RC"
        echo "export PATH=\"$AUTOCRON_BIN:\$PATH\"" >> "$SHELL_RC"
        echo "🔗 Added to PATH in $SHELL_RC"
    fi
fi

echo ""
echo "✅ AutoCron installed!"
echo ""
echo "   Open a new terminal, then run:  copaw app"
echo ""
