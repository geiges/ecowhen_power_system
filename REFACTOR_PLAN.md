# Refactor: `ecowhen_power_system` — greenfield 4-process rebuild

> **Status:** Phase 1 in progress. Implementation paused for plan review.
> See [Implementation status](#implementation-status) at the bottom for exactly where
> it stopped and what to pick up first.

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
   exists only as Kalman-filter output over voltage/current/temperature. Estimation is therefore
   inseparable from polling and belongs here, not in the Logger (where it lives today) or in Control.
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
    │   • poll D-Bus + aux → correct → simulate SOC    │
    │   • persists own soc_state.json (durable)        │
    └──────────────────────────────────────────────────┘
              │                              │
   system_configuration.yaml             state.json
   ── TOPOLOGY CONTRACT ──              ── STATE CONTRACT ──
   poll map + ACTUATOR REGISTRY:        live values, SOC_Kf, SOC_counted,
   name → transport + address           time_to_low_battery,
        + on/off values                 soc_above_threshold_since,
   (Tasmota URL declared ONCE)          aux power keys, ISO timestamp
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
`power_system.py`, `config_default.py`.

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

### A4 — the harvest source was a dirty tree

`victron_system_monitor` has uncommitted changes. `components.py` was copied from the **working
tree**, not `HEAD`. Inspected: the diff is whitespace plus two `flaot`→`float` typo fixes — benign,
and strictly better than `HEAD`. Recorded so nobody later wonders why the copy ≠ the last commit.

Also uncommitted there and relevant to **Phase 4**: `tests/test_wallbox_optimal_charge.py` (−17 lines),
and `REFACTOR_PLAN.md` itself was never committed.

---

# Implementation status

**Phase 1 complete.** 52 tests, green, no warnings. Paused for code review before Phase 2.

```
uv run python -m pytest tests/      # 52 passed
```

## What Phase 1 delivered

**① Power System** — runs end-to-end and publishes both contracts.

| File | Role |
|---|---|
| `paths.py` | `RUNTIME_DIR` (ephemeral) vs `DATA_DIR` (durable), both env-overridable |
| `discovery.py` | bus walk → topology contract, incl. the resolved actuator registry + the calibration guard (A5) |
| `poll.py` | D-Bus + aux → correct → estimate SOC → state contract; `SocTracker`; durable SOC persistence |
| `power_system_main.py` | the process: `setup()` once, then poll; `--once` for a single shot |
| `config_default.py` | gained the `actuators` declaration (A8, A9) |

**The Coordinator spike** — `control/{service,mode,coordinator}.py`, validated by
`test_coordinator.py` (18 tests, fakes only, no hardware). Found two real design faults before
anything depended on them; see A7.

**Tests** — 52 across `test_port_fidelity` (calibration pinned), `test_discovery`, `test_poll`,
`test_power_system_integration` (the *real* Battery/Kalman/System_Simulation), `test_coordinator`.
`conftest.py` carries the `fake_bus` seam (A3) and builds shared objects through factories, which is
the fix for the rot that put the predecessor's suite 75-red.

## Phase 1 verification results

1. **The real entrypoint runs off-device.** `power_system_main.main(["--once"])` against a fake bus:
   wrote both contracts, exit 0, the harvested Kalman filter estimated SOC from OCV at 51.65%.
2. **Differential against the live device — clean.** New `state.json` vs the real
   `victron_system_monitor/data/state.json`:
   - **0 keys missing.** All 28 live keys present; the new contract is a strict superset.
   - **+7 added**, every one intended: 4 aux keys (`wallbox/*`, `ac_inverter/*` — decision #4),
     `timestamp` (the ISO fix), `soc_above_threshold_since` + `minutes_at_full_soc` (decision #7).
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

- **Three of the four `ecops-*` entrypoints still point at modules that do not exist**
  (`gateway_main.py`, `logger_main.py`, `control_main.py`). `uv sync` does not validate script
  targets, so this stays latent until someone runs one. Phases 2–4 each create one.
- **No initial commit yet.** Left deliberately — not committing without being asked.
- **`running_since` changed format**, from `"26-07-07 00:29"` to ISO. Same key, so the key-level
  differential did not flag it. Nothing was found reading it, but Phase 3/4 should confirm.
- **The differential compares key names, not values.** See above — real validation needs the device.
- **`phoenix` will now fail startup if it is ever plugged in** (A5). That is intended, and the error
  says what to do, but it is a behaviour change from the predecessor, which would instead have
  crashed on every poll.
