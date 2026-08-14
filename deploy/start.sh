#!/bin/bash
# Start the four processes in a tmux session. Run ON THE DEVICE.
#
#   ./deploy/start.sh [--restart] [--no-control]
#
#   --restart     kill an existing session and start fresh. Without it, an already
#                 running session is left alone.
#   --no-control  everything but Control. The Gateway accepts writes, so a transport
#                 can be proven by hand — and it cannot be while Control runs, because
#                 its coordinator drives every service back to target on each tick and
#                 would undo the poke within a tick or two.
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
NO_CONTROL=0
for arg in "$@"; do
    case "$arg" in
        --restart)    RESTART=1 ;;
        --no-control) NO_CONTROL=1 ;;
        *) echo "ERROR: unknown option '$arg'" >&2; exit 2 ;;
    esac
done

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

export ECOPS_DATA_DIR ECOPS_RUNTIME_DIR
mkdir -p "$ECOPS_DATA_DIR" "$ECOPS_RUNTIME_DIR"

run() {   # run <window> <command...>; keep the pane open so a crash is readable
    window="$1"; shift
    tmux new-window -t "$ECOPS_SESSION" -n "$window" \
        "cd '$PWD' && $* ; echo; echo '--- $window exited ---'; read -p 'enter to close'"
}

echo "--- starting session '$ECOPS_SESSION' ---"
tmux new-session -d -s "$ECOPS_SESSION" -n power-system \
    "cd '$PWD' && uv run --no-dev --extra device ecops-power-system ; echo; echo '--- power-system exited ---'; read -p 'enter to close'"

sleep 3     # let the topology contract appear before the Gateway looks for it
run gateway "uv run --no-dev --extra device ecops-gateway --port $ECOPS_PORT"
run logger  "uv run --no-dev --extra device ecops-logger"

STARTED="power-system, gateway (:$ECOPS_PORT), logger"
if [ "$NO_CONTROL" -eq 0 ]; then
    run control "uv run --no-dev --extra device ecops-control"
    STARTED="$STARTED, control"
fi

echo
echo "Started '$ECOPS_SESSION': $STARTED"
if [ "$NO_CONTROL" -eq 1 ]; then
    echo
    echo "  CONTROL IS NOT RUNNING. Nothing is arbitrating the hardware — whatever"
    echo "  you switch by hand stays switched. Prove the write transports, then:"
    echo "    ./deploy/start.sh --restart          # brings Control up"
fi
echo "  attach:   tmux attach -t $ECOPS_SESSION"
echo "  data:     $ECOPS_DATA_DIR"
echo "  contracts:$ECOPS_RUNTIME_DIR"
