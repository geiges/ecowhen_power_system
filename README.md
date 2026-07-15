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
   `command("ac_inverter", on=True)`; the Gateway resolves that to D-Bus `/Mode` = 3.
3. **The Logger is a pure sink.** If something needs to read a logged value, that
   value belongs in `state.json` instead.
4. **Ordering is declarative.** A Mode declares the Services it requires; Services are
   reference-counted and own their own actuation, verification and retry. Ordering
   falls out of the dependency graph rather than hand-written step lists.

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
