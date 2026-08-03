"""④ Control — the brain, off-device.

Nothing here touches hardware, and that is not a testing compromise: Control's only
actuation path is `GatewayClient`, so replacing it with a fake replaces *all* of its
I/O. What is left under test is the real config tree, the real state reader, the real
agents and the real coordinator from the Phase 1 spike.

`state_factory` follows the conftest rule: build the shared object through a factory,
never inline. The predecessor grew 46 inline `CurrentState(...)` constructions and
one added field turned its whole suite red.
"""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from ecowhen_power_system.control import actuation
from ecowhen_power_system.control.agents.soc_wallbox_charge import SocWallboxChargeAgent
from ecowhen_power_system.control.agents.system_safety import SystemSafetyAgent
from ecowhen_power_system.control.config import ControlConfig, now
from ecowhen_power_system.control.coordinator import Coordinator
from ecowhen_power_system.control.state import (CurrentState, StateUnavailableError,
                                                read_current_state)
from ecowhen_power_system.control import gateway_client


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeGateway:
    """The Gateway, as far as Control can tell: names in, booleans out.

    `latency` is the number of ticks a write takes to show up on a read — the
    default, because hardware that switches instantly is the exception and a state
    machine that only works against it does not work.
    """

    def __init__(self, latency=0, unreachable=()):
        self.actual = {}
        self.commands = []
        self.latency = latency
        self.unreachable = set(unreachable)
        self._pending = {}

    def read(self, actuator):
        if actuator in self.unreachable:
            return None
        if actuator in self._pending:
            target, ticks = self._pending[actuator]
            ticks -= 1
            if ticks <= 0:
                self.actual[actuator] = target
                del self._pending[actuator]
            else:
                self._pending[actuator] = (target, ticks)
        return self.actual.get(actuator, False)

    def command(self, actuator, on):
        self.commands.append((actuator, on))
        if self.latency == 0:
            self.actual[actuator] = on
        else:
            self._pending[actuator] = (on, self.latency)
        return True


@pytest.fixture
def config():
    return ControlConfig()


@pytest.fixture
def state_factory():
    def make(**overrides):
        values = dict(
            timestamp=now(),
            age_seconds=1.0,
            soc=0.8,
            battery_voltage=26.4,
            battery_current=-5.0,
            battery_temp=22.0,
            solar_power_w=300.0,
            ac_load_w=210.0,
            mppt_150_power_w=200.0,
            mppt_100_power_w=100.0,
            minutes_at_full_soc=0.0,
        )
        values.update(overrides)
        return CurrentState(**values)
    return make


@pytest.fixture
def projection_factory(state_factory):
    def make(**overrides):
        return SimpleNamespace(current=state_factory(**overrides), forecast=None,
                               horizon_hours=96, steps=[])
    return make


def write_state(paths_mod, **overrides):
    """A state.json as the Power System publishes it, post-A12."""
    state = {
        "timestamp": now().isoformat(),
        "SOC_counted": 0.8,
        "SOC_Kf": 0.79,
        "battery/voltage": 26.4,
        "battery/current": -5.0,
        "battery/voltage_spread": 0.02,
        "system/battery_voltage": 26.23,      # raw, and no longer the same thing
        "system/battery_temperature": 22.0,
        "system/power_yield": 300.0,
        "multiplus/AC_power_output": 210.0,
        "mppt150/power_yield": 200.0,
        "mppt100/power_yield": 100.0,
        "minutes_at_full_soc": 0.0,
    }
    state.update(overrides)
    paths_mod.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    paths_mod.STATE_PATH.write_text(json.dumps(state))
    return state


# ---------------------------------------------------------------------------
# Config — bug #3 was two sets of defaults disagreeing
# ---------------------------------------------------------------------------

def test_from_dict_does_not_invent_its_own_defaults():
    """The predecessor's `from_dict` said 24 h / 200 W where its fields said 96 / 20.

    An empty dict must produce exactly the defaults, or a config file that omits a
    key means something different from one that was never written.
    """
    assert ControlConfig.from_dict({}) == ControlConfig()


def test_config_round_trips_through_yaml(runtime_dirs, tmp_path):
    path = tmp_path / "control_config.yaml"
    original = ControlConfig()
    original.agents.soc_wallbox_charge.soc_on_minutes = 45
    original.save(path)
    assert ControlConfig.load(path) == original


def test_the_battery_is_not_restated_in_control(config):
    """Control's battery defaults are the Power System's battery, not a copy."""
    from ecowhen_power_system import config_default

    assert config.battery.capacity_ah == config_default.battery.Q_tot
    assert config.battery.max_voltage == config_default.battery.max_safety_voltage
    assert config.battery.min_soc == config_default.battery.SOC_low


# ---------------------------------------------------------------------------
# State — A12's rename, and staleness as a safety event
# ---------------------------------------------------------------------------

def test_the_derived_clamp_values_are_what_control_reads(runtime_dirs):
    """A12: `battery/voltage` is the battery's; `system/battery_voltage` is the GX's."""
    write_state(runtime_dirs)
    state = read_current_state()
    assert state.battery_voltage == 26.4
    assert state.battery_current == -5.0


def test_a_frozen_state_is_refused(runtime_dirs):
    """The predecessor stamped today's date onto a bare "01:04:27", so a state frozen
    yesterday read as current and Control could not tell the Power System had died."""
    write_state(runtime_dirs,
                timestamp=(now() - timedelta(minutes=10)).isoformat())
    with pytest.raises(StateUnavailableError, match="old"):
        read_current_state(max_age_seconds=120)


def test_a_state_without_derived_values_is_refused(runtime_dirs):
    state = write_state(runtime_dirs)
    del state["battery/voltage"]
    runtime_dirs.STATE_PATH.write_text(json.dumps(state))
    with pytest.raises(StateUnavailableError, match="derived"):
        read_current_state()


def test_minutes_at_full_soc_comes_from_the_contract_not_the_logger(runtime_dirs):
    """Decision #7: the Logger is a pure sink. Nothing may glob `sim_*.csv`."""
    write_state(runtime_dirs, minutes_at_full_soc=42.0)
    assert read_current_state().minutes_at_full_soc == 42.0


# ---------------------------------------------------------------------------
# The safety agent — bug #2 lived here
# ---------------------------------------------------------------------------

def test_safety_stays_enabled_unless_both_flags_say_otherwise(config):
    """Bug #2: `is_enabled` was a bare `True` with no `return`, so it answered None,
    and the runner never called it. Both flags, or the agent runs."""
    agent = SystemSafetyAgent()
    assert agent.is_enabled(config) is True

    config.agents.system_safety.enabled = False
    assert agent.is_enabled(config) is True, "one flag is not enough"

    config.agents.system_safety.confirmed_disable = True
    assert agent.is_enabled(config) is False


def test_a_low_voltage_breach_cuts_the_drains_and_inhibits(config, projection_factory):
    result = SystemSafetyAgent().run(projection_factory(battery_voltage=23.0), config)

    assert dict((c.actuator, c.on) for c in result.commands) == {
        "multiplus_mode": False, "mppt100_load": False,
    }
    assert "below limit" in result.inhibit
    assert result.rationale.startswith("SAFETY ACTION")


def test_an_over_voltage_hard_cuts_mains_to_the_deye(config, projection_factory):
    result = SystemSafetyAgent().run(projection_factory(battery_voltage=30.0), config)
    assert [(c.actuator, c.on) for c in result.commands] == [("ac_inverter_relay", False)]


def test_a_quiet_system_neither_commands_nor_inhibits(config, projection_factory):
    result = SystemSafetyAgent().run(projection_factory(), config)
    assert result.commands == []
    assert result.inhibit == ""


def test_heat_asks_for_the_cooling_mode_rather_than_writing_the_fan(config,
                                                                   projection_factory):
    """The fan is shared with the wallbox, so cooling has to go through the same
    reference count — a direct write would fight whoever else is holding it."""
    result = SystemSafetyAgent().run(projection_factory(ac_load_w=1500.0), config)

    assert [(r.mode, r.active) for r in result.requests] == [("cooling", True)]
    assert result.commands == []


def test_heat_memory_is_per_instance(config, projection_factory):
    """It used to be a class attribute, shared by every agent ever constructed."""
    hot = SystemSafetyAgent()
    hot.run(projection_factory(ac_load_w=1500.0), config)
    assert SystemSafetyAgent().heat_memory["cooling_AC_load"] == 0


# ---------------------------------------------------------------------------
# The SOC agent — a request, not a sequence
# ---------------------------------------------------------------------------

def test_a_long_full_battery_requests_the_wallbox(config, projection_factory):
    projection = projection_factory(soc=1.0, minutes_at_full_soc=45.0)
    result = SocWallboxChargeAgent().run(projection, config)
    assert [(r.mode, r.active) for r in result.requests] == [("wallbox", True)]


def test_a_low_battery_releases_it(config, projection_factory):
    projection = projection_factory(soc=0.1, minutes_at_full_soc=0.0)
    result = SocWallboxChargeAgent().run(projection, config)
    assert [(r.mode, r.active) for r in result.requests] == [("wallbox", False)]


# ---------------------------------------------------------------------------
# The gateway client — unknown is not off
# ---------------------------------------------------------------------------

def test_the_client_speaks_names_and_booleans(monkeypatch):
    posted = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"value": 3, "on": True}

    monkeypatch.setattr(gateway_client.requests, "post",
                        lambda url, json=None, headers=None, timeout=None:
                        posted.update(url=url, body=json) or _Resp())
    monkeypatch.setattr(gateway_client.requests, "get",
                        lambda url, headers=None, timeout=None: _Resp())

    client = gateway_client.GatewayClient("http://gw:5101", api_key="k")
    assert client.command("multiplus_mode", True) is True
    assert posted["body"] == {"on": True}, "never an integer — decision #5"
    assert posted["url"].endswith("/actuators/multiplus_mode")
    assert client.read("multiplus_mode") is True


def test_an_unreachable_gateway_reads_unknown_not_off(monkeypatch):
    def boom(*a, **kw):
        raise OSError("no route to host")

    monkeypatch.setattr(gateway_client.requests, "get", boom)
    monkeypatch.setattr(gateway_client.requests, "post", boom)

    client = gateway_client.GatewayClient("http://gw:5101")
    assert client.read("multiplus_mode") is None
    assert client.command("multiplus_mode", True) is False


# ---------------------------------------------------------------------------
# The whole graph, driven through the coordinator
# ---------------------------------------------------------------------------

@pytest.fixture
def system(config):
    gateway = FakeGateway(latency=1)
    services, modes = actuation.build(gateway, config)
    coordinator = Coordinator(services, modes)
    return SimpleNamespace(gateway=gateway, services=services, modes=modes,
                           coordinator=coordinator, config=config)


def tick_until(system, predicate, limit=10):
    for _ in range(limit):
        system.coordinator.tick(system.config)
        if predicate():
            return True
    return False


def want(system, *modes):
    """State this cycle's wants. Wholesale, because silence is release (A7)."""
    from ecowhen_power_system.control.coordinator import ModeRequest

    system.coordinator.set_agent_requests([
        ModeRequest(mode=m, active=True, agent=agent) for m, agent in modes
    ])


def test_the_wallbox_waits_for_the_inverter_and_the_fan(system):
    """The plan's live round trip, off-device: activate → services converge → plug on.

    The ordering is never stated here. It falls out of `required_services`, which is
    the entire point of the refactor.
    """
    want(system, ("wallbox", "user_agent"))

    system.coordinator.tick(system.config)
    assert ("wallbox_relay", True) not in system.gateway.commands, \
        "the plug must not be driven before its prerequisites are satisfied"

    assert tick_until(system, lambda: system.modes["wallbox"].state == "active")
    assert system.gateway.actual["multiplus_mode"] is True
    assert system.gateway.actual["mppt100_load"] is True
    assert system.gateway.actual["wallbox_relay"] is True

    order = [a for a, on in system.gateway.commands if on]
    assert order.index("wallbox_relay") > order.index("multiplus_mode")


def test_the_plug_drops_before_the_services_do(system):
    want(system, ("wallbox", "user_agent"))
    assert tick_until(system, lambda: system.modes["wallbox"].state == "active")
    system.gateway.commands.clear()

    want(system)
    assert tick_until(system, lambda: system.gateway.actual["multiplus_mode"] is False)
    assert system.modes["wallbox"].state == "inactive"

    offs = [a for a, on in system.gateway.commands if not on]
    assert offs.index("wallbox_relay") < offs.index("multiplus_mode"), \
        "the inverter must not vanish from under a live plug"


def test_the_fan_stays_up_while_cooling_still_wants_it(system):
    """Reference counting is the reason cooling is a mode and not a write."""
    want(system, ("wallbox", "soc_wallbox_charge"), ("cooling", "system_safety"))
    assert tick_until(system, lambda: system.modes["wallbox"].state == "active")

    want(system, ("cooling", "system_safety"))
    assert tick_until(system, lambda: system.modes["wallbox"].state == "inactive")

    assert system.services["fan"].required_by == {"cooling"}
    assert system.gateway.actual["mppt100_load"] is True

    want(system)
    assert tick_until(system, lambda: system.gateway.actual["mppt100_load"] is False)


def test_an_unreachable_actuator_waits_instead_of_driving_blind(config):
    """`None` is unknown, and unknown is not a mismatch (A7)."""
    from ecowhen_power_system.control.coordinator import ModeRequest

    gateway = FakeGateway(unreachable=["multiplus_mode"])
    services, modes = actuation.build(gateway, config)
    coordinator = Coordinator(services, modes)
    coordinator.set_agent_requests([ModeRequest("wallbox", True, "user_agent")])

    for _ in range(6):
        coordinator.tick(config)

    assert ("multiplus_mode", True) not in gateway.commands
    assert services["ac_inverter"].phase == "converging"


# ---------------------------------------------------------------------------
# The process
# ---------------------------------------------------------------------------

@pytest.fixture
def unit(runtime_dirs, config):
    from ecowhen_power_system.control_main import ControlUnit

    unit = ControlUnit(config, gateway=FakeGateway())
    unit.forecast_provider = SimpleNamespace(get=lambda: None)
    return unit


def test_a_stale_state_holds_every_mode_off(unit, runtime_dirs):
    """Control treats a dead Power System as a safety event: no measurements, no
    permission to run loads."""
    write_state(runtime_dirs, timestamp=(now() - timedelta(hours=1)).isoformat())

    status = unit.cycle()

    assert status["inhibited"] is True
    assert "state unavailable" in status["inhibit_reason"]


def test_a_cycle_publishes_both_status_files(unit, runtime_dirs):
    write_state(runtime_dirs)
    unit.cycle()

    modes = json.loads(runtime_dirs.MODES_STATUS_PATH.read_text())
    services = json.loads(runtime_dirs.SERVICES_STATUS_PATH.read_text())
    assert set(modes["modes"]) == {"wallbox", "cooling", "ac_on"}
    assert set(services["services"]) == {"ac_inverter", "fan"}


def test_the_user_request_file_activates_a_mode(unit, runtime_dirs):
    write_state(runtime_dirs, SOC_counted=0.5)      # too low for the SOC agent to ask
    runtime_dirs.MODE_REQUESTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    runtime_dirs.MODE_REQUESTS_PATH.write_text(json.dumps({"modes": ["wallbox"]}))

    for _ in range(6):
        status = unit.cycle()

    assert status["modes"]["wallbox"]["state"] == "active"
    assert unit.gateway.actual["wallbox_relay"] is True


def _slow_agent(bookings_from, bookings_until):
    """A planning agent that publishes one booking and counts its own runs."""
    from ecowhen_power_system.control.agents.base import AgentResult
    from ecowhen_power_system.control.coordinator import Booking

    class Slow:
        name = "forecast_wallbox"
        fast_cycle = False
        runs = 0
        enabled = True

        def is_enabled(self, config):
            return self.enabled

        def run(self, projection, config):
            Slow.runs += 1
            return AgentResult(agent_name=self.name, rationale="window",
                               bookings=[Booking(mode="wallbox",
                                                 start=bookings_from,
                                                 end=bookings_until)])

    return Slow()


def test_a_planning_agents_booking_survives_the_ticks_between_planning_cycles(unit,
                                                                             runtime_dirs):
    """A plan stands until its own agent revises it.

    The coordinator recomputes wants every tick and treats silence as release, so an
    agent that speaks every 5 minutes would lose its mode on the next tick if the
    booking did not carry the interval itself.
    """
    write_state(runtime_dirs)
    t = now()
    agent = _slow_agent(t - timedelta(minutes=5), t + timedelta(hours=1))

    unit.agents = [agent]
    for _ in range(4):
        unit.cycle()

    assert agent.runs == 1, "the slow agent must not be re-run every cycle"
    assert unit.modes["wallbox"].requesters == {"forecast_wallbox"}


def test_a_booking_releases_the_mode_when_its_interval_ends(unit, runtime_dirs):
    """The boundary is honoured on the tick, not on the planning cycle."""
    write_state(runtime_dirs)
    t = now()
    unit.agents = [_slow_agent(t - timedelta(hours=2), t + timedelta(seconds=30))]

    unit.cycle()
    assert unit.modes["wallbox"].requesters == {"forecast_wallbox"}

    # One tick later the window has closed. The agent has not run again and does not
    # need to: the booking says when it ends.
    unit.cycle(t_now=t + timedelta(minutes=1))
    assert unit.modes["wallbox"].requesters == set()


def test_a_disabled_planning_agent_stops_driving_its_plan(unit, runtime_dirs):
    """A plan outlives its author, so being switched off has to drop it."""
    write_state(runtime_dirs)
    t = now()
    agent = _slow_agent(t - timedelta(minutes=5), t + timedelta(hours=4))
    unit.agents = [agent]

    unit.cycle()
    assert unit.modes["wallbox"].requesters == {"forecast_wallbox"}

    agent.enabled = False
    unit.cycle()
    assert unit.modes["wallbox"].requesters == set()


def test_an_agent_that_raises_stops_driving_its_plan(unit, runtime_dirs):
    """Same bound, other cause: it has told us nothing, so it speaks for nothing."""
    write_state(runtime_dirs)
    t = now()
    agent = _slow_agent(t - timedelta(minutes=5), t + timedelta(hours=4))
    unit.agents = [agent]

    unit.cycle()
    assert unit.modes["wallbox"].requesters == {"forecast_wallbox"}

    def boom(projection, config):
        raise RuntimeError("forecast unreachable")

    agent.run = boom
    unit.cycle(t_now=now() + timedelta(minutes=10))   # forces a planning cycle
    assert unit.modes["wallbox"].requesters == set()


def test_an_inhibit_suppresses_the_calendar_without_deleting_it(unit, runtime_dirs):
    """A safety trip must not silently wipe what the planning agents worked out."""
    write_state(runtime_dirs)
    t = now()
    unit.agents = [_slow_agent(t - timedelta(minutes=5), t + timedelta(hours=4))]

    unit.cycle()
    assert unit.modes["wallbox"].requesters == {"forecast_wallbox"}

    unit.coordinator.inhibit("test")
    unit.coordinator.tick(unit.config)
    assert unit.modes["wallbox"].requesters == set()
    assert unit.coordinator.schedule()["bookings"], "the plan itself must survive"

    unit.coordinator.clear_inhibit()
    unit.coordinator.tick(unit.config)
    assert unit.modes["wallbox"].requesters == {"forecast_wallbox"}


# ---------------------------------------------------------------------------
# The round trip the plan asks for, end to end and off-device
# ---------------------------------------------------------------------------
#
# Real Control -> real HTTP routing -> real Gateway -> real registry -> the fake
# bus and two stubbed device protocols. The only fakes are the wires.
#
# This is the one test that pins the actuator *names* Control speaks against the
# registry discovery actually resolves: `multiplus_mode`, `mppt100_load`,
# `wallbox_relay` (A16 renamed the last two after Phase 1 — nothing consumed them
# then, and this is what "nothing consumed them" stops being true).

class _Bridged:
    """A requests-shaped answer wrapping a Flask test-client response."""

    def __init__(self, response):
        self._response = response

    def raise_for_status(self):
        if self._response.status_code >= 400:
            raise OSError(f"HTTP {self._response.status_code}: "
                          f"{self._response.get_json()}")

    def json(self):
        return self._response.get_json()


@pytest.fixture
def wired(fake_bus, bus_values, runtime_dirs, monkeypatch):
    """Control talking to a live Gateway over its real routes."""
    from urllib.parse import urlsplit

    from ecowhen_power_system import config_default, discovery
    from ecowhen_power_system.control_main import ControlUnit
    from ecowhen_power_system.gateway import transports
    from ecowhen_power_system.gateway.app import create_app
    from conftest import MPPT100

    system_config, _ = discovery.discover(fake_bus, config_default)
    discovery.save_system_configuration(system_config, runtime_dirs.SYSTEM_CONFIG_PATH)

    monkeypatch.setenv("ECOPS_API_KEY", "k")
    app = create_app(bus=fake_bus)
    app.config["TESTING"] = True
    http = app.test_client()

    # vregd, standing in for the fan's VE.Direct register. Writing 4/0 shows up on
    # D-Bus as 4/1 — the A9 asymmetry, reproduced, because a service that assumed
    # one number for "off" would flap the fan until its retries ran out.
    class _Vregd:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(args, **kwargs):
        if "-s" in args:
            written = int(args[args.index("-s") + 2].split(":")[1])
            bus_values[MPPT100]["/Load/State"] = 4 if written else 1
        return _Vregd()

    monkeypatch.setattr(transports.subprocess, "run", fake_run)

    plugs = {}

    # One `get`, because `transports.requests` and `gateway_client.requests` are the
    # same module object: Control's calls carry no params and go to the Gateway, the
    # Gateway's carry a Tasmota `cmnd` and go to the plug.
    def fake_get(url, params=None, headers=None, timeout=None):
        if params is None:
            return _Bridged(http.get(urlsplit(url).path, headers=headers))
        cmnd = params["cmnd"].split()
        if len(cmnd) > 1:
            plugs[url] = cmnd[1] == "1"
        return _Bridged(SimpleNamespace(
            status_code=200,
            get_json=lambda: {"POWER": "ON" if plugs.get(url) else "OFF"}))

    def fake_post(url, json=None, headers=None, timeout=None):
        return _Bridged(http.post(urlsplit(url).path, json=json, headers=headers))

    monkeypatch.setattr(gateway_client.requests, "get", fake_get)
    monkeypatch.setattr(gateway_client.requests, "post", fake_post)

    unit = ControlUnit(ControlConfig())
    unit.forecast_provider = SimpleNamespace(get=lambda: None)
    return SimpleNamespace(unit=unit, http=http, bus=bus_values, plugs=plugs)


def test_activating_the_wallbox_reaches_the_hardware(wired, runtime_dirs):
    """POST /control/modes/wallbox/activate → services converge → the plug goes on."""
    from conftest import MPPT100, MULTIPLUS

    write_state(runtime_dirs, SOC_counted=0.5)      # the SOC agent stays out of it
    wired.bus[MULTIPLUS]["/Mode"] = 4               # inverter off to begin with

    assert wired.http.post("/control/modes/wallbox/activate",
                           headers={"X-API-Key": "k"}).status_code == 200

    for _ in range(6):
        status = wired.unit.cycle()

    assert status["modes"]["wallbox"]["state"] == "active"
    assert wired.bus[MULTIPLUS]["/Mode"] == 3, "derived from the component's mapping"
    assert wired.bus[MPPT100]["/Load/State"] == 4
    assert any(on for on in wired.plugs.values())

    services = json.loads(runtime_dirs.SERVICES_STATUS_PATH.read_text())["services"]
    assert services["ac_inverter"]["phase"] == "satisfied"
    assert services["fan"]["phase"] == "satisfied"


def test_a_user_booking_reaches_the_hardware_when_its_interval_opens(wired,
                                                                    runtime_dirs):
    """The calendar, end to end: POST a future interval, and nothing happens until
    the clock is inside it — at which point the same convergence runs."""
    from conftest import MULTIPLUS

    write_state(runtime_dirs, SOC_counted=0.5)
    wired.bus[MULTIPLUS]["/Mode"] = 4
    t = now()

    assert wired.http.post(
        "/control/modes/wallbox/activate", headers={"X-API-Key": "k"},
        json={"from": (t + timedelta(hours=2)).isoformat(),
              "until": (t + timedelta(hours=4)).isoformat()},
    ).status_code == 200

    for _ in range(6):
        status = wired.unit.cycle()
    assert status["modes"]["wallbox"]["state"] == "inactive"
    assert wired.bus[MULTIPLUS]["/Mode"] == 4, "a future booking must not act now"

    # Inside the window. The agents are unchanged; only the clock moved.
    for i in range(6):
        status = wired.unit.cycle(t_now=t + timedelta(hours=3, minutes=i))

    assert status["modes"]["wallbox"]["state"] == "active"
    assert wired.bus[MULTIPLUS]["/Mode"] == 3
    assert any(on for on in wired.plugs.values())

    schedule = json.loads(runtime_dirs.MODE_SCHEDULE_PATH.read_text())
    assert [b["source"] for b in schedule["bookings"]] == ["user"]


def test_deactivating_puts_it_all_back(wired, runtime_dirs):
    from conftest import MPPT100, MULTIPLUS

    write_state(runtime_dirs, SOC_counted=0.5)
    auth = {"X-API-Key": "k"}
    wired.http.post("/control/modes/wallbox/activate", headers=auth)
    for _ in range(6):
        wired.unit.cycle()

    wired.http.post("/control/modes/wallbox/deactivate", headers=auth)
    for _ in range(6):
        status = wired.unit.cycle()

    assert status["modes"]["wallbox"]["state"] == "inactive"
    assert not any(on for on in wired.plugs.values())
    assert wired.bus[MULTIPLUS]["/Mode"] == 4
    assert wired.bus[MPPT100]["/Load/State"] == 1, "off reads 1, not the 0 written"
