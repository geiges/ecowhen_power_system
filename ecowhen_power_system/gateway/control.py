"""Control's REST surface, served by the Gateway.

Deferred out of Phase 2 on purpose (A17): the Gateway is the producer of
`mode_requests.json` and `control_config.yaml` per the shared-files table, but their
schema is Control's, and Control did not exist yet. It does now.

Routes
------
GET  /control/modes                     modes_status.json, as Control last wrote it
GET  /control/services                  services_status.json
GET  /control/agents                    every agent, enabled flag, last result
GET  /control/schedule                  the calendar: every booking ahead of now
GET  /control/modes/requests            the user's standing intent and bookings
POST /control/modes/<name>/activate     want <name> now, or {"from","until"} to book it
POST /control/modes/<name>/deactivate   drop the standing want, and any bookings for it
POST /control/modes/<name>/reset        clear a mode stuck in `failed` so it retries
DELETE /control/modes/bookings/<id>     cancel one booking
GET  /control/config                    control_config.yaml
PUT  /control/config                    deep-merge a partial update into it
GET  /control/log?n=50&agent=name       the decision log, most recent last

Nothing here talks to Control. The two processes meet through files: this writes
intent, Control writes status, and neither blocks on the other being up. A mode
requested while Control is down is honoured when it starts — which is the whole
reason the request file is durable.
"""
import dataclasses
import json
import uuid
from datetime import datetime

import yaml
from flask import Blueprint, current_app, jsonify, request

from .. import paths
from ..control.config import TZ, ControlConfig, now
from ..control.decision_log import DecisionLog

control_bp = Blueprint("control", __name__)

# Agents that would otherwise take turns requesting the same mode. Enabling one
# disables the rest of its group in the same write.
_AGENT_GROUPS = [["soc_wallbox_charge", "forecast_wallbox", "wallbox_optimal_charge"]]


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f)
    tmp.replace(path)          # atomic: Control polls this file freely


def _known_modes():
    """The modes Control published, or None if it has not published any yet."""
    status = _read_json(paths.MODES_STATUS_PATH, None)
    if not status or not status.get("modes"):
        return None
    return set(status["modes"])


@control_bp.get("/modes")
def get_modes():
    status = _read_json(paths.MODES_STATUS_PATH, None)
    if status is None:
        return jsonify({"error": "no mode status yet — is Control running?"}), 503
    return jsonify(status)


@control_bp.get("/services")
def get_services():
    status = _read_json(paths.SERVICES_STATUS_PATH, None)
    if status is None:
        return jsonify({"error": "no service status yet — is Control running?"}), 503
    return jsonify(status)


@control_bp.get("/schedule")
def get_schedule():
    schedule = _read_json(paths.MODE_SCHEDULE_PATH, None)
    if schedule is None:
        return jsonify({"error": "no schedule yet — is Control running?"}), 503
    return jsonify(schedule)


# Two kinds of user intent, and the difference is whether it ends by itself:
#   modes     — wanted from now until the user says otherwise
#   bookings  — wanted over an interval, with an id so one can be cancelled
#   resets    — mode name -> timestamp, the one edge in this file (see /reset)
_EMPTY_REQUESTS = {"modes": [], "bookings": [], "resets": {}}


@control_bp.get("/modes/requests")
def get_mode_requests():
    return jsonify(_read_json(paths.MODE_REQUESTS_PATH, _EMPTY_REQUESTS))


def _parse_instant(value, field: str):
    """One ISO timestamp off the wire. Returns (datetime, error).

    Naive input is localised to the configured zone — the user meant their own wall
    clock, and an aware value is the only kind that can be compared against a
    booking's interval at all.
    """
    if not isinstance(value, str):
        return None, f"{field} must be an ISO 8601 string"
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None, f"{field} is not a valid ISO 8601 timestamp: {value!r}"
    return (t if t.tzinfo else TZ.localize(t)), None


def _load_requests() -> dict:
    payload = _read_json(paths.MODE_REQUESTS_PATH, {})
    return {
        "modes": list(payload.get("modes", [])),
        "bookings": list(payload.get("bookings", [])),
        # Carried through every edit: dropping it here would lose a reset that
        # Control had not read yet the moment anyone touched their calendar.
        "resets": dict(payload.get("resets", {})),
    }


def _save_requests(payload: dict, t_now):
    """Write the user's intent, dropping bookings that have already ended.

    Pruning happens here because this is the only process that writes this file —
    Control reads it and must not write back. A calendar nobody edits keeps its past
    rows until the next edit, which is untidy but never wrong: an ended booking
    cannot match `now`.
    """
    def outstanding(booking) -> bool:
        end, _ = _parse_instant(booking.get("end"), "end")
        return end is None or end > t_now      # unparseable rows are left for a human

    payload["bookings"] = [b for b in payload["bookings"] if outstanding(b)]
    payload["modes"] = sorted(set(payload["modes"]))
    payload["updated_at"] = t_now.isoformat()
    _write_json(paths.MODE_REQUESTS_PATH, payload)
    return jsonify(payload)


def _refuse_if_read_only():
    if current_app.config.get("READ_ONLY"):
        return jsonify({
            "error": "gateway is running read-only — mode changes are disabled",
        }), 403
    return None


def _refuse_if_unknown(name: str):
    known = _known_modes()
    if known is not None and name not in known:
        return jsonify({"error": f"unknown mode: {name}",
                        "known": sorted(known)}), 404
    return None


@control_bp.post("/modes/<name>/activate")
def activate_mode(name: str):
    """Want a mode now, or book it for an interval with `from` / `until`."""
    refusal = _refuse_if_read_only() or _refuse_if_unknown(name)
    if refusal:
        return refusal

    body = request.get_json(silent=True) or {}
    t_now = now()
    payload = _load_requests()

    if "from" in body or "until" in body:
        start, err = _parse_instant(body.get("from"), "from")
        if err:
            return jsonify({"error": err}), 400
        end, err = _parse_instant(body.get("until"), "until")
        if err:
            return jsonify({"error": err}), 400
        if end <= start:
            return jsonify({"error": "until must be after from"}), 400
        if end <= t_now:
            return jsonify({"error": "until is already in the past"}), 400
        payload["bookings"].append({
            "id": uuid.uuid4().hex[:8],
            "mode": name,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "reason": str(body.get("reason", "user booking"))[:200],
        })
    else:
        payload["modes"].append(name)

    return _save_requests(payload, t_now)


@control_bp.post("/modes/<name>/deactivate")
def deactivate_mode(name: str):
    """Drop the standing want *and* every booking for this mode.

    Someone switching a mode off means it should stop, not stop and then come back
    when a booking they forgot about starts. One booking is cancelled by its id.
    """
    refusal = _refuse_if_read_only() or _refuse_if_unknown(name)
    if refusal:
        return refusal

    payload = _load_requests()
    payload["modes"] = [m for m in payload["modes"] if m != name]
    payload["bookings"] = [b for b in payload["bookings"] if b.get("mode") != name]
    return _save_requests(payload, now())


@control_bp.post("/modes/<name>/reset")
def reset_mode(name: str):
    """Clear a mode stuck in `failed` so it converges again.

    Deliberately a separate verb from activate: `failed` is a dead end on purpose,
    and re-requesting the mode does not clear it — the mode never let go of its
    prerequisite services, so their retry budgets stay burnt. Only the release in
    `Mode.reset` hands those back.

    This leaves the standing want alone. Reset means "try again", not "and stop
    wanting it"; somebody who wants it off says `deactivate`.

    The value is a timestamp rather than a flag because the rest of this file is
    level-triggered state and a reset is an edge. Control fires once per new value
    and remembers what it acted on, so nothing here has to be cleared afterwards —
    which would mean two writers on a file that has exactly one.
    """
    refusal = _refuse_if_read_only() or _refuse_if_unknown(name)
    if refusal:
        return refusal

    t_now = now()
    payload = _load_requests()
    payload["resets"][name] = t_now.isoformat()
    return _save_requests(payload, t_now)


@control_bp.delete("/modes/bookings/<booking_id>")
def cancel_booking(booking_id: str):
    refusal = _refuse_if_read_only()
    if refusal:
        return refusal

    payload = _load_requests()
    remaining = [b for b in payload["bookings"] if b.get("id") != booking_id]
    if len(remaining) == len(payload["bookings"]):
        return jsonify({"error": f"unknown booking: {booking_id}"}), 404
    payload["bookings"] = remaining
    return _save_requests(payload, now())


@control_bp.get("/config")
def get_config():
    return jsonify(dataclasses.asdict(
        ControlConfig.load_or_default(paths.CONTROL_CONFIG_PATH)))


def _current_config_dict() -> dict:
    """The file as it stands, or the defaults if Control has not written one yet.

    The file rather than `asdict(load_or_default())` so a key this build does not
    know is carried through an edit instead of being dropped.
    """
    if paths.CONTROL_CONFIG_PATH.exists():
        with open(paths.CONTROL_CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    return dataclasses.asdict(ControlConfig())


@control_bp.get("/control_mode")
def get_control_mode():
    """`control_mode`, on its own. Named for the config key, not `/mode`, which
    sits one character from `/modes/<name>` and means something else entirely."""
    cfg = ControlConfig.load_or_default(paths.CONTROL_CONFIG_PATH)
    return jsonify({"control_mode": cfg.control_mode})


@control_bp.put("/control_mode")
def put_control_mode():
    """The dashboard's auto/manual switch.

    `PUT /control/config` can already set it; this is the one-key version so a
    button is one call that cannot accidentally carry the rest of the config with
    it. The value is validated by `ControlConfig` itself — one place, not two.
    """
    refusal = _refuse_if_read_only()
    if refusal:
        return refusal

    value = (request.get_json(silent=True) or {}).get("control_mode")
    if not isinstance(value, str):
        return jsonify({"error": "control_mode must be a string: 'auto' or 'manual'"}), 400

    current = _current_config_dict()
    current["control_mode"] = value
    try:
        updated = ControlConfig.from_dict(current)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    updated.save(paths.CONTROL_CONFIG_PATH)
    # Control picks it up on its next tick — `reload_config` runs every cycle.
    return jsonify({"control_mode": updated.control_mode})


@control_bp.put("/config")
def put_config():
    if current_app.config.get("READ_ONLY"):
        return jsonify({
            "error": "gateway is running read-only — config changes are disabled",
        }), 403

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "JSON object required"}), 400

    # Bug #2's other half: turning safety off takes two deliberate flags in one
    # request. The agent itself enforces the same rule, so neither is load-bearing
    # alone — which is the point, because the predecessor's protection existed only
    # in the docs.
    safety = body.get("agents", {}).get("system_safety", {})
    if safety.get("enabled") is False and not safety.get("confirmed_disable"):
        return jsonify({
            "error": "disabling system_safety requires confirmed_disable: true "
                     "in the same request",
        }), 400

    current = _current_config_dict()
    _deep_update(current, body)

    agents_update = body.get("agents", {})
    for group in _AGENT_GROUPS:
        for name in group:
            if agents_update.get(name, {}).get("enabled") is True:
                for other in group:
                    if other != name:
                        current.setdefault("agents", {}).setdefault(
                            other, {})["enabled"] = False
                break      # one winner per group per request

    try:
        updated = ControlConfig.from_dict(current)
    except Exception as exc:
        return jsonify({"error": f"invalid config: {exc}"}), 400

    updated.save(paths.CONTROL_CONFIG_PATH)
    return jsonify(dataclasses.asdict(updated))


def _agent_schema(agent_conf) -> list:
    """What a form needs and `asdict` throws away: type, default, unit, help.

    Read off the dataclass rather than written down again, so a knob added there
    shows up in the dashboard's settings panel without a second edit — the same
    reason the agent list itself is `ControlConfig.agents`' fields. `default` is
    the dataclass's, not the file's: it is what "reset this field" means, and the
    file's value is already in `config`.
    """
    return [
        {
            "name": f.name,
            # The *declared* type, because a null value carries none —
            # `interval_seconds` is exactly that case and a form cannot guess it.
            "type": getattr(f.type, "__name__", str(f.type)),
            "default": None if f.default is dataclasses.MISSING else f.default,
            "unit": f.metadata.get("unit", ""),
            "help": f.metadata.get("help", ""),
            # A field with no knob() defaults to *not* editable: everything
            # currently in that position is either a flag with its own control
            # (`enabled`, `confirmed_disable`) or a key nobody meant to publish.
            # Opting in is one word; a form offering the wrong knob is not.
            "editable": bool(f.metadata.get("editable", False)),
        }
        for f in dataclasses.fields(agent_conf)
    ]


@control_bp.get("/agents")
def get_agents():
    """Every agent, its enabled flag, the last thing it said, and its knobs.

    The agent list is `ControlConfig.agents`' own fields rather than a literal, so
    an agent added there appears here without a second edit. `config` is that
    agent's slice of the config file and `schema` describes each of its fields —
    together they are everything the dashboard's gear panel renders, and the edit
    goes back through `PUT /control/config`, which validates.
    """
    cfg = ControlConfig.load_or_default(paths.CONTROL_CONFIG_PATH)
    agents = dataclasses.asdict(cfg.agents)

    last = {}
    for entry in reversed(DecisionLog(paths.DECISION_LOG_PATH).tail(500)):
        name = entry.get("agent") if isinstance(entry, dict) else None
        if name and name not in last:
            last[name] = entry
        if len(last) >= len(agents):
            break

    return jsonify([
        {"name": name, "enabled": conf.get("enabled", True),
         "last_result": last.get(name),
         "config": conf,
         "schema": _agent_schema(getattr(cfg.agents, name))}
        for name, conf in agents.items()
    ])


@control_bp.get("/log")
def get_log():
    try:
        n = int(request.args.get("n", 50))
    except (ValueError, TypeError):
        return jsonify({"error": "n must be an integer"}), 400

    agent = request.args.get("agent")
    entries = DecisionLog(paths.DECISION_LOG_PATH).tail(n if not agent else n * 20)
    if agent:
        entries = [e for e in entries if e.get("agent") == agent][-n:]
    return jsonify(entries)


def _deep_update(base: dict, update: dict) -> None:
    """Recursively merge *update* into *base*, in place."""
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
