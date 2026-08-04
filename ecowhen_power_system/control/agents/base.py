"""What an agent may say, and it is deliberately little.

The predecessor's `AgentResult` carried `actions` (raw actuator writes) and
`sequences` (a named step list to run). Both are gone; an agent now expresses intent
in three ways, none of which is an ordered list of hardware writes:

    requests   ModeRequest — "I want the wallbox mode, now". Level-triggered and
               recomputed every cycle: silence is release. The fast agents use this,
               because they run on every tick anyway.
    blocks     ModeBlock — "not `ac_solar`, not while the battery is this hot". The
               mirror of a request and stronger than one: it outvotes every
               requester, where `ModeRequest(active=False)` only withdraws this
               agent's own want. Level-triggered too — silence permits.
    bookings   Booking — "I want the wallbox mode 11:00-15:00". A planning agent
               publishes its whole plan at once and the coordinator asks each tick
               which bookings cover now. The plan stands until that same agent
               revises it, which is what lets a planning cadence be slower than the
               tick without the boundaries it planned going stale.
    commands   DirectCommand — the safety exception, and only that. A hard cut goes
               straight to the Gateway ahead of everything, and does not wait for a
               service to converge.
    inhibit    a reason string. Non-empty holds every mode off until it clears.

`fast_cycle` is a cadence knob — how often the agent is asked, nothing more. It does
not change how its output is treated.

`rationale` and `metrics` are unchanged and still the point of the decision log: what
the system did is worth much less than why.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class DirectCommand:
    """An immediate actuator write. Safety only."""
    actuator: str
    on: bool
    reason: str

    def to_dict(self) -> dict:
        return {"actuator": self.actuator, "on": self.on, "reason": self.reason}


@dataclass
class AgentResult:
    agent_name: str
    rationale: str
    metrics: dict = field(default_factory=dict)
    requests: list = field(default_factory=list)    # list[ModeRequest]  — now
    blocks: list = field(default_factory=list)      # list[ModeBlock]    — not now
    bookings: list = field(default_factory=list)    # list[Booking]      — ahead
    commands: list = field(default_factory=list)    # list[DirectCommand]
    inhibit: str = ""                               # non-empty → hold all modes off

    def to_dict(self) -> dict:
        return {
            "agent": self.agent_name,
            "rationale": self.rationale,
            "metrics": self.metrics,
            "requests": [r.to_dict() for r in self.requests],
            "blocks": [b.to_dict() for b in self.blocks],
            "bookings": [b.to_dict() for b in self.bookings],
            "commands": [c.to_dict() for c in self.commands],
            "inhibit": self.inhibit,
        }


class BaseAgent(ABC):
    name: str = ""
    fast_cycle: bool = False    # True → every safety interval; False → planning only

    @abstractmethod
    def run(self, projection, config) -> AgentResult:
        """Look at the system and say what is wanted. No I/O to hardware."""

    def is_enabled(self, config) -> bool:
        agent_cfg = getattr(config.agents, self.name, None)
        if agent_cfg is None:
            return True
        return bool(agent_cfg.enabled)
