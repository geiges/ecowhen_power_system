# ecowhen_power_system

Four cooperating processes that monitor and control a solar / inverter / battery
installation. Successor to `victron_system_monitor`, rebuilt so each process owns
exactly one concern.

## The four processes

| # | Process | Concern | Hardware I/O |
|---|---|---|---|
| ① | **Power System** | The system model: discover the topology, poll every source, correct for cable losses, estimate SOC. Produces both contracts. | D-Bus **read**, aux HTTP read |
| ② | **Gateway** | REST interface and the **sole writer** of hardware. Resolves logical actuator names from the topology contract. | D-Bus/Tasmota **write**, D-Bus read (verify) |
| ③ | **Logger** | Persist the time series. Nothing reads it. | none |
| ④ | **Control** | Agents, modes, services, arbitration. Pure brain. | none |

Start order: **① → ② and ③ → ④**. ② and ③ are independent of each other; ④ needs
②'s REST and ①'s `state.json`, never ③.

## The two contracts

Both are produced by the Power System, and everything else consumes them.

- **`system_configuration.yaml`** — *topology*: components, the variable/state poll
  maps, and the actuator registry (logical name → transport + address + on/off values).
- **`state.json`** — *state*: live corrected values, estimated SOC, and derived fields.

## Design invariants

1. **Exactly one process writes hardware** (the Gateway). Reads are idempotent and
   conflict-free, so several processes may read; only one may write.
2. **The brain holds no hardware I/O and no magic numbers.** Control says
   `command("multiplus_mode", on=True)`; the Gateway resolves that to D-Bus
   `/Mode` = 3.
3. **The Logger is a pure sink.** If something needs to read a logged value, that
   value belongs in `state.json` instead.
4. **Ordering is declarative.** A Mode declares the Services it requires; Services are
   reference-counted and own their own actuation, verification and retry. Ordering
   falls out of the dependency graph rather than hand-written step lists.

## Services and modes

Every actuator is one service. A service is named for the **hardware it switches**; a
mode is named for **what the user wants**. Actuator names are the Gateway's, resolved
from the component classes onto the bus, and are left alone — `ac_inverter_relay` is
the Tasmota plug that feeds mains to the Deye, which is why the service on top of it
is `deye_inverter`.

| service | actuator | hardware |
|---|---|---|
| `multiplus_ac` | `multiplus_mode` | the Multiplus II inverter |
| `mppt100_dc_load` | `mppt100_load` | the MPPT's DC load output — the compartment fans |
| `wallbox_charger` | `wallbox_relay` | the wallbox charging plug |
| `deye_inverter` | `ac_inverter_relay` | mains to the Deye (the plug has no function of its own) |
| `other_ac_supply` | `ac_other_relay` | the spare AC socket |

| mode | primary service | prerequisites |
|---|---|---|
| `wallbox` | `wallbox_charger` | `multiplus_ac`, `mppt100_dc_load` |
| `ac_solar` | `deye_inverter` | `multiplus_ac` |
| `ac_other` | `other_ac_supply` | `multiplus_ac` — and `blocked_by` `wallbox` |
| `cooling` | — | `mppt100_dc_load` |
| `main_ac` | — | `multiplus_ac` |

The two tables are separate mappings; the rows do not line up across them.

A mode's **primary** is the service it holds only once its prerequisites are
satisfied, and lets go of first on the way down — that is the two-level ordering, and
why a plug is not simply another prerequisite. `cooling` and `main_ac` have no primary:
they *are* their requirements.

`ac_other` declares `blocked_by=["wallbox"]`, one-sidedly, and **that declaration is
the priority rule** — the mode that gives way is the one that says so. It holds from
the moment the wallbox is *wanted* until it is all the way back to `inactive`, so the
two are never both active. Do not make it mutual: `_apply_blocks` walks the modes in
order, so the first one asked would block itself, the second would then read as
un-requested and never be blocked, and which of the two starves would be decided by
dict order rather than by priority.

## Running

```bash
uv sync
uv run ecops-power-system     # ① writes system_configuration.yaml + state.json
uv run ecops-gateway          # ② REST
uv run ecops-logger           # ③ CSVs
uv run ecops-control          # ④ agents + coordinator
```

The Power System needs D-Bus, so it only runs fully on the Venus OS device:

```bash
uv sync --extra device        # adds pydbus (needs system gobject bindings)
```

Off-device, the bus is injectable — every D-Bus entry point takes a `bus` argument,
so tests pass a fake. `mock_dbus_service.py` is for on-device integration only.

## Testing

```bash
uv run python -m pytest tests/
```

The suite is green. Keep it that way: the predecessor's suite went red and a
load-bearing safety bug (`is_enabled` returning `None`) sat behind the noise with a
failing test nobody read.
