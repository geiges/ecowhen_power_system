"""Modes: capabilities that declare what they need.

A mode says "to charge the wallbox I need the AC inverter and the fan". It does not
say in which order to switch them on, or how to check they came up, or what to undo
first on the way down. That all falls out of the declaration.

This is the whole point of the refactor. The predecessor encoded the same knowledge as
an ordered list of steps per transition (`wallbox_on.py`), then wrote the teardown
again backwards in a second file (`wallbox_off.py`) — where a copy-paste left
`multiplus_mode_off = actcfg.multiplus_mode_on`.

Ordering here is two-level, and only two-level:
  * prerequisite services converge in parallel with each other;
  * the mode's own effect fires only once *all* of them are satisfied;
  * on the way down the effect drops first, *then* the requirements release, so
    services tear down afterwards in dependency order on their own.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class ModeState(str, Enum):
    INACTIVE = "inactive"
    ACTIVATING = "activating"       # wanted; waiting on prerequisite services
    ACTIVE = "active"               # prerequisites up, effect applied
    DEACTIVATING = "deactivating"   # undoing the effect before releasing requirements
    FAILED = "failed"               # a prerequisite gave up


@dataclass
class ModeStatus:
    name: str
    state: str
    requesters: list = field(default_factory=list)
    required_services: list = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "requesters": self.requesters,
            "required_services": self.required_services,
            "detail": self.detail,
        }


class Mode(ABC):
    """Base class. Subclasses supply `apply_primary` and `primary_satisfied`."""

    name: str = ""
    required_services: list = []
    user_activatable: bool = True

    def __init__(self, name=None, required_services=None):
        if name is not None:
            self.name = name
        if required_services is not None:
            self.required_services = list(required_services)
        self._requesters = set()
        self._state = ModeState.INACTIVE
        self._detail = ""

    # --- who wants this -----------------------------------------------------

    def request(self, source: str) -> None:
        """Add a requester ("user", or an agent name). Idempotent."""
        self._requesters.add(source)

    def release(self, source: str) -> None:
        self._requesters.discard(source)

    @property
    def requesters(self) -> set:
        return set(self._requesters)

    @property
    def requested(self) -> bool:
        return bool(self._requesters)

    @property
    def state(self) -> str:
        return self._state

    # --- the mode's own effect, supplied by subclasses -----------------------

    @abstractmethod
    def apply_primary(self, on: bool, config) -> bool:
        """Command this mode's own actuator (e.g. the wallbox plug)."""

    @abstractmethod
    def primary_satisfied(self, on: bool, config) -> bool:
        """Verify the effect actually reached `on`."""

    # --- the gated state machine --------------------------------------------

    def _mine(self, services: dict) -> list:
        return [services[n] for n in self.required_services if n in services]

    def declare_requirements(self, services: dict) -> None:
        """Register/withdraw interest in prerequisite services.

        Deliberately separate from `reconcile`, and called before it: the services
        have to know who wants them *before* they converge, or every activation
        costs two ticks — services would reconcile against last tick's requirers.

        Withdrawal does not release here. A mode being switched off must drop its own
        effect first (in `reconcile`) and only then let the services go, otherwise
        the inverter can vanish from under a live load.
        """
        wanted = self.requested

        if wanted and self._state in (ModeState.INACTIVE, ModeState.FAILED):
            for service in self._mine(services):
                service.require(self.name)
            self._state = ModeState.ACTIVATING
            self._detail = "waiting on prerequisites"

        elif not wanted and self._state in (ModeState.ACTIVATING, ModeState.ACTIVE):
            self._state = ModeState.DEACTIVATING
            self._detail = "undoing effect before releasing prerequisites"

    def reconcile(self, services: dict, config) -> ModeStatus:
        mine = self._mine(services)

        if self._state == ModeState.ACTIVATING:
            failed = [s.name for s in mine if s.phase == "failed"]
            if failed:
                # Do not release the requirements: a failed prerequisite is a fault
                # to look at, not a reason to start tearing the system down.
                self._state = ModeState.FAILED
                self._detail = f"prerequisite failed: {', '.join(sorted(failed))}"
            elif all(s.satisfied for s in mine):
                self.apply_primary(True, config)
                if self.primary_satisfied(True, config):
                    self._state = ModeState.ACTIVE
                    self._detail = ""
                else:
                    self._detail = "prerequisites up, effect not confirmed yet"
            else:
                pending = sorted(s.name for s in mine if not s.satisfied)
                self._detail = f"waiting on {', '.join(pending)}"

        elif self._state == ModeState.DEACTIVATING:
            self.apply_primary(False, config)
            if self.primary_satisfied(False, config):
                # Only now let go: releasing while the effect is still live would let
                # a service drop out from under it.
                for service in mine:
                    service.release(self.name)
                self._state = ModeState.INACTIVE
                self._detail = ""

        return self.status()

    def status(self) -> ModeStatus:
        return ModeStatus(
            name=self.name,
            state=self._state.value,
            requesters=sorted(self._requesters),
            required_services=list(self.required_services),
            detail=self._detail,
        )
