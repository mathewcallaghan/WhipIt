#!/usr/bin/env bash
set -euo pipefail

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"

APP_DIR="$DATA_HOME/whipit"
BIN_LINK="$HOME/.local/bin/whipit"
LEGACY_GUI_LINK="$HOME/.local/bin/whipit-gui"
DESKTOP_FILE="$DATA_HOME/applications/whipit.desktop"

CONFIG_DIR="$CONFIG_HOME/whipit"
CACHE_DIR="$CACHE_HOME/whipit"
STATE_DIR="$STATE_HOME/whipit"

PURGE=false
YES=false

for arg in "$@"; do
    case "$arg" in
        --purge|-p)
            PURGE=true
            ;;
        -y|--yes)
            YES=true
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Complete uninstaller for whipit."
            echo ""
            echo "Options:"
            echo "  --purge, -p    Purge all configuration, drive offsets, cache, and logs without prompting"
            echo "  -y, --yes      Do not ask for confirmation before removing application files"
            echo "  -h, --help     Show this help message"
            exit 0
            ;;
        *)
            echo -e "\033[0;31m[ERROR]\033[0m Unknown option: $arg"
            echo "Run '$0 --help' for usage."
            exit 1
            ;;
    esac
done

echo -e "\n\033[1m=== whipit Uninstallation ===\033[0m\n"

if [[ "$YES" != true && -t 0 ]]; then
    read -r -p "Are you sure you want to uninstall whipit? [y/N]: " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
fi

# 1. Remove Desktop Application Launcher
if [[ -f "$DESKTOP_FILE" ]]; then
    rm -f "$DESKTOP_FILE"
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$DATA_HOME/applications" >/dev/null 2>&1 || true
    fi
    echo -e "\033[0;32m[OK]\033[0m Removed desktop launcher: $DESKTOP_FILE"
else
    echo -e "\033[0;36m[INFO]\033[0m No desktop launcher found."
fi

ICON_FILE="$DATA_HOME/icons/hicolor/scalable/apps/whipit.svg"
if [[ -f "$ICON_FILE" ]]; then
    rm -f "$ICON_FILE"
    echo -e "\033[0;32m[OK]\033[0m Removed application icon: $ICON_FILE"
fi

# 2. Remove ~/.local/bin/whipit symlink
for link in "$BIN_LINK" "$LEGACY_GUI_LINK"; do
    if [[ -L "$link" && "$(readlink -f "$link")" == "$APP_DIR"/* ]]; then
        rm -f "$link"
        echo -e "\033[0;32m[OK]\033[0m Removed command: $link"
    fi
done

# 3. Remove application virtual environment (~/.local/share/whipit)
if [[ -d "$APP_DIR" ]]; then
    rm -rf "$APP_DIR"
    echo -e "\033[0;32m[OK]\033[0m Removed application directory: $APP_DIR"
else
    echo -e "\033[0;36m[INFO]\033[0m No application directory found at $APP_DIR."
fi

# 4. Remove pipx package if installed via pipx
if command -v pipx >/dev/null 2>&1; then
    if pipx list 2>/dev/null | grep -q 'package whipit'; then
        pipx uninstall whipit >/dev/null 2>&1 || true
        echo -e "\033[0;32m[OK]\033[0m Removed pipx installation."
    fi
fi

# 5. Handle user data purge (config, cache, logs)
SHOULD_PURGE="$PURGE"
if [[ "$PURGE" != true && -t 0 ]]; then
    echo ""
    echo "Application data directories found:"
    echo "  * Configuration & Offsets : $CONFIG_DIR"
    echo "  * Cache Staging           : $CACHE_DIR"
    echo "  * Logs & Job Manifests    : $STATE_DIR"
    read -r -p "Do you want to purge all configuration, cache, and logs? [y/N]: " purge_confirm
    if [[ "$purge_confirm" =~ ^[Yy]$ ]]; then
        SHOULD_PURGE=true
    fi
fi

if [[ "$SHOULD_PURGE" == true ]]; then
    rm -rf "$CONFIG_DIR" "$CACHE_DIR" "$STATE_DIR"
    echo -e "\033[0;32m[OK]\033[0m Purged application data and logs."
else
    echo -e "\033[0;36m[INFO]\033[0m Application data preserved."
fi

echo -e "\n\033[0;32m[OK]\033[0m whipit has been successfully uninstalled!\n"
