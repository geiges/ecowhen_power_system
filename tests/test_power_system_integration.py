"""End-to-end: the real Power System process against a fake bus.

Exercises the harvested numerics for real — Battery_Simulation and
ExtendedKalmanFilter, not stand-ins. If the port broke the estimator, this says so.
"""
import json
from datetime import datetime, timedelta

import pytest
import yaml

from ecowhen_power_system import config_default, discovery, paths, poll, power_system


@pytest.fixture
def real_sim(monkeypatch):
    monkeypatch.setattr(poll, "retrieve_aux_data", lambda c, debug=False: {})
    from ecowhen_power_system import battery_simulation
    return battery_simulation.Battery_Simulation(config_default.battery, debug=False)


@pytest.fixture
def psystem():
    return power_system.Power_system(config_default.system_components)


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


def test_battery_voltage_is_derived_from_every_source(fake_bus, system_config, psystem,
                                                      real_sim):
    """The clamp voltage is the mean of one estimate per source, each the source's
    terminal voltage less the drop its own current causes. If a cable constant were
    silently ignored the SOC would just drift, plausibly, with nothing else to say so.
    """
    t = datetime(2026, 7, 15, 12, 0)
    state = _poll(fake_bus, psystem, system_config, real_sim, poll.SocTracker(), t)

    estimates = [
        26.24 - 0.011 * 4.5 - 0.1,      # mppt150, charging: sits above the clamps
        26.24 - 0.015 * 4.5 - 0.1,      # mppt100, same
        26.2 - 0.0035 * -8.1 - 0.16,    # multiplus, drawing: sits below them
    ]
    assert state["battery/voltage"] == pytest.approx(sum(estimates) / 3)
    assert state["battery/voltage_spread"] == pytest.approx(
        max(estimates) - min(estimates)
    )

    # The raw readings survive untouched alongside the derived value — a measurement
    # and an estimate must never be mistaken for one another.
    assert state["multiplus/DC_0_voltage"] == 26.2
    assert state["system/battery_voltage"] == 26.23
    assert state["battery/voltage"] != state["system/battery_voltage"]


def test_battery_current_drops_the_sources_own_idle_draw(fake_bus, system_config,
                                                         psystem, real_sim):
    """The GX aggregate does not include what each source spends on itself."""
    t = datetime(2026, 7, 15, 12, 0)
    state = _poll(fake_bus, psystem, system_config, real_sim, poll.SocTracker(), t)

    idle = sum(psystem[name].const_consumption for name in ("mppt150", "mppt100",
                                                            "multiplus"))
    assert state["battery/current"] == pytest.approx(-0.15 - idle)


def test_a_source_that_stops_reporting_is_dropped_not_fatal(fake_bus, system_config,
                                                            psystem, real_sim):
    """A11: a Multiplus that has not run since boot reports no DC values at all.
    The average must lose that source and carry on, not divide by three anyway."""
    del fake_bus._values["com.victronenergy.vebus.ttyUSB2"]["/Dc/0/Voltage"]
    t = datetime(2026, 7, 15, 12, 0)

    state = _poll(fake_bus, psystem, system_config, real_sim, poll.SocTracker(), t)

    estimates = [26.24 - 0.011 * 4.5 - 0.1, 26.24 - 0.015 * 4.5 - 0.1]
    assert state["battery/voltage"] == pytest.approx(sum(estimates) / 2)


def test_the_filter_is_fed_discharge_positive_current(fake_bus, system_config, psystem,
                                                      real_sim):
    """A13. `kalman.py`'s B matrix is written for discharge-positive current; every
    other current in this package is charge-positive. Fed the wrong way the prediction
    step walks SOC in the opposite direction to coulomb counting, and the measurement
    update undoes it every step — plausible output, useless prediction. Pin the
    handoff, because nothing about the numbers looks wrong when it regresses.
    """
    t = datetime(2026, 7, 15, 12, 0)
    _poll(fake_bus, psystem, system_config, real_sim, poll.SocTracker(), t)   # init

    seen = {}
    predict, update = real_sim.Kf.predict, real_sim.Kf.update
    real_sim.Kf.predict = lambda time_delta, u: seen.update(predict_u=u) or \
        predict(time_delta=time_delta, u=u)
    real_sim.Kf.update = lambda z, u: seen.update(update_u=u) or update(z, u)

    state = _poll(fake_bus, psystem, system_config, real_sim, poll.SocTracker(),
                  t + timedelta(minutes=1))

    assert seen["predict_u"] == pytest.approx(-state["battery/current"])
    assert seen["update_u"] == pytest.approx(-state["battery/current"])
    # The fake bus is net-discharging, so the filter must be handed a positive number.
    assert state["battery/current"] < 0 < seen["predict_u"]


def test_prediction_and_counting_move_soc_the_same_way(real_sim):
    """The other half of A13, stated as physics rather than as a call signature:
    charge the battery and both estimators must agree on the direction."""
    sim = real_sim
    sim.set_state(0.5, t_now=datetime(2026, 7, 15, 12, 0), RC_voltage=0.0)

    charging = +30.0                       # A, Victron convention: positive = charging
    sim.count_charge(3600, charging)
    sim.Kf.predict(time_delta=3600, u=-charging)

    assert sim.state_of_charge > 0.5
    assert float(sim.Kf.x[0, 0]) > 0.5
    assert float(sim.Kf.x[0, 0]) == pytest.approx(sim.state_of_charge)


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
    from ecowhen_power_system import battery_simulation

    t = datetime(2026, 7, 15, 12, 0)
    sim = battery_simulation.Battery_Simulation(config_default.battery, debug=False)
    state = _poll(fake_bus, psystem, system_config, sim, poll.SocTracker(), t)

    poll.save_soc_state(
        runtime_dirs.SOC_STATE_PATH, soc=state["SOC_counted"],
        rc_voltage=float(sim.Kf.x[1, 0]), t_now=t, soc_above_threshold_since=None,
    )

    revived = battery_simulation.Battery_Simulation(config_default.battery, debug=False)
    loaded = poll.load_soc_state(runtime_dirs.SOC_STATE_PATH)
    assert poll.restore_simulator(
        revived, loaded, timedelta(hours=1), t + timedelta(minutes=1)
    )
    assert revived.state_of_charge == pytest.approx(
        state["SOC_counted"], rel=1e-6
    )
