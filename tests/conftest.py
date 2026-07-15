"""Shared fixtures.

The important one is `fake_bus`. Every D-Bus entry point in this package takes the bus
as an argument — `get_interface(dbus)`, `get_device_variables(dbus)`, `retrieve_data(bus, …)`
— so the whole discovery and poll path can be exercised off-device with a plain object.
No `pydbus`, no `gi`, no D-Bus daemon, no `mock_dbus_service.py`.

(`mock_dbus_service.py` stands up a real service on a real session bus. That needs the
system gobject bindings, which is why the predecessor's `test_mock_dbus.py` skipped
everywhere and quietly tested nothing. It is for on-device integration only.)

Note `state_factory` and `system_config` below: build shared objects through a factory,
never inline in each test. The predecessor grew 46 identical inline `CurrentState(...)`
constructions, so adding one required field broke all 46 at once and the suite went red
and stayed red — which is how a real safety bug sat behind the noise for months.
"""
import pytest

from ecowhen_power_system import config_default, discovery


# Product names must match config_default.system_components exactly — get_interface()
# reads /ProductName off the bus and compares.
MPPT150 = "com.victronenergy.solarcharger.ttyUSB0"
MPPT100 = "com.victronenergy.solarcharger.ttyUSB1"
MULTIPLUS = "com.victronenergy.vebus.ttyUSB2"
PHOENIX = "com.victronenergy.inverter.ttyUSB3"
SYSTEM = "com.victronenergy.system"


class _FakeItem:
    def __init__(self, value):
        self._value = value

    def GetValue(self):
        return self._value


class _FakeDbusProxy:
    def __init__(self, names):
        self._names = names

    def ListNames(self):
        return list(self._names)


class FakeBus:
    """Minimal stand-in for a pydbus bus.

    Constructed from {service: {path: value}}. Reading an undeclared path raises,
    exactly as a real bus does for a missing object — several code paths depend on
    that being an exception rather than a None.
    """

    def __init__(self, values: dict):
        self._values = values
        self.dbus = _FakeDbusProxy(values.keys())

    def get(self, service, path="/"):
        if service not in self._values:
            raise KeyError(f"no such service: {service}")
        if path not in self._values[service]:
            raise KeyError(f"no such path: {service}{path}")
        return _FakeItem(self._values[service][path])

    def set_value(self, service, path, value):
        self._values.setdefault(service, {})[path] = value


def _solarcharger(product_name, *, load=False):
    values = {
        "/ProductName": product_name,
        "/Yield/Power": 120.0,
        "/Dc/0/Voltage": 26.24,
        "/Dc/0/Current": 4.5,
        "/Yield/System": 519.43,
        "/State": 3,          # bulk
        "/Mode": 1,           # working
    }
    if load:
        values["/Load/I"] = 0.0
        values["/Load/State"] = 1     # 1 == "off" per the load_state mapping
    return values


def phoenix_service():
    """The Phoenix inverter, for tests that need it present.

    Deliberately NOT in the default fixture: it is configured in config_default but
    has never actually been on the bus (the live device's state.json carries only
    system/mppt150/mppt100/multiplus). The default bus mirrors reality.
    """
    return PHOENIX, {
        "/ProductName": "Phoenix Inverter 24V 800VA 230V",
        "/Ac/Out/P": 0.0,
        "/Ac/Out/L1/F": 50.0,
        "/Dc/0/Voltage": 26.2,
        "/Dc/0/Current": 0.0,
        "/Alarms/HighTemperature": 0,
        "/Alarms/LowBattery": 0,
        "/Alarms/Overload": 0,
    }


@pytest.fixture
def bus_values():
    """The bus as it actually is on the device: phoenix absent, everything else up."""
    return {
        SYSTEM: {
            "/ProductName": "system",
            "/Dc/Battery/Voltage": 26.23,
            "/Dc/Battery/Current": -0.15,
            "/Dc/Battery/Temperature": 22.5,
        },
        MPPT150: _solarcharger("SmartSolar Charger MPPT 150/35"),
        MPPT100: _solarcharger("SmartSolar Charger MPPT 100/20 48V", load=True),
        MULTIPLUS: {
            "/ProductName": "MultiPlus-II 24/3000/70-32",
            "/Ac/Out/P": 210.0,
            "/Ac/Out/L1/F": 50.0,
            "/Dc/0/Voltage": 26.2,
            "/Dc/0/Current": -8.1,
            "/Alarms/TemperatureSensor": 0,
            "/Alarms/LowBattery": 0,
            "/Alarms/Overload": 0,
            "/Mode": 3,       # 3 == "on" per the inverter_mode mapping
        },
    }


@pytest.fixture
def fake_bus(bus_values):
    return FakeBus(bus_values)


@pytest.fixture
def system_config(fake_bus):
    """The topology contract, as discovery would write it."""
    config, _psystem = discovery.discover(fake_bus, config_default)
    return config


@pytest.fixture
def runtime_dirs(tmp_path, monkeypatch):
    """Point both file roots at tmp_path. Import paths *after* this fixture runs."""
    monkeypatch.setenv("ECOPS_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("ECOPS_DATA_DIR", str(tmp_path / "data"))
    import importlib

    from ecowhen_power_system import paths
    importlib.reload(paths)
    paths.ensure_dirs()
    return paths
