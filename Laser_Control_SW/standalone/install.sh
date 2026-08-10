#!/usr/bin/env bash
# One-time setup for the standalone Tamadenshi laser driver on a fresh Linux PC.
#
#   ./install.sh          # system packages + venv + udev rules
#   ./install.sh --no-sudo   # skip anything needing root (udev, apt)
#
# Everything lands inside this directory (venv/, log/); nothing is written to
# your home directory or anywhere else on the system except the udev rule.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USE_SUDO=1
[[ "${1:-}" == "--no-sudo" ]] && USE_SUDO=0

echo "=== Tamadenshi laser driver -- standalone setup ==="
echo "Install directory: $HERE"
echo

# --- 1. system packages ---------------------------------------------------
if [[ $USE_SUDO -eq 1 ]] && command -v apt-get >/dev/null 2>&1; then
    echo "[1/4] Installing system packages (python3, venv, libusb, libudev)..."
    sudo apt-get update -qq
    # libhidapi-* lets pip use a prebuilt backend; libudev/libusb dev headers
    # are needed if pip has to compile hidapi from source instead.
    sudo apt-get install -y python3 python3-venv python3-pip \
        libhidapi-hidraw0 libhidapi-libusb0 libudev-dev libusb-1.0-0-dev
else
    echo "[1/4] Skipping system packages (no sudo or non-apt distro)."
    echo "      Ensure these exist: python3, python3-venv, libhidapi, libudev."
fi
echo

# --- 2. python environment ------------------------------------------------
echo "[2/4] Creating virtualenv in $HERE/venv ..."
python3 -m venv "$HERE/venv"
"$HERE/venv/bin/pip" install --quiet --upgrade pip
"$HERE/venv/bin/pip" install --quiet -r "$HERE/requirements.txt"
echo "      done."
echo

# --- 3. udev rules --------------------------------------------------------
if [[ $USE_SUDO -eq 1 ]]; then
    echo "[3/4] Installing udev rules (USB access without root)..."
    sudo cp "$HERE/99-tamadenshi.rules" /etc/udev/rules.d/
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    echo "      done."
else
    echo "[3/4] Skipping udev rules -- you will need to run as root, or install"
    echo "      99-tamadenshi.rules manually. See the file's header."
fi
echo

# --- 4. smoke test --------------------------------------------------------
echo "[4/4] Looking for attached boards..."
"$HERE/venv/bin/python" "$HERE/laser_cli.py" list || true
echo
echo "=== Setup complete ==="
echo
echo "IMPORTANT: unplug and replug the laser's USB cable now, so the new udev"
echo "rule applies to it. Then:"
echo
echo "    $HERE/venv/bin/python $HERE/laser_cli.py status"
echo
echo "Logs are written to: ${LASER_LOG_DIR:-$HERE/log}"
echo "Change that anytime with:  export LASER_LOG_DIR=/your/path"
