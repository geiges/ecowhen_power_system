#!/bin/bash
# One-time setup on the Venus OS box. Run ON THE DEVICE, from the project dir.
#
#   ./deploy/install.sh [--boot]
#
#   --boot   also add a launch line to /data/rc.local, which Venus OS keeps
#            across firmware updates. Leave it off while validating.
#
# The venv is built against the SYSTEM python with --system-site-packages, because
# pydbus needs the OS gobject bindings and there is no working PyPI substitute for
# them. That is the same recipe the predecessor uses, so it is known to work here.
set -eu
cd "$(dirname "$0")/.."
. deploy/config.sh

BOOT=0
[ "${1:-}" = "--boot" ] && BOOT=1

# --- uv ---------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv not found. Install it with:" >&2
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

# --- the venv ---------------------------------------------------------------
SYS_PYTHON="${SYS_PYTHON:-/usr/bin/python3}"
echo "--- system python: $($SYS_PYTHON -V 2>&1) at $SYS_PYTHON ---"

if [ ! -d .venv ]; then
    echo "--- creating .venv (system site packages, for pydbus) ---"
    uv venv --system-site-packages --python "$SYS_PYTHON"
fi

# --extra device is what installs pydbus. It is an *optional* extra in this package
# so that the tests install and run off-device (A3), which means a plain `uv sync`
# leaves it out — unlike the predecessor, where pydbus is a hard dependency and
# arrives without being asked for.
echo "--- syncing dependencies (with the device extra) ---"
uv sync --no-dev --extra device

# `uv sync` will silently DESTROY and rebuild .venv on a different interpreter if
# anything tells it to prefer one — a `.python-version` file is enough — and the
# rebuilt venv loses --system-site-packages, which is the only route to `gi`. That
# exact sequence cost an afternoon, and it leaves no trace except a venv that cannot
# import gi. So: check that the venv survived the sync, and say what happened if not.
if ! grep -q "include-system-site-packages = true" .venv/pyvenv.cfg 2>/dev/null; then
    echo
    echo "ERROR: uv rebuilt .venv without --system-site-packages during the sync."
    echo "       venv python: $(.venv/bin/python -V 2>&1)"
    echo "       system python: $($SYS_PYTHON -V 2>&1)"
    echo "       Something is asking uv for a different interpreter than $SYS_PYTHON."
    echo "       Check for a .python-version file (there must not be one) and for"
    echo "       UV_PYTHON / UV_PROJECT_ENVIRONMENT in the environment, then:"
    echo "         rm -rf .venv && ./deploy/install.sh"
    echo "       ./deploy/doctor.sh prints everything needed to tell which it is."
    exit 1
fi

# Two different things have to line up, and they fail in different ways:
#   pydbus  — pure Python, from PyPI, installed into the venv by the sync above
#   gi      — PyGObject, compiled, from the OS, visible only via --system-site-packages
# `import pydbus` pulls in `gi`, so check them separately or the message is useless.
echo "--- checking the bus bindings ---"
if err="$(uv run --no-dev --extra device python -c 'import gi' 2>&1)"; then
    echo "    gi (PyGObject, from the OS) OK"
else
    echo "    ERROR: $err"
    echo "    The venv cannot see the OS gobject bindings. Either PyGObject is not"
    echo "    installed on this box, or .venv was built without --system-site-packages."
    echo "      opkg install python3-pygobject"
    echo "      rm -rf .venv && ./deploy/install.sh"
fi

if err="$(uv run --no-dev --extra device python -c 'import pydbus' 2>&1)"; then
    echo "    pydbus OK"
else
    echo "    ERROR: $err"
    echo "    The Gateway and Logger will still run; the Power System will not."
fi

# --- the API key ------------------------------------------------------------
# The Gateway fails closed: no key, every request is a 503. Generate one rather
# than leave the operator to discover that from the far end of the wire.
if [ ! -f .env ]; then
    KEY="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    printf 'ECOPS_API_KEY=%s\n' "$KEY" > .env
    chmod 600 .env
    echo "--- generated .env with a new ECOPS_API_KEY ---"
    echo "    $KEY"
else
    echo "--- .env already present, left alone ---"
fi

# --- boot hook --------------------------------------------------------------
if [ "$BOOT" -eq 1 ]; then
    RC_LOCAL=/data/rc.local
    MARKER="# ecowhen_power_system startup"
    if grep -qF "$MARKER" "$RC_LOCAL" 2>/dev/null; then
        echo "--- boot hook already in $RC_LOCAL ---"
    else
        [ -f "$RC_LOCAL" ] || { echo "#!/bin/bash" > "$RC_LOCAL"; chmod +x "$RC_LOCAL"; }
        printf '\n%s\n%s/deploy/start.sh\n' "$MARKER" "$ECOPS_REMOTE_DIR" >> "$RC_LOCAL"
        echo "--- added boot hook to $RC_LOCAL ---"
    fi
fi

echo
echo "Installed. Start with:  ./deploy/start.sh"
