"""Hardware, grouped by who made it.

`ActuatorType` lives here rather than in one manufacturer's module because both the
D-Bus components and the HTTP `aux_components` declare actuators, and they must speak
the same vocabulary — discovery resolves both through one code path.
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
