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
6. **There is one `system_components` list**, bus and HTTP devices alike. An HTTP device
   is a component with `hardware = None`, which every bus walk already skips, and it is
   identified by being able to `get_labeled_data()` — `psystem.http_components()`, the
   only place that asks. Adding a plug is one entry in `config_default`; discovery,
   polling, the CSV split and the actuator registry all follow from it.

## Files

```
paths.py                RUNTIME_DIR (tmpfs, ephemeral) vs DATA_DIR (durable). Both env-overridable.
config_default.py       THE calibration for this installation. Physical measurements — never retype.
                        One `system_components` list; `aggregation.AUX_POWER_COLUMNS` is the
                        one thing a new plug does not get for free.
specifications.py       Cell chemistry incl. the OCV curve; safety limits derive from n_cells x spec.
components.py           Composition layer over manufacturer/.
manufacturer/victron.py DBusComponent (bus primitives, get_bus, read_number/write_number) + Victron classes.
manufacturer/__init__.py ActuatorType and HttpComponent — the vocabulary more than one maker needs.
manufacturer/tasmota.py Tasmota smart plugs.  manufacturer/deye.py  Deye Sun inverter.
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
deploy/                 push / install / start / doctor / compare. README.md is the procedure
                        and the reasoning behind the deployment's shape.
```

## The two contracts

Both produced by the Power System, in `RUNTIME_DIR`.

- **`system_configuration.yaml`** — topology: components, variable/state poll maps, and the
  actuator registry (logical name → transport + address + on/off, separate `write` and `read`
  endpoints; a `read` may carry a `confirm` naming a variable and a threshold that must also
  hold for `on`).
- **`state.json`** — raw bus readings verbatim, plus derived `battery/voltage`,
  `battery/current`, `battery/voltage_spread`, `SOC_Kf`, `SOC_counted`,
  `time_to_low_battery`, `soc_above_threshold_since`, aux power keys, and a tz-aware ISO
  `timestamp`.

**`system/battery_voltage` and every `mppt*/DC_0_voltage` are raw terminal readings.** The
derived clamp values are `battery/*`. Anything wanting the number the estimator uses reads
`battery/voltage`.

Durable: `soc_state.json`, `mode_requests.json`, `control_config.yaml`, `control_log.jsonl`,
the CSVs. Everything else is regenerated at startup. `control_log.jsonl` is the one durable
file that is *bounded* rather than kept — `log_max_entries` (200), trimmed amortised, so it
holds between N and 2N lines. It is a scrollback, not an archive; the time series is in the
CSVs.

## Actuators, services, modes

Five actuators, resolved from the component classes onto the bus: `multiplus_mode`,
`mppt100_load`, `wallbox_relay`, `ac_inverter_relay`, `ac_other_relay`.

**Every actuator is a service, and a mode is a set of services.** The two layers are still
deliberately named differently, and the rule is now explicit: a **service is named for the
hardware it switches**, a **mode for what the user wants**. Actuator names are the Gateway's
and are left alone — `ac_inverter_relay` is the Tasmota that feeds mains to the Deye, which
is exactly why the service on top of it is called `deye_inverter`.

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
| `ac_other` | `other_ac_supply` | `multiplus_ac` (and `blocked_by` `wallbox`) |
| `cooling` | — | `mppt100_dc_load` |
| `ac_on` | — | `multiplus_ac` — the user's "I want mains" |

A mode's **primary** service is the one it holds only once its prerequisites are satisfied,
and lets go of first on the way down. That is the two-level ordering, and it is why a plug is
not simply another prerequisite: parallel convergence would switch the wallbox on before the
inverter had mains. The primary is required and released in the **declare pass**, not in the
mode reconcile — the tick order is declare → services → modes, so a release deferred to the
reconcile would come one pass too late, and the tick where safety cuts the Deye plug by
direct command would find the service still targeting `on` with the hardware off and drive it
straight back on. There is a test for exactly that.

The plugs gained what commanding them from the mode never had: a retry budget, a `failed`
phase and readback verification. The cost is one extra tick each way — the primary is
required in the declare pass and driven by the service pass, instead of being POSTed inline.

`deye_inverter` has exactly one owner, the `ac_solar` mode, so its reference count never
exceeds one; safety still cuts it as a direct command. **When the breach clears and the mode
is still wanted, the mode switches the plug back on** — it used to take a human. The plug
does not cycle on the limit, because safety *also* blocks the mode, and that block releases
on `warn_max` rather than on the limit (see below).

Only `multiplus_ac` and `mppt100_dc_load` carry a `services.*_default_on` knob. The three
plugs do not: a socket nobody asked for is off, and a setting saying otherwise is a setting
that switches the wallbox on by accident.

### Saying no

A mode is wanted by requesters and forbidden by **blockers**, and one blocker outvotes every
requester. Blocking is not releasing: the requesters stay, so the want takes effect again by
itself when the block lifts. A blocked mode stands down through the ordinary DEACTIVATING
path — effect first, then the services — so nothing new happens on the way out. Both sets are
in `GET /control/modes` (`requesters`, `blockers`) and the detail line names who.

Two sources block, both recomputed every tick like everything else here (silence permits):

- **`ModeBlock(mode, agent, reason)`** — an agent saying "not this one, not now". Its own type
  rather than `ModeRequest(active=False)`, which means something weaker and already taken: that
  is *one agent withdrawing its own want*, and the mode stays up if somebody else still asks.
  Safety uses it for `ac_solar` — the direct command cuts the plug, the block stops the mode
  putting it back on. It arms at the metric's **`warn_max`, not its limit**, and that asymmetry
  is the deadband: cutting solar lowers the voltage, which clears the breach, which would
  re-arm the solar. Nothing latches; the hysteresis is entirely in the two thresholds, and
  both live in `battery` (`warn_max_voltage` 28.4 V under a 28.8 V limit, `warn_max_temp`
  40 °C under 45 °C) so tuning it is a `PUT /control/config`, not a deploy. A warn threshold
  on the wrong side of its limit is refused at load with a 400 naming it — it would otherwise
  read as configured and arm nothing.
- **`blocked_by`** on the mode class — one mode excluding another, declared **one-sidedly, and
  that declaration is the priority rule**: the mode that gives way is the one that says so.
  `ac_other` declares `blocked_by=["wallbox"]` (one AC budget, and the charger wins); `wallbox`
  knows nothing about it. It holds from the moment the wallbox is *wanted* until it is all the
  way back to `inactive`, both edges, so the two never overlap across a tick boundary. It is an
  exclusion, not a hardware interlock — within a single tick both can move.

There is deliberately **no way to forbid a service.** Services are reference-counted precisely
because several modes may legitimately hold one; forcing one off would pull the inverter out
from under a mode still using it. Block the mode, and the service falls away by its own count.

**Actuators are declared explicitly in `component_actuators`; never auto-derive them from
`component_states`.** That list *is* the allowlist of what may be written. Deriving would also
break on the first non-binary state — `mppt_mode` maps `{0: "ideal", 1: "working", 4: "off"}`
with no `"on"` label, so `_on_off_from_mapping` raises. Where a mapping does carry on/off
labels, on/off are derived from it rather than restated; a VE.Direct register carries no
labels, which is why the fan is the one actuator declaring them per-actuator.

Agents name modes and nothing else — no actuator, no value, no ordering. There are two ways
to want a mode, and both are resolved level-triggered on every tick:

- **`ModeRequest(mode, active, agent, reason)`** — "now". Recomputed wholesale every time its
  agent *runs*, so **silence is release** — measured from that agent's own last run, not from
  the tick. An agent that did not run this tick has not fallen silent: `control_main` latches
  each agent's last `(requests, blocks)` and answers for it, which is what makes a per-agent
  interval safe. Without that latch a five-minute agent would release the wallbox on the
  twenty-nine ticks between its runs and re-request it on the thirtieth.
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
has its plan dropped. The last two are explicit in `control_main._forget` — nothing else would
do it — and they drop the latched requests along with the plan, because both are how an agent
speaks. Dropping half would leave a switched-off agent still holding the wallbox on.

An **inhibit suppresses the calendar, it does not delete it.** Requesters are released, the
plans stay; when the inhibit clears, bookings still covering now take effect again.

Every plan — agents' and the user's — is published to `mode_schedule.json` (ephemeral) and
served at `GET /control/schedule`: a flat list sorted by `start`, each row carrying its
`source` and that source's `generated_at`, because a booking computed against a stale forecast
must not render as fact. A user's `generated_at` is when they last edited their calendar; an
agent's is when it last planned.
A booking is a *request*, not a guarantee of hardware state at that instant: the mode starts
activating at `start` and its services converge over the next tick or two.

Any agent may also publish `blocks` (see "Saying no"). Safety additionally carries `commands`
(immediate writes) and `inhibit`, both synchronous and neither a booking — and an inhibit
clears the blocks along with the requesters, since nothing is running to forbid.

### Giving up, and coming back

A service that burns its retry budget goes `failed` and stops driving; a mode whose
prerequisite failed goes `failed` and **keeps holding it** — a fault is something to look
at, not a reason to start tearing the system down. That is a genuine dead end and is meant
to be: nothing clears it on a timer.

**`POST /control/modes/<name>/reset` is the only way out.** Re-requesting the mode is not
one — the mode never released the service, so `require()` is a no-op on a name already in
the set and the burnt `_attempts` stays burnt. `Mode.reset` *releases*, and
`SystemService.release` zeroes the budget; that release is the whole feature. The standing
want is left in place, so a mode still wanted converges again from the next tick. Reset
means "try again", not "and stop wanting it" — that is `deactivate`.

Note the two states that are *not* dead ends and need no reset: a mode is only `failed` on
a **prerequisite**, never on its primary, so a mode whose primary service gave up sits in
`activating` and is recovered by an ordinary `deactivate` (the release happens on the way
down). And an *unknown* readback burns no budget at all — that one waits forever instead,
which is why an actuator that cannot answer needs a `confirm`.

`resets` is a third key in `mode_requests.json`, and the **one edge in an otherwise
level-triggered file**: mode name → timestamp. The Gateway writes a new token, Control
fires once per new value and remembers what it acted on in memory. A flag would have to be
cleared, and clearing it would mean two writers on a file that has exactly one. A restart
replays every token against modes that are all `inactive`, where `reset` is a no-op — so
nothing is persisted. `_load_requests` carries `resets` through every other edit, or a
calendar change would drop a reset Control had not read yet.

### Cadence

**The tick is 10 s** (`safety_interval_seconds`), and it is the only clock that decides how
fast the mode graph converges: bringing the wallbox up costs four ticks — declare,
prerequisites satisfied, primary required, plug confirmed — which was four minutes at 60 s and
is forty seconds now. `state.json` is republished every 5 s, so every tick sees fresh
measurements.

**Every agent runs on its own `interval_seconds`**, declared on the agent class (0 = every
tick) and overridable per agent in `control_config.yaml`, where `null` means "the agent's
own" so the default is written down once. The clock lives in `ControlUnit`, not on the agent —
`run` stays a pure function of `(projection, config)`, which is why an agent test needs no
fixtures. **Safety is the exception and is not configurable**: it runs every tick outside the
agent loop, because its `commands` and `inhibit` are applied synchronously.
`control_interval_seconds` now gates only the projection CSV and its printed summary.

Two things this changed that are easy to miss: a service's `max_attempts = 5` counts *ticks*,
so the retry budget is a 50 s window rather than a 5 min one (it only burns on a successful
read that disagrees — an unknown read costs nothing); and the loop is
`sleep(interval - elapsed)`, so an overrun degrades to back-to-back rather than queueing.

**`log_max_entries` and the agent intervals are coupled**, which is why it is 200 and not a
rounder number. The log fills at roughly two entries a tick (the cycle line plus safety), so
200 is ~16 minutes — comfortably past the 300 s planning interval. `GET /control/agents` finds
each agent's last result by scanning that file, so anything under ~60 makes the planners
intermittently read as `last_result: null`. Shortening an agent's interval is free here;
lengthening one past ~8 minutes means raising this too.

## Things that will bite you

- **`mppt100_load` is asymmetric, and the two sides are unrelated value spaces.** Write is
  VE.Direct register `0xEDAB`, which takes `0/1/4/5` and where **on is `4`**. Read is D-Bus
  `/Load/State`, which only ever answers `0` or `1` and where **on is `1`**. The bus is *not*
  the register read back — nothing on it represents the register's `4` or `5`. Reading the
  register's numbers off the bus is what jammed the fan on the device: `/Load/State == 1` was
  labelled "off", so a fan that had come up read as still off, mismatched every tick, and
  burned its retry budget. The registry models `write` and `read` separately for this reason.
- **The fan's read declares a `confirm`, and both halves must hold.** `/Load/State == 1` says
  the output is switched on, not that anything is turning, so it is confirmed against
  `/Load/I >= 0.1 A` — the current actually flowing, which is what the predecessor verified
  this step on (`wallbox_on.py`, and where `max_attempts=5` comes from). **0.1 A is exactly
  what the fans draw**, so the comparison is `>=` and the threshold sits on the boundary; it
  is a calibration knob on `VictronSolarMPPTWithDCLoad`, measured, not guessed. A `confirm`
  makes a read *total*: `on` is null only when the bus is unreadable.
- **`on: null` is a real answer**, not `false`, for any read without a `confirm`. The service
  waits rather than driving blind — but note it waits *forever*: unknown burns no retry budget
  and has no timeout, so an actuator whose state cannot answer the question needs a `confirm`
  rather than a shrug. Collapsing null to `false` instead flaps the actuator every tick.
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
- **The forecast provider times its *attempts*, not its results.** A success is held for
  `cache_minutes` (60), a failure for `retry_minutes` (5). Without the second clock a failed
  fetch was retried every cycle — three `requests.get` at `timeout=10`, which on a 10 s loop
  means the control loop does nothing else for as long as computepi is down. The cost is that
  a forecast that comes back is picked up up to 5 minutes late.
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
GET  /control/modes | /services | /schedule | /modes/requests | /config | /log | /agents
POST /control/modes/<name>/activate  {} = standing want; {"from","until"} = a booking
POST /control/modes/<name>/deactivate  drops the standing want AND every booking for it
POST /control/modes/<name>/reset     clears `failed`; the standing want is left alone
DELETE /control/modes/bookings/<id>  cancel one booking
PUT  /control/config                 deep-merge into control_config.yaml
GET  /files/runtime/… | /files/data/…
```

`/control/agents` is each agent with its `enabled` flag and the last thing it said, and the
agent list comes from `ControlConfig.agents`' own fields — adding an agent there is the only
edit needed. It finds "the last thing it said" by scanning `control_log.jsonl` backwards, so
**`log_max_entries` has to cover the slowest agent's interval** or a planner reads as
`last_result: null` — see below.

**Two file endpoints, and the split matters to consumers:** `runtime` serves the contracts
(`state.json`, `system_configuration.yaml`, the status files), `data` serves the durable time
series and user intent. A client that wants both — the dashboard does — must sync both.
Subdirectory patterns are listed explicitly in `files.py`'s allow-list (`aggregations/*.csv`
for `daily.csv`) because `list_files` uses `Path.glob`, which does not recurse: a bare `*.csv`
would serve a file on request but never list it, and a client syncing from the listing would
never learn it exists.

A status file Control has not written yet answers **503 naming Control**, never 404. Same for
the registry before the Power System's first publish.

Booking timestamps are ISO 8601; a naive one is taken as the configured local zone, because
that is the clock the user meant. Bad intervals are refused at the boundary (`until` before
`from`, `until` already past, anything that is not a parseable string). Ended bookings are
pruned from `mode_requests.json` whenever the Gateway next writes it — never by Control, which
would break the single-producer rule on that file.

## Working here

```bash
uv run python -m pytest tests/     # 189 tests, green. Keep it that way.
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

**Status:** cut over. All four processes run on the device with full control
(`ECOPS_PARALLEL=0`); the predecessor is stopped. `GET /control/modes`, `/control/services`
and `/control/schedule` say what it thinks it is doing, and `data/control_log.jsonl` says why.
Revert = kill the `ecops` tmux session, restart the old `control_runner.py`.

## Open

- **`R1` is ~4x too large.** The dominant remaining model error. Calibration, not refactoring
  — score a fit with `tools/replay.py`. Worth doing before Control leans on the projection.
- **Capacity is configured at 310 Ah; the recorded data measures ~288 Ah.**
- **`SOC_counted` is path-dependent** and will never match another independently-started
  system. Compare `SOC_Kf`, or compare at a rest anchor.
- **Control switches the Multiplus off when no mode wants it** — confirmed as the wanted
  behaviour for this installation, so `services.multiplus_ac_default_on` stays `false`. It is
  still the one setting that changes what an otherwise idle system does, which is why it is a
  knob rather than a literal in `actuation.py`.
- **The mode graph has never converged against real hardware latency.** Services retry five
  times, then sit in `failed` until someone calls `POST /control/modes/<name>/reset`.
- **The startup sequence has only run against the fake bus.** The 5 s / 10 s waits are a guess;
  they are class attributes on `VictronMultiplusII`, tunable without touching logic.
- **There is no process supervision.** `start.sh` opens four tmux windows and holds a crashed
  pane open so the traceback is readable — nothing restarts. Start ordering is a `sleep 3`,
  which is politeness rather than a dependency: the Gateway and Control both recover on their
  own if they start first. Real units are still to be written.
