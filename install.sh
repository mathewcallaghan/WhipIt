#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/whipit"
VENV_DIR="$APP_DIR/venv"
BIN_DIR="$HOME/.local/bin"

NEW_VER=$(python3 -c "import sys; sys.path.insert(0, '$SCRIPT_DIR'); import whipit; print(whipit.__version__)" 2>/dev/null || echo "unknown")

if [[ -x "$VENV_DIR/bin/whipit" ]]; then
    OLD_VER=$("$VENV_DIR/bin/python" -c "import whipit; print(whipit.__version__)" 2>/dev/null || echo "unknown")
    echo -e "\033[1;36m[INFO]\033[0m Updating whipit: \033[1m${OLD_VER}\033[0m -> \033[1;32m${NEW_VER}\033[0m..."
else
    echo -e "\033[1;36m[INFO]\033[0m Installing whipit \033[1;32m${NEW_VER}\033[0m..."
fi

mkdir -p "$APP_DIR" "$BIN_DIR"
if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install --upgrade "${SCRIPT_DIR}" --quiet

rm -f "$BIN_DIR/whipit-gui"
ln -sf "$VENV_DIR/bin/whipit" "$BIN_DIR/whipit"
"$VENV_DIR/bin/python" -c "from whipit.launcher import install_launcher; install_launcher()" >/dev/null 2>&1 || true

echo -e "\033[0;32m[OK]\033[0m whipit is now up to date (version: \033[1m${NEW_VER}\033[0m)!"
