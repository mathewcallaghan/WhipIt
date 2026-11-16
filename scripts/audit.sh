#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT" || exit 1

STRICT=false
for arg in "$@"; do
    case "$arg" in
        --strict)
            STRICT=true
            ;;
        -h|--help)
            echo "Usage: $0 [--strict]"
            echo ""
            echo "Runs WhipIt static analysis, security scanning, and branch-coverage tests."
            echo "  --strict    Fail if optional audit tools (e.g. pip-audit, shellcheck, pytest-cov) are missing"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 1
            ;;
    esac
done

VENV_DIR="${WHIPIT_VENV_DIR:-$HOME/.local/share/whipit/venv}"
if [[ -f "$VENV_DIR/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
fi

FAILURES=0

run_check() {
    local label="$1"
    shift
    echo -e "\n\033[1;36m==>\033[0m \033[1m${label}\033[0m"
    if "$@"; then
        echo -e "\033[0;32m[PASS]\033[0m ${label}"
    else
        echo -e "\033[0;31m[FAIL]\033[0m ${label}" >&2
        FAILURES=$((FAILURES + 1))
    fi
}

require_or_skip() {
    local cmd="$1"
    local label="$2"
    shift 2
    if command -v "$cmd" >/dev/null 2>&1; then
        run_check "$label" "$@"
    elif [[ "$STRICT" == true ]]; then
        echo -e "\n\033[0;31m[FAIL]\033[0m ${label}: required tool '${cmd}' not found (--strict)." >&2
        FAILURES=$((FAILURES + 1))
    else
        echo -e "\n\033[0;33m[SKIP]\033[0m ${label}: '${cmd}' not installed."
    fi
}

# 1. Ruff Linter
require_or_skip ruff "Ruff Check (Base)" ruff check --config pyproject.toml .
require_or_skip ruff "Ruff Check (Extended)" \
    ruff check --config pyproject.toml --extend-select B,DTZ,S,RUF006,RUF012,RUF015 --ignore B027,B905,S101,S110,S310,S404,S603,S606,S607 whipit

# 2. Mypy Static Typing
require_or_skip mypy "Mypy Static Type Check" mypy whipit/services whipit/models.py

# 3. Bandit Security Scan
require_or_skip bandit "Bandit Security Scan" bandit -r whipit -x whipit/tests -ll -q

# 4. Pytest & Branch Coverage
if python3 -c "import pytest_cov" >/dev/null 2>&1; then
    run_check "Pytest & Branch Coverage" ./scripts/test.sh --cov=whipit --cov-branch --cov-report=term-missing
elif [[ "$STRICT" == true ]]; then
    echo -e "\n\033[0;31m[FAIL]\033[0m Pytest & Branch Coverage: 'pytest-cov' not installed (--strict)." >&2
    FAILURES=$((FAILURES + 1))
else
    echo -e "\n\033[0;33m[WARN]\033[0m 'pytest-cov' not installed (run ./install.sh --dev for coverage); running tests without coverage."
    run_check "Pytest" ./scripts/test.sh
fi

# 5. ShellCheck
require_or_skip shellcheck "ShellCheck" shellcheck install.sh uninstall.sh scripts/test.sh scripts/audit.sh

# 6. Pip-Audit
require_or_skip pip-audit "Pip-Audit (requirements.lock)" pip-audit -r requirements.lock

echo ""
if [[ "$FAILURES" -gt 0 ]]; then
    echo -e "\033[0;31m[AUDIT FAILED]\033[0m ${FAILURES} audit check(s) failed." >&2
    exit 1
fi

echo -e "\033[0;32m[AUDIT PASSED]\033[0m All executed checks succeeded."
exit 0
