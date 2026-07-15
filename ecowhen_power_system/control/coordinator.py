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
"""
import json
from dataclasses import dataclass


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


USER = "user"


class Coordinator:
    def __init__(self, services: dict, modes: dict, mode_requests_path=None,
                 modes_status_path=None, services_status_path=None):
        self.services = services
        self.modes = modes
        self.mode_requests_path = mode_requests_path
        self.modes_status_path = modes_status_path
        self.services_status_path = services_status_path
        self._agent_requests = []
        self._inhibit_reason = None

    # --- intake -------------------------------------------------------------

    def set_agent_requests(self, requests) -> None:
        """Replace this cycle's agent-sourced requests.

        Authoritative and recomputed every cycle: an agent that stops asking for a
        mode has, by that silence, released it.
        """
        self._agent_requests = list(requests)

    def read_user_requests(self) -> set:
        """User intent, from the file the Gateway writes.

        On disk because it must survive a restart — someone who switched the wallbox
        on should not find it off because the process bounced.
        """
        if self.mode_requests_path is None:
            return set()
        try:
            with open(self.mode_requests_path) as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return set()
        return {m for m in raw.get("modes", []) if m in self.modes}

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

    def tick(self, config) -> dict:
        self._apply_requests()

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
        return status

    def _wanted_by(self, name: str, user_wants: set) -> set:
        sources = {USER} if name in user_wants else set()
        sources |= {
            r.agent for r in self._agent_requests if r.mode == name and r.active
        }
        return sources

    def _apply_requests(self) -> None:
        if self.inhibited:
            # Drop every requester. Modes deactivate, and services with nothing left
            # requiring them revert to their default on their own.
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
            wanted = self._wanted_by(name, user_wants)
            for source in mode.requesters - wanted:
                mode.release(source)
            for source in wanted:
                mode.request(source)

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
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w") as f:
                json.dump(payload, f)
            tmp.replace(path)
