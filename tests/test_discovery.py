"""Discovery builds the topology contract off a fake bus."""
import pytest

from ecowhen_power_system import config_default, discovery
from ecowhen_power_system.manufacturer.victron import VictronMultiplusII

from conftest import MPPT100, MULTIPLUS, FakeBus, phoenix_service


MULTIPLUS_ON, MULTIPLUS_OFF = 3, 4
# An "invalid" value as Victron reports it: an empty array, not an error, not None.
DBUS_INVALID = []


@pytest.fixture
def fast_wake(monkeypatch):
    """Shrink the startup waits so the give-up path does not cost 15 s of sleeping."""
    monkeypatch.setattr(VictronMultiplusII, "MEASUREMENT_WAKE_TIMEOUT", 0.05)
    monkeypatch.setattr(VictronMultiplusII, "MEASUREMENT_POLL_INTERVAL", 0.01)


def _asleep_multiplus(bus_values):
    """A Multiplus on the bus but never run since boot: no DC measurements."""
    bus_values[MULTIPLUS]["/Mode"] = MULTIPLUS_OFF
    bus_values[MULTIPLUS]["/Dc/0/Voltage"] = DBUS_INVALID
    bus_values[MULTIPLUS]["/Dc/0/Current"] = DBUS_INVALID
    return bus_values


class WakingBus(FakeBus):
    """A Multiplus whose measurement hardware comes alive once the inverter runs."""

    def get(self, service, path="/"):
        if self._values.get(MULTIPLUS, {}).get("/Mode") == MULTIPLUS_ON:
            self._values[MULTIPLUS]["/Dc/0/Voltage"] = 26.2
            self._values[MULTIPLUS]["/Dc/0/Current"] = -8.1
        return super().get(service, path)


def test_discovers_every_configured_component(system_config):

    comps = system_config["components"]
    assert set(comps) == {"system", "mppt150", "mppt100", "multiplus", "phoenix",
                          'DS18B20_inside', 'DS18B20_DC_multiplus', 'DS18B20_shunt'
                          }, print(set(comps))
    assert comps["multiplus"]["service"] == MULTIPLUS

    # phoenix is configured but has never been on the real bus, so it is reported
    # as unavailable rather than omitted — a configured-but-missing component is a
    # fact worth surfacing, not a silent gap.
    assert comps["phoenix"]["available"] is False
    assert all(
        c["available"] for name, c in comps.items() if name != "phoenix"
    )


def test_uncalibrated_voltage_component_on_the_bus_fails_at_startup(bus_values):
    """If the Phoenix were ever plugged in, it would contribute DC_0_voltage to the
    battery-voltage average with no connector_R0 — and voltage_measurement() would
    raise on every single poll, killing SOC estimation. Fail once, at startup,
    instead.
    """
    service, values = phoenix_service()
    bus_values[service] = values

    with pytest.raises(ValueError, match="on the bus but not calibrated: phoenix"):
        discovery.discover(FakeBus(bus_values), config_default)


def test_poll_maps_travel_with_the_contract(system_config):
    """The Logger must never need to re-discover; the maps ride along."""
    variables = system_config["variables_to_log"]
    assert "mppt100/power_yield" in variables
    assert variables["mppt100/power_yield"]["address"] == "/Yield/Power"
    assert variables["mppt100/power_yield"]["dbus_device"] == MPPT100
    assert "multiplus/inverter_mode" in system_config["states_to_log"]


def test_dbus_actuator_derives_on_off_from_the_component_mapping(system_config):
    """The Multiplus already declares {3: "on", 4: "off"} — nothing restates it."""
    act = system_config["actuators"]["multiplus_mode"]
    assert act["available"]
    assert act["write"] == {
        "transport": "dbus",
        "service": MULTIPLUS,
        "path": "/Mode",
        "on": 3,
        "off": 4,
    }
    assert act["read"] == act["write"]


def test_fan_actuator_is_asymmetric_write_vedirect_read_dbus(system_config):
    """The one actuator whose read and write disagree — see config_default.actuators.

    Writing "off" is register value 0; reading "off" comes back as /Load/State == 1.
    If these ever collapse to one number the reconcile loop will flap the fan, so
    pin both halves.
    """
    act = system_config["actuators"]["mppt100_load"]

    assert act["write"] == {
        "transport": "vedirect",
        "component": "mppt100",
        "port": "/dev/ttyUSB1",
        "register": 0xEDAB,
        "on": 4,
        "off": 0,
    }
    assert act["read"] == {
        "transport": "dbus",
        "service": MPPT100,
        "path": "/Load/State",
        "on": 4,
        "off": 1,
    }
    assert act["write"]["off"] != act["read"]["off"]


def test_tasmota_actuator_takes_its_url_from_the_aux_component(system_config):
    """One declaration, two uses. The predecessor had the reading URL and the
    writing URL in separate files; they drifted and the AC hard-cut stopped firing.
    """
    aux = {c.short_name: c for c in config_default.aux_components}

    wallbox = system_config["actuators"]["wallbox_charge"]
    assert wallbox["write"]["url"] == aux["wallbox"].url
    assert wallbox["write"]["fallback_url"] == aux["wallbox"].fallback_url
    assert (wallbox["write"]["on"], wallbox["write"]["off"]) == (1, 0)

    ac_cut = system_config["actuators"]["ac_inverter_plug"]
    assert ac_cut["write"]["url"] == aux["ac_inverter"].url
    assert ac_cut["write"]["url"] != wallbox["write"]["url"]


def test_missing_component_yields_an_unavailable_actuator_not_a_missing_one(bus_values):
    """"The Multiplus is gone" and "no such actuator" are different 3am problems."""
    del bus_values[MULTIPLUS]
    config, _ = discovery.discover(FakeBus(bus_values), config_default)

    assert config["components"]["multiplus"]["available"] is False
    act = config["actuators"]["multiplus_mode"]
    assert act["available"] is False
    assert "write" not in act          # no address to offer
    assert "wallbox_charge" in config["actuators"]   # unrelated actuators survive


def test_startup_wakes_a_multiplus_with_dead_measurement_hardware(bus_values, fast_wake):
    """Switched off since boot, the Multiplus reports no DC voltage/current at all.

    Left alone it is discovered as present, contributes both variables to the poll
    map, and then drops them on every poll — the battery-voltage average quietly
    loses a component. Starting it once initialises the hardware; it is put back in
    the mode it was found in.
    """
    bus = WakingBus(_asleep_multiplus(bus_values))

    discovery.discover(bus, config_default)

    assert bus_values[MULTIPLUS]["/Dc/0/Voltage"] == 26.2
    assert bus_values[MULTIPLUS]["/Mode"] == MULTIPLUS_OFF     # found off, left off


def test_startup_gives_up_without_killing_discovery(bus_values, fast_wake, capsys):
    """A Multiplus that never reports measurements must not block startup."""
    bus = FakeBus(_asleep_multiplus(bus_values))     # never wakes

    config, _ = discovery.discover(bus, config_default)

    assert config["components"]["multiplus"]["available"] is True
    assert bus_values[MULTIPLUS]["/Mode"] == MULTIPLUS_OFF
    assert "still unavailable" in capsys.readouterr().out


def test_a_running_multiplus_is_left_alone(bus_values, fast_wake):
    """The default bus already reports DC measurements: no start, no AC pulse."""
    bus = FakeBus(bus_values)

    discovery.discover(bus, config_default)

    assert bus_values[MULTIPLUS]["/Mode"] == MULTIPLUS_ON      # untouched


def test_contract_round_trips_through_yaml(system_config, tmp_path):
    """It crosses a process boundary as YAML, so it must be plainly serialisable."""
    path = tmp_path / "system_configuration.yaml"
    discovery.save_system_configuration(system_config, path)
    assert discovery.load_system_configuration(path) == system_config


def test_state_without_on_off_labels_cannot_back_an_actuator(fake_bus):
    """tracking_state maps {0: 'off', 3: 'bulk', ...} — no "on", so it is not binary."""
    broken = dict(config_default.actuators)
    broken["bogus"] = dict(
        description="tracking_state is not a binary actuator",
        component="mppt150",
        write=dict(transport="dbus", state="tracking_state"),
        read=dict(transport="dbus", state="tracking_state"),
    )

    class Cfg:
        system_components = config_default.system_components
        measurement_components = config_default.measurement_components
        aux_components = config_default.aux_components
        actuators = broken

    with pytest.raises(ValueError, match="no 'on'/'off' labels"):
        discovery.discover(fake_bus, Cfg)


def test_vedirect_write_value_must_be_allowed_by_the_register(fake_bus):
    broken = dict(config_default.actuators)
    broken["mppt100_load"] = dict(
        description="99 is not in allowed_values [0, 1, 4, 5]",
        component="mppt100",
        write=dict(transport="vedirect", vedirect_set="load_control", on=99, off=0),
        read=dict(transport="dbus", state="load_state"),
    )

    class Cfg:
        system_components = config_default.system_components
        measurement_components = config_default.measurement_components
        aux_components = config_default.aux_components
        actuators = broken

    with pytest.raises(ValueError, match="not in allowed_values"):
        discovery.discover(fake_bus, Cfg)
