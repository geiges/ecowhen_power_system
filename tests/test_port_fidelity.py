"""Port-fidelity guard for the physical constants.

These are *measurements of one specific installation* — cable resistances, voltage
offsets, battery capacity. Nothing else in the suite would notice if a refactor
perturbed them: the SOC estimate would just drift, slowly and plausibly.

The values below were read off `victron_system_monitor/config_default.py` at the time
of the port. This test exists to catch a retyped digit, not to specify physics. If the
hardware is genuinely recalibrated, update the config and these numbers together.
"""
from ecowhen_power_system import config_default as config


def test_cable_correction_constants_match_the_installation():
    assert config.measurement_components == {
        "mppt150": {"connector_R0": 0.011, "voltage_offset": -0.1},
        "mppt100": {"connector_R0": 0.015, "voltage_offset": -0.1},
        "multiplus": {"connector_R0": 0.0035, "voltage_offset": -0.16},
    }


def test_battery_model_constants_match_the_installation():
    batt = config.batt_config_V1
    assert batt["Q_tot"] == 210      # Ah
    assert batt["ncells"] == 8
    assert batt["R0"] == 0.01        # ohm
    assert batt["R1"] == 0.04        # ohm
    assert batt["C1"] == 2000        # farad


def test_expected_components_are_configured():
    """Discovery is driven off these lists; a dropped entry silently stops logging it."""
    assert [c.short_name for c in config.system_components] == [
        "system", "mppt150", "mppt100", "multiplus", "phoenix",
    ]
    assert [c.short_name for c in config.aux_components] == [
        "wallbox", "ac_inverter", "ac_mppt",
    ]
