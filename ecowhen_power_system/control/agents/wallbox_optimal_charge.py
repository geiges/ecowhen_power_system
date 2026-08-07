"""Day-by-day brute force for the wallbox block that best matches solar production.

The search (`_mismatch_cost`, `_simulate_day`, `_min_charge_duration`, `_plan_day`)
is harvested verbatim: it is the algorithm, and its reasoning is in its own
docstrings. Only the output changed — `SequenceIntent("wallbox_on"/"wallbox_off")`
became one level-triggered `ModeRequest`.

That deletes the note the predecessor had to write about `execute_at` being `now`
rather than the window boundary: an intent that fires within +-30 s of a timestamp
will silently never fire if the cycle that would notice runs 5 minutes late. A
request has no firing moment to miss.

Mutually exclusive with the other two wallbox agents — enforced by the REST config,
not here — because they would otherwise take turns requesting the same mode.
"""
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ... import paths
from ..config import now
from ..coordinator import Booking
from ..projection import make_battery, step_soc
from .base import AgentResult, BaseAgent

SCHEDULE_FILENAME = "wallbox_optimal_charge_schedule.json"


@dataclass
class _DayWindow:
    start: datetime
    end: datetime      # exclusive


def _day_buckets(start_hour: datetime, horizon_days: int) -> list:
    """Hourly timestamps from start_hour, grouped by calendar date.

    The first bucket covers only the rest of today; later ones are full days (the
    last may be partial, at the horizon boundary).
    """
    hours = [start_hour + timedelta(hours=i) for i in range(horizon_days * 24)]
    buckets = []
    for t in hours:
        if buckets and buckets[-1][-1].date() == t.date():
            buckets[-1].append(t)
        else:
            buckets.append([t])
    return buckets


def _mismatch_cost(solar_w: list, base_load_w: float, wallbox_w: float,
                   start: int, duration: int) -> float:
    """Raw power mismatch: how far consumption (load+wallbox) is from solar
    production, summed over the day. Deliberately ignores battery SOC so it rewards
    routing solar straight to the wallbox rather than round-tripping it through the
    battery, and still rewards capturing solar that would otherwise be curtailed."""
    total = 0.0
    for i, solar in enumerate(solar_w):
        active = start <= i < start + duration
        total += abs(solar - (base_load_w + (wallbox_w if active else 0.0)))
    return total


def _simulate_day(battery, solar_w: list, base_load_w: float, wallbox_w: float,
                  start: int, duration: int, start_soc: float, trace=None):
    """Clamped hourly SOC simulation for one day. Returns (min_soc, end_soc).

    If *trace* is given, the SOC after each hour is appended to it (used to record
    the winning candidate; left None during the search to save the bookkeeping)."""
    battery.set_state_of_charge(start_soc)
    min_soc = start_soc
    for i, solar in enumerate(solar_w):
        active = start <= i < start + duration
        soc = step_soc(battery, solar, base_load_w + (wallbox_w if active else 0.0),
                       dt_seconds=3600)
        min_soc = min(min_soc, soc)
        if trace is not None:
            trace.append(soc)
    return min_soc, battery.state_of_charge


def _min_charge_duration(battery, solar_w: list, base_load_w: float,
                         wallbox_w: float, start_soc: float) -> int:
    """Minimum wallbox-on hours this day: the full-power hours needed to consume the
    day's curtailed (wasted) solar, rounded up. 0 if the day never reaches 100%."""
    trace = []
    _simulate_day(battery, solar_w, base_load_w, 0.0, 0, 0, start_soc, trace=trace)
    wasted_wh = sum(
        max(0.0, solar - base_load_w)
        for solar, soc in zip(solar_w, trace)
        if soc >= 0.999
    )
    if wasted_wh <= 0:
        return 0
    return math.ceil(wasted_wh / wallbox_w)


def _plan_day(battery, solar_w: list, base_load_w: float, wallbox_w: float,
              min_storage_fraction: float, start_soc: float):
    """Brute-force every (start, duration) block for one day.

    Returns (winning (start, duration) or None, projected SOC at day's end).
    Candidates that would drop SOC below min_storage_fraction at any point are
    rejected; among the rest the lowest power-mismatch cost wins.

    "No charge" is only itself a candidate on days that never reach 100% SOC — a
    whole-day mismatch comparison otherwise always prefers accepting curtailment to
    the (larger, but harmless given ample floor headroom) mismatch of running the
    wallbox on weak-solar days, silently wasting hours of curtailed solar.
    """
    n = len(solar_w)
    min_duration = _min_charge_duration(battery, solar_w, base_load_w, wallbox_w,
                                        start_soc)
    if min_duration > 0:
        min_duration = min(n, min_duration)
        candidates = [(start, duration)
                      for start in range(n)
                      for duration in range(min_duration, n - start + 1)]
    else:
        candidates = [(0, 0)] + [(start, duration)
                                 for start in range(n)
                                 for duration in range(1, n - start + 1)]

    best_choice = None
    best_cost = None
    best_end_soc = start_soc

    for start, duration in candidates:
        min_soc, end_soc = _simulate_day(battery, solar_w, base_load_w, wallbox_w,
                                         start, duration, start_soc)
        if min_soc < min_storage_fraction:
            continue
        cost = _mismatch_cost(solar_w, base_load_w, wallbox_w, start, duration)
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_choice = (start, duration) if duration > 0 else None
            best_end_soc = end_soc

    if best_cost is None:
        # Nothing keeps storage above the floor (already very low) — don't charge.
        _, end_soc = _simulate_day(battery, solar_w, base_load_w, wallbox_w, 0, 0,
                                   start_soc)
        return None, end_soc

    return best_choice, best_end_soc


class WallboxOptimalChargeAgent(BaseAgent):
    name = "wallbox_optimal_charge"
    interval_seconds = 300      # several projections per run

    def __init__(self, data_dir: Path = None):
        self.data_dir = data_dir if data_dir is not None else paths.DATA_DIR

    def run(self, projection, config) -> AgentResult:
        if projection.forecast is None:
            return AgentResult(
                agent_name=self.name,
                rationale="inactive — no solar forecast available",
            )

        cfg = config.agents.wallbox_optimal_charge
        t_now = now()
        wallbox_dc_w = cfg.wallbox_power_w / cfg.inverter_efficiency
        base_load_w = config.estimated_load_w

        buckets = _day_buckets(t_now.replace(minute=0, second=0, microsecond=0),
                               cfg.horizon_days)
        battery = make_battery(config)
        soc = projection.current.soc

        windows = []
        schedule_rows = []
        for bucket in buckets:
            solar_w = [projection.forecast.get_hour(t) for t in bucket]
            day_start_soc = soc
            choice, soc = _plan_day(battery, solar_w, base_load_w, wallbox_dc_w,
                                    cfg.min_storage_fraction, day_start_soc)
            start, duration = choice if choice is not None else (0, 0)
            if choice is not None:
                windows.append(_DayWindow(
                    start=bucket[start],
                    end=bucket[start + duration - 1] + timedelta(hours=1),
                ))

            trace = []
            _simulate_day(battery, solar_w, base_load_w, wallbox_dc_w,
                          start, duration, day_start_soc, trace=trace)
            for i, t in enumerate(bucket):
                schedule_rows.append({
                    "time": t.isoformat(),
                    "solar_w": round(solar_w[i], 1),
                    "wallbox_on": start <= i < start + duration,
                    "projected_soc": round(trace[i], 4),
                })

        current_window = next((w for w in windows if w.start <= t_now < w.end), None)
        in_window = current_window is not None

        if windows:
            window_strs = [f"{w.start.strftime('%m-%d %H:%M')}–{w.end.strftime('%H:%M')}"
                           for w in windows]
            rationale = (f"{'ON' if in_window else 'OFF'} — {len(windows)} window(s): "
                         f"{', '.join(window_strs)}")
        else:
            rationale = "no charge windows — no feasible solar surplus found"

        self._save_schedule(t_now, cfg, schedule_rows)

        return AgentResult(
            agent_name=self.name,
            rationale=rationale,
            metrics={
                "wallbox_power_w": cfg.wallbox_power_w,
                "planned_windows": len(windows),
                "projected_end_soc": round(soc, 3),
            },
            bookings=[
                Booking(mode="wallbox", start=w.start, end=w.end,
                        reason="optimal charge window")
                for w in windows
            ],
        )

    def _save_schedule(self, t_now, cfg, rows) -> None:
        """Persist the planned solar/wallbox/SOC trace for offline inspection."""
        path = self.data_dir / SCHEDULE_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump({
                "generated_at": t_now.isoformat(),
                "wallbox_power_w": cfg.wallbox_power_w,
                "min_storage_fraction": cfg.min_storage_fraction,
                "rows": rows,
            }, f, indent=2)
