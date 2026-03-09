#!/bin/bash
set -e

# AutoCron Installer
# Usage: curl -fsSL https://raw.githubusercontent.com/miltosdoc/autocron/main/install.sh | bash

echo "🤖 Installing AutoCron..."

VENV_DIR="$HOME/.autocron-env"
REPO="https://github.com/miltosdoc/autocron.git"

# 1. Create venv (silently)
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR" 2>/dev/null
fi
source "$VENV_DIR/bin/activate"

# 2. Install packages
pip install -q copaw "autocron-agent @ git+$REPO" 2>/dev/null

# 3. Register CoPaw skill
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
    fi
fi

echo ""
echo "✅ AutoCron installed!"
echo ""
echo "   To use now:  source $SHELL_RC"
echo "   Then:        copaw app"
echo ""
echo "   Or just open a new terminal and run: copaw app"
echo ""
