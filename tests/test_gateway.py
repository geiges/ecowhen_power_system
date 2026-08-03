"""The Gateway: the only process allowed to write hardware.

Everything here runs off-device. The D-Bus half goes through the same `FakeBus` the
Power System's tests use — it writes through, so a POST is observable as a changed
value on the bus. The other two transports shell out or make HTTP calls, so those
are stubbed at exactly that boundary and no further: the registry lookup, the on/off
resolution and the route logic are the real thing in every test below.
"""
import json
from datetime import datetime, timedelta

import pytest

from ecowhen_power_system import config_default, discovery
from ecowhen_power_system.gateway import transports
from ecowhen_power_system.gateway.app import create_app

from conftest import MPPT100, MULTIPLUS, FakeBus

API_KEY = "test-key-123"
AUTH = {"X-API-Key": API_KEY}


@pytest.fixture
def system_config_file(fake_bus, tmp_path):
    """A real topology contract, written by real discovery off the fake bus."""
    config, _ = discovery.discover(fake_bus, config_default)
    path = tmp_path / "system_configuration.yaml"
    discovery.save_system_configuration(config, path)
    return path


@pytest.fixture
def client(fake_bus, system_config_file, runtime_dirs, monkeypatch):
    monkeypatch.setenv("ECOPS_API_KEY", API_KEY)
    app = create_app(bus=fake_bus, system_config_path=system_config_file)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def vregd_calls(monkeypatch):
    """Capture vregd invocations instead of running them."""
    calls = []

    class _Result:
        returncode = 0
        stdout = "0x0004"
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append(args)
        return _Result()

    monkeypatch.setattr(transports.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def tasmota_calls(monkeypatch):
    """Capture Tasmota HTTP calls; answer as a plug that is currently off."""
    calls = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"POWER": "OFF"}

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        return _Resp()

    monkeypatch.setattr(transports.requests, "get", fake_get)
    return calls


# ---------------------------------------------------------------------------
# Auth — it fails closed, which is the whole point of harvesting it verbatim
# ---------------------------------------------------------------------------

def test_health_needs_no_key(client):
    assert client.get("/health").status_code == 200


def test_every_other_route_needs_the_key(client):
    assert client.get("/actuators/").status_code == 401
    assert client.post("/actuators/multiplus_mode", json={"on": True}).status_code == 401


def test_an_unset_server_key_refuses_rather_than_opens(client, monkeypatch):
    monkeypatch.delenv("ECOPS_API_KEY")
    assert client.get("/actuators/", headers=AUTH).status_code == 503


# ---------------------------------------------------------------------------
# The registry is consumed, never re-derived
# ---------------------------------------------------------------------------

def test_read_only_refuses_actuation_but_still_reads(fake_bus, system_config_file,
                                                     bus_values, monkeypatch):
    """Running beside the live old system, reads are safe and writes are an incident."""
    monkeypatch.setenv("ECOPS_API_KEY", API_KEY)
    app = create_app(bus=fake_bus, system_config_path=system_config_file,
                     read_only=True)
    client = app.test_client()

    resp = client.post("/actuators/multiplus_mode", json={"on": False}, headers=AUTH)

    assert resp.status_code == 403
    assert bus_values[MULTIPLUS]["/Mode"] == 3          # untouched
    assert client.get("/actuators/multiplus_mode", headers=AUTH).status_code == 200


def test_actuators_come_from_the_topology_contract(client):
    body = client.get("/actuators/", headers=AUTH).get_json()
    assert set(body["actuators"]) == {
        "multiplus_mode", "mppt100_load", "wallbox_relay", "ac_inverter_relay",
    }


def test_unknown_actuator_is_a_404_that_says_what_is_known(client):
    resp = client.get("/actuators/nonesuch", headers=AUTH)
    assert resp.status_code == 404
    assert "multiplus_mode" in resp.get_json()["known"]


def test_missing_hardware_is_503_not_404(bus_values, tmp_path, monkeypatch):
    """"The Multiplus is gone" and "no such actuator" are different 3am problems."""
    values = dict(bus_values)
    del values[MULTIPLUS]
    config, _ = discovery.discover(FakeBus(values), config_default)
    path = tmp_path / "system_configuration.yaml"
    discovery.save_system_configuration(config, path)

    monkeypatch.setenv("ECOPS_API_KEY", API_KEY)
    app = create_app(bus=FakeBus(values), system_config_path=path)
    resp = app.test_client().post("/actuators/multiplus_mode",
                                  json={"on": True}, headers=AUTH)

    assert resp.status_code == 503
    assert "not present" in resp.get_json()["error"]


def test_a_gateway_started_before_the_power_system_recovers(fake_bus, tmp_path,
                                                            monkeypatch):
    """The registry is re-read when it changes, so start order is not load-bearing."""
    path = tmp_path / "system_configuration.yaml"
    monkeypatch.setenv("ECOPS_API_KEY", API_KEY)
    client = create_app(bus=fake_bus, system_config_path=path).test_client()

    early = client.get("/actuators/multiplus_mode", headers=AUTH)
    assert early.status_code == 503
    assert "Power System" in early.get_json()["error"]

    config, _ = discovery.discover(fake_bus, config_default)
    discovery.save_system_configuration(config, path)

    assert client.get("/actuators/multiplus_mode", headers=AUTH).status_code == 200


# ---------------------------------------------------------------------------
# Set semantics: a boolean in, the component's own integer out
# ---------------------------------------------------------------------------

def test_commanding_off_writes_the_components_own_value(client, bus_values):
    """Control says on/off. That "off" is 4 came from the Multiplus's mapping."""
    assert bus_values[MULTIPLUS]["/Mode"] == 3

    resp = client.post("/actuators/multiplus_mode", json={"on": False}, headers=AUTH)

    assert resp.status_code == 200
    assert resp.get_json() == {
        "actuator": "multiplus_mode", "on": False, "value_written": 4,
    }
    assert bus_values[MULTIPLUS]["/Mode"] == 4


def test_commanding_is_idempotent(client, bus_values):
    """Set, not toggle. The predecessor's dbus_toggle would have flipped it back."""
    for _ in range(3):
        client.post("/actuators/multiplus_mode", json={"on": True}, headers=AUTH)
    assert bus_values[MULTIPLUS]["/Mode"] == 3


def test_the_body_must_be_a_boolean(client):
    """No integer from a client ever reaches the hardware — that is the registry's job."""
    for bad in ({"on": 3}, {"on": "true"}, {}, {"value": 4}):
        resp = client.post("/actuators/multiplus_mode", json=bad, headers=AUTH)
        assert resp.status_code == 400, bad


def test_reading_maps_the_raw_value_back_to_a_boolean(client, bus_values):
    bus_values[MULTIPLUS]["/Mode"] = 4
    body = client.get("/actuators/multiplus_mode", headers=AUTH).get_json()
    assert body == {"actuator": "multiplus_mode", "value": 4, "on": False}


def test_an_unrecognised_reading_is_unknown_not_off(client, bus_values):
    """/Load/State reports 5 for "USER". A service must wait, not assume off."""
    bus_values[MPPT100]["/Load/State"] = 5
    body = client.get("/actuators/mppt100_load", headers=AUTH).get_json()
    assert body["value"] == 5
    assert body["on"] is None


def test_an_unreadable_device_is_an_error_not_a_guess(client, bus_values):
    bus_values[MULTIPLUS]["/Mode"] = []      # Victron's "invalid"
    resp = client.get("/actuators/multiplus_mode", headers=AUTH)
    assert resp.status_code == 502
    assert "unreadable" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# A9: the fan writes VE.Direct and reads D-Bus, in different value spaces
# ---------------------------------------------------------------------------

def test_the_fan_writes_vedirect_with_the_registers_own_off_value(client, vregd_calls):
    resp = client.post("/actuators/mppt100_load", json={"on": False}, headers=AUTH)

    assert resp.status_code == 200
    assert resp.get_json()["value_written"] == 0
    assert vregd_calls == [
        ["vregd", "-c", "/dev/ttyUSB1", "-s", "0xEDAB", "un8:0"]
    ]


def test_the_fan_reads_dbus_where_off_is_a_different_number(client, bus_values,
                                                            vregd_calls):
    """Writing off is register value 0; reading off comes back as /Load/State == 1.

    If these ever collapse to one number a reconcile loop writes 0, reads 1, calls it
    a mismatch, and flaps the fan until it exhausts its retries.
    """
    assert bus_values[MPPT100]["/Load/State"] == 1

    body = client.get("/actuators/mppt100_load", headers=AUTH).get_json()

    assert body == {"actuator": "mppt100_load", "value": 1, "on": False}
    assert vregd_calls == []          # the read never touched VE.Direct


def test_a_failing_vregd_is_reported_not_swallowed(client, monkeypatch):
    class _Result:
        returncode = 1
        stdout = ""
        stderr = "no such port"

    monkeypatch.setattr(transports.subprocess, "run", lambda *a, **k: _Result())

    resp = client.post("/actuators/mppt100_load", json={"on": True}, headers=AUTH)
    assert resp.status_code == 502
    assert "no such port" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# Tasmota: one URL declaration, both directions (this is where bug #1 died)
# ---------------------------------------------------------------------------

def test_tasmota_writes_to_the_url_the_aux_component_declared(client, tasmota_calls):
    aux = {c.short_name: c for c in config_default.aux_components}

    client.post("/actuators/ac_inverter_relay", json={"on": True}, headers=AUTH)

    url, params = tasmota_calls[0]
    assert url == f"{aux['ac_inverter'].url}/cm"
    assert params == {"cmnd": "Power 1"}


def test_tasmota_falls_back_to_the_static_ip(client, monkeypatch):
    """mDNS names do not resolve on every host; the fallback is why both are declared."""
    import requests as _requests
    seen = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"POWER": "ON"}

    def fake_get(url, params=None, timeout=None):
        seen.append(url)
        if "tasmota-" in url:
            raise _requests.exceptions.ConnectionError("no mDNS here")
        return _Resp()

    monkeypatch.setattr(transports.requests, "get", fake_get)

    resp = client.get("/actuators/wallbox_relay", headers=AUTH)

    assert resp.get_json()["on"] is True
    assert len(seen) == 2 and "192.168.1.185" in seen[1]


def test_an_unreachable_plug_is_an_error_not_a_silent_no_op(client, monkeypatch):
    """The predecessor's AC hard-cut failed silently. It must never do that again."""
    import requests as _requests

    def fake_get(url, params=None, timeout=None):
        raise _requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(transports.requests, "get", fake_get)

    resp = client.post("/actuators/ac_inverter_relay", json={"on": False}, headers=AUTH)
    assert resp.status_code == 502
    assert "unreachable" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def test_serves_a_runtime_file(client, runtime_dirs):
    (runtime_dirs.RUNTIME_DIR / "state.json").write_text(json.dumps({"SOC_Kf": 0.5}))

    body = client.get("/files/runtime/state.json", headers=AUTH).get_json()
    assert body == {"SOC_Kf": 0.5}


def test_traversal_and_allow_list_both_hold(client, runtime_dirs):
    (runtime_dirs.RUNTIME_DIR / "secret.txt").write_text("nope")

    assert client.get("/files/runtime/../../etc/passwd", headers=AUTH).status_code in (400, 404)
    assert client.get("/files/runtime/secret.txt", headers=AUTH).status_code == 403
    assert client.get("/files/nosuch/x.json", headers=AUTH).status_code == 404


# ---------------------------------------------------------------------------
# Control's REST surface — deferred out of Phase 2 (A17), built in Phase 4
# ---------------------------------------------------------------------------

def test_activating_a_mode_writes_the_durable_request_file(client, runtime_dirs):
    """The Gateway is the producer of `mode_requests.json`; Control only reads it.

    Durable on purpose: a mode requested while Control is down is honoured when it
    starts, and someone who switched the wallbox on does not find it off because a
    process bounced.
    """
    body = client.post("/control/modes/wallbox/activate", headers=AUTH).get_json()
    assert body["modes"] == ["wallbox"]

    on_disk = json.loads(runtime_dirs.MODE_REQUESTS_PATH.read_text())
    assert on_disk["modes"] == ["wallbox"]

    client.post("/control/modes/wallbox/deactivate", headers=AUTH)
    assert json.loads(runtime_dirs.MODE_REQUESTS_PATH.read_text())["modes"] == []


def test_an_unknown_mode_is_refused_once_control_has_published(client, runtime_dirs):
    runtime_dirs.MODES_STATUS_PATH.write_text(json.dumps({"modes": {"wallbox": {}}}))

    assert client.post("/control/modes/wallbox/activate", headers=AUTH).status_code == 200
    resp = client.post("/control/modes/nosuch/activate", headers=AUTH)
    assert resp.status_code == 404
    assert resp.get_json()["known"] == ["wallbox"]


def test_mode_status_says_who_is_missing(client, runtime_dirs):
    """503 naming Control, never 404 — the same distinction the actuator routes make
    between "no such thing" and "the thing that publishes it is not running"."""
    resp = client.get("/control/modes", headers=AUTH)
    assert resp.status_code == 503
    assert "Control" in resp.get_json()["error"]

    runtime_dirs.MODES_STATUS_PATH.write_text(
        json.dumps({"modes": {"wallbox": {"state": "active"}}}))
    assert client.get("/control/modes", headers=AUTH).get_json()["modes"]["wallbox"][
        "state"] == "active"


def test_a_user_can_book_a_mode_for_an_interval(client):
    from ecowhen_power_system.control.config import now
    start = now() + timedelta(hours=1)
    end = start + timedelta(hours=2)

    body = client.post("/control/modes/wallbox/activate", headers=AUTH,
                       json={"from": start.isoformat(), "until": end.isoformat(),
                             "reason": "leaving at 18:00"}).get_json()

    booking, = body["bookings"]
    assert booking["mode"] == "wallbox"
    assert booking["reason"] == "leaving at 18:00"
    assert booking["id"]
    # A booking is not a standing want: it must not also switch the mode on now.
    assert body["modes"] == []


def test_a_booking_without_an_interval_is_still_a_standing_want(client):
    body = client.post("/control/modes/wallbox/activate", headers=AUTH).get_json()
    assert body["modes"] == ["wallbox"]
    assert body["bookings"] == []


@pytest.mark.parametrize("payload, expected", [
    ({"from": "not-a-date", "until": "2030-01-01T12:00"}, "ISO 8601"),
    ({"from": "2030-01-01T12:00", "until": "2030-01-01T10:00"}, "after"),
    ({"from": "2020-01-01T10:00", "until": "2020-01-01T12:00"}, "past"),
    ({"from": 12345, "until": "2030-01-01T12:00"}, "ISO 8601 string"),
])
def test_a_bad_interval_is_refused_at_the_boundary(client, payload, expected):
    resp = client.post("/control/modes/wallbox/activate", headers=AUTH, json=payload)
    assert resp.status_code == 400
    assert expected in resp.get_json()["error"]


def test_a_naive_timestamp_is_taken_as_local_wall_clock(client):
    """The user means their own clock. An aware value is the only comparable kind."""
    from ecowhen_power_system.control.config import TZ, now
    start = (now() + timedelta(hours=1)).replace(tzinfo=None)
    end = start + timedelta(hours=1)

    booking, = client.post("/control/modes/wallbox/activate", headers=AUTH,
                           json={"from": start.isoformat(),
                                 "until": end.isoformat()}).get_json()["bookings"]
    assert datetime.fromisoformat(booking["start"]) == TZ.localize(start)


def test_one_booking_can_be_cancelled_without_touching_the_others(client):
    from ecowhen_power_system.control.config import now
    t = now()
    ids = []
    for hours in (1, 5):
        body = client.post("/control/modes/wallbox/activate", headers=AUTH,
                           json={"from": (t + timedelta(hours=hours)).isoformat(),
                                 "until": (t + timedelta(hours=hours + 1)).isoformat()})
        ids.append(body.get_json()["bookings"][-1]["id"])

    body = client.delete(f"/control/modes/bookings/{ids[0]}", headers=AUTH).get_json()
    assert [b["id"] for b in body["bookings"]] == [ids[1]]

    assert client.delete(f"/control/modes/bookings/{ids[0]}",
                         headers=AUTH).status_code == 404


def test_deactivating_a_mode_also_drops_its_bookings(client):
    """Switching a mode off must not leave it to come back from a forgotten booking."""
    from ecowhen_power_system.control.config import now
    t = now()
    client.post("/control/modes/wallbox/activate", headers=AUTH)
    client.post("/control/modes/wallbox/activate", headers=AUTH,
                json={"from": (t + timedelta(hours=3)).isoformat(),
                      "until": (t + timedelta(hours=4)).isoformat()})

    body = client.post("/control/modes/wallbox/deactivate", headers=AUTH).get_json()
    assert body["modes"] == []
    assert body["bookings"] == []


def test_ended_bookings_are_pruned_on_the_next_edit(client, runtime_dirs):
    from ecowhen_power_system.control.config import now
    t = now()
    runtime_dirs.MODE_REQUESTS_PATH.write_text(json.dumps({
        "modes": [],
        "bookings": [{"id": "old", "mode": "wallbox",
                      "start": (t - timedelta(hours=3)).isoformat(),
                      "end": (t - timedelta(hours=1)).isoformat()}],
    }))

    body = client.post("/control/modes/wallbox/activate", headers=AUTH).get_json()
    assert body["bookings"] == []


def test_the_daily_aggregate_is_listed_not_just_servable(client, runtime_dirs):
    """A client that syncs from the listing never learns about a file the listing
    omits, so serving it on request is not enough."""
    aggregations = runtime_dirs.DATA_DIR / "aggregations"
    aggregations.mkdir(parents=True, exist_ok=True)
    (aggregations / "daily.csv").write_text("date,solar_total\n26-08-03,1.5\n")
    listed = client.get("/files/data/", headers=AUTH).get_json()["files"]
    assert "aggregations/daily.csv" in [f["name"] for f in listed]

    resp = client.get("/files/data/aggregations/daily.csv", headers=AUTH)
    assert resp.status_code == 200


def test_the_agent_list_comes_from_the_config_not_a_literal(client, runtime_dirs):
    """The dashboard's per-agent cards read this. Adding an agent to ControlConfig
    must be the only edit needed to make it appear."""
    runtime_dirs.DECISION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    runtime_dirs.DECISION_LOG_PATH.write_text(
        json.dumps({"agent": "system_safety", "rationale": "OK"}) + "\n")

    agents = client.get("/control/agents", headers=AUTH).get_json()
    by_name = {a["name"]: a for a in agents}

    assert set(by_name) == {"system_safety", "soc_wallbox_charge",
                            "forecast_wallbox", "wallbox_optimal_charge"}
    assert by_name["system_safety"]["last_result"]["rationale"] == "OK"
    assert by_name["soc_wallbox_charge"]["last_result"] is None
    assert by_name["system_safety"]["enabled"] is True


def test_the_schedule_is_served_and_says_who_is_missing(client, runtime_dirs):
    """The calendar a UI renders. Same 503-not-404 rule as every other status file."""
    resp = client.get("/control/schedule", headers=AUTH)
    assert resp.status_code == 503
    assert "Control" in resp.get_json()["error"]

    runtime_dirs.MODE_SCHEDULE_PATH.write_text(json.dumps({
        "now": "2026-08-03T12:00:00+00:00",
        "inhibited": False,
        "bookings": [{"mode": "wallbox", "start": "2026-08-03T13:00:00+00:00",
                      "end": "2026-08-03T15:00:00+00:00", "reason": "solar",
                      "source": "forecast_wallbox",
                      "generated_at": "2026-08-03T11:55:00+00:00"}],
    }))
    booking, = client.get("/control/schedule", headers=AUTH).get_json()["bookings"]
    assert booking["mode"] == "wallbox"
    assert booking["source"] == "forecast_wallbox"


def test_disabling_safety_takes_two_deliberate_flags(client):
    """Bug #2's other half. The predecessor documented this protection; the code
    returned None from `is_enabled` and the runner never called it."""
    resp = client.put("/control/config", headers=AUTH,
                      json={"agents": {"system_safety": {"enabled": False}}})
    assert resp.status_code == 400

    resp = client.put("/control/config", headers=AUTH,
                      json={"agents": {"system_safety": {"enabled": False,
                                                         "confirmed_disable": True}}})
    assert resp.status_code == 200
    assert resp.get_json()["agents"]["system_safety"]["enabled"] is False


def test_enabling_one_wallbox_agent_disables_the_others(client):
    body = client.put("/control/config", headers=AUTH,
                      json={"agents": {"forecast_wallbox": {"enabled": True}}}).get_json()
    agents = body["agents"]
    assert agents["forecast_wallbox"]["enabled"] is True
    assert agents["soc_wallbox_charge"]["enabled"] is False
    assert agents["wallbox_optimal_charge"]["enabled"] is False


def test_a_config_edit_survives_the_round_trip(client, runtime_dirs):
    client.put("/control/config", headers=AUTH, json={"estimated_load_w": 35.0})
    assert client.get("/control/config", headers=AUTH).get_json()["estimated_load_w"] == 35.0


def test_read_only_refuses_mode_and_config_changes(fake_bus, system_config_file,
                                                   runtime_dirs, monkeypatch):
    """Same interlock as the actuators (A19): while the predecessor is still live,
    nothing this process serves may cause a write."""
    monkeypatch.setenv("ECOPS_API_KEY", API_KEY)
    app = create_app(bus=fake_bus, system_config_path=system_config_file, read_only=True)
    app.config["TESTING"] = True
    client = app.test_client()

    assert client.post("/control/modes/wallbox/activate", headers=AUTH).status_code == 403
    assert client.delete("/control/modes/bookings/anything",
                         headers=AUTH).status_code == 403
    assert client.put("/control/config", headers=AUTH, json={}).status_code == 403
    assert client.get("/control/modes/requests", headers=AUTH).status_code == 200
