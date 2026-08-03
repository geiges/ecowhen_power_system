# ecowhen_power_system

Four processes monitoring and controlling a solar / inverter / battery installation on a
Venus OS box. One package, four entrypoints.

*This file describes the system as it is now. `docs/soc_estimator_diagnostic.md` holds the
estimator's calibration evidence, and `deploy/README.md` the deployment procedure.*

## The four processes

| # | Entrypoint | Concern | Hardware I/O |
|---|---|---|---|
| ① | `ecops-power-system` | The system model: discover topology, poll every source, derive battery clamp values, estimate SOC. Produces both contracts. | D-Bus **read**, aux HTTP read |
| ② | `ecops-gateway` | REST, and the **sole writer** of hardware. Resolves logical actuator names from the topology contract. | D-Bus / VE.Direct / Tasmota **write** + read |
| ③ | `ecops-logger` | Persist the time series. Nothing reads it. | none |
| ④ | `ecops-control` | Agents, modes, services, arbitration. Pure brain. | none |

Start order **① → ② and ③ → ④**. ② and ③ are independent; ④ needs ②'s REST and ①'s
`state.json`, never ③.

## Invariants

1. **Exactly one process writes hardware** — the Gateway. The one exception is bounded and
   deliberate: `discovery.startup_sequence()` writes `/Mode` to wake the Multiplus so its DC
   measurements exist, at startup only, restoring what it found. `--no-startup-sequence`
   disables it. Nothing else in the Power System may write.
2. **The brain holds no hardware I/O and no magic numbers.** Control says
   `command("multiplus_mode", on=True)`; the Gateway resolves that to `/Mode` = 3.
   `grep -rn "dbus-send\|pydbus" ecowhen_power_system/control/` must return no code.
3. **The Logger is a pure sink.** If something needs to read a logged value, that value
   belongs in `state.json` instead.
4. **Ordering is declarative.** A Mode declares required Services; Services are
   reference-counted and own their own actuation, verification and retry.
5. **A component class carries everything needed to reach the hardware it describes** —
   transport, register, on/off values, in `component_actuators`. `config_default` declares
   no actuators. The exception is `connector_R0` / `voltage_offset`: those describe the
   *cable between a source and the battery clamps*, so they live on `Generic_Battery`.

## Files

```
paths.py                RUNTIME_DIR (tmpfs, ephemeral) vs DATA_DIR (durable). Both env-overridable.
config_default.py       THE calibration for this installation. Physical measurements — never retype.
specifications.py       Cell chemistry incl. the OCV curve; safety limits derive from n_cells x spec.
components.py           Composition layer over manufacturer/.
manufacturer/victron.py DBusComponent (bus primitives, get_bus, read_number/write_number) + Victron classes.
manufacturer/__init__.py ActuatorType — declared here because D-Bus and aux components both use it.
aux_components.py       HTTP components (Tasmota plugs, Deye).
discovery.py            startup sequence → bus walk → system_configuration.yaml + actuator registry + calibration guard.
poll.py                 D-Bus + aux → estimator → state.json. SocTracker, durable SOC persistence.
battery_simulation.py   The battery: Thevenin model, clamp derivation, coulomb counting, EKF. One class.
kalman.py               Harvested EKF. np.matrix on purpose — see below.
logger.py               split_state, CsvLogger (rotation, dedup, growable columns), StateLogger.
aggregation.py          daily.csv — time-weighted energies, per-day max/min, idempotent per date.
gateway/                app (auth, mtime-reloading registry), transports, actuators, control, files.
control/                config, state, forecast, projection, gateway_client, service/mode/coordinator
                        (Booking + Plan live in coordinator.py; it imports nothing from the
                        package, so the spike tests carry no hardware in their import graph),
                        actuation (this installation's services + modes), agents/, decision_log.
tools/replay.py         Score estimator changes against recorded CSVs. Use this, not argument.
deploy/                 push / install / start / doctor / compare. See deploy/README.md.
```

## The two contracts

Both produced by the Power System, in `RUNTIME_DIR`.

- **`system_configuration.yaml`** — topology: components, variable/state poll maps, and the
  actuator registry (logical name → transport + address + on/off, separate `write` and `read`
  endpoints).
- **`state.json`** — raw bus readings verbatim, plus derived `battery/voltage`,
  `battery/current`, `battery/voltage_spread`, `SOC_Kf`, `SOC_counted`,
  `time_to_low_battery`, `soc_above_threshold_since`, aux power keys, and a tz-aware ISO
  `timestamp`.

**`system/battery_voltage` and every `mppt*/DC_0_voltage` are raw terminal readings.** The
derived clamp values are `battery/*`. Anything wanting the number the estimator uses reads
`battery/voltage`.

Durable: `soc_state.json`, `mode_requests.json`, `control_config.yaml`, `control_log.jsonl`,
the CSVs. Everything else is regenerated at startup.

## Actuators, services, modes

Four actuators, resolved from the component classes onto the bus:
`multiplus_mode`, `mppt100_load`, `wallbox_relay`, `ac_inverter_relay`.

Two layers, deliberately different names:

| service / mode | actuator |
|---|---|
| `ac_inverter` (service) | `multiplus_mode` |
| `fan` (service) | `mppt100_load` |
| `wallbox` (mode) | `wallbox_relay`, requires `ac_inverter` + `fan` |
| `cooling` (mode) | nothing of its own; requires `fan` |

`ac_inverter_relay` (the Tasmota that hard-cuts mains to the Deye) is **not** a service. Only
the safety agent touches it, as a direct command — nothing in the mode graph may switch it
back on by reference counting.

**Actuators are declared explicitly in `component_actuators`; never auto-derive them from
`component_states`.** That list *is* the allowlist of what may be written. Deriving would also
break on the first non-binary state — `mppt_mode` maps `{0: "ideal", 1: "working", 4: "off"}`
with no `"on"` label, so `_on_off_from_mapping` raises. Where a mapping does carry on/off
labels, on/off are derived from it rather than restated; a VE.Direct register carries no
labels, which is why the fan is the one actuator declaring them per-actuator.

Agents name modes and nothing else — no actuator, no value, no ordering. There are two ways
to want a mode, and both are resolved level-triggered on every tick:

- **`ModeRequest(mode, active, agent, reason)`** — "now". Recomputed wholesale each cycle, so
  **silence is release**. The fast agents use it; they run every tick anyway.
- **`Booking(mode, start, end, reason)`** — an interval, `end` exclusive. An agent publishes
  its whole plan at once via `coordinator.set_plan(agent, bookings)`, and the coordinator asks
  each tick which bookings cover *now*.

The user is a requester like any other (source `user`), and wants a mode in two ways, both in
the durable `mode_requests.json`: `modes` is a standing want with no end, `bookings` is their
calendar. User bookings resolve through the same code as an agent's plan — the differences are
that they are durable, that the **Gateway** writes that file and Control only reads it, and
that Control re-reads it every tick rather than holding it.

Bookings detach planning cadence from execution: **an agent that runs republishes its plan;
one that does not keeps the plan it last published.** So a planning agent may run every hour
(or every day) and the boundary it planned is still honoured to within one tick.

Three bounds stop a plan outliving its author: a booking ends at its own `end` (pruned each
tick), an agent that **raises** has its plan dropped, and an agent that becomes **disabled**
has its plan dropped. The last two are explicit in `control_main._run_planning` — nothing
else would do it.

An **inhibit suppresses the calendar, it does not delete it.** Requesters are released, the
plans stay; when the inhibit clears, bookings still covering now take effect again.

Every plan — agents' and the user's — is published to `mode_schedule.json` (ephemeral) and
served at `GET /control/schedule`: a flat list sorted by `start`, each row carrying its
`source` and that source's `generated_at`, because a booking computed against a stale forecast
must not render as fact. A user's `generated_at` is when they last edited their calendar; an
agent's is when it last planned.
A booking is a *request*, not a guarantee of hardware state at that instant: the mode starts
activating at `start` and its services converge over the next tick or two.

Safety additionally carries `commands` (immediate writes) and `inhibit`, both synchronous and
neither a booking. `fast_cycle` is only a cadence knob — it does not change how output is
treated.

## Things that will bite you

- **`mppt100_load` is asymmetric.** Write is VE.Direct register `0xEDAB` (`on=4`, `off=0`);
  read is D-Bus `/Load/State` (`on=4`, `off=1`, and `5` = "USER"). Writing off is `0`, reading
  off is `1`. The registry models `write` and `read` separately for exactly this reason.
- **`on: null` is a real answer**, not `false`. An unknown readback means the service waits
  rather than driving blind. Collapsing it to `false` flaps the fan every tick.
- **A POST never reads back.** It reports what it wrote. Convergence takes two ticks on
  purpose; confirming in the same breath would trust a value the device may not have applied.
- **`hardware = None` means "real component, nothing on the bus reports it"** (the battery,
  for one). Anything iterating `psystem.values()` and touching the bus must skip those.
- **Victron reports "invalid" as an empty array**, not an error and not `None`. Reads that
  matter type-check; `read_number()` returns `None` for that case.
- **`kalman.py` uses `np.matrix` deliberately.** Converting to `ndarray` silently changes `*`
  from matrix-multiply to element-wise. Only port it if numpy actually removes the subclass,
  and then audit every `*` into an explicit `@` with the SOC output diffed before and after.
  The EKF is fed **discharge-positive** current (`discharge_positive = -current` at the call
  site); the coulomb counter takes charge-positive. Pinned by a test, because nothing about
  the output looks wrong when it regresses.
- **`ECOPS_API_KEY` must be set** or the Gateway 503s everything — fail-closed, on purpose.
  Control's `GatewayClient` reads the same variable, so an unset key makes every command fail
  and every readback come back unknown. `ECOWHEN_DATA_API_KEY` is a *different* key, for the
  solar forecast; without it the forecast agents just report "inactive".
- **There must be no `.python-version` file.** `uv sync` silently rebuilds `.venv` on the
  interpreter it names, losing `--system-site-packages` and with it `gi`. `install.sh` asserts
  this after syncing. `requires-python = ">=3.12"` is the real constraint.
- **`pydbus` is an optional `device` extra**, not a hard dependency, so the suite installs
  off-device. `uv sync --extra device` on the box; every `uv run` in `start.sh` passes it too.
- **A component on the bus that feeds the battery voltage but has no calibration raises at
  startup**, naming the component. That is correct — a wrong battery voltage feeds the Kalman
  filter and the SOC drifts plausibly. Measure the cable; do not guess a number.

## REST (Gateway, port 5101)

```
GET  /actuators/                     list
GET  /actuators/<name>               {"value": .., "on": true|false|null}
POST /actuators/<name>               {"on": bool}
GET  /control/modes | /services | /schedule | /modes/requests | /config | /log
POST /control/modes/<name>/activate  {} = standing want; {"from","until"} = a booking
POST /control/modes/<name>/deactivate  drops the standing want AND every booking for it
DELETE /control/modes/bookings/<id>  cancel one booking
PUT  /control/config                 deep-merge into control_config.yaml
GET  /files/runtime/… | /files/data/…
```

A status file Control has not written yet answers **503 naming Control**, never 404. Same for
the registry before the Power System's first publish.

Booking timestamps are ISO 8601; a naive one is taken as the configured local zone, because
that is the clock the user meant. Bad intervals are refused at the boundary (`until` before
`from`, `until` already past, anything that is not a parseable string). Ended bookings are
pruned from `mode_requests.json` whenever the Gateway next writes it — never by Control, which
would break the single-producer rule on that file.

## Working here

```bash
uv run python -m pytest tests/     # 164 tests, green. Keep it that way.
uv run ecops-power-system --once   # single poll; --no-startup-sequence to never write the bus
uv run ecops-control --dry-run     # run every agent, command nothing
uv run ecops-logger --once
```

Never bare `python`. Off-device, the bus is injectable — every D-Bus entry point takes a
`bus`, and `conftest.py`'s `FakeBus` reads *and* writes, so actuation is testable without
hardware. `mock_dbus_service.py` is for on-device integration only.

Build shared test objects through the `conftest.py` factories. The predecessor's suite went
75-red because 46 tests each constructed `CurrentState` inline, and a load-bearing safety bug
sat behind the noise with a failing test nobody read.

## Deployment

`deploy/config.sh` holds the settings; `deploy/README.md` the procedure.
`ECOPS_PARALLEL=1` (default) is an interlock, not a habit: it passes
`--no-startup-sequence` and `--read-only`, so the package cannot write hardware at all while
the predecessor is still live. `ECOPS_PARALLEL=0` is full control and requires the
predecessor to be **confirmed stopped** — two writers on one actuator is the one genuinely
dangerous failure mode here. `deploy/compare.py` diffs `state.json` against the predecessor's;
`deploy/doctor.sh` is for venv/`gi` problems.

**Status:** phases ①–③ have run on the device in parallel mode. ④ has not and cannot until the
cutover — it is the exclusive step.

### The cutover

Stop the predecessor's `control_runner.py` — confirm it is stopped, not merely quiet — then
`ECOPS_PARALLEL=0 ./deploy/start.sh --restart`, which is what starts Control at all. Revert =
kill the `ecops` tmux session, restart the old runner.

Four things run for the first time in that first minute. Watch them in this order:

1. **The startup sequence**, if the Multiplus's DC measurements are missing — it wakes the
   inverter, energising AC output for a few seconds.
2. **`vregd`** — the fan's VE.Direct write, and the one place `int(token, 0)` parses the output
   of a tool that has never run. `POST /actuators/mppt100_load {"on": true}` by hand, then
   `GET`: the write is `4`, the readback should be `4`; off writes `0` but reads `1`. A 502 is
   the transport talking, not the fan.
3. **The Tasmota write path** — same, by hand, on `wallbox_relay`. Its old failure mode was
   silently doing nothing; the Gateway answers 502 instead, so believe the code.
4. **Control's first tick**, which switches the Multiplus **off** if no mode wants it. Decide
   whether this installation wants `services.ac_inverter_default_on: true` *before* starting,
   not after.

Then let it run: `GET /control/modes`, `/control/services` and `/control/schedule` say what it
thinks it is doing, and `data/control_log.jsonl` says why.

## Open

- **`R1` is ~4x too large.** The dominant remaining model error. Calibration, not refactoring
  — score a fit with `tools/replay.py`. Worth doing before Control leans on the projection.
- **Capacity is configured at 310 Ah; the recorded data measures ~288 Ah.**
- **`SOC_counted` is path-dependent** and will never match another independently-started
  system. Compare `SOC_Kf`, or compare at a rest anchor.
- **Control switches the Multiplus off when no mode wants it.** That is the model working, and
  it is what the predecessor's `wallbox_off` did by hand — but it is a behaviour change on an
  idle system. `services.ac_inverter_default_on` is the knob.
- **The Gateway's `vregd` and Tasmota *write* transports have never run**, on device or off —
  parallel mode 403s every POST. The VE.Direct output parse and the Tasmota `{"POWER": "ON"}`
  answer are assumptions about tools nobody has executed. Check by hand at the cutover.
- **The mode graph has never converged against real hardware latency.** Services retry five
  times, then sit in `failed`.
- **The startup sequence has only run against the fake bus.** The 5 s / 10 s waits are a guess;
  they are class attributes on `VictronMultiplusII`, tunable without touching logic.
- **There is no process supervision.** `start.sh` opens four tmux windows and holds a crashed
  pane open so the traceback is readable — nothing restarts. Start ordering is a `sleep 3`,
  which is politeness rather than a dependency: the Gateway and Control both recover on their
  own if they start first. Real units are still to be written.
