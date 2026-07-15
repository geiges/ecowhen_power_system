"""The Mode/Service/Coordinator model, driven by fakes.

This is the design spike. It is the entire point of the refactor, and it ships in
Phase 4 — so it gets validated here, in Phase 1, before three processes are built on
top of it. Pure logic: no hardware, no Gateway, no Power System, no network.

The fakes stand in for the real actuators, but the state machine under test is the
real one.
"""
import json

import pytest

from ecowhen_power_system.control.coordinator import Coordinator, ModeRequest
from ecowhen_power_system.control.mode import Mode, ModeState
from ecowhen_power_system.control.service import ServicePhase, SystemService

CONFIG = object()      # the fakes ignore it; it only has to be passed through


class FakeService(SystemService):
    """An actuator that comes up after `latency` drive calls.

    Real hardware does not switch instantly — a Multiplus takes a moment to reflect
    /Mode. Latency is the default here, not the exception, because a state machine
    that only works against instant actuators does not work.
    """

    def __init__(self, name, latency=0, unreachable=False, accepts=True, **kw):
        super().__init__(name=name, **kw)
        self.latency = latency
        self.unreachable = unreachable
        self.accepts = accepts
        self.actual = self.default_on
        self.drives = []
        self._pending = None
        self._ticks_left = 0

    def read_actual_on(self, config):
        if self.unreachable:
            return None
        if self._pending is not None:
            self._ticks_left -= 1
            if self._ticks_left <= 0:
                self.actual = self._pending
                self._pending = None
        return self.actual

    def drive(self, on, config):
        self.drives.append(on)
        if not self.accepts:
            return False
        if self.latency == 0:
            self.actual = on
        elif self._pending != on:
            # Re-driving toward a target already in flight must not restart the
            # device. Writing 3 to /Mode again while it is already on its way to 3
            # changes nothing — which is exactly why the set has to be idempotent.
            self._pending = on
            self._ticks_left = self.latency
        return True


class FakeMode(Mode):
    def __init__(self, name, required_services, latency=0):
        super().__init__(name=name, required_services=required_services)
        self.latency = latency
        self.primary_on = False
        self.primary_calls = []
        self._pending = None
        self._ticks_left = 0

    def apply_primary(self, on, config):
        self.primary_calls.append(on)
        if self.latency == 0:
            self.primary_on = on
        else:
            self._pending = on
            self._ticks_left = self.latency
        return True

    def primary_satisfied(self, on, config):
        if self._pending is not None:
            self._ticks_left -= 1
            if self._ticks_left <= 0:
                self.primary_on = self._pending
                self._pending = None
        return self.primary_on == on


def build(mode_specs, service_specs, **kw):
    services = {n: FakeService(n, **s) for n, s in service_specs.items()}
    modes = {n: FakeMode(n, **m) for n, m in mode_specs.items()}
    return Coordinator(services, modes, **kw), services, modes


def activate(coord, mode, agent="agent", ticks=2):
    """Request a mode and tick until it can be up.

    Two ticks even for instant fakes, and that is not an artefact: a service drives
    on one tick and confirms on the next. Reading back in the same breath as the
    write would be trusting a value the device may not have applied yet.
    """
    coord.set_agent_requests([ModeRequest(mode, True, agent)])
    for _ in range(ticks):
        coord.tick(CONFIG)


# --- ordering ----------------------------------------------------------------

def test_primary_effect_waits_for_every_prerequisite():
    """The wallbox plug must not close before the inverter and fan are confirmed up.

    This is the property the predecessor spent two hand-written step files to get,
    once per direction.
    """
    coord, services, modes = build(
        {"wallbox": dict(required_services=["ac_inverter", "fan"])},
        {"ac_inverter": dict(latency=2), "fan": dict(latency=1)},
    )
    coord.set_agent_requests([ModeRequest("wallbox", True, "agent")])

    coord.tick(CONFIG)
    assert modes["wallbox"].state == ModeState.ACTIVATING
    assert modes["wallbox"].primary_calls == []      # nothing fired yet

    coord.tick(CONFIG)
    assert services["fan"].satisfied                 # fan is up, inverter is not
    assert not services["ac_inverter"].satisfied
    assert modes["wallbox"].primary_calls == []      # still gated

    coord.tick(CONFIG)
    assert services["ac_inverter"].satisfied
    assert modes["wallbox"].primary_calls == [True]  # only now
    assert modes["wallbox"].state == ModeState.ACTIVE


def test_prerequisites_converge_in_parallel_not_in_sequence():
    """Independent services must not queue behind each other."""
    coord, services, _ = build(
        {"wallbox": dict(required_services=["ac_inverter", "fan"])},
        {"ac_inverter": dict(latency=1), "fan": dict(latency=1)},
    )
    coord.set_agent_requests([ModeRequest("wallbox", True, "agent")])

    coord.tick(CONFIG)
    assert services["ac_inverter"].drives == [True]
    assert services["fan"].drives == [True]          # same tick, not the next


def test_teardown_drops_the_effect_before_releasing_prerequisites():
    """Reverse order falls out; nobody writes it down.

    Releasing first would let the inverter drop while the wallbox was still drawing.
    """
    coord, services, modes = build(
        {"wallbox": dict(required_services=["ac_inverter"])},
        {"ac_inverter": dict()},
    )
    activate(coord, "wallbox")
    assert modes["wallbox"].state == ModeState.ACTIVE

    coord.set_agent_requests([ModeRequest("wallbox", False, "agent")])
    coord.tick(CONFIG)

    assert modes["wallbox"].primary_calls == [True, False]
    assert modes["wallbox"].state == ModeState.INACTIVE
    assert services["ac_inverter"].required_by == set()

    coord.tick(CONFIG)
    assert services["ac_inverter"].actual is False   # reverted once nothing held it


# --- reference counting ------------------------------------------------------

def test_shared_service_stays_on_while_any_mode_needs_it():
    coord, services, modes = build(
        {
            "wallbox": dict(required_services=["ac_inverter"]),
            "heater": dict(required_services=["ac_inverter"]),
        },
        {"ac_inverter": dict()},
    )
    coord.set_agent_requests([
        ModeRequest("wallbox", True, "agent_a"),
        ModeRequest("heater", True, "agent_b"),
    ])
    coord.tick(CONFIG)
    coord.tick(CONFIG)
    assert services["ac_inverter"].required_by == {"wallbox", "heater"}

    # One mode lets go; the other still needs the inverter.
    coord.set_agent_requests([
        ModeRequest("wallbox", False, "agent_a"),
        ModeRequest("heater", True, "agent_b"),
    ])
    coord.tick(CONFIG)
    assert services["ac_inverter"].required_by == {"heater"}
    assert services["ac_inverter"].target_on is True
    assert services["ac_inverter"].actual is True

    coord.set_agent_requests([ModeRequest("heater", False, "agent_b")])
    coord.tick(CONFIG)
    coord.tick(CONFIG)
    assert services["ac_inverter"].actual is False   # last requirer gone


def test_two_agents_wanting_the_same_mode_both_have_to_let_go():
    coord, _, modes = build(
        {"wallbox": dict(required_services=[])}, {},
    )
    coord.set_agent_requests([
        ModeRequest("wallbox", True, "agent_a"),
        ModeRequest("wallbox", True, "agent_b"),
    ])
    coord.tick(CONFIG)
    assert modes["wallbox"].requesters == {"agent_a", "agent_b"}

    # Every enabled agent re-states its whole opinion each cycle, so agent_b must
    # still be asking here. Omitting it would not mean "b is unchanged" — it would
    # mean b released, which is what the next test covers.
    coord.set_agent_requests([
        ModeRequest("wallbox", False, "agent_a"),
        ModeRequest("wallbox", True, "agent_b"),
    ])
    coord.tick(CONFIG)
    assert modes["wallbox"].requesters == {"agent_b"}
    assert modes["wallbox"].state == ModeState.ACTIVE     # agent_b still wants it


def test_an_agent_that_goes_silent_has_released_the_mode():
    """Silence is release. An agent that gets disabled, or errors out mid-cycle,
    stops producing requests — and must not leave a mode latched on for ever.
    """
    coord, _, modes = build({"wallbox": dict(required_services=[])}, {})
    coord.set_agent_requests([ModeRequest("wallbox", True, "agent")])
    coord.tick(CONFIG)
    assert modes["wallbox"].state == ModeState.ACTIVE

    coord.set_agent_requests([])           # the agent said nothing at all
    coord.tick(CONFIG)
    assert modes["wallbox"].requesters == set()
    assert modes["wallbox"].state == ModeState.INACTIVE


# --- convergence -------------------------------------------------------------

def test_service_is_idempotent_once_satisfied():
    """No re-driving an actuator that already reads correct. The predecessor's
    Gateway could only toggle, which made this the difference between holding a
    value and oscillating around it.
    """
    coord, services, _ = build(
        {"wallbox": dict(required_services=["ac_inverter"])},
        {"ac_inverter": dict()},
    )
    coord.set_agent_requests([ModeRequest("wallbox", True, "agent")])
    for _ in range(5):
        coord.tick(CONFIG)

    assert services["ac_inverter"].drives == [True]       # once, not five times


def test_service_retries_then_fails_and_the_mode_reports_why():
    coord, services, modes = build(
        {"wallbox": dict(required_services=["ac_inverter"])},
        {"ac_inverter": dict(accepts=False, max_attempts=3)},
    )
    coord.set_agent_requests([ModeRequest("wallbox", True, "agent")])
    for _ in range(6):
        coord.tick(CONFIG)

    assert services["ac_inverter"].phase == ServicePhase.FAILED
    assert len(services["ac_inverter"].drives) == 3        # bounded by max_attempts
    assert modes["wallbox"].state == ModeState.FAILED
    assert "ac_inverter" in modes["wallbox"].status().detail
    assert modes["wallbox"].primary_calls == []            # never fired


def test_failed_prerequisite_does_not_silently_tear_down_the_requirement():
    """A fault is something to look at, not a reason to start dropping services."""
    coord, services, _ = build(
        {"wallbox": dict(required_services=["ac_inverter"])},
        {"ac_inverter": dict(accepts=False, max_attempts=2)},
    )
    coord.set_agent_requests([ModeRequest("wallbox", True, "agent")])
    for _ in range(4):
        coord.tick(CONFIG)

    assert services["ac_inverter"].required_by == {"wallbox"}


def test_unreachable_hardware_waits_rather_than_driving_blind():
    """A None readback means we cannot see the device. Writing anyway would mean
    commanding it every tick with no idea whether it landed."""
    coord, services, _ = build(
        {"wallbox": dict(required_services=["ac_inverter"])},
        {"ac_inverter": dict(unreachable=True)},
    )
    coord.set_agent_requests([ModeRequest("wallbox", True, "agent")])
    for _ in range(4):
        coord.tick(CONFIG)

    assert services["ac_inverter"].drives == []
    assert services["ac_inverter"].phase == ServicePhase.CONVERGING
    assert "unknown" in services["ac_inverter"].status().detail


# --- safety ------------------------------------------------------------------

def test_inhibit_deactivates_modes_and_lets_services_revert():
    coord, services, modes = build(
        {"wallbox": dict(required_services=["ac_inverter"])},
        {"ac_inverter": dict()},
    )
    activate(coord, "wallbox")
    assert services["ac_inverter"].actual is True
    assert modes["wallbox"].state == ModeState.ACTIVE

    coord.inhibit("battery over-temperature")
    coord.tick(CONFIG)
    assert modes["wallbox"].primary_calls == [True, False]
    assert modes["wallbox"].state == ModeState.INACTIVE

    coord.tick(CONFIG)
    assert services["ac_inverter"].actual is False


def test_inhibit_beats_an_agent_that_keeps_asking():
    """The agent has no idea safety intervened; it just re-requests every cycle."""
    coord, _, modes = build(
        {"wallbox": dict(required_services=[])}, {},
    )
    coord.inhibit("battery over-temperature")
    for _ in range(3):
        coord.set_agent_requests([ModeRequest("wallbox", True, "agent")])
        coord.tick(CONFIG)

    assert modes["wallbox"].state == ModeState.INACTIVE
    assert modes["wallbox"].requesters == set()


def test_clearing_the_inhibit_lets_modes_come_back():
    coord, _, modes = build({"wallbox": dict(required_services=[])}, {})
    coord.inhibit("battery over-temperature")
    coord.set_agent_requests([ModeRequest("wallbox", True, "agent")])
    coord.tick(CONFIG)
    assert modes["wallbox"].state == ModeState.INACTIVE

    coord.clear_inhibit()
    coord.set_agent_requests([ModeRequest("wallbox", True, "agent")])
    coord.tick(CONFIG)
    assert modes["wallbox"].state == ModeState.ACTIVE


# --- user intent across the process boundary ---------------------------------

def test_user_request_is_read_from_disk_each_tick(tmp_path):
    """The Gateway writes this file; the runner reads it. Separate processes, so the
    only channel is the filesystem — and it must survive a restart.
    """
    path = tmp_path / "mode_requests.json"
    path.write_text(json.dumps({"modes": ["wallbox"]}))

    coord, _, modes = build(
        {"wallbox": dict(required_services=[])}, {}, mode_requests_path=path,
    )
    coord.tick(CONFIG)
    assert modes["wallbox"].requesters == {"user"}
    assert modes["wallbox"].state == ModeState.ACTIVE

    path.write_text(json.dumps({"modes": []}))
    coord.tick(CONFIG)
    assert modes["wallbox"].state == ModeState.INACTIVE


def test_missing_or_unparsable_request_file_is_not_fatal(tmp_path):
    coord, _, _ = build(
        {"wallbox": dict(required_services=[])}, {},
        mode_requests_path=tmp_path / "nope.json",
    )
    assert coord.read_user_requests() == set()

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    coord.mode_requests_path = bad
    assert coord.read_user_requests() == set()


def test_unknown_mode_names_are_ignored(tmp_path):
    path = tmp_path / "mode_requests.json"
    path.write_text(json.dumps({"modes": ["wallbox", "teleporter"]}))
    coord, _, _ = build(
        {"wallbox": dict(required_services=[])}, {}, mode_requests_path=path,
    )
    assert coord.read_user_requests() == {"wallbox"}


def test_user_and_agent_are_independent_requesters(tmp_path):
    """An agent losing interest must not switch off something the user asked for."""
    path = tmp_path / "mode_requests.json"
    path.write_text(json.dumps({"modes": ["wallbox"]}))
    coord, _, modes = build(
        {"wallbox": dict(required_services=[])}, {}, mode_requests_path=path,
    )
    coord.set_agent_requests([ModeRequest("wallbox", True, "agent")])
    coord.tick(CONFIG)
    assert modes["wallbox"].requesters == {"user", "agent"}

    coord.set_agent_requests([])
    coord.tick(CONFIG)
    assert modes["wallbox"].requesters == {"user"}
    assert modes["wallbox"].state == ModeState.ACTIVE


# --- status ------------------------------------------------------------------

def test_status_files_are_written_for_the_gateway_to_serve(tmp_path):
    modes_path = tmp_path / "modes_status.json"
    services_path = tmp_path / "services_status.json"
    coord, _, _ = build(
        {"wallbox": dict(required_services=["ac_inverter"])},
        {"ac_inverter": dict()},
        modes_status_path=modes_path, services_status_path=services_path,
    )
    activate(coord, "wallbox")

    modes_status = json.loads(modes_path.read_text())
    assert modes_status["modes"]["wallbox"]["state"] == "active"
    assert modes_status["inhibited"] is False

    services_status = json.loads(services_path.read_text())
    assert services_status["services"]["ac_inverter"]["requirers"] == ["wallbox"]
    assert list(tmp_path.glob("*.tmp")) == []      # written atomically
