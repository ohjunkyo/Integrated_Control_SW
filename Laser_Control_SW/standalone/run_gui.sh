#!/usr/bin/env bash
# Launch the laser GUI using this directory's virtualenv.
# Created so the desktop shortcut (and users who never touch a terminal) do not
# have to know about activating a venv.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -x "$HERE/venv/bin/python" ]]; then
    PY="$HERE/venv/bin/python"
else
    echo "No virtualenv found -- falling back to system python3." >&2
    echo "Run ./install.sh first if imports fail." >&2
    PY=python3
fi

exec "$PY" "$HERE/laser_gui.py" "$@"
