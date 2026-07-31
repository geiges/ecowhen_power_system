# Refactor: `ecowhen_power_system` — greenfield 4-process rebuild

> **Status:** Phase 1 complete, with the A12–A15 revisions applied on top. Phase 2 not started.
> See [Implementation status](#implementation-status) at the bottom for exactly where
> it stopped and what to pick up first. **Amendments A1–A15 change the plan below** — read them
> alongside it; A12 in particular supersedes the measurement handling described in Phase 1.

## Context

`victron_system_monitor` has two structural problems that in-place edits keep failing to fix.

**Tangled process responsibilities.** `dbus_logger.py` is the de-facto init process: it discovers
components, writes `system_configuration.yaml`, *and rewrites another process's config file*
(`_regenerate_api_config_commands()` edits `api_config.yml`). D-Bus is written from two places with
no arbiter. Two near-identical poll loops (`dbus_logger` + `aux_logger`) run in parallel.

**Two parallel execution mechanisms.** Atomic `ScheduledAction`s (no verification) and
`Sequence`/`SequenceRunner` state machines (ordered, verified) both end in `execute_action()`.
Inter-actuator dependencies are hand-written as ordered step lists per transition, duplicated for
the reverse teardown, and dispatched on a path that bypasses arbitration.

The fix is a **declarative** model — a **Mode** declares which **System Services** it needs; services
are shared, reference-counted resources owning their own actuation + verification; ordering falls
out of the dependency graph — delivered as a **new package built greenfield**, harvesting the
working parts of the two existing repos.

**Why greenfield rather than in-place migration:** the earlier plan needed an actuation *shim* and
two behaviour-changing cutovers purely to keep one system runnable through the transition. A new
package deletes that constraint. The old system runs untouched until the new one is ready, so
phases 1–3 (read-only) can run **side by side with the live system** and every verification becomes
a differential against a running reference. Only phase 4 is exclusive.

### Baseline reality (measured, not assumed)

`uv run python -m pytest tests/` in `victron_system_monitor`: **75 failed, 113 passed**. CLAUDE.md
claims only `test_aggregation_logger.py` / `test_forecast.py` fail — that note has rotted.

**62 of the 75 are stale test signatures, not broken logic:**
- 46 × `TypeError: CurrentState.__init__() missing 2 required positional arguments` — `control/state.py:25-26`
  gained `mppt_150_power_w`/`mppt_100_power_w` without defaults; tests never updated.
- 16 × `SequenceRunner.__init__() missing 'tz'` — in a file being deleted anyway.

Only ~13 are real. **The production logic is sound; the tests are the rotted artifact.** Consequence:
*the tests cannot be used to decide what is "well working"*. Harvest test **intent**, never fixtures.

Evidence of the cost: `test_safety_is_enabled_by_default` asserts `is_enabled(...) is True`, gets
`None` (bug #2). A load-bearing safety bug has a test, and it has been failing unnoticed because the
suite is red. **The new package must land green and stay green.**

By contrast `../ecowhen_data_api`: **37 passed, 0 failed.** It is a trustworthy harvest source.

### Settled design decisions

1. **Four processes**, in dependency order: Power System → Gateway → Logger → Control.
2. **Power System owns the system model** — topology (`system_configuration.yaml`) and present state
   (`state.json`). Both contracts, one producer. SOC is not measurable: no sensor reports it, it
   exists only as Kalman-filter output over voltage/current/temperature and by counting Ampere hours. 
   While the system is running, the colomn counting method does produce the reliable values for SOC.   
   Estimation is therefore inseparable from polling and belongs here, not in the Logger (where it lives today) or in Control.
   Forward projection is **not** here — that is Control's `projection.py`.
3. **Exactly one process WRITES hardware** (Gateway). Reads are idempotent and conflict-free, so
   the Gateway keeps its own `dbus_read` for verify-readback rather than routing reads through
   `state.json` (which would be stale by `log_interval` and can't see un-polled actuators).
4. **Aux/Tasmota components move into Power System.** PS reads everything; `state.json` gains the aux
   keys; Logger derives `aux_*.csv` from it; `aux_logger.py` dies.
5. **Gateway owns hardware value semantics.** `system_configuration.yaml` declares
   `ac_inverter: {transport: dbus, service: ..., path: /Mode, on: 3, off: 4}`; Control says
   `command("ac_inverter", on=True)`. `ActuatorsConfig`'s value fields all delete. The brain never
   learns a magic number — this matches "services are binary" exactly.
6. **Gateway is absorbed into the new package.** No `ecowhen_data_api` dependency, no cross-repo
   commit, no `dbus_toggle` legacy, no `api_config.yml` round-trip.
7. **Logger is a pure sink** — nothing reads it. Requires PS to export `soc_above_threshold_since`
   (so Control's `minutes_at_full_soc` stops globbing `sim_*.csv`) and to persist its own durable
   `soc_state.json` (so the simulator restore stops reading back the Logger's `sim_*.csv`).

### Confirmed during implementation

- **`config_default.py` is the real calibration.** The `from config import *` override hook was
  vestigial; no `config.py` ever existed. Dropped in the port (see amendment A2).
- Manufacturer references stay (`com.victronenergy.*`, `VictronSystem`, …) — they name real hardware.
- Layout: sibling repo `/home/and/python/ecowhen_power_system`, one package, four `ecops-*`
  entrypoints — matching the `ecowhen_*` sibling convention.

---

## Architecture

```
              D-Bus (read)      HTTP (aux read: Tasmota power)
                   │                 │
                   ▼                 ▼
    ┌──────────────────────────────────────────────────┐
    │ ① POWER SYSTEM              the system model     │
    │   • discover topology + calibration              │
    │   • poll D-Bus + aux → derive battery → sim SOC  │
    │   • persists own soc_state.json (durable)        │
    └──────────────────────────────────────────────────┘
              │                              │
   system_configuration.yaml             state.json
   ── TOPOLOGY CONTRACT ──              ── STATE CONTRACT ──
   poll map + ACTUATOR REGISTRY:        raw values, battery/voltage+current
   name → transport + address           (derived, A12), SOC_Kf, SOC_counted,
        + on/off values                 time_to_low_battery,
   (Tasmota URL declared ONCE)          soc_above_threshold_since,
                                        aux power keys, ISO timestamp
              │                        ┌────┴────┐
              ▼                        ▼         ▼
   ┌────────────────────────┐   ┌─────────────────────┐
   │ ② GATEWAY              │   │ ③ LOGGER            │
   │   • REST server        │   │   state.json → CSVs │
   │   • actuator registry  │   │   dedup / rotation  │
   │     from system_config │   └─────────────────────┘
   │   • SOLE WRITER ───────┼─▶ D-Bus + Tasmota  PURE SINK
   │   • keeps dbus_read    │                    (no readers)
   └────────────────────────┘
        ▲            │  modes_status.json / services_status.json
   REST │            ▼  mode_requests.json ─▶
   ┌─────────────────────────────────────┐
   │ ④ CONTROL   pure brain, no I/O      │
   └─────────────────────────────────────┘
```

**Startup DAG:** ① first; ② and ③ are independent of each other; ④ needs ②'s REST and ①'s
`state.json`, never ③.

**Shared files:**

| File | Producer | Consumers | Lifetime |
|---|---|---|---|
| `system_configuration.yaml` | Power System | Gateway (actuators), Logger (columns) | ephemeral |
| `state.json` | Power System | Logger, Control | ephemeral |
| `soc_state.json` | Power System | Power System (restart restore) | **durable** |
| `mode_requests.json` | Gateway (REST) | Control | **durable** |
| `modes_status.json`, `services_status.json` | Control | Gateway (serves GETs) | ephemeral |
| `control_config.yaml` | Gateway (REST) | Control | **durable** |
| `log_*.csv`, `sim_*.csv`, `aux_*.csv` | Logger | *nobody* ← the point | **durable** |

Ephemeral → `RUNTIME_DIR` (`/dev/shm/ecowhen_power_system`); durable → `DATA_DIR` (`data/`).

---

## Phase 1 — Power System

**Implement.** Discovery → `system_configuration.yaml` (poll maps + actuator registry). Poll loop:
D-Bus vars/states **+ aux HTTP** → calibrate → simulate → `state.json`. Durable `soc_state.json`.

**Harvest verbatim** — physics/numerics, hard to rewrite, no test coverage to catch silent drift:
`battery.py`, `kalman.py`, `simulation.py`, `utils.py`, `components.py`, `aux_components.py`,
`power_system.py`, `config_default.py`. (`components.py` has since been split by manufacturer and
extended — see A10. `battery.py` + `simulation.py` are now one `battery_simulation.py`, and the
measurement correction became a derivation — see **A12**.)

**Plus a startup sequence** — the Multiplus must be run once before its DC measurements exist, or the
battery voltage is silently averaged without it. See A11.

**`config_default.py` — copy the file, do not re-derive.** Cable resistances and voltage offsets are
*physical measurements of this specific installation*. Retyping is silent corruption with no test to
catch it.

**Reuse:** `psystem.get_variables_to_log()` / `get_states_to_log()` (`power_system.py:46,71`),
`save_system_configuration()` (`dbus_logger.py:320`).

**Do not carry over:** the simulator restore that reads back `sim_*.csv` (`dbus_logger.py:532-553`) —
it's a backward edge against the DAG. Use `soc_state.json`.

**New in `state.json`:** full ISO timestamp (today it's `time = "01:04:27"`, no date, and
`control/state.py:31-36` stamps *today* onto it — a state frozen since yesterday reads as current,
so Control cannot detect that PS died). Plus aux keys and `soc_above_threshold_since`.

**Verify.** Fake-bus unit tests off-device (see amendment A3). Then run PS beside the live old logger
and **diff `state.json`** — key-by-key, tolerance on floats.

**Also in phase 1 — the Coordinator design spike.** The Mode/Service/Coordinator model is the entire
point of the refactor and the only genuinely new design, yet it ships last. It is pure logic: write
`service.py`, `mode.py`, `coordinator.py` and `test_coordinator.py` with **fake** services now — no
hardware, no Gateway, no PS. Validates the model before three processes depend on it.

## Phase 2 — Gateway

**Implement.** Flask app; actuator registry read from `system_configuration.yaml`; command endpoints
keyed by **logical actuator name** with **set** semantics (`command(actuator, on: bool)`); sole writer.

**Harvest from `../ecowhen_data_api`** (green suite — trustworthy): `auth.py` (36 lines),
`routes/files.py` (100), `app.py` factory (68), the endpoint dataclasses in `config.py` (177),
`_handle_dbus_read` / `_handle_vedirect_get` / `_handle_vedirect_set` / `_vregd`
(`routes/commands.py:73,111-186` — VE.Direct is a thin `vregd` shell-out, not hand-rolled HEX), and
its **tests** (37, green).

**Generalize, don't invent:** `_resolve_vedirect_port` (`routes/commands.py:111`) already resolves a
component to its port by reading `system_configuration.yaml`. That is the actuator-registry pattern,
already working and tested — extend it to the D-Bus and Tasmota transports.

**Drop:** `dbus_toggle` (`routes/commands.py:80`). It is read-modify-write "advance to next value in
cycle" — it cannot express "go to 3". It happens to work on the 2-value cycles in use today because
it re-reads before advancing, but it discards the target a binary service already knows absolutely
and re-derives it from a read that can be stale, on an actuator the safety agent also writes.
Replace with `dbus_set` (model it on the existing `vedirect_set`, which is already a proper
idempotent setter with `allowed_values`). Add `tasmota_set`.

**Bug #1 dies structurally here.** Today the Tasmota URLs are declared twice — `config_default.py:103-113`
(`aux_comp.TasmotaSmartPlug`, for reading) and `control/config.py:146-150` (`ActuatorsConfig`, for
writing) — and `control/actuator.py:35` reads the wrong attr name, so `getattr` returns `""` and the
safety agent's AC hard-cut **silently never fires**. One declaration in the registry, one consumer.

**Verify.** `curl` each actuator on/off; confirm via `dbus_read` readback. Harvested tests stay green.

## Phase 3 — Logger

**Implement.** `state.json` → `log_*.csv` / `sim_*.csv` / `aux_*.csv` / `daily.csv`. Dedup + rotation.

**Harvest:** `File_Logger` (`utils.py:19`), `AggregationLogger` (`dbus_logger.py:28`), the
`_columns_expanded` column-migration check from `aux_logger.py`.

The apparent coupling in `dbus_logger.py:577-600` is incidental: `File_Logger.log_step`
(`utils.py:114`) returns `row_data` = `data` + a `time` field, and that is the simulator's input. The
CSV writer computes nothing the simulator needs. `state.json` already carries the sim output
(`SOC_Kf`, `SOC_counted`, `time_to_low_battery`), so it is a superset of both CSV rows.

Dedup (`logger_skip_no_changes`) is a *storage* concern and correctly lands here.

**Verify.** Byte-compare CSVs against the old logger's output for the same day.

## Phase 4 — Control

**Implement.** Ship `service.py` / `mode.py` / `coordinator.py` from the phase-1 spike. `gateway_client`
(`command(actuator, on)` / `read(actuator)`). Concrete `AcInverterService`, `FanService`,
`WallboxMode(required_services=["ac_inverter", "fan"])`. Safety trips `coordinator.inhibit(reason)`.

**Harvest:** `control/forecast.py`, `projection.py`, `config.py`, `state.py`, `schedule.py`,
`decision_log.py`, `agents/*`, `api_routes.py`. Note `control/projection.py` imports `battery` — the
Control process needs the battery model too, not just the Power System.

**Fix on the way through** (they don't get "fixed" — they get written correctly):
- Bug #2: `system_safety.py:52` — `is_enabled` is a bare `True` with no `return`. Note the code is
  *further* from CLAUDE.md than the old plan said: it returns `None`, and the runner never calls it
  (`control_runner.py:156` runs safety unconditionally), so the documented "both `enabled=False` and
  `confirmed_disable=True`" protection **does not exist**. Build it deliberately.
- Bug #3: `config.py:174-175` — `from_dict` fallbacks (24 / 200.0) disagree with field defaults
  (96 / 20.0).
- `ActuatorsConfig` value fields (`multiplus_mode_on`, `mppt100_load_on/off/auto`, Tasmota URLs) all
  delete — decision #5.
- `control/state.py:49-108` — `minutes_at_full_soc` / `_read_last_soc_from_sim` stop globbing
  `sim_*.csv`; read `soc_above_threshold_since` from `state.json`.
- Control treats a stale `state.json` timestamp as a safety event.

**Not carried over:** `control/sequence.py`, `sequence_runner.py`, `sequences/`, `control/actuator.py`,
`aux_logger.py`, `rest_api_app.py`, `tests/test_sequence_runner.py`.

**Verify.** `test_coordinator.py` (from the spike) green. Live round-trip:
`POST /control/modes/wallbox/activate` → `services_status.json` shows `ac_inverter`/`fan` converge →
wallbox plug on; deactivate → plug off, then services revert.
`grep -rn "dbus-send\|pydbus" control/` returns nothing.

---

## Testing

**Harvest intent, not fixtures.** A stale test copied forward is worse than no test — it fails for a
reason unrelated to what it names, and a red suite is precisely why bug #2 survived.

**Fix the rot's root cause:** 46 tests broke because each constructs `CurrentState` inline. Put one
shared factory in `conftest.py` — then a field addition breaks one line, not forty-six.

`uv run python -m pytest tests/` — project convention, never bare `python`.

## Verification (end to end)

1. Each phase's own verification above passes before the next begins.
2. **Differential validation, phases 1–3:** new package runs beside the live old system (reads are
   safe; the Gateway only writes when commanded, and Control is not yet running). Diff `state.json`;
   byte-compare CSVs.
3. **Phase 4 is the only exclusive step** — two writers would fight. Revert = stop new Control,
   restart old `control_runner.py`.
4. Start-order smoke: PS → `system_configuration.yaml` + `state.json` appear; Gateway → REST up,
   actuators enumerated from the registry; Logger → CSVs; Control → reads state, actuates via REST.

## Open

- Supervision (`ecops-*` units, ordering) — deferred to a tail-end phase; not part of 1–4.
- `CLAUDE.md` for the new package: write fresh. The predecessor's CLAUDE.md documents behaviour that
  does not exist (the safety-disable protection) and a test baseline four times cleaner than reality.

---
---

# Amendments discovered during implementation

These were found while executing Phase 1 and **change the plan above**. Review these alongside it.

### A1 — `SOC_estimator.py` is dead code; dropped from the harvest

The plan listed it as a "harvest verbatim" item on the strength of CLAUDE.md calling it the
"Orchestrator … coordinates battery model + Kalman filter on 60-second intervals".

It is on **no runtime path**. The only importer in the whole repo is `test_init_soc_est.py`, a
top-level scratch script. The real path is `dbus_logger.py → simulation.py → battery + kalman`.
Its `Measurement` class ("corrects raw voltage/current") duplicates what `components.py:156-185`
already does on the live path:

```python
voltage = raw_voltage_value - (self.connector_R0 * current) + self.voltage_offset
```

…which is applied via `power_system.py:104` → `init_measurement_correction(**measurement_setup)` and
consumed at `simulation.py:170`. **Not harvested.** If `test_init_soc_est.py` matters, it stays in the
old repo.

> Superseded by **A12**: every symbol named in that snippet is now deleted. The conclusion stands and
> got stronger — the duplicated "correction" was the wrong model in *both* copies.

### A2 — the `from config import *` override hook is dropped

`config_default.py` ended with:

```python
try:
    from config import *
except ImportError:
    print('Using default_config.py, create config.py for personal setup ')
```

`config.py` is absent from the repo and **not** gitignored. Confirmed with the user: it never
existed; `config_default.py` **is** the real calibration. The hook is therefore vestigial and is an
active footgun — any stray `config.py` on `sys.path` would silently replace every physical constant
with no error. Removed, with a `ponytail:` comment naming the upgrade path (a *named*, env-pointed
override) if a second installation ever needs one.

### A3 — off-device testing uses a fake bus, not `mock_dbus_service.py`

**The plan's Phase 1 verification ("`mock_dbus_service.py` off-device") does not work.** `pydbus`
cannot import off-device — `ModuleNotFoundError: No module named 'gi'` — even though
`victron_system_monitor/pyproject.toml` lists it as a hard dependency. That is why
`tests/test_mock_dbus.py` is the "1 skipped" in the old suite: it `pytest.importorskip("pydbus")`s.

There is a better seam, and it needs no `gi` at all: **the bus is already injected as a parameter
everywhere** — `is_avaiable_on_bus(dbus)`, `get_device_variables(dbus)`, `get_device_states(dbus)`,
`retrieve_data(bus, …)`. A plain fake object exercises the entire discovery + poll path off-device.

Consequences, already applied:
- `pydbus` is an **optional `device` extra** in the new `pyproject.toml`, not a hard dependency, so
  the package installs and tests off-device.
- `mock_dbus_service.py` was still harvested, but it is for **on-device integration only**.

### A5 — `phoenix` is configured but has never been on the bus, and would crash the estimator

The live device's `state.json` carries only `system`, `mppt150`, `mppt100`, `multiplus`. `phoenix`
is in `config_default.system_components` but has never actually appeared, so its code path has never
run.

It is a landmine. `measurement_components` calibrates only mppt150/mppt100/multiplus. If the Phoenix
inverter were ever plugged in, it would contribute `phoenix/DC_0_voltage` to the battery-voltage
average, `simulation.py:171` would call `voltage_measurement()`, and `components.py:183` would raise
`Exception('Connector resisitance not set')` **on every poll, forever** — SOC estimation dead.

The exception is correct; its timing is not. Added `discovery.check_voltage_calibration()`: any
component that is *actually on the bus* and feeds the voltage average must be calibrated, checked
once at startup with a message naming the component. Explicitly **not** "fall back to the raw
voltage" — a wrong battery voltage feeds the Kalman filter and the SOC drifts plausibly, which is
far worse than a loud stop. The fix for a real Phoenix is to measure its cable, not to guess.

The default test bus mirrors reality (phoenix absent); `phoenix_service()` in `conftest.py` adds it
for the test that proves the guard fires.

> Amended by **A12**: the guard, its message and its reasoning are unchanged, but it now checks the
> bus against the battery's declared derivation sources — `measurement_components` and
> `voltage_measurement()` no longer exist. It also warns (not raises) when a declared source stops
> reporting, which is the A11 case below.

### A6 — `kalman.py` uses `np.matrix`; left alone deliberately

numpy has pending-deprecated the matrix subclass, and it emits 30 warnings per test run. **Not
ported.** Converting to `ndarray` silently changes `*` from matrix-multiply to element-wise, and a
Kalman filter that is quietly wrong is far worse than one that prints a deprecation notice. Filtered
in `pyproject.toml` so the warnings that matter are still visible, with the upgrade path recorded
there: only if numpy actually removes `np.matrix`, and then every `*` needs auditing into an explicit
`@` with the SOC output diffed before and after.

### A7 — the Coordinator spike found two design faults (this is why it was worth doing early)

Both would have shipped into Phase 4 and been found against real hardware.

1. **Activation took two ticks, not one.** The natural tick order — requests, then services, then
   modes — is wrong, because it is `mode.reconcile()` that calls `service.require()`. Services
   therefore converged against *last* tick's requirers. Fixed by splitting
   `Mode.declare_requirements()` out of `Mode.reconcile()` so it can run before the service pass:
   `requests → declare → services → modes`.
2. **An agent that went silent never released its mode.** Requests were merged rather than replaced,
   so a mode stayed latched on for ever the first time an agent was disabled or errored out
   mid-cycle. Agent requests are now recomputed wholesale each tick: silence *is* release.

Two further things the spike settled, which are properties rather than bugs:
- **Activation costs two ticks even with instant hardware** (drive, then confirm on the next look).
  Reading back in the same breath as the write would trust a value the device may not have applied —
  the exact race the design exists to avoid.
- **An unknown readback waits rather than driving blind.** `None` means the device is unreachable;
  writing anyway would command it every tick with no idea whether it landed.

### A8 — `ac_inverter` was an ambiguous name; the registry uses the existing ones

Settled decision #5's example declared the Multiplus actuator as `ac_inverter`. But `ac_inverter` is
already the `short_name` of a **different device** — a Tasmota plug that hard-cuts mains AC to the
Deye inverter (`system_safety.py:27`, `_TASMOTA_SPECS["ac_inverter_plug"]`). Two different things.

The registry therefore uses the existing unambiguous actuator names — `multiplus_mode`,
`mppt100_load`, `wallbox_charge`, `ac_inverter_plug`. Service names (`ac_inverter`, `fan`) are a
Phase 4 concern and a *different layer*: `AcInverterService` drives the `multiplus_mode` actuator.

### A9 — `mppt100_load` is asymmetric, and a naive reconcile loop would flap it

Writing and reading the fan use different transports **and different value spaces**:
- **write** — VE.Direct register `0xEDAB`, `on=4` / `off=0` (`/Load/State` is read-only on this model).
- **read** — D-Bus `/Load/State`, mapping `{1: "off", 4: "on", 5: "USER"}` → `on=4` / `off=1`.

So writing off is `0` but reading off is `1`. A service that assumed one number for "off" would
write 0, read 1, call it a mismatch, and flap the fan until it exhausted its retries. The registry
models `write` and `read` as separate endpoints for exactly this reason; `test_discovery.py` pins
`write["off"] != read["off"]` so it cannot silently collapse.

Everywhere else, on/off are **derived** from the component's existing `mapping` rather than restated —
the Multiplus already declares `{3: "on", 4: "off"}`, so `ActuatorsConfig.multiplus_mode_on = 3` was
always a duplicate. A VE.Direct register carries no labels, which is why the fan is the one place
on/off are declared per-actuator.

### A10 — components split by manufacturer, and `hardware = None` means "not on the bus"

`components.py` was the harvested single file. It is now a thin composition layer over
`manufacturer/victron.py`, which holds `DBusComponent` (everything bus-related: `get_interface`,
`is_avaiable_on_bus`, `get_device_variables/states`) and the Victron classes. The split is what makes
non-Victron hardware expressible: `Generic_DS18B20_Temperature_Sensor` is a 1-Wire sensor bridged onto
the Victron D-Bus by [VenusOS-TemperatureService](https://github.com/Rikkert-RS/VenusOS-TemperatureService)
— it speaks `com.victronenergy.temperature` but is not a Victron product, so it inherits
`DBusComponent`, not `VictronBaseComponent`. Three of them are configured (`DS18B20_DC_multiplus`,
`DS18B20_shunt`, `DS18B20_inside`).

`Generic_Battery` moved the other way: cell chemistry now comes from `specifications.py`
(`SPECS_BATTERY_CELL`), so safety limits are derived from `n_cells × per-cell spec` rather than
retyped per installation. It is a **member of `system_components`** but carries `hardware = None`,
the sentinel for "this component is real but nothing on the bus reports it". Discovery and both
`get_*_to_log()` skip those. That is what lets the battery sit in the system model — where the
simulator can reach it by name — without ever being looked up on D-Bus.

**Consequence for later phases:** anything iterating `psystem.values()` and touching the bus must
skip `hardware is None`. Three places already do; a fourth that forgets will `AttributeError` on
`get_interface`.

### A11 — a Multiplus that has not run since boot has no measurement hardware

Found by the predecessor the hard way: with the Multiplus switched off, `victron_system_monitor`
could not derive the battery voltage. The device is **on the bus and fully discoverable** — only its
`/Dc/0/Voltage` and `/Dc/0/Current` are invalid, and Victron reports "invalid" as an **empty array**,
not an error and not `None`. So nothing fails loudly: the component is discovered as available, both
variables enter the poll map, `poll.retrieve_data` then drops them silently on every poll (`round([])`
raises, the exception is caught), and `simulation.update` averages the battery voltage over the MPPTs
alone. A wrong-but-plausible battery voltage feeds the Kalman filter — the exact failure mode A5
exists to prevent, arriving by a different door.

> **A12** adds a second, cheaper line of defence for the same failure: startup now names any declared
> voltage source that is not reporting. The startup sequence below is still what *fixes* it; the
> warning is what makes it visible when the sequence does not work.

**Phase 1 therefore gained a startup sequence**, `discovery.startup_sequence()`, run before the poll
maps are built so a woken device is discovered in the same pass:

- Duck-typed on `ensure_measurements_available(dbus)`. Only `VictronMultiplusII` implements it; no
  base-class hook for one implementation.
- The sequence: read `/Mode` → `manual_start()` → poll both DC paths every 0.5 s for 5 s → **retry
  once** with a 10 s wait → restore the mode it found (`off` if `/Mode` was unreadable, the safe guess
  — we only got there because the inverter was not running).
- Failure is **not** fatal: it warns and startup continues. A Multiplus that is genuinely dead or
  unplugged must not stop the system model from running. Contrast A5, which does raise, because an
  uncalibrated component produces a *wrong* number rather than a missing one.
- `manual_start` / `manual_stop` take `dbus` as an argument, like every other bus entry point in the
  package, and derive `3`/`4` from the declared `inverter_mode` mapping — no restated integers (A9).

**This is the one place the Power System writes D-Bus**, against decision #3. Deliberate and bounded:
it runs at startup, before the Gateway process exists and before Control can command anything, and it
restores what it found. Nothing else in the process may write. If a second component ever needs
waking, it implements the same method — it does **not** get its own write path.

Two D-Bus details worth keeping, both learned here:
- `BusItem.SetValue` takes a **variant** and answers `0` on success / `-1` on refusal. On the device
  the value must be wrapped (`GLib.Variant("i", …)`); `gi` is imported lazily inside the helper for
  the same reason `power_system_main.get_bus()` imports pydbus lazily — the package must stay
  importable off-device.
- Reads that matter must **type-check** the result. `_read_number()` returns `None` for the empty
  array, which is the only way to tell "invalid" from a real reading.

**Cost, accepted:** waking a deliberately-stopped Multiplus energises AC output for a few seconds at
every restart. That is the price of a correct battery voltage, and it happens only when the
measurements are actually missing.

### A4 — the harvest source was a dirty tree

`victron_system_monitor` has uncommitted changes. `components.py` was copied from the **working
tree**, not `HEAD`. Inspected: the diff is whitespace plus two `flaot`→`float` typo fixes — benign,
and strictly better than `HEAD`. Recorded so nobody later wonders why the copy ≠ the last commit.

Also uncommitted there and relevant to **Phase 4**: `tests/test_wallbox_optimal_charge.py` (−17 lines),
and `REFACTOR_PLAN.md` itself was never committed.

### A12 — the battery is one object, and its clamp values are derived, not corrected

Applied 2026-07-31, after Phase 1 and before Phase 2. Two changes, requested together because
they touch the same code. **Supersedes the parts of A1, A5 and A11 that name deleted code**, and
supersedes the "correct" step in the architecture diagram and the Phase 1 harvest list.

**1. `battery.py` + `simulation.py` → `battery_simulation.py`.** `System_Simulation` was a battery
simulation wrapping a `Battery` it was the sole user of; the Thevenin model, the coulomb counter and
the Kalman filter all describe the same battery and were separated only by file. Now one class,
`Battery_Simulation`, which passes `self` to `ExtendedKalmanFilter` (which only ever needed `R1`,
`C1`, `total_capacity`, `OCV_model`).

Two things fell out of the merge:
- **`Battery._RC_voltage` and `Battery.voltage` deleted.** The EKF already owns the RC state
  (`x[1,0]`), and the counting copy was both dead and *unstable*: `simulation.py:182` called
  `update(-time_delta, …)`, so `exp(-Δt/τ)` evaluated as `exp(+Δt/τ)` and that voltage diverged
  geometrically. Nothing on the live path read it, which is why it survived. The negative-`time_delta`
  hack went with it — counting now takes positive seconds and charge-positive current.
- **The per-cell OCV curve moved to `specifications.py`** (`SPECS_BATTERY_CELL["lifepo4"]["ocv_curve"]`).
  It is a chemistry property and `cell_type` was already the key there. `battery.py` declared two of
  these lists back to back, the first silently dead — that ambiguity had to be resolved to merge at
  all, so it is now pinned by `test_port_fidelity`.

**Verified as a numerical no-op.** The pre-merge `System_Simulation` was extracted from `HEAD` and run
beside the new class for 200 polls over a charge/discharge swing. Worst absolute difference:
**0.000e+00** on derived voltage, derived current, `SOC_Kf`, `SOC_counted` and `time_to_low_battery`.
The one deliberate behaviour change is inert at present calibration: charge efficiency now applies
only while charging (a 1 A discharge really does remove 1 A), and it is configured at 1.0.

**2. "Measurement correction" was the wrong model.** The old code took each component's `DC_0_voltage`,
"corrected" it via `VictronBaseComponent.voltage_measurement()`, and averaged the results. But no
component's reading is wrong, and its error is unknown and uninteresting. What is actually happening is
the *derivation of a quantity nobody measures*: there is no shunt at the battery clamps and no BMS on
the bus, so the clamp voltage is inferred from each source's terminal voltage less the drop its own
current causes:

```
V_clamp = V_source + offset_source - R_source * I_source
```

Identical arithmetic, honest name — and it relocates the constants. `connector_R0`/`voltage_offset`
describe the **cable between a source and the clamps**, not the source, so they are declared on the
battery (`Generic_Battery.init_derrived_measurements`, called from `config_default`) instead of being
installed onto each component. Consequences:

- **Deleted:** `VictronBaseComponent.init_measurement_correction` and `voltage_measurement`,
  `config_default.measurement_components`, and `power_system.init_power_system` — which, once it no
  longer installed calibration, was a one-line wrapper around `Power_system(config.system_components)`.
- **`state.json` gains three keys and loses none:** `battery/voltage`, `battery/current`,
  `battery/voltage_spread`. Per-component "corrected" values are gone; every `mppt*/DC_0_voltage` and
  `system/battery_*` key now carries **exactly what the bus said**. The simulator used to overwrite
  `system/battery_voltage` with its own result, so a measurement and an estimate were indistinguishable
  in the contract — they no longer are. **Phase 3/4 note:** anything that read `system/battery_voltage`
  expecting the corrected number must read `battery/voltage`.
- **`battery/voltage_spread`** (max − min across the per-source estimates) is the one diagnostic kept.
  Sources agreeing to a few mV are right about the clamps; a widening spread is a drifting cable or a
  stale constant, and nothing else in the system would ever report it.
- **Battery current is unchanged**: still the GX aggregate less each source's idle draw. The GX already
  sums the DC sources, and `const_consumption` remains a property of the device, so it stays on the
  component.
- **A5's guard survives with a new source of truth.** `check_voltage_calibration` no longer reaches
  into a component for `connector_R0`; it compares the components on the bus reporting `DC_0_voltage`
  against the battery's declared sources. Same failure, same message, same reason not to guess a
  number. It now *also* warns when a **declared** source is not reporting — which is the A11 failure
  mode, previously silent.

### A13 — the Kalman filter's current sign was inverted (found by A12, **fixed**)

`kalman.py:94` builds `B = [[-Δt/capacity], [R1(1-exp_coeff)]]`, which expects **discharge-positive**
current. It is fed **charge-positive** current — Victron's convention, and the coulomb counter's. The
same +30 A charging input for one hour:

```
EKF predict step : SOC 0.5 → 0.4032   (and RC state +1.20 V, wrong sign against Hx = OCV − x[1])
coulomb counting : SOC 0.5 → 0.5968
```

Exactly mirrored. The measurement update then drags the estimate back toward the OCV-implied SOC every
step, which is why the output looks plausible and why this survived the port: the prediction is fighting
the measurement rather than informing it. It matters because `SOC_Kf` is what rails the *counted* SOC
to 0.0/1.0 near the ends of the battery.

**Pre-existing** — identical in `victron_system_monitor`, so every SOC estimate this installation has
ever produced was made with the prediction step inverted.

**The fix, and why it needed evidence.** Negating `u` at the two call sites also moves the noise gate at
`kalman.py:104` (`if u>10 or u<-3`), which is asymmetric and therefore means a different thing under
each convention — "inflate the measurement noise when charging above 10 A or discharging above 3 A"
becomes "…discharging above 10 A or charging above 3 A". That is a calibration question, not a refactor
decision, so it was settled against 37 days of the predecessor's recorded `sim_*.csv` (2026-06-01 →
07-07, 538 603 rows, currents −101 A … +79 A) rather than by argument. Three variants, capacity 210 Ah
as recorded:

| variant | anchor RMSE | innov mean | innov RMS | d(innov)/dI | Kf−count RMSE |
|---|---|---|---|---|---|
| A  as-is | 0.0150 | **−0.08660** | 0.78889 | +0.04016 | 0.1386 |
| B  sign fixed, gate follows | 0.0154 | **+0.00266** | 0.60399 | −0.03093 | **0.1233** |
| C  sign fixed, gate meaning preserved | 0.0157 | +0.02180 | 0.60331 | −0.03088 | 0.1240 |

**B, applied.** Mean innovation — the standard test that a filter is consistent with its own
measurements — falls **32×**, from −87 mV to +2.7 mV (10×, −92 mV → −9.1 mV, at 310 Ah). Innovation RMS
drops 23 % at both capacities, and the filter and the coulomb counter move 11 % closer together. That
margin is the sign bug and it is not close.

B over C is much finer: B has the smaller mean innovation at both capacities but by 8× at 210 Ah and
only 1.2× at 310 Ah, with innovation RMS a wash. B is taken on that consistent-but-narrow edge plus
being the smaller change — the gate follows the sign rather than having its thresholds restated. **C is
not ruled out by this data**; revisit it if the gate is ever re-tuned, alongside A15.
The implementation names the convention at the call site (`discharge_positive = -current`) instead of
burying a minus sign, and `test_the_filter_is_fed_discharge_positive_current` pins the handoff, because
nothing about the output looks wrong when it regresses.

**The anchor RMSE column is not evidence and must not be read as such.** Anchors are rest points, where
the prediction contributes almost nothing and every variant relaxes to the OCV-implied SOC — which is
exactly how the anchor itself is computed. All three land within 0.0007 of each other. The metric is
near-circular by construction; it is in the table to be discounted, not to be weighed.

**What the same replay says about the model** (see A15): the residual `d(innov)/dI` is still −0.031 Ω
after the fix, so the model still over-predicts how far the voltage moves with current.

### A14 — the recorded data measures the pack at ~285 Ah, not 210

Same replay, independent of the filter. Between two genuine rest points the SOC change is known by
inverting the OCV curve at each end, and the charge that moved is ∫I dt, so their ratio is a *measured*
capacity. 58 clean anchors over the 37 days, 27 pairs swinging more than 10 % SOC:

```
implied capacity   pooled 288 Ah   median 280 Ah   IQR 247-313 Ah   n=27
```

`config_default` said **210 Ah** for the whole of the recorded period, so coulomb counting was
overstating every SOC swing by roughly a third. That is a large part of why `SOC_Kf` and `SOC_counted`
sit ~12 points apart in these logs. The working-tree change to **310 Ah** moves decisively toward the
measurement and is supported by it; 288 would be better still, but the difference is inside the spread.

Two caveats. LiFePO4's mid-range OCV curve is flat — at 50 % SOC one percentage point of SOC is 15.6 mV,
so 10 mV of noise is ±0.6 % SOC per anchor and short swings divide two noisy anchors by each other. The
pooled figure weights by swing size and is the one to trust. And this is an *effective* capacity at
these temperatures and rates, not a nameplate.

**A trap found while building this, worth keeping:** low current alone does not mean "at rest". During
absorption the charger holds the pack at 28.3 V while the current tapers below 1 A — above the top of
the OCV curve, where the inversion clamps to 100 % and produces nonsense (one such pair implied 97 Ah).
Anchors are only valid when the voltage lands strictly inside the curve's invertible range.

### A15 — R1 is roughly 4× too large (open, not acted on)

The innovation's slope against current is the resistance the model still owes after its own R0 and RC
terms are removed. Post-fix it is **−0.031 Ω**, i.e. the model moves the predicted voltage far more per
amp than the pack actually does. Solving the two variants' slopes for the pack's effective resistance
gives ≈0.010–0.019 Ω against the model's `R0 + R1 = 0.05 Ω`.

So the RC branch is oversized by something like 3–4×, and it is the dominant remaining error — larger
than the sign bug was on the innovation RMS. **Not acted on:** unlike the sign, this is genuine
calibration with no correct answer derivable from first principles, and re-tuning `R1`/`C1` (and
plausibly `R_var`, which at 0.5 V² assumes far more voltage noise than the data shows) is a fitting
exercise on its own. The replay harness in `tools/replay.py` is what such a fit should be scored
with. Worth doing before Phase 4 leans on this model, but it is not a refactor task.

---

# Implementation status

**Phase 1 complete**, plus the A12/A13 revisions applied on 2026-07-31. 59 tests, green, no warnings.

```
uv run python -m pytest tests/      # 59 passed
```

## What Phase 1 delivered

**① Power System** — runs end-to-end and publishes both contracts.

| File | Role |
|---|---|
| `paths.py` | `RUNTIME_DIR` (ephemeral) vs `DATA_DIR` (durable), both env-overridable |
| `discovery.py` | startup sequence (A11) → bus walk → topology contract, incl. the resolved actuator registry + the calibration guard (A5) |
| `poll.py` | D-Bus + aux → estimator → state contract; `SocTracker`; durable SOC persistence. Publishes raw readings verbatim (A12) |
| `battery_simulation.py` | the battery: Thevenin model, clamp-value derivation, coulomb counting, EKF — one class (A12) |
| `power_system_main.py` | the process: `setup()` once, then poll; `--once` for a single shot |
| `components.py` + `manufacturer/victron.py` + `specifications.py` | component layer split by manufacturer; `hardware = None` for non-bus components (A10); cell OCV curve (A12) |
| `config_default.py` | gained the `actuators` declaration (A8, A9), the three DS18B20 sensors (A10), and the battery's derivation declaration in place of `measurement_components` (A12) |

**The Coordinator spike** — `control/{service,mode,coordinator}.py`, validated by
`test_coordinator.py` (18 tests, fakes only, no hardware). Found two real design faults before
anything depended on them; see A7.

**Tests** — 59 across `test_port_fidelity` (calibration pinned), `test_discovery`, `test_poll`,
`test_power_system_integration` (the *real* `Battery_Simulation` + Kalman filter), `test_coordinator`.
`conftest.py` carries the `fake_bus` seam (A3) and builds shared objects through factories, which is
the fix for the rot that put the predecessor's suite 75-red. The fake bus now also **writes**
(`SetValue` writes through and answers `0`), which is what lets the startup sequence — and, in
Phase 2, the Gateway's actuation — be tested off-device.

## Phase 1 verification results

1. **The real entrypoint runs off-device.** `power_system_main.main(["--once"])` against a fake bus:
   wrote both contracts, exit 0, the harvested Kalman filter estimated SOC from OCV at 51.65%.
2. **Differential against the live device — clean.** New `state.json` vs the real
   `victron_system_monitor/data/state.json`:
   - **0 keys missing.** All 28 live keys present; the new contract is a strict superset.
   - **+7 added**, every one intended: 4 aux keys (`wallbox/*`, `ac_inverter/*` — decision #4),
     `timestamp` (the ISO fix), `soc_above_threshold_since` + `minutes_at_full_soc` (decision #7).
   - **A12 then added 3 more** (`battery/voltage`, `battery/current`, `battery/voltage_spread`) and
     changed the *meaning* of `system/battery_voltage` and every `mppt*/DC_0_voltage` back to the raw
     reading. The key-level differential above therefore no longer covers those; re-run it against the
     device with A12's mapping in mind.
3. **Actuator registry resolves**: `multiplus_mode`, `mppt100_load`, `wallbox_charge`,
   `ac_inverter_plug` — with `multiplus_mode.write` = `{dbus, com.victronenergy.vebus.ttyUSB2,
   /Mode, on: 3, off: 4}`, derived from the component mapping, not restated.

**Still outstanding from the plan's Phase 1 verification:** the differential has only been run
key-by-key off a fake bus. Running the new Power System *beside the live logger on the device* and
diffing values (not just key names) has not been done — it needs the hardware.

## Pick up here (Phase 2 — Gateway)

Per the plan above, plus what Phase 1 learned:
- The registry is already resolved and waiting in `system_configuration.yaml`; the Gateway consumes
  it and imports no component code.
- `dbus_toggle` is **not** being ported — the Gateway needs `dbus_set`, `tasmota_set`, and it keeps
  `dbus_read` for verify-readback (decision #3).
- Harvest from `../ecowhen_data_api` (37 tests, green): `auth.py`, `routes/files.py`, `app.py`,
  the endpoint dataclasses, and `_handle_dbus_read` / `_handle_vedirect_*` / `_vregd`.
- **A9 is a live trap for the Gateway**: `mppt100_load` writes VE.Direct and reads D-Bus, with
  different integers for "off". The registry already models this; the Gateway must honour both halves.

## Carried risks

- **`R1` is ~4x too large (A15).** Open. The dominant remaining model error now that A13 is fixed, and
  worth a calibration pass against `tools/replay.py` before Phase 4 leans on this model.
- **Capacity is configured at 310 Ah; the data measures ~288 Ah (A14).** Much better than the 210 Ah it
  replaced, but still the largest single lever on coulomb-counting accuracy.
- **A12 changed the state contract's meaning, not just its keys.** `system/battery_voltage` and the
  per-component `DC_0_voltage` keys are now raw readings; the derived clamp values live under
  `battery/*`. Nothing consumes them yet — Phase 3 and 4 must not carry over the old assumption.
- **Three of the four `ecops-*` entrypoints still point at modules that do not exist**
  (`gateway_main.py`, `logger_main.py`, `control_main.py`). `uv sync` does not validate script
  targets, so this stays latent until someone runs one. Phases 2–4 each create one.
- **No initial commit yet.** Left deliberately — not committing without being asked.
- **`running_since` changed format**, from `"26-07-07 00:29"` to ISO. Same key, so the key-level
  differential did not flag it. Nothing was found reading it, but Phase 3/4 should confirm.
- **The differential compares key names, not values.** See above — real validation needs the device.
- **`phoenix` will now fail startup if it is ever plugged in** (A5, retargeted by A12). That is
  intended, and the error says which cable to measure, but it is a behaviour change from the
  predecessor, which would instead have crashed on every poll.
- **The startup sequence has only been exercised against the fake bus** (A11). The 5 s / 10 s waits
  are a guess at how long a real Multiplus takes to bring its DC measurement hardware up; they are
  class attributes on `VictronMultiplusII` so they can be tuned on the device without touching logic.
  Whether the measurements *stay* readable after the inverter is stopped again — the assumption the
  whole sequence rests on — is untested on hardware.
- **Waking the Multiplus energises AC output for a few seconds** at every restart where its
  measurements are missing. Bounded and intended, but it is a side effect on real loads.
