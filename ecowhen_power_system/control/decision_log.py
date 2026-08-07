"""Why the system did what it did, one JSON object per line.

Harvested from `control/decision_log.py`. The entry shape follows the new
`AgentResult` (requests and commands instead of actions and sequences), and the
cycle entry now carries the coordinator's view — which modes and services exist, and
what phase each is in — because that is where the answer to "why is the fan on" lives
in this design.
"""
import json
from pathlib import Path

from .config import now


class DecisionLog:
    """Append-only, bounded to the last `max_entries` — but not on every write.

    Rewriting the file to drop one line would multiply what this costs the box's
    flash by the number of lines kept: the same 30 entries re-serialised on every
    append. So the trim is *amortised* — append cheaply, and once every
    `max_entries` writes rewrite the file down to the last `max_entries` lines. The
    file therefore holds between N and 2N entries and the extra bytes written work
    out at roughly one duplicate of each line, which is the price of bounding it.

    `max_entries=None` keeps the old unbounded behaviour.
    """

    def __init__(self, path: Path, max_entries: int = None):
        self._path = path
        self.max_entries = max_entries
        self._since_trim = 0

    def append(self, entry: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a") as f:
            f.write(json.dumps(entry) + "\n")

        if not self.max_entries:
            return
        self._since_trim += 1
        if self._since_trim >= self.max_entries:
            self._trim()
            self._since_trim = 0

    def _trim(self) -> None:
        """Keep the last `max_entries` lines. Written whole, then renamed, so a
        reader (the Gateway serves this file) never sees a half-written log."""
        try:
            with open(self._path) as f:
                lines = f.readlines()
        except OSError:
            return
        if len(lines) <= self.max_entries:
            return
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp, "w") as f:
            f.writelines(lines[-self.max_entries:])
        tmp.replace(self._path)

    def append_agent_result(self, result, timestamp: str = None) -> None:
        self.append({
            "timestamp": timestamp or now().isoformat(timespec="seconds"),
            **result.to_dict(),
        })

    def tail(self, n: int = 50) -> list:
        if not self._path.exists():
            return []
        with open(self._path) as f:
            lines = f.readlines()
        entries = []
        for line in lines[-n:]:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return entries


def build_cycle_entry(state, projection, status: dict) -> dict:
    """One line summarising a whole cycle: what was, what is projected, what runs."""
    socs = [s.projected_soc for s in projection.steps]
    return {
        "timestamp": now().isoformat(timespec="seconds"),
        "agent": "cycle",
        "current": {
            "soc": state.soc,
            "battery_voltage": state.battery_voltage,
            "battery_current": state.battery_current,
            "battery_temp": state.battery_temp,
            "solar_power_w": state.solar_power_w,
            "ac_load_w": state.ac_load_w,
            "state_age_seconds": round(state.age_seconds, 1),
        },
        "projection": {
            "horizon_hours": projection.horizon_hours,
            "forecast_available": projection.forecast is not None,
            "soc_at_end": socs[-1] if socs else None,
            "min_soc": min(socs) if socs else None,
            "max_soc": max(socs) if socs else None,
        },
        "inhibited": status["inhibited"],
        "inhibit_reason": status["inhibit_reason"],
        "modes": {n: m["state"] for n, m in status["modes"].items()},
        "services": {n: s["phase"] for n, s in status["services"].items()},
    }
