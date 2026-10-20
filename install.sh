#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"

APP_DIR="$DATA_HOME/whipit"
VENV_DIR="$APP_DIR/venv"
BIN_DIR="$HOME/.local/bin"
CONFIG_DIR="$CONFIG_HOME/whipit"
CONFIG_FILE="$CONFIG_DIR/config"

EDITABLE_FLAG=""
for arg in "$@"; do
    case "$arg" in
        -e|--editable)
            EDITABLE_FLAG="-e"
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Installs or updates WhipIt in ~/.local/share/whipit/venv"
            echo ""
            echo "Options:"
            echo "  -e, --editable    Install in editable mode (pip install -e .) for development"
            echo "  -h, --help        Show this help message"
            exit 0
            ;;
    esac
done

# 1. Reliable Version Extraction
INIT_FILE="$SCRIPT_DIR/whipit/__init__.py"
NEW_VER=""
if [[ -f "$INIT_FILE" ]]; then
    NEW_VER=$(sed -nE 's/^__version__[[:space:]]*=[[:space:]]*["'"'"']([^"'"'"']+)["'"'"'].*/\1/p' "$INIT_FILE" 2>/dev/null || true)
fi

if [[ -z "$NEW_VER" ]]; then
    NEW_VER=$(python3 -c "import sys; sys.path.insert(0, '$SCRIPT_DIR'); import whipit; print(whipit.__version__)" 2>/dev/null || echo "unknown")
fi

# 2. Check Python Environment
if ! command -v python3 >/dev/null 2>&1; then
    echo -e "\033[0;31m[ERROR]\033[0m python3 is not installed on this system."
    exit 1
fi

# 3. Detect Previous Version & Announce
if [[ -x "$VENV_DIR/bin/python" ]]; then
    OLD_VER=$("$VENV_DIR/bin/python" -c "import whipit; print(whipit.__version__)" 2>/dev/null || echo "unknown")
    if [[ "$OLD_VER" != "unknown" && "$OLD_VER" != "$NEW_VER" ]]; then
        echo -e "\033[1;36m[INFO]\033[0m Updating WhipIt: \033[1m${OLD_VER}\033[0m -> \033[1;32m${NEW_VER}\033[0m..."
    elif [[ "$OLD_VER" == "$NEW_VER" ]]; then
        echo -e "\033[1;36m[INFO]\033[0m Reinstalling/refreshing WhipIt \033[1;32m${NEW_VER}\033[0m..."
    else
        echo -e "\033[1;36m[INFO]\033[0m Installing WhipIt \033[1;32m${NEW_VER}\033[0m..."
    fi
else
    echo -e "\033[1;36m[INFO]\033[0m Installing WhipIt \033[1;32m${NEW_VER}\033[0m..."
fi

mkdir -p "$APP_DIR" "$BIN_DIR" "$CONFIG_DIR"

# 4. Create or Repair Virtual Environment
if [[ ! -d "$VENV_DIR" || ! -x "$VENV_DIR/bin/python" ]]; then
    rm -rf "$VENV_DIR"
    echo -e "\033[0;36m[INFO]\033[0m Creating Python virtual environment in $VENV_DIR..."
    if ! python3 -m venv "$VENV_DIR"; then
        echo -e "\033[0;31m[ERROR]\033[0m Failed to create virtual environment."
        echo "If you are on Debian/Ubuntu/Mint, run: sudo apt install python3-venv"
        exit 1
    fi
fi

# 5. Upgrade pip and install package
"$VENV_DIR/bin/pip" install --upgrade pip --quiet

if [[ -n "$EDITABLE_FLAG" ]]; then
    echo -e "\033[0;36m[INFO]\033[0m Installing in editable development mode..."
    "$VENV_DIR/bin/pip" install --upgrade -e "${SCRIPT_DIR}" --quiet
else
    "$VENV_DIR/bin/pip" install --upgrade "${SCRIPT_DIR}" --quiet
fi

# 6. Configure source_dir for Git Clones (for GUI Check for Updates)
if [[ -d "$SCRIPT_DIR/.git" ]]; then
    if [[ -f "$CONFIG_FILE" ]]; then
        grep -v '^source_dir=' "$CONFIG_FILE" > "${CONFIG_FILE}.tmp" 2>/dev/null || true
        echo "source_dir=${SCRIPT_DIR}" >> "${CONFIG_FILE}.tmp"
        mv "${CONFIG_FILE}.tmp" "$CONFIG_FILE"
    else
        echo "source_dir=${SCRIPT_DIR}" > "$CONFIG_FILE"
    fi
fi

# 7. Symlink binary and register Desktop Entry
rm -f "$BIN_DIR/whipit-gui"
ln -sf "$VENV_DIR/bin/whipit" "$BIN_DIR/whipit"

# Install application launcher and SVG icon
"$VENV_DIR/bin/python" -c "from whipit.launcher import install_launcher; install_launcher()" >/dev/null 2>&1 || true

INSTALLED_VER=$("$VENV_DIR/bin/python" -c "import whipit; print(whipit.__version__)" 2>/dev/null || echo "$NEW_VER")

echo -e "\033[0;32m[OK]\033[0m WhipIt is now installed and up to date (version: \033[1m${INSTALLED_VER}\033[0m)!"

# 8. Check PATH
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo -e "\n\033[1;33m[NOTE]\033[0m '$BIN_DIR' is not in your current PATH."
    echo -e "To launch 'whipit' directly from your terminal, add this to your ~/.bashrc or ~/.zshrc:"
    echo -e "  \033[1mexport PATH=\"\$HOME/.local/bin:\$PATH\"\033[0m\n"
fi
