#!/usr/bin/env bash
set -euo pipefail

# Prevent ambient user/conda environment variables from contaminating the build
unset PYTHONPATH PYTHONHOME

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
DATA_HOME="${DATA_HOME%/}"
CONFIG_HOME="${CONFIG_HOME%/}"

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
        *)
            echo -e "\033[0;31m[ERROR]\033[0m Unknown option: $arg"
            echo "Run '$0 --help' for usage."
            exit 1
            ;;
    esac
done

if ! command -v python3 >/dev/null 2>&1; then
    echo -e "\033[0;31m[ERROR]\033[0m python3 is not installed on this system."
    exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "unknown")
    echo -e "\033[0;31m[ERROR]\033[0m Python 3.9 or newer is required (found Python ${PY_VER})."
    exit 1
fi

INIT_FILE="$SCRIPT_DIR/whipit/__init__.py"
NEW_VER=""
if [[ -f "$INIT_FILE" ]]; then
    NEW_VER=$(sed -nE 's/^__version__[[:space:]]*=[[:space:]]*["'"'"']([^"'"'"']+)["'"'"'].*/\1/p' "$INIT_FILE" 2>/dev/null || true)
fi

if [[ -z "$NEW_VER" ]]; then
    NEW_VER=$(SCRIPT_DIR="$SCRIPT_DIR" python3 - <<'PY' 2>/dev/null || echo "unknown"
import os, sys
sys.path.insert(0, os.environ["SCRIPT_DIR"])
import whipit; print(whipit.__version__)
PY
)
fi

if [[ -x "$VENV_DIR/bin/python" ]] && "$VENV_DIR/bin/python" -c "import sys" >/dev/null 2>&1; then
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
chmod 700 "$CONFIG_DIR"

if [[ ! -d "$VENV_DIR" || ! -x "$VENV_DIR/bin/python" ]] || ! "$VENV_DIR/bin/python" -c "import sys" >/dev/null 2>&1; then
    rm -rf "$VENV_DIR"
    echo -e "\033[0;36m[INFO]\033[0m Creating Python virtual environment in $VENV_DIR..."
    if ! python3 -m venv "$VENV_DIR"; then
        echo -e "\033[0;31m[ERROR]\033[0m Failed to create virtual environment."
        echo "If you are on Debian/Ubuntu/Mint, run: sudo apt install python3-venv"
        exit 1
    fi
fi

if ! "$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1; then
    echo -e "\033[0;36m[INFO]\033[0m Bootstrapping pip in virtual environment..."
    if ! "$VENV_DIR/bin/python" -m ensurepip --default-pip >/dev/null 2>&1; then
        echo -e "\033[0;31m[ERROR]\033[0m pip is not available in the virtual environment."
        echo "If you are on Debian/Ubuntu/Mint, run: sudo apt install python3-pip python3-venv"
        exit 1
    fi
fi

# Require a reviewed, hash-pinned dependency lock.
LOCK_FILE="$SCRIPT_DIR/requirements.lock"

if [[ ! -f "$LOCK_FILE" ]]; then
    echo -e "\033[0;31m[ERROR]\033[0m Missing requirements.lock; refusing an unpinned installation."
    exit 1
fi

echo -e "\033[0;36m[INFO]\033[0m Installing locked dependencies with hash verification..."
if ! "$VENV_DIR/bin/python" -m pip install \
    --upgrade \
    --require-hashes \
    --no-input \
    --disable-pip-version-check \
    -r "$LOCK_FILE"; then
    echo -e "\033[0;31m[ERROR]\033[0m Locked dependency installation failed."
    exit 1
fi

# Install the local application using locked build tools without resolving dependencies.
if ! "$VENV_DIR/bin/python" -m pip install \
    --upgrade \
    --no-deps \
    --no-build-isolation \
    --no-input \
    --disable-pip-version-check \
    ${EDITABLE_FLAG:+-e} \
    "$SCRIPT_DIR"; then
    echo -e "\033[0;31m[ERROR]\033[0m WhipIt installation failed."
    exit 1
fi

if [[ -d "$SCRIPT_DIR/.git" ]]; then
    if [[ -f "$CONFIG_FILE" ]]; then
        grep -v '^source_dir=' "$CONFIG_FILE" > "${CONFIG_FILE}.tmp" 2>/dev/null || true
        echo "source_dir=${SCRIPT_DIR}" >> "${CONFIG_FILE}.tmp"
        mv "${CONFIG_FILE}.tmp" "$CONFIG_FILE"
    else
        echo "source_dir=${SCRIPT_DIR}" > "$CONFIG_FILE"
    fi
fi

if [[ -f "$CONFIG_FILE" ]]; then
    chmod 600 "$CONFIG_FILE"
fi

rm -f "$BIN_DIR/whipit" "$BIN_DIR/whipit-gui"
ln -sf "$VENV_DIR/bin/whipit" "$BIN_DIR/whipit"

if "$VENV_DIR/bin/python" -c "from whipit.launcher import install_launcher; install_launcher()" >/dev/null 2>&1; then
    echo -e "\033[0;32m[OK]\033[0m Installed desktop launcher and application icon."
else
    echo -e "\033[0;33m[WARN]\033[0m Could not install desktop launcher automatically."
fi

INSTALLED_VER=$("$VENV_DIR/bin/python" -c "import whipit; print(whipit.__version__)" 2>/dev/null || echo "$NEW_VER")

echo -e "\033[0;32m[OK]\033[0m WhipIt is now installed and up to date (version: \033[1m${INSTALLED_VER}\033[0m)!"

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo -e "\n\033[1;33m[NOTE]\033[0m '$BIN_DIR' is not in your current PATH."
    echo -e "To launch 'whipit' directly from your terminal, add this to your ~/.bashrc or ~/.zshrc:"
    echo -e "  \033[1mexport PATH=\"\$HOME/.local/bin:\$PATH\"\033[0m\n"
fi
