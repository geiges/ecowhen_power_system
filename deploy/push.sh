#!/bin/bash
# Copy this working tree to the Venus OS box. Run from the dev machine.
#
#   ./deploy/push.sh
#
# rsync rather than git pull, because validating against the device is an iterate-
# and-look loop and none of it should need a commit. Once it settles, `git pull`
# on the device does the same job.
#
# Never deletes on the far side: the device's data/ and .env are its own.
set -eu
cd "$(dirname "$0")/.."
. deploy/config.sh

echo "--- pushing $(pwd) -> $ECOPS_DEVICE:$ECOPS_REMOTE_DIR ---"

ssh "$ECOPS_DEVICE" "mkdir -p '$ECOPS_REMOTE_DIR'"

rsync -avz \
    --exclude '.venv/' \
    --exclude '.git/' \
    --exclude '__pycache__/' \
    --exclude '.pytest_cache/' \
    --exclude 'data/' \
    --exclude '.env' \
    ./ "$ECOPS_DEVICE:$ECOPS_REMOTE_DIR/"

echo
echo "Pushed. On the device:"
echo "  cd $ECOPS_REMOTE_DIR && ./deploy/install.sh     # first time only"
echo "  cd $ECOPS_REMOTE_DIR && ./deploy/start.sh"
