#!/usr/bin/env bash
# One-command Linux launcher: installs the distro wxPython/WebKitGTK
# packages if missing, sets up a venv, installs sdrsync into it, and
# launches the GUI. See README.md's "Install" section for the manual
# steps this automates.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"

manual_install_message() {
    echo "Install wxPython + WebKitGTK 4.1 manually, then re-run this script." >&2
    echo "See README.md's \"Install\" section for your distro's equivalent packages." >&2
}

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found. Install it first (e.g. sudo apt install python3)." >&2
    exit 1
fi

if ! python3 -c "import wx.html2" >/dev/null 2>&1; then
    echo "wxPython (with its WebView module) not found in the system Python."
    echo "sdrsync needs the distro's wxPython + WebView packages -- PyPI has no Linux wheels for either."

    DISTRO_ID=""
    DISTRO_ID_LIKE=""
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        DISTRO_ID="${ID:-}"
        DISTRO_ID_LIKE="${ID_LIKE:-}"
    fi

    INSTALL_CMD=()
    CONFIDENCE=""
    if [[ "$DISTRO_ID $DISTRO_ID_LIKE" =~ (debian|ubuntu) ]]; then
        CONFIDENCE="confirmed by this project's own testing (Debian 12)"
        INSTALL_CMD=(sudo apt install -y python3-wxgtk4.0 python3-wxgtk-webview4.0 libwebkit2gtk-4.1-0)
    elif [[ "$DISTRO_ID $DISTRO_ID_LIKE" =~ fedora ]]; then
        CONFIDENCE="best-effort -- NOT live-tested by this project"
        INSTALL_CMD=(sudo dnf install -y python3-wxpython4 python3-wxpython4-webview)
    elif [[ "$DISTRO_ID $DISTRO_ID_LIKE" =~ arch ]]; then
        CONFIDENCE="best-effort -- NOT live-tested by this project"
        INSTALL_CMD=(sudo pacman -S --needed python-wxpython webkit2gtk-4.1)
    else
        echo "Distro '${DISTRO_ID:-unknown}' has no known package mapping in this script." >&2
        manual_install_message
        exit 1
    fi

    echo "Detected distro: ${DISTRO_ID:-unknown} -- package set is $CONFIDENCE."
    echo "About to run: ${INSTALL_CMD[*]}"
    read -r -p "Proceed? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        "${INSTALL_CMD[@]}"
    else
        manual_install_message
        exit 1
    fi

    if ! python3 -c "import wx.html2" >/dev/null 2>&1; then
        echo "wxPython's WebView module still not importable after install." >&2
        manual_install_message
        exit 1
    fi
fi

if [[ ! -d "$VENV_DIR" ]]; then
    echo "Creating virtualenv at $VENV_DIR (with access to system site-packages for wxPython)..."
    python3 -m venv --system-site-packages "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

pip install -q -e "$SCRIPT_DIR"

exec python -m sdrsync.main "$@"
