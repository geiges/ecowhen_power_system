"""End-to-end: the real Power System process against a fake bus.

Exercises the harvested numerics for real — Battery, ExtendedKalmanFilter and
System_Simulation, not stand-ins. If the port broke the estimator, this is what says so.
"""
import json
from datetime import datetime, timedelta

import pytest
import yaml

from ecowhen_power_system import config_default, discovery, paths, poll, power_system


@pytest.fixture
def real_sim(monkeypatch):
    monkeypatch.setattr(poll, "retrieve_aux_data", lambda c, debug=False: {})
    from ecowhen_power_system import simulation
    return simulation.System_Simulation(config_default.battery, config_default.batt_config_V1, debug=False)


@pytest.fixture
def psystem():
    return power_system.init_power_system(
        config_default.system_components, config_default.measurement_components
    )


def _poll(fake_bus, psystem, system_config, sim, tracker, t):
    return poll.build_state(
        fake_bus, psystem, system_config, config_default,
        simulator=sim, soc_tracker=tracker, t_now=t, running_since=t,
    )


def test_real_estimator_produces_a_plausible_soc(fake_bus, system_config, psystem, real_sim):
    t = datetime(2026, 7, 15, 12, 0)
    state = _poll(fake_bus, psystem, system_config, real_sim, poll.SocTracker(), t)

    assert 0.0 <= state["SOC_Kf"] <= 1.0
    assert 0.0 <= state["SOC_counted"] <= 1.0
    assert "OCV_est" in state
    assert "time_to_low_battery" in state


def test_cable_correction_is_actually_applied(fake_bus, system_config, psystem, real_sim):
    """The corrected voltage must differ from the raw reading, or the calibration
    is being silently ignored — the exact failure the constants have no other guard
    against. multiplus: 26.2 V raw at -8.1 A, R0=0.0035, offset=-0.16.
    """
    t = datetime(2026, 7, 15, 12, 0)
    state = _poll(fake_bus, psystem, system_config, real_sim, poll.SocTracker(), t)

    raw = 26.2
    expected = raw - (0.0035 * -8.1) + (-0.16)
    assert state["multiplus/DC_0_voltage"] == pytest.approx(expected)
    assert state["multiplus/DC_0_voltage"] != raw


def test_soc_evolves_over_successive_polls(fake_bus, system_config, psystem, real_sim):
    """Two polls a minute apart: the estimator must advance, not freeze."""
    tracker = poll.SocTracker()
    t0 = datetime(2026, 7, 15, 12, 0)

    first = _poll(fake_bus, psystem, system_config, real_sim, tracker, t0)
    assert real_sim.initilized

    fake_bus.set_value("com.victronenergy.system", "/Dc/Battery/Current", -20.0)
    second = _poll(fake_bus, psystem, system_config, real_sim, tracker, t0 + timedelta(minutes=1))

    assert second["SOC_counted"] != first["SOC_counted"]


def test_setup_publishes_the_topology_contract(fake_bus, runtime_dirs, monkeypatch):
    """The full startup path, including writing the file the other three read."""
    monkeypatch.setattr(poll, "retrieve_aux_data", lambda c, debug=False: {})
    import pytz

    from ecowhen_power_system import power_system_main
    monkeypatch.setattr(power_system_main, "paths", runtime_dirs)

    tz = pytz.timezone(config_default.tz)
    system_config, psystem, simulator, tracker = power_system_main.setup(fake_bus, tz)

    written = yaml.safe_load(runtime_dirs.SYSTEM_CONFIG_PATH.read_text())
    assert set(written["components"]) == {
        "system", "mppt150", "mppt100", "multiplus", "phoenix",
        'DS18B20_DC_multiplus','DS18B20_shunt', 'DS18B20_inside', 
    }
    assert "variables_to_log" in written
    assert "actuators" in written
    assert simulator is not None       # config_default.simulate_system is True


def test_soc_state_survives_a_restart(fake_bus, system_config, psystem, runtime_dirs,
                                      monkeypatch):
    """Poll, persist, then restore into a fresh simulator — no CSV involved."""
    monkeypatch.setattr(poll, "retrieve_aux_data", lambda c, debug=False: {})
    from ecowhen_power_system import simulation

    t = datetime(2026, 7, 15, 12, 0)
    sim = simulation.System_Simulation(config_default.battery, config_default.batt_config_V1, debug=False)
    state = _poll(fake_bus, psystem, system_config, sim, poll.SocTracker(), t)

    poll.save_soc_state(
        runtime_dirs.SOC_STATE_PATH, soc=state["SOC_counted"],
        rc_voltage=float(sim.Kf.x[1, 0]), t_now=t, soc_above_threshold_since=None,
    )

    revived = simulation.System_Simulation(config_default.battery, config_default.batt_config_V1, debug=False)
    loaded = poll.load_soc_state(runtime_dirs.SOC_STATE_PATH)
    assert poll.restore_simulator(
        revived, loaded, timedelta(hours=1), t + timedelta(minutes=1)
    )
    assert revived.battery_simulation.state_of_charge == pytest.approx(
        state["SOC_counted"], rel=1e-6
    )
