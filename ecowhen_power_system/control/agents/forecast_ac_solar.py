"""
Agent to schedule the AC balcony mppt solar input. Since the AC MPPT requires 
the AC inverter, optimized operating hours are advantages. 
This simple agents activates the AC MPPT and its prequistits if the forecasted
solar input from is greater than `min_yield_w`. To avoid many on/off cycles, the
config parameter `max_windows_per_day` controls how many windows are scheduled
each day. 

The one thing that is not a plain threshold is **switching cost**. Every gap in the
forecast above the threshold — an hour of cloud at noon — would otherwise be a plug
cycle, so the day is capped at `max_windows_per_day` windows and the runs that are
closest together are *merged*, not dropped: the excess hours cost standby power, and
dropping them would cost the yield instead. Merging is what "at most two windows"
means here — coverage is kept, the switching is what is bounded.

Both knobs are configurable (`agents.forecast_ac_solar`), because 100 W is a guess
about the Deye's own draw and two windows a guess about how much cycling is
tolerable. Neither is a measurement.

The output is `Booking`s, like the other planners: an interval, resolved by the
coordinator on every tick, so this agent's 5-minute cadence does not blunt a window
boundary. Safety independently blocks `ac_solar` on voltage or heat, and this agent
neither knows nor needs to know that — a block outvotes every requester.
"""
from collections import defaultdict
from datetime import timedelta

from ..config import now
from ..coordinator import Booking
from .base import AgentResult, BaseAgent

HOUR = timedelta(hours=1)


def _windows_for_day(hours: list, max_windows: int) -> list:
    """Group hourly timestamps into at most *max_windows* (start, end) intervals.

    `hours` is sorted and each entry stands for the hour beginning at it, so `end` is
    exclusive — the plug goes off at exactly that time. Adjacent hours form one run;
    while there are too many runs the pair separated by the smallest gap is merged,
    which is the cheapest possible concession in yield-hours per switch saved.
    """
    windows = []
    for t in hours:
        if windows and windows[-1][1] == t:
            windows[-1][1] = t + HOUR
        else:
            windows.append([t, t + HOUR])

    while len(windows) > max_windows:
        gaps = [windows[i + 1][0] - windows[i][1] for i in range(len(windows) - 1)]
        i = gaps.index(min(gaps))
        windows[i][1] = windows[i + 1][1]
        del windows[i + 1]

    return [(start, end) for start, end in windows]


class AC_SolarScheduler(BaseAgent):
    name = "forecast_ac_solar"
    interval_seconds = 300      # the forecast moves hourly; this is cheap either way

    def run(self, state, projection, config) -> AgentResult:
        forecast = projection.forecast
        if forecast is None:
            return AgentResult(
                agent_name=self.name,
                rationale="inactive — no solar forecast available",
            )

        cfg = config.agents.forecast_ac_solar
        t_now = now()
        # The hour we are *in* still counts: a window that began at 12:00 is still
        # worth booking at 12:30, and `Booking.covers` says so.
        this_hour = t_now.replace(minute=0, second=0, microsecond=0)

        yield_w = {e.time: e.mppt_AC_w for e in forecast.entries}
        by_day = defaultdict(list)
        for entry in forecast.entries:
            if entry.time >= this_hour and entry.mppt_AC_w >= cfg.min_yield_w:
                by_day[entry.time.date()].append(entry.time)

        bookings = []
        for day in sorted(by_day):
            hours = sorted(by_day[day])
            for start, end in _windows_for_day(hours, max(1, cfg.max_windows_per_day)):
                # Hourly entries, so watts and watt-hours are the same number.
                wh = sum(w for t, w in yield_w.items() if start <= t < end)
                bookings.append(Booking(
                    mode="ac_solar", start=start, end=end,
                    reason=f"balcony forecast {wh:.0f}Wh "
                           f"(>= {cfg.min_yield_w:.0f}W)",
                ))

        in_window = any(b.covers(t_now) for b in bookings)
        total_wh = sum(w for t, w in yield_w.items()
                       if any(b.start <= t < b.end for b in bookings))

         #realtime assessment of actual AC solar input
         
        average_recent_AC_power = state.averages.get("ac_inverter/power_w", None)
         
        if average_recent_AC_power is not None and average_recent_AC_power > 75:
             
            #make short-term booking to continue AC solar, independent of 
            # planing based on the forecast. Make it lasting for the duration
            # of the interval and one minute more to ensure next evaluation
            # is still with the current realtime booking
            booking = Booking(
                 mode='ac_solar', 
                 start=t_now, 
                 end=t_now + timedelta(seconds =AC_SolarScheduler.interval_seconds + 60),
                 reason = f'Average recent AC solar is sufficient high({average_recent_AC_power:.0f}W)'
                 )
            bookings.append(booking)
     
        if bookings:
            window_strs = [f"{b.start.strftime('%m-%d %H:%M')}–{b.end.strftime('%H:%M')}"
                           for b in bookings]
            rationale = (f"{'ON' if in_window else 'OFF'} — {len(bookings)} window(s): "
                         f"{', '.join(window_strs)}; balcony {total_wh:.0f}Wh")
        else:
            rationale = (f"no windows — balcony forecast never reaches "
                         f"{cfg.min_yield_w:.0f}W in the horizon")
        
       
            
        return AgentResult(
            agent_name=self.name,
            rationale=rationale,
            metrics={
                "min_yield_w": cfg.min_yield_w,
                "max_windows_per_day": cfg.max_windows_per_day,
                "planned_windows": len(bookings),
                "total_balcony_wh": round(total_wh),
            },
            bookings=bookings,
        )
