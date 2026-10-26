"""Polling: turn a bus + the topology contract into the state contract.

One pass reads D-Bus variables, D-Bus states and the aux HTTP devices, derives the
battery's own voltage and current from them, runs the SOC estimator, and returns the
dict that becomes `state.json`.

Every D-Bus value here is exactly what the bus said. The derived quantities — the
ones nothing measures — arrive from the estimator under their own `battery/*` keys,
so a raw reading and an estimate are never confused for one another.

SOC is the reason estimation lives here rather than in the Logger or in Control:
nothing reports it. It exists only as Kalman-filter output over voltage, current and
temperature, so "read the sensors" and "estimate SOC" are not separable steps.
(Forward projection is a different thing entirely and lives in Control.)
"""
import json
from datetime import datetime, timedelta
from collections import deque


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


def retrieve_aux_data(http_components, debug=False) -> dict:
    """Poll the HTTP devices. A component that fails is skipped, not fatal.

    Still named `aux` because that is where the values land: aux_<date>.csv.
    """
    data = {}
    for component in http_components:
        try:
            data.update(component.get_labeled_data())
        except Exception as exc:
            print(f"[power_system] aux fetch failed for {component.short_name}: {exc}")
    return data

class Averager:
    """
    The Averager is used to build x minute averages for configured values dependent 
    on the configuration. Each average is reset after a restart and initiated 
    with zeros.
    """
    def __init__(self, config):
        self.n_average = int(config.average_interval / config.log_interval)
        self.past_values = dict()
        
    def step(self, state):
        state['averages'] = dict()
        
        for key, value in state.items():
            
            if not key.endswith('power_w') :
                # currently only average current power values
                continue
        
            if key not in self.past_values:
                #create new list 
                try:
                    float(value)
                except:
                    continue  
                self.past_values[key] = deque([value,])
                
            else:
                try:
                    float(value)
                except:
                    value is None  
                
                if value is not None:
                    # add new value to data
                    self.past_values[key].append(value)

                    if len(self.past_values[key]) > self.n_average:     
                        self.past_values[key].popleft()
                else:
                    #remove last entry since no new one was added (fading average out)
                    
                    if len(self.past_values[key]) > 0:  
                        self.past_values[key].popleft()
                
            
                if len(self.past_values[key]) > 0:
                    state["averages"][key] = sum(self.past_values[key]) / len(self.past_values[key])
                else:
                    state["averages"][key] = None
        return state
        
        
class SocTracker:
    """Tracks SOC metrics over time
    1) time of SOC above treshold
    2) Last time SOC over treshold
    """
    SOC_FULL_THRESHOLD = 0.99


    def __init__(self, above_threshold_since=None, threshold=SOC_FULL_THRESHOLD):
        self.threshold = threshold
        self.above_threshold_since = above_threshold_since        # datetime of the rise above threshold, or None
        self.last_above_threshold = None # new system init
        
    def update(self, soc, t_now):
        if soc is None:
            return self.above_threshold_since
        
        if soc >= self.threshold:
            self.last_above_threshold = t_now
            
        if soc < self.threshold:
            self.above_threshold_since = None            # below: the clock is not running
        elif self.above_threshold_since is None:
            self.above_threshold_since = t_now           # just crossed up: start the clock
        return self.above_threshold_since

    def minutes_soc_above_treshold(self, t_now):
        if self.above_threshold_since is None:
            return 0.0
        return (t_now - self.above_threshold_since).total_seconds() / 60.0

    def last_time_above_threshold(self):
        return self.last_above_threshold

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
                state_averager, t_now, running_since, debug=False) -> dict:
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
    state.update(retrieve_aux_data(psystem.http_components(), debug))

    if simulator is not None:
        sim_row = simulator.update(raw_data=row_data, t_now=t_now, psystem=psystem)
        state.update(sim_row)
        state["time_to_low_battery"] = simulator.time_to_low_battery()

        since = soc_tracker.update(sim_row.get("SOC_counted"), t_now)
        state["soc_above_threshold_since"] = since.isoformat() if since else None
        state["minutes_at_full_soc"] = soc_tracker.minutes_soc_above_treshold(t_now)
        state['last_time_battery_full'] = soc_tracker.last_time_above_threshold()

    for sum_var in STATE_VARIABLES_TO_SUM:
        vars_to_sum = [
            x for x in row_data
            if not x.startswith("system") and x.endswith(sum_var)
        ]
        state[f"system/{sum_var}"] = sum(row_data[x] for x in vars_to_sum)

    # Averages currently all variables ending with power_w
    state = state_averager.step(state)
    return state


def save_state(path, state) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f)
    tmp.replace(path)      # atomic: the Logger and Control poll this file freely
