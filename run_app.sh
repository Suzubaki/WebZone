#!/bin/bash
set -e

export PATH="/usr/local/bin:$HOME/.local/bin:/root/.local/bin:$PATH"
export PIP_BREAK_SYSTEM_PACKAGES=1

# Clean up any dangling symlinks left over from previous partial runs
for pbin in /usr/local/bin/pip /usr/local/bin/pip3; do
    if [ -L "$pbin" ] && [ ! -e "$pbin" ]; then
        rm -f "$pbin" 2>/dev/null || true
    fi
done

# Auto-link pip/pip3 if installed in ~/.local/bin
for pdir in "$HOME/.local/bin" "/root/.local/bin"; do
    if [ -f "$pdir/pip3" ] && [ ! -e /usr/local/bin/pip3 ] && [ "$(id -u)" -eq 0 ]; then
        ln -sf "$pdir/pip3" /usr/local/bin/pip3 2>/dev/null || true
        ln -sf "$pdir/pip" /usr/local/bin/pip 2>/dev/null || true
    fi
done

PIP_ARGS=""
if ls /usr/lib/python3*/EXTERNALLY-MANAGED >/dev/null 2>&1; then
    PIP_ARGS="--break-system-packages"
fi

USER_FLAG=""
if [ "$(id -u)" -ne 0 ]; then
    USER_FLAG="--user"
fi

# Auto-install dependencies if running in a fresh container
if ! python3 -c "import flask, flask_wtf, openpyxl" 2>/dev/null; then
    echo "[AI Studio] Installing Python dependencies..."
    if ! command -v pip3 &>/dev/null && ! command -v pip &>/dev/null && ! python3 -m pip --version &>/dev/null; then
        if [ -f "get-pip.py" ]; then
            python3 get-pip.py $USER_FLAG $PIP_ARGS
        fi
    fi

    if python3 -m pip --version &>/dev/null; then
        python3 -m pip install $USER_FLAG $PIP_ARGS -r requirements.txt
    elif command -v pip3 &>/dev/null; then
        pip3 install $USER_FLAG $PIP_ARGS -r requirements.txt
    elif [ -x "$HOME/.local/bin/pip3" ]; then
        "$HOME/.local/bin/pip3" install $USER_FLAG $PIP_ARGS -r requirements.txt
    fi
fi

TARGET_PORT="${PORT:-${DEFAULT_APP_PORT:-3000}}"
echo "[AI Studio] Starting Python Flask application on port ${TARGET_PORT}..."
exec python3 app.py
