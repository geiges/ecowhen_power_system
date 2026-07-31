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


def build_actuator_registry(psystem, components_status, aux_by_name, actuators_config):
    """Resolve the declared actuators into concrete, Gateway-consumable addresses.

    An actuator whose component is not on the bus is emitted with available=False
    rather than dropped: the Gateway should report "the Multiplus is missing" instead
    of "no such actuator", which are very different faults to debug at 3am.
    """
    registry = {}

    for name, decl in actuators_config.items():
        where = f"actuator {name!r}"
        entry = {"description": decl.get("description", "")}

        if "aux_component" in decl:
            aux = aux_by_name.get(decl["aux_component"])
            if aux is None:
                raise ValueError(f"{where}: no aux component {decl['aux_component']!r}")
            component = service = None
            entry["aux_component"] = decl["aux_component"]
            available = True          # HTTP devices are not discovered; assume reachable
        else:
            short_name = decl["component"]
            if short_name not in psystem:
                raise ValueError(f"{where}: no component {short_name!r}")
            component = psystem[short_name]
            aux = None
            service = components_status[short_name]["service"]
            available = components_status[short_name]["available"]
            entry["component"] = short_name

        entry["available"] = available

        if available:
            for half in ("write", "read"):
                entry[half] = _resolve_endpoint(decl[half], component, service, aux, where)

        registry[name] = entry

    return registry


def check_voltage_calibration(psystem, variables_to_log) -> None:
    """Every component feeding the battery-voltage average must be calibrated.

    The estimator averages each component's `DC_0_voltage` after correcting it for
    cable drop, so an uncalibrated contributor either corrupts the average silently
    or — as the code actually behaves — raises `Connector resisitance not set` from
    `voltage_measurement()` on *every* poll, killing SOC estimation for good.

    Raising the alarm here converts that into one clear failure at startup. It fires
    only for components genuinely on the bus: `phoenix` is configured but has never
    been present, and an absent component contributes no variables to correct.

    Deliberately not "fall back to the raw voltage": a wrong battery voltage feeds the
    Kalman filter and the SOC drifts plausibly, which is far worse than a loud stop.
    The fix for a real Phoenix is to *measure* its cable and add it to
    `config_default.measurement_components` — not to guess a number here.
    """
    uncalibrated = sorted({
        var.split("/")[0] for var in variables_to_log
        if var.endswith("DC_0_voltage")
        and not var.startswith("system")
        and getattr(psystem[var.split("/")[0]], "connector_R0", None) is None
    })
    if uncalibrated:
        raise ValueError(
            f"on the bus but not calibrated: {', '.join(uncalibrated)}. "
            f"These contribute DC_0_voltage to the battery-voltage average, so they "
            f"need a connector_R0/voltage_offset entry in "
            f"config_default.measurement_components. Measure the cable — do not guess."
        )


def discover(bus, config):
    """Walk the bus and build the whole topology contract as a plain dict."""
    from . import power_system

    psystem = power_system.init_power_system(
        system_components=config.system_components,
        measurement_components=config.measurement_components,
    )

    variables_to_log, _ = psystem.get_variables_to_log(bus)
    states_to_log, _ = psystem.get_states_to_log(bus)

    check_voltage_calibration(psystem, variables_to_log)

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

    registry = build_actuator_registry(
        psystem, components_status, aux_by_name, config.actuators
    )

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
