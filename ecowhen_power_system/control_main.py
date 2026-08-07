"""④ Control — the brain. No hardware, no D-Bus, no files it also reads back.

Inputs:  `state.json` (the Power System), `mode_requests.json` + `control_config.yaml`
         (the Gateway's REST surface), the solar forecast (computepi).
Outputs: `modes_status.json`, `services_status.json`, `control_log.jsonl`, and HTTP
         POSTs to the Gateway — which is the only thing in this system that writes
         hardware.

`grep -rn "dbus-send\\|pydbus" control/` returns nothing, and that is a property of
the design rather than a habit: the only actuation path out of this process is
`GatewayClient`.

One cycle, in order:

    reload config → read state → safety → forecast+projection → agents → tick

Safety runs every cycle. Every other agent runs on its own `interval_seconds`, and
the ones that did not run this cycle still speak through their latched last answer —
see `_run_planning`, where that latch is the reason an interval is safe at all.

The tick is `Coordinator.tick`, which is where ordering lives: modes declare what
they need, services converge, modes then act on services that are already satisfied.
Nothing in this file knows that the wallbox needs the inverter first.
"""
import argparse
import sys
import time

from dotenv import load_dotenv

from . import paths
from .control import actuation
from .control.agents.forecast_wallbox import ForecastWallboxAgent
from .control.agents.soc_wallbox_charge import SocWallboxChargeAgent
from .control.agents.system_safety import SystemSafetyAgent
from .control.agents.wallbox_optimal_charge import WallboxOptimalChargeAgent
from .control.config import ControlConfig, now
from .control.coordinator import Coordinator
from .control.decision_log import DecisionLog, build_cycle_entry
from .control.forecast import SolarForecastProvider
from .control.gateway_client import GatewayClient
from .control.projection import (BatteryProjector, SystemProjection,
                                 save_projection_csv)
from .control.state import StateUnavailableError, read_current_state


class ControlUnit:
    """One process's worth of state: the coordinator, the agents, the clocks."""

    def __init__(self, config: ControlConfig, gateway=None):
        self.config = config
        self.gateway = gateway if gateway is not None else GatewayClient(
            config.gateway.base_url, config.gateway.timeout_seconds)

        self.services, self.modes = actuation.build(self.gateway, config)
        self.coordinator = Coordinator(
            self.services, self.modes,
            mode_requests_path=paths.MODE_REQUESTS_PATH,
            modes_status_path=paths.MODES_STATUS_PATH,
            services_status_path=paths.SERVICES_STATUS_PATH,
            schedule_path=paths.MODE_SCHEDULE_PATH,
        )

        self.safety = SystemSafetyAgent()
        self.agents = [
            SocWallboxChargeAgent(),
            ForecastWallboxAgent(),
            WallboxOptimalChargeAgent(),
        ]
        self.forecast_provider = SolarForecastProvider(config.forecast)
        self.log = DecisionLog(paths.DECISION_LOG_PATH, config.log_max_entries)

        self._last_planning = None
        self._last_run = {}          # agent name -> when it last ran
        self._last_output = {}       # agent name -> its latest (requests, blocks)
        self._safety_requests = []
        self._safety_blocks = []

    # --- one cycle ----------------------------------------------------------

    def reload_config(self) -> None:
        """Pick up REST edits. The objects survive; only their knobs move.

        Rebuilding the services here would throw away exactly what they exist for —
        the attempt counter and the set of requirers.
        """
        try:
            self.config = ControlConfig.load_or_default(paths.CONTROL_CONFIG_PATH)
        except Exception as exc:
            print(f"[control] config unreadable ({exc}) — keeping the loaded one")
            return
        self.services["multiplus_ac"].default_on = self.config.services.multiplus_ac_default_on
        self.services["mppt100_dc_load"].default_on = self.config.services.mppt100_dc_load_default_on
        # Or it would read as configured and need a restart to take effect.
        self.log.max_entries = self.config.log_max_entries

    def cycle(self, t_now=None) -> dict:
        self.reload_config()
        cfg = self.config
        # One clock for the whole cycle: the planning interval, and the instant the
        # coordinator resolves bookings at, are the same reading.
        t_now = t_now or now()

        try:
            state = read_current_state(max_age_seconds=cfg.state_max_age_seconds)
        except StateUnavailableError as exc:
            # A missing or frozen state.json is a safety event, not a quiet skip: we
            # have no measurements, so nothing may keep running on the last opinion.
            print(f"[control] {exc}")
            self.coordinator.inhibit(f"state unavailable: {exc}")
            return self.coordinator.tick(cfg, t_now)

        projection = self._project(state)

        if self.safety.is_enabled(cfg):
            self._run_safety(projection, cfg)
        else:
            print("[control] SAFETY AGENT DISABLED (enabled=false + confirmed_disable)")
            self.coordinator.clear_inhibit()

        requests = list(self._safety_requests)
        blocks = list(self._safety_blocks)
        planned_requests, planned_blocks = self._run_planning(projection, cfg, t_now)
        requests += planned_requests
        blocks += planned_blocks

        self.coordinator.set_agent_requests(requests)
        self.coordinator.set_agent_blocks(blocks)
        status = self.coordinator.tick(cfg, t_now)

        self.log.append(build_cycle_entry(state, projection, status))
        return status

    def _project(self, state) -> SystemProjection:
        """The forward view. Never fatal: safety must run without one."""
        forecast = self.forecast_provider.get()
        try:
            return BatteryProjector(self.config).project(state, forecast)
        except Exception as exc:
            print(f"[control] projection failed: {exc}")
            return SystemProjection(current=state, forecast=forecast,
                                    horizon_hours=self.config.horizon_hours, steps=[])

    def _run_safety(self, projection, cfg) -> None:
        try:
            result = self.safety.run(projection, cfg)
        except Exception as exc:
            # Not silent, and not fatal: a safety agent that raised has told us
            # nothing, so the modes stand down until it speaks again.
            print(f"[control] safety agent raised: {exc}")
            self.coordinator.inhibit(f"safety agent error: {exc}")
            self._safety_requests = []
            self._safety_blocks = []
            return

        print(f"[{result.agent_name}] {result.rationale}")
        self.log.append_agent_result(result)
        self._safety_requests = result.requests
        self._safety_blocks = result.blocks

        for command in result.commands:
            print(f"[control] SAFETY: {command.actuator} -> "
                  f"{'on' if command.on else 'off'} ({command.reason})")
            self.gateway.command(command.actuator, command.on)

        if result.inhibit:
            self.coordinator.inhibit(result.inhibit)
        else:
            self.coordinator.clear_inhibit()

    def _run_planning(self, projection, cfg, t_now) -> tuple:
        """Ask each agent that is due, and answer for all of them.

        Every agent runs on its own clock — `interval_seconds`, 0 meaning every
        tick — so a planner that costs several projections is not paid for on the
        safety cadence. The clock lives here rather than on the agent, which keeps
        `run` a pure function of `(projection, config)`.

        Returns `(requests, blocks)` for *every* enabled agent, not only the ones
        that ran this tick. That latch is what makes an interval safe: the
        coordinator resolves requests wholesale each tick, so an agent absent from
        this list has released its modes. Without the latch a five-minute agent
        would release the wallbox on the twenty-nine ticks between its runs and
        re-request it on the thirtieth. An agent that did not run has not fallen
        silent — its last answer stands until it speaks again, which is exactly how
        its bookings already behaved.

        Silence therefore still releases; it is now measured from the agent's own
        last run. Three things still end an agent's say, and all three drop the
        latched answer along with the plan: its interval elapsing with a new answer,
        being disabled, and raising.
        """
        requests, blocks = [], []
        for agent in self.agents:
            if not agent.is_enabled(cfg):
                self._forget(agent.name)
                continue

            last = self._last_run.get(agent.name)
            if last is None or (t_now - last).total_seconds() >= agent.interval(cfg):
                self._last_run[agent.name] = t_now
                try:
                    result = agent.run(projection, cfg)
                except Exception as exc:
                    # It has told us nothing, so it no longer speaks — for the
                    # future via its plan, or for now via its latched answer.
                    print(f"[control] agent {agent.name} raised: {exc}")
                    self._forget(agent.name)
                    continue
                print(f"[{result.agent_name}] {result.rationale}")
                self.log.append_agent_result(result)
                self._last_output[agent.name] = (result.requests, result.blocks)
                self.coordinator.set_plan(agent.name, result.bookings, t_now)

            latched = self._last_output.get(agent.name)
            if latched is not None:
                requests += latched[0]
                blocks += latched[1]

        due = (self._last_planning is None or
               (t_now - self._last_planning).total_seconds() >= cfg.control_interval_seconds)
        if due:
            self._last_planning = t_now
            if projection.steps:
                save_projection_csv(projection, paths.PROJECTION_PATH)
                print(BatteryProjector(cfg).summarise(projection))

        return requests, blocks

    def _forget(self, agent_name: str) -> None:
        """Drop everything an agent still speaks through: its plan and its latch.

        Both, or half of a disabled agent's opinion keeps driving hardware.
        """
        self.coordinator.drop_plan(agent_name)
        self._last_output.pop(agent_name, None)

    # --- the loop -----------------------------------------------------------

    def run_forever(self) -> None:
        intervals = ", ".join(f"{a.name}={a.interval(self.config)}s"
                              for a in self.agents)
        print(f"[control] starting — cycle={self.config.safety_interval_seconds}s, "
              f"agents: {intervals}, "
              f"gateway={self.config.gateway.base_url}")
        while True:
            t_start = now()
            try:
                self.cycle()
            except Exception as exc:
                print(f"[control] cycle failed: {exc}")
            elapsed = (now() - t_start).total_seconds()
            time.sleep(max(0.0, self.config.safety_interval_seconds - elapsed))


def _dry_run(unit: ControlUnit) -> int:
    """Read everything, decide everything, command nothing."""
    try:
        state = read_current_state(max_age_seconds=unit.config.state_max_age_seconds)
    except StateUnavailableError as exc:
        print(f"state UNAVAILABLE: {exc}")
        return 1

    print(f"state       : {state.timestamp.isoformat()} ({state.age_seconds:.0f}s old)")
    print(f"  SOC       : {state.soc:.1%}   full for {state.minutes_at_full_soc:.0f} min")
    print(f"  battery   : {state.battery_voltage:.2f} V  {state.battery_current:+.2f} A  "
          f"{state.battery_temp:.1f} °C")
    print(f"  solar     : {state.solar_power_w:.0f} W    AC load {state.ac_load_w:.0f} W")

    projection = unit._project(state)
    print(f"forecast    : {'available' if projection.forecast is not None else 'UNAVAILABLE'}")
    if projection.steps:
        print(BatteryProjector(unit.config).summarise(projection))

    for agent in [unit.safety] + unit.agents:
        if not agent.is_enabled(unit.config):
            print(f"[{agent.name}] disabled")
            continue
        result = agent.run(projection, unit.config)
        print(f"[{result.agent_name}] {result.rationale}")
        for r in result.requests:
            print(f"    wants mode {r.mode}={r.active} ({r.reason})")
        for b in result.bookings:
            print(f"    books mode {b.mode} {b.start:%m-%d %H:%M}–{b.end:%H:%M} "
                  f"({b.reason})")
        for c in result.commands:
            print(f"    would command {c.actuator} -> {'on' if c.on else 'off'}")
    return 0


def main(argv=None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Control unit")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="decide out loud, command nothing")
    args = parser.parse_args(argv)

    paths.ensure_dirs()
    config = ControlConfig.load_or_default(paths.CONTROL_CONFIG_PATH)
    unit = ControlUnit(config)

    if args.dry_run:
        return _dry_run(unit)

    if args.once:
        status = unit.cycle()
        print(f"[control] modes={ {n: m['state'] for n, m in status['modes'].items()} } "
              f"services={ {n: s['phase'] for n, s in status['services'].items()} }")
        return 0

    unit.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
