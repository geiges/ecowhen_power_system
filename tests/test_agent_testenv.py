"""The two pieces of `tools/agent_testenv.py` that are not display code.

Everything else in the tool is printing and plotting. These two decide which
intervals get shaded green and what load the second SOC curve is priced with — the
one place a ported agent output could be read wrong without it looking wrong.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import agent_testenv  # noqa: E402

from ecowhen_power_system.control.agents.base import AgentResult  # noqa: E402
from ecowhen_power_system.control.coordinator import Booking, ModeRequest  # noqa: E402

NOW = datetime(2026, 8, 8, 12, 0)
HORIZON_END = NOW + timedelta(hours=96)


def test_the_mode_is_read_off_the_agent_not_declared_in_the_tool():
    """An agent planning `ac_solar` needs no entry in agent_testenv."""
    result = AgentResult(agent_name="x", rationale="", bookings=[
        Booking(mode="ac_solar", start=NOW, end=NOW + timedelta(hours=8)),
    ])
    assert agent_testenv._mode_windows(result, NOW, HORIZON_END) == (
        "ac_solar", [(NOW, NOW + timedelta(hours=8))])


def test_bookings_for_other_modes_are_ignored():
    result = AgentResult(agent_name="x", rationale="", bookings=[
        Booking(mode="wallbox", start=NOW + timedelta(hours=1),
                end=NOW + timedelta(hours=3)),
        Booking(mode="ac_solar", start=NOW, end=NOW + timedelta(hours=8)),
    ])
    assert agent_testenv._mode_windows(result, NOW, HORIZON_END) == (
        "wallbox", [(NOW + timedelta(hours=1), NOW + timedelta(hours=3))])


def test_live_request_runs_to_the_horizon_and_a_dropped_one_does_not():
    wanted = AgentResult(agent_name="x", rationale="", requests=[
        ModeRequest(mode="wallbox", active=True, agent="x")])
    dropped = AgentResult(agent_name="x", rationale="", requests=[
        ModeRequest(mode="wallbox", active=False, agent="x")])

    assert agent_testenv._mode_windows(wanted, NOW, HORIZON_END) == (
        "wallbox", [(NOW, HORIZON_END)])
    assert agent_testenv._mode_windows(dropped, NOW, HORIZON_END) == (None, [])


def test_extra_load_is_dc_side_and_only_inside_a_window():
    windows = [(NOW, NOW + timedelta(hours=2))]
    load = agent_testenv._extra_load_fn(windows, wallbox_w=1600.0, efficiency=0.8)

    assert load(NOW) == 2000.0
    assert load(NOW + timedelta(hours=1)) == 2000.0
    assert load(NOW + timedelta(hours=2)) == 0.0     # end is exclusive
    assert load(NOW - timedelta(minutes=1)) == 0.0
