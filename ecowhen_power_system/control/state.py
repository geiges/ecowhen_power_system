"""Reading the state contract — Control's only input from the system model.

Three things changed against the predecessor's `control/state.py`:

* **No CSV globbing.** `minutes_at_full_soc` used to be recomputed by scanning the
  Logger's `sim_*.csv`, which made a pure sink into a dependency and reset the clock
  at midnight. The Power System now tracks the crossing itself and publishes
  `soc_above_threshold_since` / `minutes_at_full_soc` (decision #7).
* **A12's rename.** `battery_voltage` / `battery_current` come from `battery/*`, the
  *derived* clamp values. `system/battery_voltage` is now exactly what the bus said —
  a terminal voltage, not the battery's.
* **Age is load-bearing.** The predecessor stamped today's date onto a bare
  `"01:04:27"`, so a state frozen yesterday read as current. The timestamp is now
  full ISO and a stale one raises: that the Power System died is a safety event, not
  a reason to keep acting on its last opinion.
"""
import json
from dataclasses import dataclass
from datetime import datetime

from .. import paths
from .config import now


class StateUnavailableError(Exception):
    """No usable present state. The caller must treat this as a safety event."""


@dataclass
class CurrentState:
    timestamp: datetime
    age_seconds: float
    soc: float                    # 0-1, coulomb-counted (Kalman estimate is fallback)
    battery_voltage: float        # V, derived at the clamps (A12)
    battery_current: float        # A, charge-positive
    battery_temp: float           # °C
    solar_power_w: float          # W, all MPPTs summed
    ac_load_w: float              # W, Multiplus AC output
    mppt_150_power_w: float       # W
    mppt_100_power_w: float       # W
    minutes_at_full_soc: float    # published by the Power System's SocTracker


def _float(state: dict, key: str, default=None):
    try:
        return float(state[key])
    except (KeyError, TypeError, ValueError):
        return default


def read_current_state(path=None, max_age_seconds: float = 120.0,
                       _now: datetime = None) -> CurrentState:
    path = path if path is not None else paths.STATE_PATH

    try:
        with open(path) as f:
            state = json.load(f)
    except OSError as exc:
        raise StateUnavailableError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise StateUnavailableError(f"{path} is not valid JSON: {exc}") from exc

    try:
        timestamp = datetime.fromisoformat(state["timestamp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StateUnavailableError(
            f"{path} carries no usable ISO timestamp — is the Power System running?"
        ) from exc

    age = ((_now or now()) - timestamp).total_seconds()
    if age > max_age_seconds:
        raise StateUnavailableError(
            f"state.json is {age:.0f}s old (limit {max_age_seconds:.0f}s) — "
            f"the Power System has stopped publishing"
        )

    # SOC: prefer the coulomb count; the filter is the fallback. Both come out of the
    # same estimator, so if neither is there, estimation is not running at all.
    soc = _float(state, "SOC_counted")
    if soc is None:
        soc = _float(state, "SOC_Kf")
    if soc is None:
        raise StateUnavailableError(
            "no SOC in state.json — is simulate_system on in the Power System?"
        )

    voltage = _float(state, "battery/voltage")
    current = _float(state, "battery/current")
    if voltage is None or current is None:
        raise StateUnavailableError(
            "battery/voltage and battery/current missing from state.json. They are "
            "derived, not measured (A12) — the estimator did not run this poll."
        )

    return CurrentState(
        timestamp=timestamp,
        age_seconds=age,
        soc=soc,
        battery_voltage=voltage,
        battery_current=current,
        battery_temp=_float(state, "system/battery_temperature", 25.0),
        solar_power_w=_float(state, "system/power_yield", 0.0),
        ac_load_w=_float(state, "multiplus/AC_power_output", 0.0),
        mppt_150_power_w=_float(state, "mppt150/power_yield", 0.0),
        mppt_100_power_w=_float(state, "mppt100/power_yield", 0.0),
        minutes_at_full_soc=_float(state, "minutes_at_full_soc", 0.0),
    )
