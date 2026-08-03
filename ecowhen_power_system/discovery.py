"""Discovery: build the topology contract.

Runs once, at Power System startup. Walks the D-Bus, works out which configured
components are actually present, and writes `system_configuration.yaml` — the single
description of *what is out there and how to reach it*.

Everything downstream reads that file and re-discovers nothing:
  * the Logger gets its columns,
  * the Gateway gets its actuator registry (it never imports `components.py`),
  * nobody edits anybody else's config file.

A device appearing or disappearing mid-run needs a Power System restart, which matches
the predecessor's behaviour.
  ponytail: if hot-plug ever matters, re-run discover() on an interval and rewrite the
  yaml on change; consumers reload on mtime. Not built (YAGNI).
"""
from datetime import datetime

import yaml

TASMOTA_ON = 1     # Tasmota `Power 1` / `Power 0` — a protocol constant, not config.
TASMOTA_OFF = 0


def _on_off_from_mapping(mapping: dict, where: str) -> tuple:
    """Invert a component state's {value: label} mapping into (on_value, off_value).

    The component definitions already carry the semantics — the Multiplus declares
    `mapping={3: "on", 4: "off"}` — so the on/off integers are derived here rather
    than restated in the actuator registry, where they could drift out of sync.
    """
    inverse = {label: value for value, label in mapping.items()}
    if "on" not in inverse or "off" not in inverse:
        raise ValueError(
            f"{where}: mapping {mapping} has no 'on'/'off' labels, so it cannot "
            f"back a binary actuator"
        )
    return inverse["on"], inverse["off"]


def _find_state(component, basename: str, where: str):
    for state in component.component_states:
        if state.basename == basename:
            return state
    raise ValueError(f"{where}: component {component.short_name!r} has no state {basename!r}")


def _find_vedirect_set(component, basename: str, where: str):
    for vs in component.vedirect_sets:
        if vs.basename == basename:
            return vs
    raise ValueError(
        f"{where}: component {component.short_name!r} has no vedirect_set {basename!r}"
    )


def _port_from_service(service: str, where: str) -> str:
    """"com.victronenergy.solarcharger.ttyUSB1" -> "/dev/ttyUSB1"."""
    tty = service.split(".")[-1]
    if not tty.startswith("tty"):
        raise ValueError(f"{where}: cannot derive a serial port from service {service!r}")
    return f"/dev/{tty}"


def _resolve_endpoint(spec, component, service, aux, where):
    """Resolve one half (read or write) of an actuator into a concrete address."""
    transport = spec["transport"]

    if transport == "dbus":
        state = _find_state(component, spec["state"], where)
        on, off = _on_off_from_mapping(state.mapping, where)
        return {
            "transport": "dbus",
            "service": service,
            "path": state.subaddress,
            "on": on,
            "off": off,
        }

    if transport == "vedirect":
        vs = _find_vedirect_set(component, spec["vedirect_set"], where)
        # on/off are declared per-actuator here: unlike a D-Bus state, a VE.Direct
        # register carries no label mapping to derive them from.
        on, off = spec["on"], spec["off"]
        for value in (on, off):
            if vs.allowed_values and value not in vs.allowed_values:
                raise ValueError(
                    f"{where}: value {value} not in allowed_values {vs.allowed_values} "
                    f"for register 0x{vs.register:04X}"
                )
        return {
            "transport": "vedirect",
            "component": component.short_name,
            "port": _port_from_service(service, where),
            "register": vs.register,
            "value_type": vs.value_type,
            "on": on,
            "off": off,
        }

    if transport == "tasmota":
        return {
            "transport": "tasmota",
            "url": aux.url,
            "fallback_url": aux.fallback_url,
            "on": TASMOTA_ON,
            "off": TASMOTA_OFF,
        }

    raise ValueError(f"{where}: unknown transport {transport!r}")


def build_actuator_registry(psystem, components_status, aux_by_name):
    """Resolve every component's declared actuators into Gateway-consumable addresses.

    Each component class declares what *it* can switch (`component_actuators`); the
    installation only chooses the `short_name` that prefixes the logical name. So
    nothing here — and nothing in `config_default` — restates a D-Bus path, a register
    or an on/off integer.

    An actuator whose component is not on the bus is emitted with available=False
    rather than dropped: the Gateway should report "the Multiplus is missing" instead
    of "no such actuator", which are very different faults to debug at 3am.
    """
    registry = {}

    for short_name, component in psystem.items():
        if component.hardware is None:      # A10: not a bus device, nothing to drive
            continue
        service = components_status[short_name]["service"]
        available = components_status[short_name]["available"]

        for decl in component.component_actuators:
            name = f"{short_name}_{decl.basename}"
            where = f"actuator {name!r}"
            entry = {
                "description": decl.description,
                "component": short_name,
                "available": available,
            }
            if available:
                entry["write"] = _resolve_endpoint(decl.write, component, service, None, where)
                entry["read"] = _resolve_endpoint(decl.read, component, service, None, where)
            registry[name] = entry

    for short_name, aux in aux_by_name.items():
        for decl in aux.component_actuators:
            name = f"{short_name}_{decl.basename}"
            where = f"actuator {name!r}"
            registry[name] = {
                "description": decl.description,
                "aux_component": short_name,
                # HTTP devices are not discovered; assume reachable.
                "available": True,
                "write": _resolve_endpoint(decl.write, None, None, aux, where),
                "read": _resolve_endpoint(decl.read, None, None, aux, where),
            }

    return registry


def check_voltage_calibration(variables_to_log, battery) -> None:
    """Every component that can see the battery clamps must declare its cable.

    The clamp voltage is derived per source — terminal voltage minus the drop the
    source's own current causes — and the estimates are averaged. A component on the
    bus reporting `DC_0_voltage` that the battery does not declare is therefore a
    source silently dropped from that average; used blind instead, it would
    contribute a cable drop nobody measured.

    It fires only for components genuinely on the bus: `phoenix` is configured but
    has never been present, and an absent component reports no variables.

    Deliberately not "assume zero resistance": a wrong clamp voltage feeds the Kalman
    filter and the SOC drifts plausibly, which is far worse than a loud stop. The fix
    for a real Phoenix is to *measure* its cable and add it to
    `config_default.battery.init_derrived_measurements` — not to guess a number here.
    """
    on_bus = {
        var.split("/")[0] for var in variables_to_log
        if var.endswith("DC_0_voltage") and not var.startswith("system")
    }
    declared = set(battery.indirect_voltage_measurements)

    uncalibrated = sorted(on_bus - declared)
    if uncalibrated:
        raise ValueError(
            f"on the bus but not calibrated: {', '.join(uncalibrated)}. "
            f"These can see the battery clamps, so each needs a "
            f"connector_R0/voltage_offset entry in "
            f"config_default.battery.init_derrived_measurements. "
            f"Measure the cable — do not guess."
        )

    # The other direction is not fatal — the average simply loses a source — but it
    # is exactly the A11 failure that used to happen in silence, so it gets said out
    # loud once, at startup.
    absent = sorted(declared - on_bus)
    if absent:
        print(f"[discovery] declared voltage sources not reporting: "
              f"{', '.join(absent)} — the battery voltage will be derived without them")


def startup_sequence(bus, psystem) -> None:
    """Bring the hardware into a state where its measurements can be read.

    Runs once, before the poll maps are built, so a device woken here is discovered
    in the same pass. Today only the Multiplus needs it (its DC measurement hardware
    stays dead until the inverter has run once); a component that declares no
    `ensure_measurements_available` is simply skipped — no base-class ceremony for
    one implementation.

    This is the *one* place the Power System writes D-Bus, against the "exactly one
    writer" rule. It is deliberate and bounded: it happens at startup, before the
    Gateway exists and before Control can command anything, and it restores the mode
    it found. Nothing else in this process may write.
    """
    for component in psystem.values():
        wake = getattr(component, "ensure_measurements_available", None)
        if wake is not None:
            wake(bus)


def discover(bus, config, run_startup_sequence=True):
    """Walk the bus and build the whole topology contract as a plain dict.

    `run_startup_sequence=False` makes this pass strictly read-only. That matters
    while the predecessor is still the live system: the wake-up below is the one
    place the Power System writes D-Bus, and a second process pulsing the inverter
    underneath a running system is not a validation, it is an incident.
    """
    from . import power_system

    psystem = power_system.Power_system(config.system_components)

    if run_startup_sequence:
        startup_sequence(bus, psystem)

    variables_to_log, _ = psystem.get_variables_to_log(bus)
    states_to_log, _ = psystem.get_states_to_log(bus)

    check_voltage_calibration(variables_to_log, config.battery)

    components_status = {}
    for short_name, component in psystem.items():
        
        if component.hardware is None:
            continue
        service = component.get_interface(bus)
        components_status[short_name] = {
            "product_name": component.product_name,
            "service": service,
            "available": service is not None,
        }

    aux_by_name = {c.short_name: c for c in config.aux_components}
    aux_status = {
        short_name: {"protocol": c.protocol, "variables": c.variable_list}
        for short_name, c in aux_by_name.items()
    }

    registry = build_actuator_registry(psystem, components_status, aux_by_name)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "components": components_status,
        "aux_components": aux_status,
        # The poll maps travel with the contract so the Logger never re-discovers.
        "variables_to_log": variables_to_log,
        "states_to_log": states_to_log,
        "actuators": registry,
    }, psystem


def save_system_configuration(system_config: dict, path) -> None:
    with open(path, "w") as f:
        yaml.dump(
            system_config, f,
            default_flow_style=False, allow_unicode=True, sort_keys=False,
        )


def load_system_configuration(path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}
