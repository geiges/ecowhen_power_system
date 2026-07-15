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

# --- durable ---
# The SOC estimate is durable on purpose: it is the one thing the Power System
# cannot re-derive at startup. Voltage alone pins SOC only very loosely on LFP,
# whose OCV curve is famously flat across the mid-range.
SOC_STATE_PATH = DATA_DIR / "soc_state.json"


def ensure_dirs() -> None:
    """Create both roots. Safe to call from any process; the first one wins."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
