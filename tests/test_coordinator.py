"""The Mode/Service/Coordinator model, driven by fakes.

This is the design spike. It is the entire point of the refactor, and it ships in
Phase 4 — so it gets validated here, in Phase 1, before three processes are built on
top of it. Pure logic: no hardware, no Gateway, no Power System, no network.

The fakes stand in for the real actuators, but the state machine under test is the
real one.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from ecowhen_power_system.control.coordinator import (Booking, Coordinator,
                                                      ModeBlock, ModeRequest)
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
    def __init__(self, name, required_services, latency=0, blocked_by=None):
        super().__init__(name=name, required_services=required_services,
                         blocked_by=blocked_by)
        self.latency = latency
        self.primary_on = False
        self.primary_calls = []
        self._pending = None
        self._ticks_left = 0

    def apply_primary(self, on, config):
        self.primary_calls.append(on)
        if self.latency == 0:
            self.primary_on = on
        elif self._pending != on:
            # Same idempotence as FakeService: re-applying a target already in
            # flight must not restart the clock, or a latent mode never converges.
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


def test_failed_is_a_dead_end_until_the_user_resets_it(tmp_path):
    """`failed` never clears itself, and re-requesting does not clear it either.

    The mode still holds its prerequisite, so `require` is a no-op on a name already
    in the set and the service keeps its burnt retry budget. Only the reset releases
    it, which is what hands the budget back.
    """
    path = tmp_path / "mode_requests.json"
    path.write_text(json.dumps({"modes": ["wallbox"]}))
    coord, services, modes = build(
        {"wallbox": dict(required_services=["ac_inverter"])},
        {"ac_inverter": dict(accepts=False, max_attempts=2)},
        mode_requests_path=path,
    )
    for _ in range(6):
        coord.tick(CONFIG)
    assert modes["wallbox"].state == ModeState.FAILED
    assert services["ac_inverter"].phase == ServicePhase.FAILED
    drives_before = len(services["ac_inverter"].drives)

    for _ in range(3):                       # still wanted, and still going nowhere
        coord.tick(CONFIG)
    assert len(services["ac_inverter"].drives) == drives_before
    assert modes["wallbox"].state == ModeState.FAILED

    # The Gateway's `POST /control/modes/wallbox/reset`, on disk.
    path.write_text(json.dumps({"modes": ["wallbox"], "resets": {"wallbox": "t1"}}))
    services["ac_inverter"].accepts = True
    for _ in range(3):
        coord.tick(CONFIG)

    assert len(services["ac_inverter"].drives) > drives_before
    assert modes["wallbox"].state == ModeState.ACTIVE


def test_a_reset_fires_once_per_token(tmp_path):
    """The one edge in a level-triggered file. Re-reading the same token must not
    keep clearing a mode that has failed again since."""
    path = tmp_path / "mode_requests.json"
    path.write_text(json.dumps({"modes": ["wallbox"], "resets": {"wallbox": "t1"}}))
    coord, services, modes = build(
        {"wallbox": dict(required_services=["ac_inverter"])},
        {"ac_inverter": dict(accepts=False, max_attempts=1)},
        mode_requests_path=path,
    )
    for _ in range(6):
        coord.tick(CONFIG)
    assert modes["wallbox"].state == ModeState.FAILED

    path.write_text(json.dumps({"modes": ["wallbox"], "resets": {"wallbox": "t2"}}))
    coord.tick(CONFIG)
    assert modes["wallbox"].state == ModeState.ACTIVATING


def test_reset_of_a_mode_that_has_not_failed_does_nothing(tmp_path):
    """Including every token replayed after a restart, when nothing has failed yet."""
    path = tmp_path / "mode_requests.json"
    path.write_text(json.dumps({"modes": ["wallbox"], "resets": {"wallbox": "t1"}}))
    coord, services, modes = build(
        {"wallbox": dict(required_services=["ac_inverter"])}, {"ac_inverter": {}},
        mode_requests_path=path,
    )
    for _ in range(3):
        coord.tick(CONFIG)
    assert modes["wallbox"].state == ModeState.ACTIVE
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


def test_unreadable_and_unwanted_is_idle_not_converging():
    """Converging toward what? Nothing requires it and it is already at its default.
    Calling that "converging" showed the unreachable plugs as permanently on their
    way somewhere."""
    coord, services, _ = build({}, {"ac_inverter": dict(unreachable=True)})
    coord.tick(CONFIG)

    assert services["ac_inverter"].phase == ServicePhase.IDLE
    assert services["ac_inverter"].drives == []


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


# --- blocking ----------------------------------------------------------------

def test_a_block_outvotes_every_requester_without_releasing_them():
    """The mirror of a request, and stronger. `ModeRequest(active=False)` only
    withdraws one agent's want; a block holds the mode off however many still ask,
    and leaves them asking so the want returns by itself."""
    coord, services, modes = build(
        {"ac_solar": dict(required_services=["ac_inverter"])},
        {"ac_inverter": dict()},
    )
    activate(coord, "ac_solar", agent="user_ish")
    assert modes["ac_solar"].state == ModeState.ACTIVE

    coord.set_agent_requests([ModeRequest("ac_solar", True, "user_ish")])
    coord.set_agent_blocks([ModeBlock("ac_solar", "system_safety", "battery hot")])
    coord.tick(CONFIG)

    assert modes["ac_solar"].primary_calls == [True, False], "effect dropped first"
    assert modes["ac_solar"].state == ModeState.INACTIVE
    assert modes["ac_solar"].requesters == {"user_ish"}, "still wanted, not released"
    assert modes["ac_solar"].blockers == {"system_safety"}

    coord.tick(CONFIG)
    assert services["ac_inverter"].actual is False, "and the service let go after"


def test_a_block_is_level_triggered_so_silence_permits():
    coord, _, modes = build({"ac_solar": dict(required_services=[])}, {})
    coord.set_agent_requests([ModeRequest("ac_solar", True, "agent")])
    coord.set_agent_blocks([ModeBlock("ac_solar", "system_safety", "battery hot")])
    coord.tick(CONFIG)
    assert modes["ac_solar"].state == ModeState.INACTIVE

    coord.set_agent_blocks([])          # the reading came back down
    coord.set_agent_requests([ModeRequest("ac_solar", True, "agent")])
    for _ in range(2):
        coord.tick(CONFIG)

    assert modes["ac_solar"].state == ModeState.ACTIVE
    assert modes["ac_solar"].blockers == set()


def test_one_mode_excludes_another_from_the_moment_it_is_wanted():
    """`ac_other` yields to `wallbox` rather than sharing the AC budget with it.

    The declaration is one-sided and that *is* the priority rule — the mode that
    gives way is the one that says so. It gives way as soon as the wallbox is
    wanted, not once the wallbox is up, or the two overlap while it winds up.
    """
    coord, _, modes = build(
        {"wallbox": dict(required_services=["ac_inverter"], latency=1),
         "ac_other": dict(required_services=["ac_inverter"],
                          blocked_by=["wallbox"])},
        {"ac_inverter": dict()},
    )
    activate(coord, "ac_other")
    assert modes["ac_other"].state == ModeState.ACTIVE

    coord.set_agent_requests([ModeRequest("ac_other", True, "agent"),
                              ModeRequest("wallbox", True, "agent")])
    coord.tick(CONFIG)

    assert modes["ac_other"].blockers == {"mode:wallbox"}
    assert modes["ac_other"].primary_calls[-1] is False, "yielded on the same tick"
    # Both move in that one tick, because they share a service that is already up.
    # This is an exclusion, not a hardware interlock: it keeps the two off each
    # other over any interval that matters, not within a single tick.

    # The wallbox lets go: the standing want for ac_other takes effect again.
    coord.set_agent_requests([ModeRequest("ac_other", True, "agent")])
    for _ in range(4):
        coord.tick(CONFIG)

    assert modes["wallbox"].state == ModeState.INACTIVE
    assert modes["ac_other"].state == ModeState.ACTIVE


def test_the_exclusion_holds_until_the_other_mode_is_all_the_way_down():
    """Released is not the same as off. The wallbox plug is still closed while it
    deactivates, so unblocking on the release alone would overlap the two."""
    coord, _, modes = build(
        {"wallbox": dict(required_services=[], latency=2),
         "ac_other": dict(required_services=[], blocked_by=["wallbox"])},
        {},
    )
    activate(coord, "wallbox")
    assert modes["wallbox"].state == ModeState.ACTIVE

    coord.set_agent_requests([ModeRequest("ac_other", True, "agent")])
    coord.tick(CONFIG)
    assert modes["wallbox"].state == ModeState.DEACTIVATING
    assert modes["ac_other"].blockers == {"mode:wallbox"}, "still winding down"

    for _ in range(3):
        coord.tick(CONFIG)
    assert modes["wallbox"].state == ModeState.INACTIVE
    assert modes["ac_other"].state == ModeState.ACTIVE


def test_an_inhibit_clears_the_blocks_it_makes_redundant():
    """Everything is off under an inhibit, so a leftover blocker would only be a
    stale line on the status page."""
    coord, _, modes = build({"ac_solar": dict(required_services=[])}, {})
    coord.set_agent_requests([ModeRequest("ac_solar", True, "agent")])
    coord.set_agent_blocks([ModeBlock("ac_solar", "system_safety", "battery hot")])
    coord.tick(CONFIG)

    coord.inhibit("battery over-temperature")
    coord.tick(CONFIG)
    assert modes["ac_solar"].blockers == set()
    assert modes["ac_solar"].requesters == set()


def test_a_blocked_mode_says_who_is_holding_it_off():
    coord, _, modes = build({"ac_solar": dict(required_services=[])}, {})
    coord.set_agent_requests([ModeRequest("ac_solar", True, "agent")])
    coord.set_agent_blocks([ModeBlock("ac_solar", "system_safety", "battery hot")])
    status = coord.tick(CONFIG)["modes"]["ac_solar"]

    assert status["blockers"] == ["system_safety"]
    assert status["requesters"] == ["agent"]
    assert "blocked by system_safety" in status["detail"]


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


# --- bookings ----------------------------------------------------------------
#
# A booking is a mode wanted over an interval. The coordinator resolves which ones
# cover `now` on every tick, so nothing latches and no firing moment can be missed.

T0 = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def booked(coord, *, agent="planner", mode="wallbox", start=None, end=None):
    coord.set_plan(agent, [Booking(mode=mode,
                                   start=start or T0,
                                   end=end or T0 + timedelta(hours=2))])


def test_a_booking_is_inert_outside_its_interval_and_active_inside_it():
    coord, _, modes = build({"wallbox": dict(required_services=[])}, {})
    booked(coord)

    coord.tick(CONFIG, T0 - timedelta(minutes=1))
    assert modes["wallbox"].requesters == set()

    coord.tick(CONFIG, T0)
    assert modes["wallbox"].requesters == {"planner"}

    # `end` is exclusive: at exactly 14:00 the window is over.
    coord.tick(CONFIG, T0 + timedelta(hours=2))
    assert modes["wallbox"].requesters == set()


def test_a_plan_stands_across_ticks_without_being_restated():
    """The whole point: the agent speaks once, the boundary still lands on a tick."""
    coord, _, modes = build({"wallbox": dict(required_services=[])}, {})
    booked(coord)

    for minute in range(0, 120, 15):
        coord.tick(CONFIG, T0 + timedelta(minutes=minute))
        assert modes["wallbox"].requesters == {"planner"}, f"dropped at +{minute}min"


def test_an_agent_replaces_only_its_own_plan():
    coord, _, modes = build({"wallbox": dict(required_services=[]),
                             "cooling": dict(required_services=[])}, {})
    coord.set_plan("planner_a", [Booking("wallbox", T0, T0 + timedelta(hours=2))])
    coord.set_plan("planner_b", [Booking("cooling", T0, T0 + timedelta(hours=2))])

    coord.set_plan("planner_a", [])              # a_a revises: it wants nothing now
    coord.tick(CONFIG, T0 + timedelta(minutes=5))

    assert modes["wallbox"].requesters == set()
    assert modes["cooling"].requesters == {"planner_b"}


def test_dropping_a_plan_releases_the_mode_it_was_driving():
    """A plan outlives its author, so a disabled or broken agent must lose it."""
    coord, _, modes = build({"wallbox": dict(required_services=[])}, {})
    booked(coord)
    coord.tick(CONFIG, T0)
    assert modes["wallbox"].requesters == {"planner"}

    coord.drop_plan("planner")
    coord.tick(CONFIG, T0 + timedelta(minutes=1))
    assert modes["wallbox"].requesters == set()


def test_a_booking_and_a_request_for_one_mode_are_two_requesters():
    """Reference counting spans both ways of wanting a mode."""
    coord, _, modes = build({"wallbox": dict(required_services=[])}, {})
    booked(coord)
    coord.set_agent_requests([ModeRequest("wallbox", True, "fast_agent")])
    coord.tick(CONFIG, T0)
    assert modes["wallbox"].requesters == {"planner", "fast_agent"}

    # The fast agent falls silent; the booking still holds the mode up.
    coord.set_agent_requests([])
    coord.tick(CONFIG, T0 + timedelta(minutes=1))
    assert modes["wallbox"].requesters == {"planner"}


def test_the_schedule_is_published_with_its_sources_and_ages(tmp_path):
    schedule_path = tmp_path / "mode_schedule.json"
    coord, _, _ = build({"wallbox": dict(required_services=[])}, {},
                        schedule_path=schedule_path)
    coord.set_plan("planner", [Booking("wallbox", T0 + timedelta(hours=1),
                                       T0 + timedelta(hours=3), reason="solar")],
                   generated_at=T0)
    coord.tick(CONFIG, T0)

    published = json.loads(schedule_path.read_text())
    row, = published["bookings"]
    assert row["mode"] == "wallbox"
    assert row["source"] == "planner"
    assert row["reason"] == "solar"
    assert row["generated_at"] == T0.isoformat()
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_user_booking_drives_the_mode_over_its_interval(tmp_path):
    """The user's calendar is durable and lives in a file the Gateway writes, but it
    resolves through exactly the same path as an agent's plan."""
    requests_path = tmp_path / "mode_requests.json"
    requests_path.write_text(json.dumps({
        "modes": [],
        "bookings": [{"id": "abc12345", "mode": "wallbox",
                      "start": T0.isoformat(),
                      "end": (T0 + timedelta(hours=2)).isoformat(),
                      "reason": "leaving at 14:00"}],
        "updated_at": (T0 - timedelta(hours=6)).isoformat(),
    }))
    coord, _, modes = build({"wallbox": dict(required_services=[])}, {},
                            mode_requests_path=requests_path)

    coord.tick(CONFIG, T0 - timedelta(minutes=1))
    assert modes["wallbox"].requesters == set()

    coord.tick(CONFIG, T0 + timedelta(minutes=30))
    assert modes["wallbox"].requesters == {"user"}

    coord.tick(CONFIG, T0 + timedelta(hours=2))
    assert modes["wallbox"].requesters == set()


def test_a_user_booking_is_re_read_every_tick(tmp_path):
    """Control never holds it in memory — the Gateway may rewrite it at any moment."""
    requests_path = tmp_path / "mode_requests.json"
    requests_path.write_text(json.dumps({"modes": [], "bookings": []}))
    coord, _, modes = build({"wallbox": dict(required_services=[])}, {},
                            mode_requests_path=requests_path)

    coord.tick(CONFIG, T0)
    assert modes["wallbox"].requesters == set()

    requests_path.write_text(json.dumps({"modes": [], "bookings": [
        {"id": "x", "mode": "wallbox", "start": T0.isoformat(),
         "end": (T0 + timedelta(hours=1)).isoformat()}]}))
    coord.tick(CONFIG, T0 + timedelta(minutes=1))
    assert modes["wallbox"].requesters == {"user"}


def test_a_standing_want_and_a_booking_coexist(tmp_path):
    """`modes` has no end; a booking does. Both are the user, so one requester."""
    requests_path = tmp_path / "mode_requests.json"
    requests_path.write_text(json.dumps({
        "modes": ["cooling"],
        "bookings": [{"id": "x", "mode": "wallbox", "start": T0.isoformat(),
                      "end": (T0 + timedelta(hours=1)).isoformat()}],
    }))
    coord, _, modes = build({"wallbox": dict(required_services=[]),
                             "cooling": dict(required_services=[])}, {},
                            mode_requests_path=requests_path)

    coord.tick(CONFIG, T0)
    assert modes["wallbox"].requesters == {"user"}
    assert modes["cooling"].requesters == {"user"}

    # The booking ends; the standing want does not.
    coord.tick(CONFIG, T0 + timedelta(hours=2))
    assert modes["wallbox"].requesters == set()
    assert modes["cooling"].requesters == {"user"}


def test_an_unparseable_user_booking_does_not_cost_the_others(tmp_path):
    requests_path = tmp_path / "mode_requests.json"
    requests_path.write_text(json.dumps({"modes": [], "bookings": [
        {"id": "bad", "mode": "wallbox", "start": "not-a-date", "end": "also-not"},
        {"id": "good", "mode": "wallbox", "start": T0.isoformat(),
         "end": (T0 + timedelta(hours=1)).isoformat()},
    ]}))
    coord, _, modes = build({"wallbox": dict(required_services=[])}, {},
                            mode_requests_path=requests_path)

    coord.tick(CONFIG, T0)
    assert modes["wallbox"].requesters == {"user"}


def test_the_user_calendar_is_published_alongside_the_agents(tmp_path):
    requests_path = tmp_path / "mode_requests.json"
    schedule_path = tmp_path / "mode_schedule.json"
    requests_path.write_text(json.dumps({"modes": [], "bookings": [
        {"id": "x", "mode": "wallbox", "start": (T0 + timedelta(hours=4)).isoformat(),
         "end": (T0 + timedelta(hours=6)).isoformat(), "reason": "leaving at 18:00"}]}))
    coord, _, _ = build({"wallbox": dict(required_services=[])}, {},
                        mode_requests_path=requests_path, schedule_path=schedule_path)
    coord.set_plan("planner", [Booking("wallbox", T0 + timedelta(hours=1),
                                       T0 + timedelta(hours=2))], generated_at=T0)
    coord.tick(CONFIG, T0)

    rows = json.loads(schedule_path.read_text())["bookings"]
    assert [r["source"] for r in rows] == ["planner", "user"]   # sorted by start


def test_bookings_that_have_ended_leave_the_schedule(tmp_path):
    schedule_path = tmp_path / "mode_schedule.json"
    coord, _, _ = build({"wallbox": dict(required_services=[])}, {},
                        schedule_path=schedule_path)
    booked(coord)

    coord.tick(CONFIG, T0 + timedelta(hours=3))
    assert json.loads(schedule_path.read_text())["bookings"] == []
