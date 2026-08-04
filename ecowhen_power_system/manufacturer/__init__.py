"""Hardware, grouped by who made it.

What lives here is what more than one manufacturer's module needs: `ActuatorType`,
because the D-Bus and the HTTP components both declare actuators and discovery
resolves both through one code path, and `HttpComponent`, because Tasmota and Deye
are different makers of the same kind of device — one reached over HTTP rather than
the bus.
"""
from typing import NamedTuple


class ActuatorType(NamedTuple):
    """One thing on this device model that can be switched on and off.

    Declared on the component *class*, because it describes the device and not the
    installation: a MultiPlus-II has /Mode wherever it is bolted, and an MPPT 100/20
    has register 0xEDAB. What the installation gets to choose is the component's
    `short_name`, which prefixes the logical name Control speaks — `mppt100` plus
    basename `load` is the actuator `mppt100_load`.

    `write` and `read` are separate endpoints on purpose. They are usually identical,
    but the MPPT's load output is written over VE.Direct and read over D-Bus, in
    different value spaces — see VictronSolarMPPTWithDCLoad.
    """
    basename: str
    write: dict
    read: dict
    description: str = ""


class HttpVariableType(NamedTuple):
    basename: str
    unit: str


class HttpComponent:
    """Base for components reached over HTTP rather than the D-Bus.

    `hardware = None` puts them in `system_components` alongside the bus devices:
    every loop that walks the bus already skips that, so nothing downstream has to
    know two lists exist. What makes them pollable is `get_labeled_data()`, and that
    is the discriminator — `Generic_Battery` is also `hardware = None` and is not
    polled at all.

    Subclasses implement `fetch()` and declare `component_variables`.
    """
    hardware = None
    protocol: str = 'http'
    component_variables: list = []
    component_actuators: list = []

    def __init__(self, short_name: str):
        self.short_name = short_name
        self.variable_list = [
            f"{short_name}/{var.basename}" for var in self.component_variables
        ]

    def fetch(self) -> dict:
        """Current values from the device, as a flat {basename: value} dict."""
        raise NotImplementedError(f"{type(self).__name__} must implement fetch()")

    def get_labeled_data(self) -> dict:
        """Return fetch() output with keys prefixed by short_name."""
        return {f"{self.short_name}/{k}": v for k, v in self.fetch().items()}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(short_name={self.short_name!r})"
