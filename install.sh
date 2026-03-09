#!/bin/bash
# AutoCron Installer
# Usage: curl -fsSL https://raw.githubusercontent.com/miltosdoc/autocron/main/install.sh | bash

echo ""
echo "🤖 Installing AutoCron..."
echo ""

VENV_DIR="$HOME/.autocron-env"
REPO="https://github.com/miltosdoc/autocron.git"

# 1. Create or reuse venv
echo "📦 Setting up Python environment at $VENV_DIR ..."
python3 -m venv "$VENV_DIR" || { echo "❌ Failed to create venv. Is python3 installed?"; exit 1; }

# 2. Activate
echo "   Activating..."
. "$VENV_DIR/bin/activate" || { echo "❌ Failed to activate venv"; exit 1; }
echo "   Using python: $(which python)"
echo "   Using pip: $(which pip)"

# 3. Upgrade pip
echo "📥 Upgrading pip..."
pip install --upgrade pip 2>&1 | tail -1

# 4. Install AutoCron
echo "📥 Installing AutoCron from GitHub (this takes ~30 seconds)..."
pip install "autocron-agent @ git+$REPO" 2>&1 | tail -5
if [ $? -ne 0 ]; then
    echo "❌ Failed to install autocron-agent"
    exit 1
fi

# 5. Register CoPaw skill
echo ""
echo "🔧 Registering CoPaw skill..."
"$VENV_DIR/bin/autocron" install || { echo "❌ Failed to register skill"; exit 1; }

# 6. Add to PATH
AUTOCRON_BIN="$VENV_DIR/bin"
SHELL_RC=""
if [ -f "$HOME/.zshrc" ]; then
    SHELL_RC="$HOME/.zshrc"
elif [ -f "$HOME/.bashrc" ]; then
    SHELL_RC="$HOME/.bashrc"
fi

if [ -n "$SHELL_RC" ]; then
    if ! grep -q "autocron-env/bin" "$SHELL_RC" 2>/dev/null; then
        printf '\n# AutoCron\nexport PATH="%s:$PATH"\n' "$AUTOCRON_BIN" >> "$SHELL_RC"
        echo "🔗 Added to PATH in $SHELL_RC"
    else
        echo "🔗 Already in PATH"
    fi
fi

echo ""
echo "✅ AutoCron installed!"
echo ""
echo "   Open a new terminal, then run:  copaw app"
echo ""
