#!/usr/bin/env bash
# One-time setup for the standalone Tamadenshi laser control software.
#
#   ./install.sh              full install (system deps, venv, udev, shortcut)
#   ./install.sh --no-sudo    skip everything needing root (apt, udev rules)
#   ./install.sh --no-gui     driver + CLI only, skip matplotlib/pandas/tk
#
# Safe to re-run. Everything lands inside this directory (venv/, log/) except
# the udev rule and the desktop shortcut.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USE_SUDO=1
WITH_GUI=1
for arg in "$@"; do
    case "$arg" in
        --no-sudo) USE_SUDO=0 ;;
        --no-gui)  WITH_GUI=0 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

# Root cannot be asked for a password non-interactively in every environment,
# and running the whole install as root would leave a root-owned venv the
# normal user cannot update. Bail early rather than half-install.
if [[ $EUID -eq 0 && $USE_SUDO -eq 1 ]]; then
    echo "Do not run install.sh as root -- it will create a root-owned venv." >&2
    echo "Run it as your normal user; it calls sudo only where needed." >&2
    exit 1
fi

# udev, apt and .desktop files are all Linux-only. Without this check the
# script dies at step 3 on macOS with "cp: /etc/udev/rules.d: No such file or
# directory" -- after already having built a venv, so it fails halfway.
OS="$(uname -s)"
case "$OS" in
    Linux)  PLATFORM=linux ;;
    Darwin) PLATFORM=macos ;;
    *)      PLATFORM=other ;;
esac

echo "=================================================="
echo " Tamadenshi Laser Control -- standalone setup"
echo "=================================================="
echo " Platform          : $OS"
echo " Install directory : $HERE"
echo " GUI components    : $([[ $WITH_GUI -eq 1 ]] && echo yes || echo no)"
echo " Use sudo          : $([[ $USE_SUDO -eq 1 ]] && echo yes || echo no)"
echo

if [[ $PLATFORM != linux ]]; then
    echo "  NOTE: this software targets Linux. On $OS the Python parts install"
    echo "        and the driver may work, but USB permission setup and the"
    echo "        application-menu entry are Linux-specific and are skipped."
    echo
fi

# A venv inside a cloud-synced folder gets slow and uploads thousands of files.
case "$HERE" in
    *Dropbox*|*"Google Drive"*|*OneDrive*|*iCloud*)
        echo "  WARNING: this directory looks cloud-synced. The virtualenv"
        echo "           created here will be large and slow to sync. Consider"
        echo "           installing to a local path instead:"
        echo "               ./install.sh --dir ~/laser_control   (installer)"
        echo "           or copy this folder out of the synced directory first."
        echo
        ;;
esac

# --- 1. system packages ---------------------------------------------------
echo "[1/5] System packages..."
if [[ $USE_SUDO -eq 1 && $PLATFORM == linux ]] && command -v apt-get >/dev/null 2>&1; then
    PKGS=(python3 python3-venv python3-pip
          libhidapi-hidraw0 libhidapi-libusb0 libudev-dev libusb-1.0-0-dev)
    # tkinter is a system package, never a pip one -- installing it here is the
    # difference between the GUI starting and dying on `import tkinter`.
    [[ $WITH_GUI -eq 1 ]] && PKGS+=(python3-tk)
    sudo apt-get update -qq
    sudo apt-get install -y "${PKGS[@]}"
    echo "      done."
elif [[ $PLATFORM == macos ]]; then
    if command -v brew >/dev/null 2>&1; then
        echo "      Homebrew found -- installing hidapi."
        brew list hidapi >/dev/null 2>&1 || brew install hidapi
        [[ $WITH_GUI -eq 1 ]] && { brew list python-tk >/dev/null 2>&1 || \
            echo "      For the GUI you may also need: brew install python-tk"; }
        echo "      done."
    else
        echo "      skipped -- Homebrew not found."
        echo "      Install it from https://brew.sh then: brew install hidapi"
    fi
else
    echo "      skipped (no sudo, or non-apt distro)."
    echo "      Ensure these exist: python3, python3-venv, libhidapi, libudev"
    [[ $WITH_GUI -eq 1 ]] && echo "      ...and python3-tk for the GUI."
fi
echo

# --- 2. python environment ------------------------------------------------
echo "[2/5] Python virtualenv in $HERE/venv ..."
python3 -m venv "$HERE/venv"
"$HERE/venv/bin/pip" install --quiet --upgrade pip
if [[ $WITH_GUI -eq 1 ]]; then
    "$HERE/venv/bin/pip" install --quiet -r "$HERE/requirements.txt"
else
    "$HERE/venv/bin/pip" install --quiet hidapi
fi
echo "      done."
echo

# --- 3. udev rules --------------------------------------------------------
echo "[3/5] USB permissions (udev)..."
if [[ $PLATFORM != linux ]]; then
    # macOS grants HID access to user processes directly; there is no udev.
    echo "      skipped -- udev is Linux-only ($OS needs no equivalent step)."
elif [[ $USE_SUDO -eq 1 ]]; then
    sudo cp "$HERE/99-tamadenshi.rules" /etc/udev/rules.d/
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    echo "      done."
else
    echo "      skipped -- install 99-tamadenshi.rules manually or run as root."
fi
echo

# --- 4. launchers ---------------------------------------------------------
echo "[4/5] Launchers..."
chmod +x "$HERE/run_gui.sh" "$HERE/laser_cli.py" "$HERE/laser_gui.py" 2>/dev/null || true
if [[ $WITH_GUI -eq 1 && $PLATFORM == linux ]]; then
    DESKTOP_DIR="$HOME/.local/share/applications"
    mkdir -p "$DESKTOP_DIR"
    cat > "$DESKTOP_DIR/laser-control.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Laser Control
Comment=Tamadenshi LD board control
Exec=$HERE/run_gui.sh
Path=$HERE
Terminal=false
Categories=Science;Utility;
EOF
    chmod +x "$DESKTOP_DIR/laser-control.desktop"
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    echo "      Application menu entry: 'Laser Control'"
fi
echo "      Shell:  $HERE/run_gui.sh"
echo

# --- 5. smoke test --------------------------------------------------------
echo "[5/5] Looking for attached boards..."
"$HERE/venv/bin/python" "$HERE/laser_cli.py" list || true
echo

echo "=================================================="
echo " Setup complete"
echo "=================================================="
echo
if [[ $PLATFORM == linux ]]; then
    echo " IMPORTANT: unplug and replug the laser's USB cable now, so the new"
    echo "            udev rule applies to it."
    echo
fi
[[ $WITH_GUI -eq 1 ]] && echo " Start the GUI :  $HERE/run_gui.sh"
echo " Command line  :  $HERE/venv/bin/python $HERE/laser_cli.py status"
echo
echo " Logs go to    :  ${LASER_LOG_DIR:-$HERE/log}"
echo " Change with   :  export LASER_LOG_DIR=/your/path"
