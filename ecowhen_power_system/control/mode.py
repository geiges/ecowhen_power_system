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

A mode can also be *blocked*: a named source saying "not this one, not now".
Blocking is the mirror of requesting and deliberately not a separate mechanism — a
blocked mode is simply not wanted, so it stands down through the same DEACTIVATING
path, in the same order, and comes back by itself when the block lifts. Nothing
latches. Two sources block today: the safety agent (`ac_solar` while the battery is
hot or high) and `blocked_by`, one mode excluding another (`ac_other` yields to
`wallbox` rather than sharing the AC budget with it).

Blocking is a *mode*-level idea. There is deliberately no way to forbid a service:
services are reference-counted precisely because several modes may legitimately want
one, and letting anything force one off would pull the inverter out from under a
mode that is holding it. Block the mode, and the service falls away by its own
count.
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
    blockers: list = field(default_factory=list)
    required_services: list = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "requesters": self.requesters,
            # Both sides of the question, because "wanted, and held off by whom" is
            # the thing somebody is actually trying to find out at 3am.
            "blockers": self.blockers,
            "required_services": self.required_services,
            "detail": self.detail,
        }


class Mode(ABC):
    """Base class. Subclasses supply `apply_primary` and `primary_satisfied`."""

    name: str = ""
    required_services: list = []
    blocked_by: list = []           # other modes whose activity excludes this one
    user_activatable: bool = True

    def __init__(self, name=None, required_services=None, blocked_by=None):
        if name is not None:
            self.name = name
        if required_services is not None:
            self.required_services = list(required_services)
        if blocked_by is not None:
            self.blocked_by = list(blocked_by)
        self._requesters = set()
        self._blockers = set()
        self._state = ModeState.INACTIVE
        self._detail = ""

    # --- who wants this, and who forbids it ---------------------------------

    def request(self, source: str) -> None:
        """Add a requester ("user", or an agent name). Idempotent."""
        self._requesters.add(source)

    def release(self, source: str) -> None:
        self._requesters.discard(source)

    def block(self, source: str) -> None:
        """Forbid this mode on behalf of `source`. Idempotent."""
        self._blockers.add(source)

    def unblock(self, source: str) -> None:
        self._blockers.discard(source)

    @property
    def requesters(self) -> set:
        return set(self._requesters)

    @property
    def blockers(self) -> set:
        return set(self._blockers)

    @property
    def requested(self) -> bool:
        """Wanted *and* permitted. One blocker outvotes every requester.

        Asymmetric on purpose: a block is a constraint, and no number of things
        wanting the mode makes an overloaded AC circuit or a hot battery safe. The
        requesters are left in place, so the want returns by itself when the block
        lifts — this is not a release.
        """
        return bool(self._requesters) and not self._blockers

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

        if self._blockers and self._requesters:
            # Wanted but not permitted: say so, or the mode reads as "inactive" and
            # nothing on the status page explains why it will not come up.
            self._detail = f"blocked by {', '.join(sorted(self._blockers))}"

    def reset(self, services: dict) -> None:
        """Clear a mode that gave up, so it may be tried again.

        `failed` is a dead end by design — nothing clears it on a timer, because a
        mode that exhausted its prerequisites' retries is a fault somebody should
        look at. But it needs a way *out*, and re-requesting it is not one: the mode
        never released its services (see `reconcile`), so `require` is a no-op on a
        name already in the set and the service keeps its burnt budget.

        The release is therefore the whole of this method — `SystemService.release`
        zeroes `_attempts`, so a mode still wanted converges again from the next
        tick, and one nobody wants any more simply stays down.
        """
        if self._state != ModeState.FAILED:
            return
        for service in self._mine(services):
            service.release(self.name)
        self._state = ModeState.INACTIVE
        self._detail = "reset"

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
            blockers=sorted(self._blockers),
            required_services=list(self.required_services),
            detail=self._detail,
        )
