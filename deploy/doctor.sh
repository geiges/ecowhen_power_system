#!/bin/sh
# Why can't this venv see `gi`? Run ON THE DEVICE, from the project dir.
#
#   ./deploy/doctor.sh
#
# `gi` (PyGObject) is a compiled OS package. A venv can only reach it if
#   1. the venv was built with --system-site-packages, AND
#   2. the venv's interpreter is the *same* one the OS installed PyGObject for.
#
# The predecessor works on this same box, so its venv is the reference: anything
# this prints differently is the answer. Paste the whole output back.
set -u
cd "$(dirname "$0")/.."

OLD_DIR="${OLD_DIR:-/data/python/victron_system_monitor}"

echo "===== 1. the OS python ====="
for py in /usr/bin/python3 python3; do
    command -v "$py" >/dev/null 2>&1 || continue
    echo "-- $py : $($py -V 2>&1)"
    $py -c "import gi, sys; print('   gi OK  ', gi.__file__)" 2>&1 | head -2
done

echo
echo "===== 2. uv ====="
echo "uv: $(command -v uv || echo MISSING) $(uv --version 2>&1)"
echo "UV_PYTHON=${UV_PYTHON:-unset}  UV_PROJECT_ENVIRONMENT=${UV_PROJECT_ENVIRONMENT:-unset}"
echo "UV_PYTHON_DOWNLOADS=${UV_PYTHON_DOWNLOADS:-unset}"
echo ".python-version: $(cat .python-version 2>/dev/null || echo '(none)')"
echo "requires-python: $(grep -m1 requires-python pyproject.toml)"

echo
echo "===== 3. this venv vs the predecessor's (the working one) ====="
for d in "$PWD" "$OLD_DIR"; do
    echo "-- $d"
    if [ ! -d "$d/.venv" ]; then echo "   no .venv"; continue; fi
    sed 's/^/   /' "$d/.venv/pyvenv.cfg" 2>/dev/null
    echo "   .venv/bin/python -> $(readlink -f "$d/.venv/bin/python" 2>/dev/null)"
    echo "   version: $("$d/.venv/bin/python" -V 2>&1)"
    "$d/.venv/bin/python" -c "import gi; print('   gi OK  ', gi.__file__)" 2>&1 | tail -1
    "$d/.venv/bin/python" -c "import pydbus; print('   pydbus OK')" 2>&1 | tail -1
    echo
done

echo "===== 4. what uv actually runs (it may not be .venv at all) ====="
uv run --no-dev --extra device python -c \
    "import sys; print('   executable:', sys.executable); print('   prefix    :', sys.prefix)" 2>&1 | tail -4
uv run --no-dev --extra device python -c "import gi; print('   gi OK')" 2>&1 | tail -1

echo
echo "===== 5. where the OS keeps gi ====="
find /usr/lib /usr/lib64 -maxdepth 4 -name "gi" -type d 2>/dev/null | head -5
echo "opkg: $(opkg list-installed 2>/dev/null | grep -i -e pygobject -e python3-gi | head -3 || echo 'n/a')"
