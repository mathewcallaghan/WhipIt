#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT" || exit 1

# 1. Auto-detect WhipIt's isolated virtual environment
VENV_DIR="${WHIPIT_VENV_DIR:-$HOME/.local/share/whipit/venv}"
if [[ -x "$VENV_DIR/bin/pytest" ]]; then
    PYTEST_EXEC="$VENV_DIR/bin/pytest"
elif command -v pytest >/dev/null 2>&1; then
    PYTEST_EXEC="$(command -v pytest)"
else
    echo "Error: pytest not found in $VENV_DIR or system PATH." >&2
    echo "Run ./install.sh --dev or activate your virtual environment." >&2
    exit 1
fi

# 2. Export offscreen platform for Qt GUI tests and execute pytest
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
exec "$PYTEST_EXEC" whipit/tests/ "$@"
