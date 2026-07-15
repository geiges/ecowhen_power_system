"""System services: shared, reference-counted actuator resources.

A service is a thing that must be *on* before something else can work — the AC
inverter, the fan. Several modes may need the same one; it stays on while any of them
does and reverts when the last releases it.

Each service owns its own convergence: drive the actuator toward the target, read
back, retry, give up. That absorbs the predecessor's `SequenceStep`, whose ordering
had to be hand-written per transition and duplicated for the teardown.

Services are binary. `target_on` is known absolutely at all times, which is exactly
why the Gateway is asked to *set* a value rather than toggle one.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class ServicePhase(str, Enum):
    IDLE = "idle"                # at default, nobody wants it
    CONVERGING = "converging"    # driving toward target, readback pending
    SATISFIED = "satisfied"      # readback agrees with target
    FAILED = "failed"            # retries exhausted


@dataclass
class ServiceStatus:
    name: str
    target_on: bool
    phase: str
    requirers: list = field(default_factory=list)
    attempts: int = 0
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "target_on": self.target_on,
            "phase": self.phase,
            "requirers": self.requirers,
            "attempts": self.attempts,
            "detail": self.detail,
        }


class SystemService(ABC):
    """Base class. Subclasses supply only `read_actual_on` and `drive`."""

    name: str = ""
    default_on: bool = False
    max_attempts: int = 5

    def __init__(self, name=None, default_on=None, max_attempts=None):
        if name is not None:
            self.name = name
        if default_on is not None:
            self.default_on = default_on
        if max_attempts is not None:
            self.max_attempts = max_attempts
        self._requirers = set()
        self._attempts = 0
        self._phase = ServicePhase.IDLE
        self._detail = ""

    # --- reference counting -------------------------------------------------

    def require(self, requirer: str) -> None:
        """Register a mode as needing this service on. Idempotent."""
        if requirer not in self._requirers:
            self._requirers.add(requirer)
            self._attempts = 0        # a new requirer deserves a fresh budget

    def release(self, requirer: str) -> None:
        """Drop a requirer. Idempotent."""
        if requirer in self._requirers:
            self._requirers.discard(requirer)
            self._attempts = 0

    @property
    def required_by(self) -> set:
        return set(self._requirers)

    @property
    def target_on(self) -> bool:
        return True if self._requirers else self.default_on

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def satisfied(self) -> bool:
        return self._phase == ServicePhase.SATISFIED

    # --- hardware, supplied by subclasses -----------------------------------

    @abstractmethod
    def read_actual_on(self, config):
        """Current hardware state. None if unknown or unreachable."""

    @abstractmethod
    def drive(self, on: bool, config) -> bool:
        """Command the actuator toward `on`. True if the Gateway accepted it."""

    # --- the convergence engine ---------------------------------------------

    def reconcile(self, config) -> ServiceStatus:
        """One tick toward the target.

        Unknown readback is not treated as a mismatch: driving blind would mean
        writing on every tick to a device we cannot see, so we wait and look again.
        """
        target = self.target_on
        actual = self.read_actual_on(config)

        if actual is None:
            self._phase = ServicePhase.CONVERGING
            self._detail = "hardware state unknown — cannot verify"
            return self.status()

        if actual == target:
            self._attempts = 0
            self._phase = (
                ServicePhase.IDLE
                if (not self._requirers and target == self.default_on)
                else ServicePhase.SATISFIED
            )
            self._detail = ""
            return self.status()

        if self._attempts >= self.max_attempts:
            self._phase = ServicePhase.FAILED
            self._detail = f"gave up after {self._attempts} attempts"
            return self.status()

        self._attempts += 1
        accepted = self.drive(target, config)
        self._phase = ServicePhase.CONVERGING
        self._detail = (
            f"drove to {'on' if target else 'off'} (attempt {self._attempts})"
            if accepted else
            f"gateway rejected the command (attempt {self._attempts})"
        )
        return self.status()

    def status(self) -> ServiceStatus:
        return ServiceStatus(
            name=self.name,
            target_on=self.target_on,
            phase=self._phase.value,
            requirers=sorted(self._requirers),
            attempts=self._attempts,
            detail=self._detail,
        )
