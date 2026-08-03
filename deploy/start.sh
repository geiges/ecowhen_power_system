#!/bin/bash
# Start the three processes in a tmux session. Run ON THE DEVICE.
#
#   ./deploy/start.sh [--restart]
#
#   --restart   kill an existing session and start fresh. Without it, an already
#               running session is left alone.
#
# --extra device on every uv run is what keeps pydbus in the venv: it is an optional
# extra so the tests install off-device (A3), so a plain `uv run` would leave it out
# and could prune it back out of an env install.sh had populated.
#
# Start order is the dependency DAG: the Power System publishes both contracts, the
# Gateway serves the actuator registry from one, the Logger reads the other. The
# other two recover on their own if they start first, so the sleeps are politeness,
# not correctness.
set -eu
cd "$(dirname "$0")/.."
. deploy/config.sh

RESTART=0
[ "${1:-}" = "--restart" ] && RESTART=1

command -v tmux >/dev/null 2>&1 || { echo "ERROR: tmux not installed" >&2; exit 1; }

if tmux has-session -t "$ECOPS_SESSION" 2>/dev/null; then
    if [ "$RESTART" -eq 1 ]; then
        echo "--- killing session '$ECOPS_SESSION' ---"
        tmux kill-session -t "$ECOPS_SESSION"
    else
        echo "Session '$ECOPS_SESSION' already running — nothing to do."
        echo "  tmux attach -t $ECOPS_SESSION      (--restart to replace it)"
        exit 0
    fi
fi

# Parallel mode is the safety interlock, not a preference: while the predecessor is
# live it owns the hardware, and a second writer on the same actuator is the one
# failure in this rebuild that can damage something.
PS_FLAGS=""
GW_FLAGS=""
if [ "$ECOPS_PARALLEL" = "1" ]; then
    PS_FLAGS="--no-startup-sequence"
    GW_FLAGS="--read-only"
    echo "--- PARALLEL MODE: read-only, the old system keeps control ---"
else
    echo "--- FULL CONTROL MODE: this system writes hardware ---"
    echo "    The predecessor must NOT be running. Ctrl-C now if it is."
    sleep 5
fi

export ECOPS_DATA_DIR ECOPS_RUNTIME_DIR
mkdir -p "$ECOPS_DATA_DIR" "$ECOPS_RUNTIME_DIR"

run() {   # run <window> <command...>; keep the pane open so a crash is readable
    window="$1"; shift
    tmux new-window -t "$ECOPS_SESSION" -n "$window" \
        "cd '$PWD' && $* ; echo; echo '--- $window exited ---'; read -p 'enter to close'"
}

echo "--- starting session '$ECOPS_SESSION' ---"
tmux new-session -d -s "$ECOPS_SESSION" -n power-system \
    "cd '$PWD' && uv run --no-dev --extra device ecops-power-system $PS_FLAGS ; echo; echo '--- power-system exited ---'; read -p 'enter to close'"

sleep 3     # let the topology contract appear before the Gateway looks for it
run gateway "uv run --no-dev --extra device ecops-gateway --port $ECOPS_PORT $GW_FLAGS"
run logger  "uv run --no-dev --extra device ecops-logger"

# Control is the exclusive step. In parallel mode the Gateway would refuse its every
# POST anyway (403), so a Control running there would do nothing but log failures —
# and the moment the interlock was lifted it would start actuating with no further
# ceremony. It starts only in full-control mode, deliberately.
STARTED="power-system, gateway (:$ECOPS_PORT), logger"
if [ "$ECOPS_PARALLEL" != "1" ]; then
    run control "uv run --no-dev --extra device ecops-control"
    STARTED="$STARTED, control"
fi

echo
echo "Started '$ECOPS_SESSION': $STARTED"
echo "  attach:   tmux attach -t $ECOPS_SESSION"
echo "  data:     $ECOPS_DATA_DIR"
echo "  contracts:$ECOPS_RUNTIME_DIR"
echo "  compare:  ./deploy/compare.py"
