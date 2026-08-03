# Refactor: `ecowhen_power_system` — greenfield 4-process rebuild

> **Status:** all four phases complete, nothing run on real hardware yet.
> See [Implementation status](#implementation-status) at the bottom for exactly where
> it stands and what to pick up first. **Amendments A1–A20 change the plan below** — read them
> alongside it; A12 supersedes the measurement handling described in Phase 1, A16/A17 together
> supersede most of Phase 2's harvest list, A18 supersedes Phase 3's, A19 adds the
> on-device deployment, and A20 supersedes Phase 4's scheduling layer.

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

### A16 — actuators are declared on the component classes, not in `config_default`

Applied 2026-08-01, before Phase 2. **Refines settled decision #5.** The rule is now: *a component
class carries everything the Gateway needs to reach the hardware it describes.* Nothing about how
to drive a device is written anywhere else — `config_default` declares no actuators at all.

**Most of this is already true.** `_resolve_endpoint` derives `multiplus_mode`'s
`{path: /Mode, on: 3, off: 4}` from `VictronMultiplusII.component_states`, not from the registry
(A9), and the Gateway imports no component code (decision #6). What is left in
`config_default.actuators` divides on one line — *device capability* vs *this installation's
wiring*:

| declared today in `config_default.actuators` | what it really is | goes to |
|---|---|---|
| `transport`, `state=` / `vedirect_set=` | this device model can be driven this way | **component class** |
| VE.Direct `on=4, off=0` | register `0xEDAB`'s value space | **component class** |
| write-VE.Direct / read-D-Bus asymmetry | `/Load/State` is read-only *on this model* | **component class** |
| logical name, `description` | what this installation calls it, and why | config / instance kwarg |

**The change.** A class-level `component_actuators` list, alongside the existing
`component_states` / `vedirect_sets` (same pattern, same default `[]` on `DBusComponent` and
`BaseAuxComponent`):

```python
class ActuatorType(NamedTuple):        # manufacturer/__init__.py
    basename: str          # -> logical name f"{short_name}_{basename}"
    write: dict            # {transport: 'vedirect', vedirect_set: 'load_control', on: 4, off: 0}
    read: dict             # {transport: 'dbus', state: 'load_state'}
    description: str = ""
```

It lives in `manufacturer/__init__.py`, not in `victron.py`, because the D-Bus components and the
HTTP `aux_components` both declare actuators and discovery resolves both through one path.
Declared on `VictronMultiplusII` (`mode`), `VictronSolarMPPTWithDCLoad` (`load`) and
`TasmotaSmartPlug` (`relay`). `build_actuator_registry` walks `psystem` + `aux_components` instead
of `config.actuators` and resolves each entry through the *unchanged* `_resolve_endpoint`;
`config_default.actuators` is gone.

**The emitted `system_configuration.yaml` is unchanged in shape** — same keys, same values, only
the two Tasmota actuators renamed. That is the point: **Phase 2 is entirely unaffected**, so this
cost nothing against the work that comes next. 59 tests still green.

**Three constraints, each learned from something already in this plan:**

1. **Declare explicitly; do not auto-derive actuators from `component_states`.** `mppt_mode` maps
   `{0: "ideal", 1: "working", 4: "off"}` — no `"on"` label, so `_on_off_from_mapping` raises. A
   rule like "every state with a mapping is an actuator" fails on the first non-binary one, and
   would also silently expose every state as writable. The explicit list *is* the allowlist.
2. **Names.** `multiplus_mode` and `mppt100_load` already read as `{short_name}_{basename}` and
   fell out for free. `wallbox_charge` / `ac_inverter_plug` did not — one `TasmotaSmartPlug` class,
   two different suffixes. Nothing consumes these names yet (Control is Phase 4), so both were
   **renamed to `wallbox_relay` / `ac_inverter_relay`** rather than adding a per-instance override.
   A8's actual requirement — that the actuator name not collide with the `ac_inverter` aux
   component — still holds. Same reasoning applies to `description`: it is the device's
   (`"Tasmota relay output"`), and what this installation wired it to is a comment in
   `config_default`, which also lists the four resulting names.
3. **This does not reopen A12.** `connector_R0` / `voltage_offset` describe the *cable between a
   source and the clamps*, not the source, which is exactly why they were moved off the components
   onto `Generic_Battery`. "All hardware config lives on the components" must not pull them back.
   Same test: a Victron MPPT 100/20 has register `0xEDAB` wherever it is installed; it does not have
   11 mΩ of cable.

**Fell out with it:** `StateType.toggle_values` (three uses) existed only to feed `dbus_toggle`,
which Phase 2 is dropping anyway. Once actuators declare on/off it had no readers — deleted.

**Tests.** `test_discovery.py` pins the registry by *content* — `write["off"] != read["off"]`,
on/off derived from the mapping, the Tasmota URL sourced from the aux component. All three intents
survive verbatim. The two negative tests got *better* out of the move: they used to build a fake
config object to inject a broken declaration, and now monkeypatch `component_actuators` on the
class that would really carry it.

### A17 — the resolved registry deleted most of the planned Phase-2 harvest

Applied 2026-08-01, building Phase 2. The plan budgeted a large harvest from
`ecowhen_data_api`; the actuator registry (decision #5, A16) makes over half of it dead on
arrival, because **that repo's config file and this package's topology contract are the same
information**, and only one of them is derived from the hardware.

**Not harvested, and why:**

| planned | why not |
|---|---|
| `config.py`'s 177 lines — `CommandEndpoint`, `DbusReadEndpoint`, `DbusToggleEndpoint`, `VeDirect*Endpoint` + their YAML parser | this *is* the registry, only hand-written. Discovery already emits transport, address, register and on/off, resolved off the live bus |
| `_resolve_vedirect_port` | it read `system_configuration.yaml` to turn a component into `/dev/ttyUSB1`. Discovery now does that once, at the source |
| `_handle_shell_command` + `CommandParam` validation | the Gateway runs no shell templates. Nothing in its job description needs one |
| `_handle_vedirect_set`'s `allowed_values` check | the client sends a **boolean**. No integer from outside ever reaches hardware, so there is no untrusted value left to validate — and discovery already checked the registry's values against the register at startup |
| `dbus_toggle` | as planned: read-modify-write cannot express "go to 3" |

**Harvested as planned:** `auth.py` (verbatim, fails closed, env var renamed to `ECOPS_API_KEY`),
`routes/files.py`, the `create_app` factory shape, and `_vregd` — VE.Direct stays a thin shell-out,
not hand-rolled HEX.

**File endpoints are derived, not configured.** `paths.py` already owns both roots, so the Gateway
serves two fixed endpoints — `runtime` (RUNTIME_DIR) and `data` (DATA_DIR) — instead of naming them
again in a YAML that could only ever drift. Both of `files.py`'s security checks survive verbatim:
traversal guard, then allow-list.

**Three things Phase 2 had to settle:**

1. **A POST never reads back.** It reports the value it wrote and stops. A7 established that
   convergence takes two ticks *on purpose* — confirming in the same breath as the write would
   trust a value the device may not have applied. Reading back is the caller's next `GET`.
2. **`on: null` is a real answer.** `/Load/State` reports `5` ("USER"), which is neither the on nor
   the off value. The read route returns `{"value": 5, "on": null}`, and a service must treat that
   as unknown and wait — the other half of A7's "an unknown readback waits rather than driving
   blind". Collapsing it to `false` would drive the fan every tick.
3. **Start order is not load-bearing.** The registry is re-read when `system_configuration.yaml`'s
   mtime changes, so a Gateway started before the Power System — or one still holding addresses
   from before a PS restart — recovers by itself. Before the first publish it answers 503 naming
   the Power System, rather than 404 "no such actuator".

**Two small moves out of Phase 1 code**, both so the Gateway does not have to import the Power
System (which would drag in numpy, pytz and the whole component layer for the sake of 20 lines):
`get_bus()` moved from `power_system_main` into `manufacturer/victron.py`, next to the other bus
primitives, and `_read_number`/`_write_number` lost their underscore. They encode Victron BusItem
conventions — empty-array-means-invalid, `SetValue` answers 0/−1 — which by A16's rule is exactly
what belongs in a manufacturer module.

**And one more application of A16:** `value_type` (the `vregd` type prefix, `un8`) was a config
field in `ecowhen_data_api`. It is a property of the register, so it is now a field on
`VeDirectSetType` and rides into the registry.

**Not built, deliberately:** the REST endpoints for `mode_requests.json` and `control_config.yaml`.
The Gateway is their producer per the shared-files table, but their schema is Control's, and
Control does not exist yet. Building them now would be guessing at a contract Phase 4 defines.

### A18 — `File_Logger` was harvested in behaviour, and the daily aggregates were wrong

Applied 2026-08-01, building Phase 3.

**`utils.py` is deleted.** It was harvested in Phase 1 in anticipation of this phase and held
`File_Logger` plus two date helpers that nothing ever imported. `File_Logger`'s *behaviour* is
what mattered and it is now `logger.CsvLogger` — rotation, dedup and growable columns — with three
faults that did not survive the rewrite:

- **Dedup off wrote nothing at all.** The condition was
  `if logger_skip_no_changes and (data_changed or is_new_day)`, so setting `logger_skip_no_changes
  = False` sent every sample down the "cache it, write nothing" branch. Never noticed because it
  was never turned off. Now `if not skip_no_changes or changed or is_new_day`.
- **An `UnboundLocalError` waiting for a quiet moment.** `now_str` is only bound when the data does
  *not* already carry a `time` key, but the dedup branch printed it unconditionally — and `sim_row`
  did carry `time`. It never fired only because SOC drifts every step, so two consecutive simulator
  rows were never equal.
- A `print("... {now_str} ...")` that was missing its `f`.

**What was worth keeping exactly:** the dedup holds back the *last* unchanged row and flushes it
when the value finally changes, so a plateau records both when it started and when it ended. Drop
that and a steady reading is indistinguishable from a dead logger. Pinned by
`test_identical_samples_collapse_but_the_plateau_keeps_its_end`.

**The column split is derived from the contract, not enumerated.** `log_` columns are the poll
map's variables plus the packed state code; `aux_` columns are the aux components' declared
variables; **`sim_` is the residue** — anything in `state.json` that is none of those and not one
of seven named Control-facing keys. That is why A12's three new `battery/*` keys reached the CSV
without anyone maintaining a column list, and why the next estimator output will too.

#### The aggregation differential — the predecessor was wrong on three days

The new `DailyAggregator` was run over the predecessor's own `log_*.csv` / `aux_*.csv` and its
output diffed against its own `daily.csv`. **151 days.** Six of the eight columns —
`AC_power_output`, both aux energies, and all three max/min — are **exactly 0.000000** different.
The two that move do so on three days, and in every case the new number is the correct one:

| day | column | cause |
|---|---|---|
| 26-06-27, 26-06-29 | `solar_total` (+2.23, +0.25 kWh) | `aux_*.csv` contains **one row of NUL bytes** — a power cut mid-write. The predecessor's aggregator threw the whole day away over that one sample, leaving the aux energies blank *and* dropping the Deye's output from `solar_total`. `_integrate` now skips the unparseable row and integrates across the gap |
| 26-06-25 | `DC_load_power` (+0.0158 kWh) | the day was aggregated **while it was still running**. `update_daily_aggregates` builds its date list with `range(1, time_delta.days)`, which is *empty* on a normal one-day rollover — so days were only ever aggregated by the full rebuild, and one caught mid-evening was never recomputed. Reconstructed exactly: the old row matches this day truncated at 21:13, where solar had already finished (hence `solar_total` matched) but the fan ran on for another 2.7 h |

`DailyAggregator` cannot reproduce either: it aggregates only dates *strictly before today*, records
which dates it has written, and survives a corrupt row. `test_each_completed_day_is_aggregated_exactly_once`
and `test_one_corrupt_row_does_not_cost_the_whole_day` pin both.

**Also changed deliberately:** a day with no readings gets an empty max/min cell rather than the
predecessor's `-inf` / `inf`, which is not a temperature.

### A19 — parallel deployment, and the two flags that make it an interlock

Added 2026-08-01, before Phase 4. `deploy/` — `push.sh`, `install.sh`, `start.sh`,
`config.sh`, `compare.py`, and a README. See that README for the procedure; what matters
to the plan is *why the shape is what it is*.

**The plan claims phases 1–3 can run beside the live system because they are read-only.
That was not quite true.** Two paths write:

- `discovery.startup_sequence()` wakes the Multiplus by writing `/Mode` (A11). It only
  fires when the DC measurements are missing, which will not normally happen with the old
  system running — but "will not normally happen" is not a safety property. It would pulse
  AC output underneath a live system.
- The Gateway writes whenever commanded, and it is on the network.

Both are now switchable, and `ECOPS_PARALLEL=1` (the default) sets them:

| flag | effect |
|---|---|
| `ecops-power-system --no-startup-sequence` | the process cannot write D-Bus at all |
| `ecops-gateway --read-only` | every `POST /actuators/...` is a 403; reads unaffected |

Structural, not a promise to remember. Pinned by `test_read_only_discovery_never_writes_the_bus`
and `test_read_only_refuses_actuation_but_still_reads`.

**Collisions with the predecessor, all separated:** its REST API binds **5100**
(`api_config.yml`), so the Gateway takes **5101**; separate project dir, separate `data/`
(absolute, so it never resolves against a process's CWD), tmpfs for the contracts, and the
tmux session is `ecops` rather than `victron`. The one accepted cost is that the two Tasmota
plugs and the Deye get polled by both systems.

**Deployment follows the predecessor's proven recipe** — `uv venv --system-site-packages
--python /usr/bin/python3` — but *not* for the reason first assumed, and the difference
broke the first install on the device:

- **`pydbus` is pure Python** (`py2.py3-none-any.whl`, no compiled parts) and comes from
  PyPI, into the venv.
- **`gi` (PyGObject) is the OS package** — compiled against the system libraries, reachable
  only through `--system-site-packages`.

The predecessor lists `pydbus` as a **hard dependency**, so a plain `uv sync` installs it
without anyone thinking about it. Here it is an *optional* `device` extra, precisely so the
suite installs off-device (A3) — so `uv sync --no-dev` silently left it out and
`import pydbus` failed. `install.sh` now syncs `--extra device`, and every `uv run` in
`start.sh` passes it too.

The check is **two checks**, because `import pydbus` imports `gi` and one test cannot tell
the failures apart: `No module named 'pydbus'` means the extra is missing;
`No module named 'gi'` means the venv cannot see the OS bindings. The first version of the
script swallowed the real error into `2>/dev/null` and printed a generic warning, which is
exactly the wrong thing to do at the one moment the message matters.

**The second failure was worse, and `.python-version` was the cause.** With `gi` still
unimportable after the extra was fixed, the mechanism turned out to be that **`uv sync`
silently destroys and rebuilds `.venv` on a different interpreter** whenever something asks
for one — and a `.python-version` file is enough. The rebuilt venv loses
`--system-site-packages`, and with it the only route to the OS `gi`. Reproduced exactly on
a machine whose system python is 3.12.3 with PyGObject:

```
with .python-version=3.13:   uv venv -> 3.12.3, system-site=true
                             uv sync -> 3.13.1, system-site=false    -> no gi
without it:                  uv venv -> 3.12.3, system-site=true
                             uv sync -> 3.12.3, system-site=true     -> gi OK, pydbus OK
```

`.python-version` is therefore **deleted** — `requires-python = ">=3.12"` is the real
constraint and is sufficient. `UV_PYTHON_DOWNLOADS=never` is *not* a fix: uv reuses a
cached managed interpreter, tested and confirmed. `install.sh` now asserts after the sync
that the venv still has `include-system-site-packages = true` and fails loudly if not,
because the event is otherwise invisible — it leaves nothing behind except a venv that
cannot import `gi`.

The predecessor has the same `.python-version` and is unaffected only because its venv
predates it and uv leaves a satisfying venv alone. Nothing about that is a property anyone
should rely on.

`deploy/doctor.sh` exists for this class of problem: it prints the OS python and its `gi`,
uv's settings, and **this venv's `pyvenv.cfg` beside the predecessor's working one** — the
comparison that makes any of these visible in one round trip rather than ten.

The boot hook goes in `/data/rc.local`, which Venus OS keeps across firmware updates.

**`compare.py` is the point of the exercise** — Phase 1's verification has only ever been
run key-by-key off a fake bus. It handles the three ways the contract legitimately differs:
`system/battery_voltage` / `system/battery_current` are compared **across the A12 rename**
to `battery/voltage` / `battery/current`; the six per-component `DC_0_*` keys changed
meaning in place and are reported separately with the reason; and the packed `state` code is
**positional and grew from 6 to 12 digits** when A10 added three DS18B20 sensors — found
while testing the script, and not something the key-level differential could ever have seen.
It also prints how far apart the two samples are, because nothing else in the output means
anything without it.

### A20 — an agent asks for a mode, and that deleted the whole scheduling layer

Applied 2026-08-01, building Phase 4. **Supersedes the parts of the Phase 4 harvest list
that name `schedule.py` and the `ScheduledAction` dispatch path.**

The plan budgeted `schedule.py` (`ScheduledAction`, `Schedule`, `is_due`) into Phase 4. It
is not harvested, and the reason is the same one that deleted half of Phase 2's list
(A17): **the mode graph is the same information, expressed once**. An agent now returns
`ModeRequest(mode, active, agent, reason)` and nothing else — no actuator, no value, no
ordering, no time.

| planned | why not |
|---|---|
| `ScheduledAction` + `Schedule.due_now` | a request is *level-triggered*. "Am I inside a solar window" is asked every cycle instead of latched at a boundary |
| `is_due(window_seconds=30)` | the +-30 s firing window only ever worked by accident. `wallbox_optimal_charge` carried a nine-line comment explaining that `execute_at` had to be `now` and never the real hour boundary, because a 5-minute planning cycle would miss a 60-second window and the intent would *silently never fire*. There is no firing moment left to miss |
| `SequenceIntent` / `sequences/` | as planned: `required_services` is the step list, and the teardown falls out of it instead of being written backwards in a second file |
| `control/agents/time_based.py` | its `run()` returned `rationale="not yet implemented"`. Ported nothing, so it ported nothing |

**Safety keeps a direct path, and only safety.** `AgentResult` carries three kinds of
intent: `requests` (modes), `commands` (immediate actuator writes) and `inhibit` (a reason
string). A breach does both of the last two — it commands the cut *and* inhibits the
coordinator — because switching the inverter off while the wallbox mode still wants it on
would have the two fight every tick. Inhibit drops every requester, modes deactivate, and
services with nothing left requiring them revert on their own.

**Cooling became a mode, which is where reference counting finally earns its keep.** The
fan is `mppt100_load`, and *two* things want it: the wallbox (for the hours of DC load it
implies) and the safety agent's cooling rule (AC load over 1000 W for `n_req` cycles). The
predecessor wrote the fan directly from `switch_on_DC_load`, so whichever of the two
finished last switched it off underneath the other. `cooling` is now a `ServiceOnlyMode`
— a mode that *is* its requirements, with no actuator of its own — and the fan stays up
until both let go. Pinned by `test_the_fan_stays_up_while_cooling_still_wants_it`.

**A design fault found the same way A7's two were.** Slow agents run on the planning
interval (5 min); the coordinator recomputes wants **every** tick (60 s) and treats silence
as release. Straight through, that meant a forecast agent's window was requested on one
tick and dropped on the next four. The runner therefore *holds* the planning agents' last
requests and re-submits them each tick, and the fast agents' requests are the only ones
recomputed per cycle. `test_a_planning_agents_request_survives_the_ticks_between_planning_cycles`
exists because nothing about the output looks wrong when this regresses — the wallbox just
flickers.

**Both named bugs are gone, and neither was "fixed":**

- **Bug #2** — `is_enabled` is now two flags and one `return`: disabled only when
  `enabled is False and confirmed_disable is True`. The runner *calls* it, and says so
  loudly in the log when the answer is no. The REST surface enforces the same rule on the
  way in, so the protection exists in both places rather than only in CLAUDE.md.
- **Bug #3** — `from_dict` has no literal fallbacks left anywhere. `_FromDict` filters
  unknown keys and lets the dataclass supply every default, so 24-vs-96 and 200-vs-20
  cannot recur. `test_from_dict_does_not_invent_its_own_defaults` asserts
  `ControlConfig.from_dict({}) == ControlConfig()`.

**One behaviour statement, made a knob rather than a constant.** A service reverts to
`default_on` when nothing requires it, and both default to **off** — so starting Control
switches the Multiplus off unless a mode wants it. That *is* the declarative model, and it
is exactly what the predecessor's `wallbox_off` sequence did by hand. It is also the single
setting that changes what happens on an otherwise idle system, which is why it is
`services.ac_inverter_default_on` / `services.fan_default_on` in the config and not a
literal in `actuation.py`.

**The battery is not restated.** `ControlConfig.battery` defaults *are*
`config_default.battery` — capacity, cell type, the Thevenin terms, and the safety limits
A10 already derives from `n_cells x` the cell spec. Only `max_ac_load_w` is declared here,
because 2500 W is the Multiplus's AC rating and not a property of the pack. Consequence:
the limits moved slightly against the predecessor's hand-typed ones (24.0 V not 24.3,
28.8 V not 28.9, 45 °C not 42.5) — derived from the cell spec, as A10 intended.

**A12 landed where it was aimed.** `control/state.py` reads `battery/voltage` and
`battery/current`, the derived clamp values. `system/battery_voltage` is now a raw terminal
reading and Control does not touch it. This was the "Phase 3/4 note" in A12, and it is
discharged.

**The Gateway gained `/control/*`** — the endpoints A17 deliberately did not build because
their schema was Control's and Control did not exist. `POST /control/modes/<name>/activate`
writes the durable `mode_requests.json`; `GET /control/modes|/services` serve what Control
published; `PUT /control/config` deep-merges into `control_config.yaml` with the safety
guard and the wallbox-agent-group exclusivity. Two properties carried over from the
actuator routes on purpose: a status file that does not exist yet answers **503 naming
Control**, never 404; and `--read-only` (A19) refuses mode and config changes as well as
actuation, because both would cause a write the moment Control looked.

**`deploy/start.sh` starts Control only in full-control mode.** In parallel mode the
Gateway 403s its every POST anyway, so it would log failures and nothing else — and the
moment the interlock lifted it would begin actuating with no further ceremony. Phase 4 is
the exclusive step; the start script now says so structurally.

---

# Implementation status

**All four phases complete.** A12/A13 applied 2026-07-31; A16/A17/A18, the Gateway and the
Logger on 2026-08-01, plus A19's deployment; A20 and Control the same day. 136 tests, green,
no warnings.

```
uv run python -m pytest tests/      # 136 passed
```

**Phases 1–3 have run on the device**, in parallel read-only mode beside the live
predecessor (2026-08-01). See [Device validation](#device-validation--phases-13-in-parallel-mode)
below for what that did and did not settle. **Phase 4 has not**, and cannot until the
cutover: it is the exclusive step.

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
3. **Actuator registry resolves**: `multiplus_mode`, `mppt100_load`, `wallbox_relay`,
   `ac_inverter_relay` — with `multiplus_mode.write` = `{dbus, com.victronenergy.vebus.ttyUSB2,
   /Mode, on: 3, off: 4}`, derived from the component mapping, not restated. Since A16 the
   declaration itself lives on the component class.

4. **On the device, beside the live predecessor** — see [Device validation](#device-validation--phases-13-in-parallel-mode).
   What parallel mode structurally could *not* exercise: the A11 startup sequence, which is
   switched off by `--no-startup-sequence` there.

## What Phase 2 delivered

**② Gateway** — the sole hardware writer, runs end-to-end.

| File | Role |
|---|---|
| `gateway/app.py` | factory, `check_api_key` (harvested, fails closed), `ActuatorRegistry` (mtime-reloading), lazy bus |
| `gateway/transports.py` | the three ways to reach hardware — D-Bus via pydbus, VE.Direct via `vregd`, Tasmota via HTTP with fallback. Each is read/write over a resolved endpoint and knows nothing else |
| `gateway/actuators.py` | `GET /actuators/`, `GET /actuators/<name>`, `POST /actuators/<name>` with `{"on": bool}` |
| `gateway/files.py` | harvested file serving, endpoints derived from `paths.py` (A17) |
| `gateway_main.py` | the process; `ecops-gateway [--host] [--port]` |

**Tests** — 21 in `test_gateway.py`, all off-device. The D-Bus half runs against the same `FakeBus`
the Power System uses, so a `POST` is observable as a changed value on the bus; `vregd` and HTTP are
stubbed at exactly that boundary and no further, leaving the registry lookup, on/off resolution and
route logic real in every test. **Bug #1's failure mode is pinned**: an unreachable Tasmota plug is
a 502, never a silent no-op.

**Phase 2 verification results.** A real Flask server against a fake bus, driven over HTTP:
`GET /actuators/` enumerates the four names; `POST multiplus_mode {"on": false}` answers
`value_written: 4` and `/Mode` on the bus **changes 3 → 4**; the following `GET` reads back
`{"value": 4, "on": false}`; `GET /files/runtime/` lists the contract.

**On the device it has only served reads.** It ran in parallel mode, where `--read-only`
answers every `POST /actuators/...` with a 403 — by design, since the predecessor still owned
the hardware. So the registry, the routes, the auth and the D-Bus *read* path are proven on
real hardware; **`vregd` and the Tasmota write path have still never run**, on device or off.
They are exercised for the first time at the cutover.

## What Phase 3 delivered

**③ Logger** — a pure sink; nothing reads what it writes.

| File | Role |
|---|---|
| `logger.py` | `split_state` (contract-driven column split), `CsvLogger` (daily rotation, dedup with plateau-end, growable columns), `StateLogger` (the three files) |
| `aggregation.py` | `daily.csv` — time-weighted energies, derived `DC_load_power`, per-day max/min, idempotent per date |
| `logger_main.py` | the process: watch `state.json`, one row per new poll; `ecops-logger --once` |

**Tests** — 20 in `test_logger.py`. `utils.py` deleted (A18).

## Phase 3 verification results

1. **The daily aggregates, differentially against 151 real days.** Six of eight columns exactly
   `0.000000`; the three days that differ are all the predecessor being wrong. See A18 — this is
   the strongest verification any phase has had, because the *inputs* were byte-identical.
2. **End to end, real Power System into real Logger** (fake bus, 8 polls across midnight): both
   days' files created, rotation correct, and the held-back plateau row landed in the **previous**
   day's file where it belongs.
3. **Column comparison against the live device's CSVs:**
   - `aux_*.csv` header **byte-identical**.
   - `log_*.csv` — **0 of the 21 live columns lost**, 3 added (`DS18B20_*/temperature`, A10).
   - `sim_*.csv` — deliberately different, exactly as A12 said: the per-component "corrected"
     values are gone, replaced by `battery/voltage`, `battery/current`, `battery/voltage_spread`,
     plus `time_to_low_battery`. Anything reading the old sim columns must be repointed.

## Device validation — phases 1–3, in parallel mode

Run on the Venus OS box on 2026-08-01, beside the live predecessor, with the default
`ECOPS_PARALLEL=1` interlock: `ecops-power-system --no-startup-sequence` and
`ecops-gateway --read-only`. **All three processes work.** The A19 deployment recipe holds
on the real box — the venv keeps `--system-site-packages`, `gi` and `pydbus` both import,
and the two contracts appear where `paths.py` says.

**The differential.** `deploy/compare.py` against the predecessor's live `state.json` came
out clean apart from `SOC_counted`, which differs — and that difference is **structural, not
a fault**:

> Coulomb counting is path-dependent. The new estimator seeds itself from open-circuit
> voltage at *its own* start (`battery_simulation.estimate_initial_SOC`) and integrates from
> there; the predecessor's count has been integrating for weeks from a different seed, over
> a period that includes the 210 → 310 Ah capacity change (A14). The two numbers are answers
> to different questions and there is no reason for them to agree.

**Consequence for any future differential:** `SOC_counted` is not a comparable quantity
between two independently-started systems. `SOC_Kf` is — the filter's measurement update
pulls it toward the OCV-implied SOC every step, so two correct filters over the same battery
must converge regardless of where each started, most tightly at rest points. Compare that,
or compare at an anchor (A14), and treat a `SOC_counted` gap as information about the seeds
rather than about the code.

**What this settles:** the bus walk and the poll map against real Victron services, the aux
HTTP polling, the estimator running for real over live readings, the Logger's files, the
Gateway's registry/auth/read path, and the deployment.

**What it structurally could not settle**, because parallel mode exists precisely to prevent
it: the A11 startup sequence (switched off), the Gateway's `vregd` and Tasmota **write**
transports (403), and Control (not started there — A20). All three run for the first time at
the cutover, which is why the cutover is the one step to watch closely.

## What Phase 4 delivered

**④ Control** — the brain. No hardware, no D-Bus, no file it also reads back.

| File | Role |
|---|---|
| `control/config.py` | the durable knobs; battery defaults *are* `config_default.battery`; one clock (`TZ`, `now()`) so nothing ever compares a naive datetime against the contract's |
| `control/state.py` | `state.json` → `CurrentState`; A12's derived clamp values; a stale timestamp raises (A20) |
| `control/forecast.py` | harvested; parsed times localised at the boundary |
| `control/projection.py` | harvested; projects with `Battery_Simulation`, configured from `ControlConfig` |
| `control/gateway_client.py` | `command(actuator, on)` / `read(actuator)`. The only actuation path out of this process |
| `control/actuation.py` | the concrete `ac_inverter` / `fan` services and `wallbox` / `cooling` modes — everything hardware-shaped, and it is 90 lines |
| `control/agents/` | `system_safety` (bug #2 gone), `soc_wallbox_charge`, `forecast_wallbox`, `wallbox_optimal_charge`. The planning algorithms harvested verbatim; only their output changed (A20) |
| `control/decision_log.py` | harvested; the cycle entry now carries the coordinator's view |
| `control_main.py` | the process: reload → read → safety → project → agents → tick. `--once`, `--dry-run` |
| `gateway/control.py` | the REST surface A17 deferred: modes, services, requests, config, log |

**Tests** — 34: 27 in `test_control.py`, 7 more in `test_gateway.py` for `/control/*`.
`service.py` / `mode.py` / `coordinator.py` and their 18 spike tests shipped **unchanged** —
the spike's whole purpose, and it held.

## Phase 4 verification results

1. **The plan's round trip, end to end and off-device.** Real Control → real HTTP routing →
   real Gateway → the real registry → the fake bus with `vregd` and Tasmota stubbed at the
   protocol boundary. `POST /control/modes/wallbox/activate` → `/Mode` on the bus goes
   **4 → 3**, `/Load/State` **1 → 4**, the wallbox plug reports ON, and
   `services_status.json` shows both services `satisfied`. Deactivate puts all three back —
   and `/Load/State` returns to **1**, not the **0** that was written, which is A9's
   asymmetry surviving a full round trip through four layers.
2. **The actuator names are pinned by that test.** It is the first thing that consumes
   `wallbox_relay` and `mppt100_load` as *names*; A16 renamed them freely precisely because
   nothing did yet.
3. **`grep -rn "dbus-send\|pydbus" control/`** matches only two docstrings that quote the
   check. No code.
4. **`ecops-control --dry-run` against a hand-written `state.json`** — read state, project
   96 h, run every agent, print what each would ask for, command nothing. Exit 0.

**Still outstanding:** none of Phase 4 has run against the real hardware, and unlike phases
1–3 it *cannot* be validated beside the predecessor. The cutover is its first run.

## Pick up here

Phases 1–3 are validated on the device (above). What is left is the one exclusive step, and
it is the only one in the whole rebuild that can break something physical.

**The cutover.** Stop the predecessor's `control_runner.py` — and confirm it is stopped, not
merely quiet — then `ECOPS_PARALLEL=0 ./deploy/start.sh --restart`, which is what brings
Control up at all (A20). Two writers on one actuator is the one genuinely dangerous failure
mode in this rebuild. Revert = kill the `ecops` session, restart the old runner.

**Four things run for the first time in that first minute**, so watch them in this order:

1. **The A11 startup sequence**, if the Multiplus's DC measurements are missing — it wakes the
   inverter, which energises AC output for a few seconds. The 5 s / 10 s waits are still a
   guess; they are class attributes on `VictronMultiplusII`, tunable without touching logic.
2. **`vregd`** — the fan's VE.Direct write, and the one place `int(token, 0)` parses the
   output of a tool that has never been run. `POST /actuators/mppt100_load {"on": true}` by
   hand, then `GET` it back: the write is `4`, the readback should be `4`, and off writes `0`
   but reads `1` (A9). A 502 here is the transport talking, not the fan.
3. **The Tasmota write path** — same, by hand, on `wallbox_relay`. Bug #1's failure mode is
   that this silently does nothing; the Gateway now answers 502 instead, so believe the code.
4. **Control's first tick**, which will switch the Multiplus **off** if no mode wants it. That
   is the model, not a fault (A20) — decide whether this installation wants
   `services.ac_inverter_default_on: true` *before* starting it, not after.

Only then let it run unattended: `GET /control/modes` and `/control/services` say what it
thinks it is doing, and `data/control_log.jsonl` says why.

**Worth doing before Control leans on the model:** A15's `R1` calibration pass, against
`tools/replay.py` — and it can now be scored against this system's *own* recorded CSVs
rather than the predecessor's. The projection is a live input to the wallbox agents.

## Carried risks

- **`R1` is ~4x too large (A15).** Open. The dominant remaining model error now that A13 is fixed, and
  worth a calibration pass against `tools/replay.py` before Phase 4 leans on this model.
- **Capacity is configured at 310 Ah; the data measures ~288 Ah (A14).** Much better than the 210 Ah it
  replaced, but still the largest single lever on coulomb-counting accuracy.
- **A12 changed the state contract's meaning, not just its keys.** `system/battery_voltage` and the
  per-component `DC_0_voltage` keys are now raw readings; the derived clamp values live under
  `battery/*`. Control reads `battery/*` (A20); the Logger writes both.
- **Control switches the Multiplus off when no mode wants it** (A20). That is the declarative
  model working as designed, and it is what the predecessor's `wallbox_off` did — but it is a
  behaviour change on an idle system, and the first minute of full-control mode is when it
  shows. `services.ac_inverter_default_on` is the knob if this installation wants otherwise.
- **The mode graph has never converged against real hardware latency.** The services retry five
  times and then sit in `failed`; whether a real Multiplus reflects `/Mode` inside five 60-second
  ticks is exactly the kind of thing only the device can answer (compare A11's 5 s/10 s guesses).
- **The Gateway's `vregd` and Tasmota *write* transports have still only been stubbed.** The
  device run did not change this and could not: `--read-only` 403s every POST, which is the
  whole point of parallel mode. The VE.Direct output parse (`int(token, 0)` off `vregd -g`) and
  the Tasmota `{"POWER": "ON"}` answer remain assumptions about tools nobody has run. First
  thing to check by hand at the cutover.
- **`ECOPS_API_KEY` must be set** or the Gateway refuses every request with a 503. That is the
  harvested fail-closed behaviour and it is correct, but it is a new deployment requirement —
  and since A20 it is Control's requirement too: `GatewayClient` reads the same variable, so an
  unset key makes every command fail and every readback come back unknown.
- **The solar forecast needs `ECOWHEN_DATA_API_KEY`** (a *different* key, for computepi's data
  API). Without it the fetch 401s, `forecast` is None, and the two forecast agents report
  "inactive" — quietly correct, but it looks like they are disabled.
- **No initial commit yet.** Left deliberately — not committing without being asked.
- **`running_since` changed format**, from `"26-07-07 00:29"` to ISO. Same key, so the key-level
  differential did not flag it. Nothing was found reading it; nothing in Phases 3–4 reads it.
- **`SOC_counted` does not match the predecessor's, and never will.** Seen on the device and
  expected: the count is path-dependent from whatever seed each system started with. Not a
  defect, but it means SOC cannot be differentially validated that way — compare `SOC_Kf`, or
  compare at a rest anchor (A14).
- **`phoenix` will now fail startup if it is ever plugged in** (A5, retargeted by A12). That is
  intended, and the error says which cable to measure, but it is a behaviour change from the
  predecessor, which would instead have crashed on every poll.
- **The startup sequence has still only been exercised against the fake bus** (A11) — parallel
  mode runs the Power System with `--no-startup-sequence`, so the device run skipped it. The 5 s / 10 s waits
  are a guess at how long a real Multiplus takes to bring its DC measurement hardware up; they are
  class attributes on `VictronMultiplusII` so they can be tuned on the device without touching logic.
  Whether the measurements *stay* readable after the inverter is stopped again — the assumption the
  whole sequence rests on — is untested on hardware.
- **Waking the Multiplus energises AC output for a few seconds** at every restart where its
  measurements are missing. Bounded and intended, but it is a side effect on real loads.
