"""Port-fidelity guard for the physical constants.

These are *measurements of one specific installation* — cable resistances, voltage
offsets, battery capacity. Nothing else in the suite would notice if a refactor
perturbed them: the SOC estimate would just drift, slowly and plausibly.

The values below were read off `victron_system_monitor/config_default.py` at the time
of the port. This test exists to catch a retyped digit, not to specify physics. If the
hardware is genuinely recalibrated, update the config and these numbers together.
"""
from ecowhen_power_system import config_default as config
from ecowhen_power_system.specifications import SPECS_BATTERY_CELL


def test_clamp_derivation_constants_match_the_installation():
    """The cables between each source and the battery clamps."""
    assert config.battery.indirect_voltage_measurements == {
        "mppt150": {"variable": "DC_0_voltage",
                    "connector_R0": 0.011, "voltage_offset": -0.1},
        "mppt100": {"variable": "DC_0_voltage",
                    "connector_R0": 0.015, "voltage_offset": -0.1},
        "multiplus": {"variable": "DC_0_voltage",
                      "connector_R0": 0.0035, "voltage_offset": -0.16},
    }
    assert config.battery.indirect_current_measurements == {
        "mppt150": {"variable": "DC_0_current"},
        "mppt100": {"variable": "DC_0_current"},
        "multiplus": {"variable": "DC_0_current"},
    }


def test_battery_model_constants_match_the_installation():
    batt = config.battery
    assert batt.Q_tot == 310      # Ah
    assert batt.n_cells == 8
    assert batt.model_parameter.R0 == 0.01        # ohm
    assert batt.model_parameter.R1 == 0.04        # ohm
    assert batt.model_parameter.C1 == 2000        # farad
    # The per-cell OCV curve the SOC estimate is inverted out of. battery.py used to
    # declare two of these and silently keep the second; this is that one.
    assert SPECS_BATTERY_CELL["lifepo4"]["ocv_curve"] == [
        2.5, 3.0, 3.19, 3.22, 3.25, 3.26, 3.27, 3.3, 3.32, 3.35, 3.5,
    ]


def test_expected_components_are_configured():
    """Discovery is driven off these lists; a dropped entry silently stops logging it."""
    assert [c.short_name for c in config.system_components] == [
        "system", "battery", "mppt150", "mppt100", "multiplus", "phoenix",
        'DS18B20_DC_multiplus','DS18B20_shunt', 'DS18B20_inside', 
    ]
    assert [c.short_name for c in config.aux_components] == [
        "wallbox", "ac_inverter", "ac_mppt",
    ]

 