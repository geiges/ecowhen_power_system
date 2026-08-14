#!/bin/sh
# Deployment settings, sourced by every script in this directory.
# Override any of these in the environment: ECOPS_DEVICE=root@10.0.0.9 ./deploy/push.sh

# The Venus OS box. The predecessor reaches it the same way (see ../rsync.sh).
ECOPS_DEVICE="${ECOPS_DEVICE:-root@192.168.1.5}"

# Deliberately NOT the predecessor's /data/python/victron_system_monitor, which is
# still on the box, stopped.
ECOPS_REMOTE_DIR="${ECOPS_REMOTE_DIR:-/data/python/ecowhen_power_system}"

# The old ecowhen_data_api binds 5100 (api_config.yml). Two servers, two ports.
ECOPS_PORT="${ECOPS_PORT:-5101}"

# tmux session. The predecessor's is "victron".
ECOPS_SESSION="${ECOPS_SESSION:-ecops}"

# Durable output. Under the project dir so `data/` never resolves relative to
# whatever directory a process happened to start in.
ECOPS_DATA_DIR="${ECOPS_DATA_DIR:-$ECOPS_REMOTE_DIR/data}"

# Ephemeral contracts. tmpfs — rewritten every boot, and it does not wear the SD card.
ECOPS_RUNTIME_DIR="${ECOPS_RUNTIME_DIR:-/dev/shm/ecowhen_power_system}"
