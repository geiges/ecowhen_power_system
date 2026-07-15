"""Polling produces the state contract."""
from datetime import datetime, timedelta

import pytest

from ecowhen_power_system import config_default, discovery, poll

from conftest import MPPT100


@pytest.fixture
def polled(fake_bus, system_config, monkeypatch):
    """A full poll with the aux HTTP devices stubbed out (no network in tests)."""
    from ecowhen_power_system import power_system

    monkeypatch.setattr(
        poll, "retrieve_aux_data",
        lambda components, debug=False: {
            "wallbox/power_w": 0.0, "ac_inverter/power_w": 12.0,
        },
    )
    psystem = power_system.init_power_system(
        config_default.system_components, config_default.measurement_components
    )
    t_now = datetime(2026, 7, 15, 12, 0, 0)
    state = poll.build_state(
        fake_bus, psystem, system_config, config_default,
        simulator=None, soc_tracker=poll.SocTracker(),
        t_now=t_now, running_since=t_now,
    )
    return state


def test_state_carries_a_full_iso_timestamp(polled):
    """The predecessor wrote a bare "01:04:27" and consumers stamped today's date
    on it, so a state frozen since yesterday read as current and nobody could tell
    the producer had died."""
    assert datetime.fromisoformat(polled["timestamp"]) == datetime(2026, 7, 15, 12, 0)
    assert datetime.fromisoformat(polled["running_since"])


def test_state_carries_dbus_variables_and_the_packed_state_code(polled):
    assert polled["mppt100/power_yield"] == 120.0
    assert polled["system/battery_voltage"] == 26.23
    assert set(polled["state"]) <= set("0123456789")


def test_state_carries_aux_values(polled):
    """Aux moved into this process, so wallbox power is in the state contract and
    the Logger derives aux_*.csv from it rather than polling separately."""
    assert polled["wallbox/power_w"] == 0.0
    assert polled["ac_inverter/power_w"] == 12.0


def test_summed_system_variables(polled):
    """mppt150 + mppt100 both yield 120 W; phoenix and multiplus have no yield."""
    assert polled["system/power_yield"] == 240.0


def test_unreadable_variable_is_skipped_not_fatal(fake_bus, system_config, monkeypatch):
    monkeypatch.setattr(poll, "retrieve_aux_data", lambda c, debug=False: {})
    del fake_bus._values[MPPT100]["/Yield/Power"]

    data = poll.retrieve_data(
        fake_bus, system_config["variables_to_log"], config_default
    )
    assert "mppt100/power_yield" not in data
    assert "mppt150/power_yield" in data       # siblings unaffected


def test_unreadable_state_becomes_none_then_encodes_as_nine(fake_bus, system_config):
    del fake_bus._values[MPPT100]["/Load/State"]
    states = poll.retrieve_states(fake_bus, system_config["states_to_log"])
    assert states["mppt100/load_state"] is None

    code = poll.encode_state_code(states, list(system_config["states_to_log"]))
    assert "9" in code


# --- SOC threshold tracking (replaces control's sim_*.csv globbing) -----------

def test_soc_tracker_clock_starts_on_the_rise_above_threshold():
    t0 = datetime(2026, 7, 15, 12, 0)
    tracker = poll.SocTracker()

    assert tracker.update(0.95, t0) is None
    assert tracker.minutes(t0) == 0.0

    tracker.update(0.995, t0 + timedelta(minutes=1))
    assert tracker.minutes(t0 + timedelta(minutes=31)) == pytest.approx(30.0)


def test_soc_tracker_clock_resets_when_soc_drops():
    t0 = datetime(2026, 7, 15, 12, 0)
    tracker = poll.SocTracker()
    tracker.update(1.0, t0)
    assert tracker.minutes(t0 + timedelta(minutes=10)) == pytest.approx(10.0)

    tracker.update(0.5, t0 + timedelta(minutes=11))
    assert tracker.since is None
    assert tracker.minutes(t0 + timedelta(minutes=12)) == 0.0


def test_soc_tracker_survives_midnight():
    """The predecessor recomputed this by scanning the current day's sim CSV, so a
    battery full since 18:00 reported "full for 5 minutes" at 00:05."""
    tracker = poll.SocTracker()
    tracker.update(1.0, datetime(2026, 7, 15, 18, 0))
    assert tracker.minutes(datetime(2026, 7, 16, 0, 5)) == pytest.approx(365.0)


# --- SOC persistence (replaces reading back the Logger's sim_*.csv) -----------

def test_soc_state_round_trips(tmp_path):
    path = tmp_path / "soc_state.json"
    t = datetime(2026, 7, 15, 12, 0)
    since = datetime(2026, 7, 15, 9, 30)

    poll.save_soc_state(path, soc=0.87, rc_voltage=0.02, t_now=t,
                        soc_above_threshold_since=since)
    loaded = poll.load_soc_state(path)

    assert loaded["soc"] == 0.87
    assert loaded["rc_voltage"] == 0.02
    assert loaded["timestamp"] == t
    assert loaded["soc_above_threshold_since"] == since


def test_missing_or_corrupt_soc_state_is_not_fatal(tmp_path):
    assert poll.load_soc_state(tmp_path / "nope.json") is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    assert poll.load_soc_state(corrupt) is None

    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text('{"soc": 0.5}')
    assert poll.load_soc_state(incomplete) is None


def test_save_soc_state_is_atomic(tmp_path):
    """The Logger and Control read this file freely; they must never catch a
    half-written estimate."""
    path = tmp_path / "soc_state.json"
    t = datetime(2026, 7, 15, 12, 0)
    poll.save_soc_state(path, 0.5, 0.0, t, None)
    poll.save_soc_state(path, 0.6, 0.0, t, None)

    assert poll.load_soc_state(path)["soc"] == 0.6
    assert list(tmp_path.glob("*.tmp")) == []


class _FakeSimulator:
    def __init__(self):
        self.initilized = False
        self.restored_to = None

    def set_state(self, SOC, t_now, RC_voltage=0):
        self.restored_to = (SOC, RC_voltage)


def test_fresh_soc_state_seeds_the_simulator():
    t = datetime(2026, 7, 15, 12, 0)
    sim = _FakeSimulator()
    soc_state = {
        "soc": 0.87, "rc_voltage": 0.02,
        "timestamp": t - timedelta(minutes=5),
        "soc_above_threshold_since": None,
    }

    assert poll.restore_simulator(sim, soc_state, timedelta(hours=1), t) is True
    assert sim.restored_to == (0.87, 0.02)
    assert sim.initilized is True


def test_stale_soc_state_is_discarded_in_favour_of_ocv():
    """Coulomb counting cannot account for a gap it did not observe, so an old
    estimate is worse than re-deriving SOC from open-circuit voltage."""
    t = datetime(2026, 7, 15, 12, 0)
    sim = _FakeSimulator()
    soc_state = {
        "soc": 0.87, "rc_voltage": 0.02,
        "timestamp": t - timedelta(hours=9),
        "soc_above_threshold_since": None,
    }

    assert poll.restore_simulator(sim, soc_state, timedelta(hours=1), t) is False
    assert sim.restored_to is None
    assert sim.initilized is False


def test_no_soc_state_on_first_boot_is_fine():
    """The predecessor restored from the Logger's sim CSV, which does not exist on a
    fresh boot — the state producer depended on its own consumer."""
    sim = _FakeSimulator()
    assert poll.restore_simulator(
        sim, None, timedelta(hours=1), datetime(2026, 7, 15, 12, 0)
    ) is False


def test_state_is_written_atomically_and_reloads(tmp_path, polled):
    path = tmp_path / "state.json"
    poll.save_state(path, polled)

    import json
    assert json.loads(path.read_text())["timestamp"] == polled["timestamp"]
    assert list(tmp_path.glob("*.tmp")) == []
