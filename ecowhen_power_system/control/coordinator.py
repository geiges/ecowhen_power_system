"""The coordinator: one tick over every mode and service.

Replaces the predecessor's `SequenceRunner` and its `_sleep_with_sequence_ticks`
special case. There is no separate dispatch path any more — modes and services are
reconciled on the same clock as everything else.

Order within a tick matters:
  1. resolve who wants what (agents recomputed each cycle; the user persists),
  2. reconcile services — they are what modes wait on,
  3. reconcile modes — they read the services' phase from step 2.

Doing services first means a mode sees this tick's convergence rather than last
tick's, so activation costs one tick, not two.

There are two ways to want a mode, and they differ only in whether they carry time:

    ModeRequest   "I want this now." Recomputed every cycle, so silence is release.
                  Used by the fast agents, which run on every tick anyway.
    Booking       "I want this from 11:00 to 15:00." A planning agent publishes its
                  whole plan at once; the coordinator asks each tick which bookings
                  cover *now*.

Bookings detach an agent's planning cadence from execution: a plan stands until the
agent that owns it revises it, so an agent may run hourly (or daily) while the
boundary it planned is still honoured to within one tick. Nothing latches and nothing
fires — resolution is level-triggered either way.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ModeRequest:
    """An agent's desire for a mode this cycle."""
    mode: str
    active: bool
    agent: str
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "mode": self.mode, "active": self.active,
            "agent": self.agent, "reason": self.reason,
        }


@dataclass
class Booking:
    """A mode wanted over an interval. `end` is exclusive."""
    mode: str
    start: datetime
    end: datetime
    reason: str = ""

    def covers(self, t: datetime) -> bool:
        return self.start <= t < self.end

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "reason": self.reason,
        }


@dataclass
class Plan:
    """One agent's entire opinion about the future, and when it formed it.

    `generated_at` is per agent, not per file: agents run on different cadences, so
    a UI showing a plan needs to know how old *that* plan is. A booking rendered as
    fact when it was computed against a six-hour-old forecast is worse than no
    calendar at all.
    """
    bookings: list = field(default_factory=list)
    generated_at: datetime = None

    def active_modes(self, t: datetime) -> set:
        return {b.mode for b in self.bookings if b.covers(t)}


USER = "user"


class Coordinator:
    def __init__(self, services: dict, modes: dict, mode_requests_path=None,
                 modes_status_path=None, services_status_path=None,
                 schedule_path=None):
        self.services = services
        self.modes = modes
        self.mode_requests_path = mode_requests_path
        self.modes_status_path = modes_status_path
        self.services_status_path = services_status_path
        self.schedule_path = schedule_path
        self._agent_requests = []
        self._plans = {}                # agent name -> Plan
        self._inhibit_reason = None

    # --- intake -------------------------------------------------------------

    def set_agent_requests(self, requests) -> None:
        """Replace this cycle's agent-sourced requests.

        Authoritative and recomputed every cycle: an agent that stops asking for a
        mode has, by that silence, released it.
        """
        self._agent_requests = list(requests)

    def set_plan(self, agent: str, bookings, generated_at=None) -> None:
        """Replace one agent's plan. Wholesale, and only its own.

        An agent that *runs* replaces its plan; an agent that does not run keeps it.
        That is the whole of the cadence detachment: a planning agent may speak once
        an hour and its 11:00-15:00 booking is still honoured to within one tick,
        because nothing has to keep re-asserting it in between.

        The plan must therefore be dropped, not merely left, when the agent stops
        being trustworthy — see `drop_plan`.
        """
        self._plans[agent] = Plan(bookings=list(bookings),
                                  generated_at=generated_at or self._now())

    def drop_plan(self, agent: str) -> None:
        """Forget an agent's plan entirely.

        A plan outlives its author, which is exactly what makes it useful and
        exactly what makes this necessary: an agent that raised, or that was
        disabled, must stop driving hardware off a plan nobody is maintaining. Its
        bookings would otherwise stand until each one's own `end`.
        """
        self._plans.pop(agent, None)

    @staticmethod
    def _now():
        """Fallback clock. Control passes its own `now()` into every tick.

        UTC and stdlib, so this module keeps importing nothing — `config` would pull
        in `config_default` and the whole component layer behind it. Both clocks are
        tz-aware, which is the only property the booking comparison needs.
        """
        return datetime.now(timezone.utc)

    def read_user_requests(self) -> set:
        """The user's standing intent: modes wanted until they say otherwise.

        On disk because it must survive a restart — someone who switched the wallbox
        on should not find it off because the process bounced.
        """
        return {m for m in self._read_user_file().get("modes", []) if m in self.modes}

    def read_user_bookings(self) -> list:
        """The user's calendar, from the same file.

        Identical in shape to an agent's plan and resolved by the same code — the
        only differences are that this one is durable and that the Gateway, not
        Control, writes it. A row that does not parse is skipped rather than taken as
        a reason to discard the rest of somebody's calendar.
        """
        bookings = []
        for row in self._read_user_file().get("bookings", []):
            try:
                booking = Booking(
                    mode=row["mode"],
                    start=datetime.fromisoformat(row["start"]),
                    end=datetime.fromisoformat(row["end"]),
                    reason=row.get("reason", ""),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if booking.mode in self.modes:
                bookings.append(booking)
        return bookings

    def _user_updated_at(self):
        """When the user last edited their calendar — their plan's `generated_at`.

        Not the time it was read: an agent's plan ages because its inputs age, and a
        user's does not age at all.
        """
        try:
            return datetime.fromisoformat(self._read_user_file()["updated_at"])
        except (KeyError, TypeError, ValueError):
            return None

    def _read_user_file(self) -> dict:
        if self.mode_requests_path is None:
            return {}
        try:
            with open(self.mode_requests_path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    # --- safety -------------------------------------------------------------

    def inhibit(self, reason: str) -> None:
        """Safety override: hold every mode off until cleared.

        Does not itself touch hardware — the safety agent's own actions still go
        straight to the Gateway at top priority. This only stops the mode machinery
        fighting it by re-requesting what safety just switched off.
        """
        self._inhibit_reason = reason

    def clear_inhibit(self) -> None:
        self._inhibit_reason = None

    @property
    def inhibited(self) -> bool:
        return self._inhibit_reason is not None

    # --- the tick -----------------------------------------------------------

    def tick(self, config, t_now=None) -> dict:
        t_now = t_now or self._now()
        self._apply_requests(t_now)
        # After resolution, not before: `covers` already ignores an ended booking, so
        # pruning is housekeeping for the published calendar — and it has to run
        # after the user's plan is read in, or the file would show the past.
        self._prune(t_now)

        # Must precede the service reconcile: a service converges toward whoever
        # requires it, so it has to learn that this tick, not next.
        for mode in self.modes.values():
            mode.declare_requirements(self.services)

        for service in self.services.values():
            service.reconcile(config)

        for mode in self.modes.values():
            mode.reconcile(self.services, config)

        status = self.status()
        self._persist(status)
        self._persist_schedule(t_now)
        return status

    def _prune(self, t_now) -> None:
        """Drop bookings that have already ended.

        A booking is self-limiting — that is the first of the three bounds on a plan
        outliving its author — and there is no reason to keep the past in a file
        whose whole purpose is to say what happens next.
        """
        for plan in self._plans.values():
            plan.bookings = [b for b in plan.bookings if b.end > t_now]

    def _wanted_by(self, name: str, user_wants: set, t_now) -> set:
        sources = {USER} if name in user_wants else set()
        sources |= {
            r.agent for r in self._agent_requests if r.mode == name and r.active
        }
        # ponytail: requesters union — any source is enough, and there is no way to
        # say "not". If blocking is ever wanted (a user booking that forbids a mode
        # even while an agent asks for it), it goes here as a second pass over the
        # same bookings: resolve the positives, then subtract the blocks.
        sources |= {
            agent for agent, plan in self._plans.items()
            if name in plan.active_modes(t_now)
        }
        return sources

    def _apply_requests(self, t_now) -> None:
        # Re-read every tick, and before the inhibit check: the user's calendar is
        # the one plan this process does not own, so it is never held in memory
        # across a tick, and it stays accurate on the published schedule even while
        # an inhibit is suppressing it.
        self._plans[USER] = Plan(bookings=self.read_user_bookings(),
                                 generated_at=self._user_updated_at() or t_now)

        if self.inhibited:
            # Drop every requester. Modes deactivate, and services with nothing left
            # requiring them revert to their default on their own.
            #
            # The plans themselves are left alone: an inhibit suppresses a calendar,
            # it does not delete one. A transient breach must not silently wipe what
            # the planning agents worked out — when the inhibit clears, the bookings
            # that still cover `now` take effect again by themselves.
            for mode in self.modes.values():
                for source in list(mode.requesters):
                    mode.release(source)
            return

        user_wants = self.read_user_requests()
        for name, mode in self.modes.items():
            # Recomputed wholesale, never merged: an agent's requests are its entire
            # opinion for the cycle, so an agent that stops asking has released the
            # mode by saying nothing. Merging would leave a mode latched on forever
            # the first time an agent fell silent (or got disabled).
            wanted = self._wanted_by(name, user_wants, t_now)
            for source in mode.requesters - wanted:
                mode.release(source)
            for source in wanted:
                mode.request(source)

    def schedule(self, t_now=None) -> dict:
        """The calendar: every outstanding booking, flat, most imminent first.

        Flat rather than grouped by agent because that is how it is read — a UI
        renders a timeline, not a per-agent breakdown. Each row carries its source
        and that source's `generated_at` so staleness stays visible per booking.
        """
        rows = [
            {**b.to_dict(), "source": agent,
             "generated_at": plan.generated_at.isoformat() if plan.generated_at else None}
            for agent, plan in self._plans.items()
            for b in plan.bookings
        ]
        rows.sort(key=lambda r: r["start"])
        return {
            "now": (t_now or self._now()).isoformat(),
            "inhibited": self.inhibited,
            "bookings": rows,
        }

    def status(self) -> dict:
        return {
            "inhibited": self.inhibited,
            "inhibit_reason": self._inhibit_reason,
            "modes": {n: m.status().to_dict() for n, m in self.modes.items()},
            "services": {n: s.status().to_dict() for n, s in self.services.items()},
        }

    def _persist(self, status) -> None:
        for path, payload in (
            (self.modes_status_path, {"modes": status["modes"],
                                      "inhibited": status["inhibited"],
                                      "inhibit_reason": status["inhibit_reason"]}),
            (self.services_status_path, {"services": status["services"]}),
        ):
            if path is None:
                continue
            self._write(path, payload)

    def _persist_schedule(self, t_now) -> None:
        if self.schedule_path is not None:
            self._write(self.schedule_path, self.schedule(t_now))

    @staticmethod
    def _write(path, payload) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f)
        tmp.replace(path)
