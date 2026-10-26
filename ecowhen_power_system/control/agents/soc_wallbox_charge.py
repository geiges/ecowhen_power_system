"""Charge the car once the battery has been full for a while; stop when it is low.

Harvested from `control/agents/soc_wallbox_charge.py`. Two changes:

* **It asks for a mode.** `SequenceIntent("wallbox_on")` and the bare
  `ScheduledAction("wallbox_charge", 0)` both become one `ModeRequest`, level-
  triggered: this agent states its opinion every cycle and stops when it stops
  wanting it. The inverter and the fan come up in order because the mode declares
  them, not because this agent knows about them.
* **No CSV.** `minutes_at_full_soc` arrives in `state.json` from the Power System's
  SocTracker (decision #7). The predecessor globbed the Logger's `sim_*.csv` for it,
  which also meant a battery full since 18:00 reported "full for 5 minutes" at 00:05,
  when the day's file rolled over.
"""
from ..coordinator import ModeRequest
from .base import AgentResult, BaseAgent


def _hours_to_full(projection, threshold: float):
    """Fractional hours until the projection first reaches *threshold*, or None."""
    t0 = projection.current.timestamp
    for step in projection.steps:
        if step.projected_soc >= threshold:
            return (step.time - t0).total_seconds() / 3600
    return None


def _time_to_threshold_h(soc, threshold, current_a, capacity_ah):
    """Hours until SOC falls to *threshold* at the present current, or None.

    Mirrors `Battery_Simulation.time_to_low_battery`: only meaningful while actually
    discharging, and only above the threshold.
    """
    if current_a >= -0.5 or soc <= threshold:
        return None
    return (soc - threshold) * capacity_ah / (-current_a)


class SocWallboxChargeAgent(BaseAgent):
    """ON once SOC has been >= soc_on_threshold for soc_on_minutes; OFF below
    soc_off_threshold."""

    name = "soc_wallbox_charge"
    interval_seconds = 0        # cheap, and it is the one that says "charge now"

    def run(self, state, projection, config) -> AgentResult:
        current = projection.current
        cfg = config.agents.soc_wallbox_charge

        time_to_off_h = _time_to_threshold_h(
            current.soc, cfg.soc_off_threshold,
            current.battery_current, config.battery.capacity_ah,
        )
        metrics = {
            "soc": round(current.soc, 4),
            "minutes_at_full": round(current.minutes_at_full_soc, 1),
            "time_to_off_threshold_h": (
                round(time_to_off_h, 2) if time_to_off_h is not None else None),
        }

        def result(active, reason, extra=""):
            return AgentResult(
                agent_name=self.name,
                rationale=f"wallbox {'ON' if active else 'OFF'} — {reason}{extra}",
                metrics=metrics,
                requests=[ModeRequest(mode="wallbox", active=active,
                                      agent=self.name, reason=reason)],
            )

        if current.soc < cfg.soc_off_threshold:
            return result(False, f"SOC {current.soc:.1%} below off-threshold "
                                 f"{cfg.soc_off_threshold:.1%}")

        if current.soc < cfg.soc_on_threshold:
            proj_h = _hours_to_full(projection, cfg.soc_on_threshold)
            metrics["proj_hours_to_full"] = proj_h
            extra = (f", projected full in ~{proj_h:.1f}h" if proj_h is not None
                     else ", full not reached in forecast horizon")
            if time_to_off_h is not None:
                extra += f", time to off-threshold {time_to_off_h:.1f}h"
            return result(False, f"SOC {current.soc:.1%} between "
                                 f"{cfg.soc_off_threshold:.0%} and "
                                 f"{cfg.soc_on_threshold:.0%}", extra)

        minutes_full = current.minutes_at_full_soc
        if minutes_full >= cfg.soc_on_minutes:
            return result(True, f"SOC at full for {minutes_full:.0f} min "
                                f"(threshold {cfg.soc_on_minutes} min)")

        return result(False, f"SOC full for {minutes_full:.0f} min, "
                             f"need {cfg.soc_on_minutes} — waiting")
