"""Charge the car inside forecast solar windows.

`_plan_windows` is the planning heuristic. Its output is published as `Booking`s —
statements about intervals, not firing moments — and the coordinator resolves which
one covers now on every tick. A window boundary is therefore honoured to within one
tick however rarely this agent runs.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import now
from ..coordinator import Booking
from ..projection import STEP_MINUTES
from .base import AgentResult, BaseAgent


@dataclass
class _ChargeWindow:
    start: datetime
    end: datetime        # exclusive — the wallbox goes off at this exact time
    solar_wh: float      # wallbox energy drawn from solar surplus
    battery_wh: float    # extra battery energy the wallbox costs (losses included)


def _plan_windows(
    steps,
    base_load_w: float,
    wallbox_w: float,
    efficiency: float,
    max_per_day: int,
    min_period_steps: int,
    merge_gap_steps: int,
    min_solar_fraction: float,
    min_soc: float,
    min_soc_buffer: float,
    bias_ratio: float,
    bias_steps: int,
) -> list:
    """Find contiguous wallbox ON windows where solar adequately covers demand.

    Steps where projected SOC is within min_soc_buffer of min_soc are excluded so the
    wallbox never deepens an already-marginal battery state.
    """
    if not steps:
        return []

    dt_h = STEP_MINUTES / 60.0
    wallbox_dc_w = wallbox_w / efficiency   # DC-side equivalent (inverter losses)

    def eff_solar(i: int) -> float:
        raw = steps[i].solar_w
        return raw * bias_ratio if i < bias_steps else raw

    def coverage(i: int) -> float:
        """Fraction of wallbox_dc_w covered by solar surplus at step i."""
        if steps[i].projected_soc - min_soc < min_soc_buffer:
            return 0.0
        surplus = eff_solar(i) - base_load_w
        if wallbox_dc_w <= 0:
            return 0.0
        return min(1.0, max(0.0, surplus / wallbox_dc_w))

    # 1. Raw contiguous candidate groups
    n = len(steps)
    raw = []
    i = 0
    while i < n:
        if coverage(i) >= min_solar_fraction:
            j = i + 1
            while j < n and coverage(j) >= min_solar_fraction:
                j += 1
            raw.append((i, j))
            i = j
        else:
            i += 1

    if not raw:
        return []

    # 2. Merge windows whose gap is within merge_gap_steps
    merged = [raw[0]]
    for s, e in raw[1:]:
        prev_s, prev_e = merged[-1]
        if s - prev_e <= merge_gap_steps:
            merged[-1] = (prev_s, e)
        else:
            merged.append((s, e))

    # 3. Drop windows shorter than the minimum block duration
    valid = [(s, e) for s, e in merged if e - s >= min_period_steps]
    if not valid:
        return []

    # 4. Per calendar day: score by total solar yield, keep the best max_per_day
    by_day = defaultdict(list)
    for s, e in valid:
        by_day[steps[s].time.date()].append((s, e))

    result = []
    for d in sorted(by_day):
        day_windows = by_day[d]

        def solar_yield(se, _eff=eff_solar, _base=base_load_w, _dt=dt_h):
            s, e = se
            return sum(max(0.0, _eff(i) - _base) * _dt for i in range(s, e))

        day_windows.sort(key=solar_yield, reverse=True)
        selected = sorted(day_windows[:max_per_day], key=lambda x: x[0])

        for s, e in selected:
            solar_wh = sum(
                min(wallbox_dc_w, max(0.0, eff_solar(i) - base_load_w)) * dt_h
                for i in range(s, e)
            )
            result.append(_ChargeWindow(
                start=steps[s].time,
                end=steps[e - 1].time + timedelta(minutes=STEP_MINUTES),
                solar_wh=round(solar_wh, 1),
                battery_wh=round(max(0.0, (e - s) * wallbox_dc_w * dt_h - solar_wh), 1),
            ))

    return result


class ForecastWallboxAgent(BaseAgent):
    name = "forecast_wallbox"
    fast_cycle = False      # planning cycles only

    def run(self, projection, config) -> AgentResult:
        if projection.forecast is None or not projection.steps:
            return AgentResult(
                agent_name=self.name,
                rationale="inactive — no solar forecast available",
            )

        cfg = config.agents.forecast_wallbox
        t_now = now()

        # Real-time bias: scale the near-term forecast by actual/forecast, but only
        # when the forecast is non-trivial, or the ratio is divide-by-zero noise.
        forecast_now_w = projection.forecast.get_power(t_now)
        actual_now_w = projection.current.solar_power_w
        if forecast_now_w > 50.0:
            bias_ratio = min(3.0, max(0.1, actual_now_w / forecast_now_w))
        else:
            bias_ratio = 1.0

        windows = _plan_windows(
            steps=projection.steps,
            base_load_w=config.estimated_load_w,
            wallbox_w=cfg.wallbox_power_w,
            efficiency=cfg.inverter_efficiency,
            max_per_day=cfg.max_periods_per_day,
            min_period_steps=max(1, cfg.min_period_minutes // STEP_MINUTES),
            merge_gap_steps=max(1, cfg.merge_gap_minutes // STEP_MINUTES),
            min_solar_fraction=cfg.min_solar_fraction,
            min_soc=config.battery.min_soc,
            min_soc_buffer=0.05,
            bias_ratio=bias_ratio,
            bias_steps=120 // STEP_MINUTES,     # bias over the next 2 hours
        )

        current_window = next((w for w in windows if w.start <= t_now < w.end), None)
        in_window = current_window is not None

        total_solar_wh = sum(w.solar_wh for w in windows)
        total_battery_wh = sum(w.battery_wh for w in windows)
        metrics = {
            "wallbox_power_w": cfg.wallbox_power_w,
            "planned_windows": len(windows),
            "total_solar_wh": round(total_solar_wh),
            "total_battery_wh": round(total_battery_wh),
            "solar_bias_ratio": round(bias_ratio, 2),
        }

        if windows:
            window_strs = [f"{w.start.strftime('%m-%d %H:%M')}–{w.end.strftime('%H:%M')}"
                           for w in windows]
            rationale = (
                f"{'ON' if in_window else 'OFF'} — {len(windows)} window(s): "
                f"{', '.join(window_strs)}; solar {total_solar_wh:.0f}Wh, "
                f"battery {total_battery_wh:.0f}Wh; bias {bias_ratio:.2f}x"
            )
        else:
            rationale = (f"no charge windows — solar below threshold "
                         f"(min_solar_fraction={cfg.min_solar_fraction:.0%}, "
                         f"bias={bias_ratio:.2f}x)")

        return AgentResult(
            agent_name=self.name,
            rationale=rationale,
            metrics=metrics,
            bookings=[
                Booking(mode="wallbox", start=w.start, end=w.end,
                        reason=(f"solar window, {w.solar_wh:.0f}Wh solar / "
                                f"{w.battery_wh:.0f}Wh battery"))
                for w in windows
            ],
        )
