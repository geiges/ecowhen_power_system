"""Actuator blueprint — the whole write surface of the system.

Routes
------
GET  /actuators/              every actuator, with its description and availability
GET  /actuators/<name>        the current reading: {"value": N, "on": true|false|null}
POST /actuators/<name>        {"on": true} — drive it, and report what was written

Set semantics, not toggle
-------------------------
The body carries a boolean, never a number. Control says `on: true` and never learns
that "on" is the integer 3 — that mapping was resolved by discovery from the
component's own declaration and lives in the registry. The predecessor's read-modify-
write "advance to the next value in the cycle" is not ported: it re-derived a target
the caller already knew, from a read that could be stale, on an actuator the safety
agent also writes.

A POST does not read back
-------------------------
It reports the value it wrote and stops. Confirming in the same breath as the write
would trust a value the device may not have applied yet — the coordinator spike (A7)
settled that convergence takes two ticks on purpose: drive, then confirm on the next
look via GET.

`on: null` on a read means the device answered with something that is neither the on
nor the off value — /Load/State can report 5 ("USER") or 1 ("auto"). It is not "off".
A caller must treat it as unknown and wait rather than drive blind.

A read is remembered
--------------------
This process is the only writer, so what it last read stays true until it writes
again — and the reading it caches is a *reading*, never the value a POST wrote,
because a write that the device silently ignored must still be caught by the next
look. Two things refresh an entry: a POST to that actuator drops it, and a caller
that says `?max_age=<seconds>` asks for anything older than that to be re-read.

Nothing expires on its own (`CACHE_MAX_AGE = None`). A periodic re-read is three
seconds of blocking per unreachable Tasmota, times two URLs, on a ten second tick —
which is what this is here to stop. The cost is that a device changed behind the
Gateway's back (the plug's physical button, a power cut) stays wrong until something
writes it or asks for `max_age=0`.

A disabled actuator is simulated
--------------------------------
`DISABLED` names actuators whose hardware is out of service (`mppt100_load`: the
MPPT's load output is broken). They stay in the registry and keep answering — a POST
records the target, a GET replays it — so callers above the Gateway need no edit and
no special case, and nothing sits retrying a device that cannot answer. Both bodies
carry `"disabled": true`, and so does the listing.

Unless the read declares a `confirm`
-----------------------------------
Then the state is only a veto and a measurement decides — the fan's /Load/State
cannot distinguish "auto" from running, so the current on /Load/I does it. Such a
read is total: `on` is true or false, null only when the hardware is unreadable. That
matters because a service treats null as "wait", with no retry budget and no timeout.
"""
import time

from flask import Blueprint, current_app, jsonify, request

from . import transports

actuators_bp = Blueprint("actuators", __name__)

CACHE_MAX_AGE = None        # seconds an entry may be reused; None = it never expires

# ponytail: the MPPT's load output is broken, so this actuator is simulated — a POST
# records the target and a GET answers it, without ever touching the bus. Everything
# above (the safety agent, the `cooling` mode, `mppt100_dc_load`) keeps working
# against an actuator that converges instead of failing. Empty this set when the load
# switch is repaired; the transports below are untouched and still covered by tests.
DISABLED = {"mppt100_load"}
_simulated = {}             # disabled actuator -> the last target it was commanded to


def _registry():
    return current_app.config["ACTUATOR_REGISTRY"].current()


def _state():
    """What each actuator last read. Cleared when the topology changes."""
    return current_app.config["ACTUATOR_REGISTRY"].state


def _lookup(name):
    """Find one actuator, or the response explaining why it cannot be reached."""
    registry = _registry()
    if not registry:
        return None, (jsonify({
            "error": "no topology published yet — is the Power System running?"
        }), 503)

    actuator = registry.get(name)
    if actuator is None:
        return None, (jsonify({
            "error": f"unknown actuator: {name}",
            "known": sorted(registry),
        }), 404)

    if not actuator.get("available"):
        # Deliberately not a 404: "the Multiplus is missing" and "no such actuator"
        # are very different faults to debug at 3am.
        return None, (jsonify({
            "error": f"actuator {name} is configured but its hardware is not present",
        }), 503)

    return actuator, None


def _as_bool(value, on, off):
    """Map a raw reading onto on/off, or None when it is neither."""
    if value == on:
        return True
    if value == off:
        return False
    return None


@actuators_bp.get("/")
def list_actuators():
    return jsonify({
        "actuators": {
            name: {
                "description": a.get("description", ""),
                "available": a.get("available", False),
                "transport": a.get("write", {}).get("transport"),
                "disabled": name in DISABLED,
            }
            for name, a in _registry().items()
        }
    })


def _read_hardware(bus, name: str, ep: dict) -> tuple:
    """Go to the device. Returns the response body and its HTTP status."""
    value, error = transports.read(bus, ep)
    if error:
        return {"error": error}, 502

    body = {"actuator": name, "value": value, "on": _as_bool(value, ep["on"], ep["off"])}

    confirm = ep.get("confirm")
    if confirm:
        measured, error = transports.dbus_read_measurement(bus, ep["service"],
                                                           confirm["path"])
        if error:
            return {"error": error}, 502
        # Both halves must hold: the output switched on, and current actually
        # flowing. A state that claims on with nothing flowing reports *off*, so
        # the service retries and then fails loudly — where reporting null would
        # leave it waiting forever, since unknown burns no retry budget.
        body["on"] = body["on"] is True and measured >= confirm["min_value"]
        body["measured"] = measured        # the number behind the verdict, for 3am

    return body, 200


@actuators_bp.get("/<name>")
def read_actuator(name: str):
    actuator, err = _lookup(name)
    if err:
        return err

    if name in DISABLED:
        # No cache and no bus: the answer is whatever was last commanded, and off
        # until something commands it. Not null — a service waits on null forever.
        on = _simulated.get(name, False)
        ep = actuator["read"]
        return jsonify({"actuator": name, "value": ep["on"] if on else ep["off"],
                        "on": on, "disabled": True}), 200

    max_age = request.args.get("max_age", CACHE_MAX_AGE, type=float)
    remembered = _state().get(name)
    if remembered is not None:
        body, status, at = remembered
        age = time.monotonic() - at
        if max_age is None or age < max_age:
            return jsonify({**body, "cached": True, "age_seconds": round(age, 1)}), status

    # A failed read is remembered like any other answer. It has to be: a plug that is
    # unreachable would otherwise be retried on every tick, at three seconds a URL,
    # which is the whole problem this cache exists for. `?max_age=0` and a POST are
    # the ways back to the hardware.
    body, status = _read_hardware(current_app.config["BUS"], name, actuator["read"])
    _state()[name] = (body, status, time.monotonic())
    return jsonify(body), status


@actuators_bp.post("/<name>")
def command_actuator(name: str):
    if current_app.config.get("READ_ONLY"):
        # Set while validating against a device whose old system is still live.
        # Two writers on one actuator is the one genuinely dangerous failure mode
        # in this rebuild, so the guard is structural rather than a promise to
        # remember not to POST.
        return jsonify({
            "error": "gateway is running read-only — actuation is disabled",
        }), 403

    body = request.get_json(silent=True) or {}
    target = body.get("on")
    if not isinstance(target, bool):
        return jsonify({"error": "body must be {\"on\": true|false}"}), 400

    actuator, err = _lookup(name)
    if err:
        return err

    ep = actuator["write"]
    if name in DISABLED:
        _simulated[name] = target
        current_app.logger.info("actuator %s is disabled — recorded %s, wrote nothing",
                                name, target)
        return jsonify({"actuator": name, "on": target, "value_written": None,
                        "disabled": True})

    value = ep["on"] if target else ep["off"]
    written, error = transports.write(current_app.config["BUS"], ep, value)
    # Unconditionally, and before the error check: a write that reported a failure
    # may still have reached the relay. Whatever we remembered is now history, so the
    # next read goes to the device — which is also what makes convergence verify.
    _state().pop(name, None)
    if error:
        return jsonify({"error": error}), 502

    current_app.logger.info("commanded %s -> %s (wrote %s)", name, target, written)
    return jsonify({"actuator": name, "on": target, "value_written": written})
