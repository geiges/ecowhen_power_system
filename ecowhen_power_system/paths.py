"""The two file roots.

Durable = user data that must survive a reboot. Ephemeral = anything a process can
regenerate at startup, so it lives in tmpfs and never wears the SD card.

Both are overridable by env var, which is what the tests use.
"""
import os
from pathlib import Path

# Regenerated every boot: the contracts, status files, projections.
RUNTIME_DIR = Path(os.environ.get("ECOPS_RUNTIME_DIR", "/dev/shm/ecowhen_power_system"))

# Survives reboot: user intent, config, the time series, the SOC estimate.
DATA_DIR = Path(os.environ.get("ECOPS_DATA_DIR", "data"))

# --- the two contracts (ephemeral: the Power System rewrites both) ---
SYSTEM_CONFIG_PATH = RUNTIME_DIR / "system_configuration.yaml"
STATE_PATH = RUNTIME_DIR / "state.json"
STATE_MAPPING_PATH = RUNTIME_DIR / "state_mapping.yaml"

# --- what Control publishes (ephemeral: recomputed on every tick) ---
MODES_STATUS_PATH = RUNTIME_DIR / "modes_status.json"
# The calendar: what the planning agents intend, ahead of now. Ephemeral because it
# is derived — every plan in it is rebuilt by its agent, and a booking nobody
# reasserts is one nobody stands behind.
MODE_SCHEDULE_PATH = RUNTIME_DIR / "mode_schedule.json"
SERVICES_STATUS_PATH = RUNTIME_DIR / "services_status.json"
PROJECTION_PATH = RUNTIME_DIR / "projection_latest.csv"

# --- durable ---
# User intent and the knobs: both written by the Gateway's REST surface, read by
# Control. Durable because someone who switched the wallbox on must not find it off
# because a process bounced.
MODE_REQUESTS_PATH = DATA_DIR / "mode_requests.json"
CONTROL_CONFIG_PATH = DATA_DIR / "control_config.yaml"
DECISION_LOG_PATH = DATA_DIR / "control_log.jsonl"
# The SOC estimate is durable on purpose: it is the one thing the Power System
# cannot re-derive at startup. Voltage alone pins SOC only very loosely on LFP,
# whose OCV curve is famously flat across the mid-range.
SOC_STATE_PATH = DATA_DIR / "soc_state.json"


def ensure_dirs() -> None:
    """Create both roots. Safe to call from any process; the first one wins."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
