#!/usr/bin/env bash
# Run MMD Character Importer from source on Linux (issue #152).
#
# Usage:
#   bash run_linux.sh
#
# What it does:
#   1. Creates a Python virtual environment in .venv (first run only).
#   2. Installs/verifies the runtime dependencies from requirements.txt.
#   3. Launches the GUI. On first run the app downloads and manages the official
#      Blender 4.5.10 Linux build (~360 MB) under ~/.MMDCharacterImporter.
#
# Notes:
#   - StudioMDL, gmad and VTFCmd are Windows programs; the Blender-side steps run
#     natively, but the final compile/packaging steps need those tools (see the
#     "Run on Linux" section of README.md).

set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "ERROR: python3 was not found. Install Python 3.12 first (Debian/Ubuntu: sudo apt install python3 python3-venv)." >&2
    exit 1
fi

# numpy 2.4 (pinned in requirements.txt) needs Python 3.11+; 3.12 is what the
# project is developed against.
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "ERROR: Python 3.11 or newer is required (3.12 recommended). Found: $("$PYTHON" --version 2>&1)" >&2
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "Creating virtual environment in .venv ..."
    if ! "$PYTHON" -m venv .venv; then
        echo "ERROR: could not create the virtual environment. On Debian/Ubuntu install venv support with: sudo apt install python3-venv" >&2
        exit 1
    fi
fi

echo "Installing/verifying Python dependencies ..."
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

if ! .venv/bin/python -c "from PySide6 import QtWidgets" 2> .venv/qt_check.log; then
    echo "ERROR: PySide6 (Qt) could not load. On Debian/Ubuntu the usual fix is:" >&2
    echo "  sudo apt install libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1 libglib2.0-0" >&2
    echo "Details:" >&2
    cat .venv/qt_check.log >&2
    exit 1
fi

# --- Wine / Proton detection -------------------------------------------------
# StudioMDL, gmad and VTFCmd are Windows programs, so the final compile and
# packaging steps need Wine or Proton on Linux. Everything up to and including
# the Blender steps runs natively either way.

WINE_CMD=""
PROTON_DIR=""

detect_wine() {
    local cmd
    for cmd in wine wine64; do
        if command -v "$cmd" >/dev/null 2>&1; then
            WINE_CMD="$cmd"
            return 0
        fi
    done
    return 1
}

detect_proton() {
    local root lib match vdf
    local roots=(
        "$HOME/.steam/steam"
        "$HOME/.steam/root"
        "$HOME/.local/share/Steam"
        "$HOME/.var/app/com.valvesoftware.Steam/.local/share/Steam"
    )
    for root in "${roots[@]}"; do
        [ -d "$root" ] || continue
        local lib_dirs=("$root")
        vdf="$root/steamapps/libraryfolders.vdf"
        if [ -f "$vdf" ]; then
            while IFS= read -r lib; do
                [ -d "$lib" ] && lib_dirs+=("$lib")
            done < <(sed -n 's/.*"path"[[:space:]]*"\(.*\)".*/\1/p' "$vdf")
        fi
        for lib in "${lib_dirs[@]}"; do
            for match in "$lib/steamapps/common/Proton"*; do
                if [ -d "$match" ]; then
                    PROTON_DIR="$match"
                    return 0
                fi
            done
        done
        # GE-Proton and other custom builds live in compatibilitytools.d
        for match in "$root/compatibilitytools.d/"*; do
            if [ -d "$match" ] && [[ "$(basename "$match")" == *[Pp]roton* ]]; then
                PROTON_DIR="$match"
                return 0
            fi
        done
    done
    return 1
}

wine_found=0
proton_found=0
detect_wine && wine_found=1
detect_proton && proton_found=1

if [ "$wine_found" -eq 1 ]; then
    echo "Found Wine: $WINE_CMD ($("$WINE_CMD" --version 2>/dev/null || echo "version unknown"))"
fi
if [ "$proton_found" -eq 1 ]; then
    echo "Found Proton: $PROTON_DIR"
fi
if [ "$wine_found" -eq 0 ] && [ "$proton_found" -eq 0 ]; then
    echo ""
    echo "WARNING: neither Wine nor Proton was found on this machine."
    echo ""
    echo "The Blender-side steps run natively, but the final compile/packaging"
    echo "steps use Windows programs (StudioMDL, gmad, VTFCmd) and need Wine or"
    echo "Proton to run them on Linux. To install one of them:"
    echo ""
    echo "  Wine:    Debian/Ubuntu: sudo apt install wine"
    echo "           Fedora:        sudo dnf install wine"
    echo "           Arch:          sudo pacman -S wine"
    echo "  Proton:  install Steam, then in Steam enable Proton under"
    echo "           Steam > Settings > Compatibility (or install any Proton"
    echo "           version from Library > Tools)."
    echo ""
    echo "You can keep working without them and install later; see the"
    echo "\"Run on Linux\" section of README.md for how the STUDIOMDL and"
    echo "VTFCMD environment variables tie in."
    if [ -t 0 ]; then
        printf "Continue without Wine/Proton? [Y/n] "
        read -r reply || reply=""
        case "$reply" in
            [Nn]*)
                echo "Install Wine or Proton, then run run_linux.sh again."
                exit 0
                ;;
        esac
    fi
    echo ""
fi
# -----------------------------------------------------------------------------

echo "Starting MMD Character Importer ..."
status=0
.venv/bin/python tools/mmd_character_importer_gui.py "$@" || status=$?
if [ "$status" -ne 0 ]; then
    echo "" >&2
    echo "MMD Character Importer exited with an error (code $status)." >&2
    echo "If the message above mentions the Qt 'xcb' platform plugin, install the" >&2
    echo "Qt system libraries (Debian/Ubuntu):" >&2
    echo "  sudo apt install libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1" >&2
    exit "$status"
fi
