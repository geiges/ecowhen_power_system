"""Polling: turn a bus + the topology contract into the state contract.

One pass reads D-Bus variables, D-Bus states and the aux HTTP devices, corrects the
raw voltages for cable losses, runs the SOC estimator, and returns the dict that
becomes `state.json`.

SOC is the reason estimation lives here rather than in the Logger or in Control:
nothing reports it. It exists only as Kalman-filter output over voltage, current and
temperature, so "read the sensors" and "estimate SOC" are not separable steps.
(Forward projection is a different thing entirely and lives in Control.)
"""
import json
from datetime import datetime, timedelta

# Matches the default in the predecessor's control/state.py:minutes_at_full_soc.
SOC_FULL_THRESHOLD = 0.99

# Variables summed across components into a single system-level figure.
STATE_VARIABLES_TO_SUM = ["power_yield", "total_yield"]


def retrieve_data(bus, variables_to_log, config, debug=False) -> dict:
    """Read the numeric D-Bus variables named by the poll map."""
    data = {}
    for var_name, var_conf in variables_to_log.items():
        try:
            var_value = bus.get(
                var_conf["dbus_device"], var_conf["address"]
            ).GetValue()
            if var_name not in config.non_numeric_var:
                var_value = round(var_value, config.round_digits)
            data[var_name] = var_value
        except Exception:
            print(f'[power_system] failed to read {var_conf["address"]} '
                  f'from {var_conf["dbus_device"]}')
    return data


def retrieve_states(bus, states_to_log, debug=False) -> dict:
    """Read the discrete D-Bus states. Unreadable states come back as None."""
    values = {}
    for var_name, conf in states_to_log.items():
        try:
            values[var_name] = bus.get(conf["dbus_device"], conf["address"]).GetValue()
        except Exception:
            values[var_name] = None
    return values


def encode_state_code(state_values, ordered_names) -> str:
    """Pack the discrete states into one digit-per-state string (9 = unknown)."""
    digits = []
    for name in ordered_names:
        raw = state_values.get(name)
        if raw is None or not (0 <= raw <= 8):
            digits.append("9")
        else:
            digits.append(str(raw))
    return "".join(digits)


def retrieve_aux_data(aux_components, debug=False) -> dict:
    """Poll the HTTP devices. A component that fails is skipped, not fatal."""
    data = {}
    for component in aux_components:
        try:
            data.update(component.get_labeled_data())
        except Exception as exc:
            print(f"[power_system] aux fetch failed for {component.short_name}: {exc}")
    return data


class SocTracker:
    """Tracks how long SOC has been continuously at or above the full threshold.

    Exists so Control can stop globbing `sim_*.csv` — the Logger is a pure sink and
    nothing may read it. Control reads `soc_above_threshold_since` out of state.json
    and subtracts.

    This also fixes a quirk of the predecessor, which recomputed the answer by
    scanning the current day's sim CSV: at midnight a fresh CSV began, so a battery
    that had been full since 18:00 reported "full for 5 minutes" at 00:05. Tracking
    the crossing directly, and persisting it, makes midnight uneventful.
    """

    def __init__(self, since=None, threshold=SOC_FULL_THRESHOLD):
        self.threshold = threshold
        self.since = since        # datetime of the rise above threshold, or None

    def update(self, soc, t_now):
        if soc is None:
            return self.since
        if soc < self.threshold:
            self.since = None            # below: the clock is not running
        elif self.since is None:
            self.since = t_now           # just crossed up: start the clock
        return self.since

    def minutes(self, t_now):
        if self.since is None:
            return 0.0
        return (t_now - self.since).total_seconds() / 60.0


def load_soc_state(path):
    """Restore the estimator across a restart.

    The predecessor restored by reading back the last row of the Logger's sim CSV.
    That is a backward edge — the state producer depending on its own consumer — and
    it could not work at all on a fresh boot, where the Logger has written nothing
    yet. The Power System now owns this file.
    """
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return {
            "soc": float(raw["soc"]),
            "rc_voltage": float(raw.get("rc_voltage", 0.0)),
            "timestamp": datetime.fromisoformat(raw["timestamp"]),
            "soc_above_threshold_since": (
                datetime.fromisoformat(raw["soc_above_threshold_since"])
                if raw.get("soc_above_threshold_since") else None
            ),
        }
    except (KeyError, TypeError, ValueError) as exc:
        print(f"[power_system] ignoring unreadable soc_state: {exc}")
        return None


def save_soc_state(path, soc, rc_voltage, t_now, soc_above_threshold_since) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "soc": soc,
        "rc_voltage": rc_voltage,
        "timestamp": t_now.isoformat(),
        "soc_above_threshold_since": (
            soc_above_threshold_since.isoformat()
            if soc_above_threshold_since else None
        ),
    }
    with open(tmp, "w") as f:
        json.dump(payload, f)
    tmp.replace(path)      # atomic: a reader never sees a half-written estimate


def restore_simulator(simulator, soc_state, max_age: timedelta, t_now) -> bool:
    """Seed the simulator from a persisted estimate. True if it was used.

    A stale estimate is worse than none: coulomb counting cannot account for
    whatever happened while the process was down, so past a certain age we let the
    simulator re-derive SOC from open-circuit voltage instead.
    """
    if soc_state is None:
        return False
    age = t_now - soc_state["timestamp"]
    if age > max_age:
        print(f"[power_system] soc_state is {age} old (> {max_age}) — "
              f"re-estimating from OCV instead")
        return False
    simulator.set_state(
        soc_state["soc"], t_now=t_now, RC_voltage=soc_state["rc_voltage"]
    )
    simulator.initilized = True
    return True


def build_state(bus, psystem, system_config, config, simulator, soc_tracker,
                t_now, running_since, debug=False) -> dict:
    """One full poll. Returns the state contract, or None if D-Bus was unreadable."""
    variables_to_log = system_config["variables_to_log"]
    states_to_log = system_config["states_to_log"]

    try:
        data = retrieve_data(bus, variables_to_log, config, debug)
        state_values = retrieve_states(bus, states_to_log, debug)
        data["state"] = encode_state_code(state_values, list(states_to_log))
    except Exception as exc:
        print(f"[power_system] poll failed: {exc} — skipping this step")
        return None

    # The predecessor took this dict back out of File_Logger.log_step(), which
    # coupled the estimator's input to the CSV writer. The 'time' field is the
    # entirety of what that call added.
    row_data = {"time": t_now.strftime(config.time_format)}
    row_data.update(data)

    state = {
        # A full ISO timestamp, unlike the predecessor's bare "01:04:27". Consumers
        # must be able to tell that this process died; with only a wall-clock time
        # they stamped today's date on it and a day-old file read as current.
        "timestamp": t_now.isoformat(),
        "running_since": running_since.isoformat(),
    }
    state.update(row_data)
    state.update(retrieve_aux_data(config.aux_components, debug))

    if simulator is not None:
        sim_row = simulator.update(raw_data=row_data, t_now=t_now, psystem=psystem)
        state.update(sim_row)
        state["time_to_low_battery"] = simulator.time_to_low_battery()

        since = soc_tracker.update(sim_row.get("SOC_counted"), t_now)
        state["soc_above_threshold_since"] = since.isoformat() if since else None
        state["minutes_at_full_soc"] = soc_tracker.minutes(t_now)

    for sum_var in STATE_VARIABLES_TO_SUM:
        vars_to_sum = [
            x for x in row_data
            if not x.startswith("system") and x.endswith(sum_var)
        ]
        state[f"system/{sum_var}"] = sum(row_data[x] for x in vars_to_sum)

    return state


def save_state(path, state) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f)
    tmp.replace(path)      # atomic: the Logger and Control poll this file freely
