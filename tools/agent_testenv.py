#!/usr/bin/env python3
"""Standalone test environment for control agents.

Syncs the two live directories from the Venus box (the ephemeral `runtime` one that
holds `state.json`, and the durable `data` one that holds `control_config.yaml`),
fetches the solar forecast, runs one agent, and plots what it decided.

Ported from the predecessor's `agent_testenv.py`. What changed with the new agents:

* An agent no longer emits actuator writes. It emits `requests` / `blocks` /
  `bookings` / `commands` / `inhibit`, so the summary prints those and the wallbox
  ON-windows are read straight off the `Booking`s for mode `wallbox`. The old
  `<agent>_schedule.json` fallback is gone — `wallbox_optimal_charge` publishes its
  plan as bookings now, so there is nothing left to reconstruct.
* State comes from `state.json` in RUNTIME_DIR, not from the data dir, and its
  freshness check is disabled: a synced file is minutes old by definition.

Usage:
    uv run python tools/agent_testenv.py                       # forecast_wallbox
    uv run python tools/agent_testenv.py --agent soc_wallbox_charge
    uv run python tools/agent_testenv.py --no-sync             # use local copies
    uv run python tools/agent_testenv.py --soc 0.5
    uv run python tools/agent_testenv.py --save plot.png
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_AGENTS = ["forecast_wallbox", "soc_wallbox_charge", "wallbox_optimal_charge",
           "forecast_ac_solar", "system_safety"]

# Where the sync lands. Not the real RUNTIME_DIR/DATA_DIR: this is somebody's laptop
# and a live `data/` here would be a different installation's.
_SYNC_DIR = _ROOT / "testenv"

# Mirrors deploy/config.sh, same env var names.
_DEVICE = os.environ.get("ECOPS_DEVICE", "root@192.168.1.5")
_REMOTE_DATA = os.environ.get("ECOPS_REMOTE_DATA",
                              "/data/python/ecowhen_power_system/data")
_REMOTE_RUNTIME = os.environ.get("ECOPS_REMOTE_RUNTIME",
                                 "/dev/shm/ecowhen_power_system")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Control agent test environment")
    p.add_argument("--agent", choices=_AGENTS, default="forecast_wallbox")
    p.add_argument("--no-sync", action="store_true",
                   help="Skip rsync, use existing local copies")
    p.add_argument("--soc", type=float, default=None, metavar="0-1",
                   help="Override current SOC (0-1)")
    p.add_argument("--load-w", type=float, default=None, dest="load_w",
                   help="Override estimated base load in W")
    p.add_argument("--wallbox-w", type=float, default=None, dest="wallbox_w",
                   help="Override wallbox power in W")
    p.add_argument("--min-solar-fraction", type=float, default=None,
                   dest="min_solar_fraction",
                   help="Override forecast_wallbox.min_solar_fraction (0-1)")
    p.add_argument("--save", metavar="FILE", default=None,
                   help="Save plot to FILE instead of showing interactively")
    return p.parse_args()


def _rsync(remote: str, local: Path) -> None:
    local.mkdir(parents=True, exist_ok=True)
    print(f"[sync] {remote} -> {local} ...")
    result = subprocess.run(
        ["rsync", "-az", "--timeout=15", "--exclude=control_config.yaml",
         f"{_DEVICE}:{remote}/", str(local)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[sync] WARNING rsync failed:\n{result.stderr.strip()}")
        print("[sync] continuing with existing local copy")


def _apply_overrides(config, args) -> None:
    if args.load_w is not None:
        config.estimated_load_w = args.load_w
    if args.wallbox_w is not None:
        config.agents.forecast_wallbox.wallbox_power_w = args.wallbox_w
        config.agents.wallbox_optimal_charge.wallbox_power_w = args.wallbox_w
    if args.min_solar_fraction is not None:
        config.agents.forecast_wallbox.min_solar_fraction = args.min_solar_fraction
    # Agents default to enabled=false; the one under test runs regardless.
    getattr(config.agents, args.agent).enabled = True


def _time_tag(t, now) -> str:
    dt = (t - now).total_seconds()
    if abs(dt) < 60:
        return "  NOW "
    if dt > 0:
        h, m = divmod(int(dt / 60), 60)
        return f"+{h:02d}h{m:02d}"
    return t.strftime("%m-%d %H:%M")


def _print_summary(result, now) -> None:
    sep = "=" * 70
    print(f"\n{sep}")
    print(f"AGENT : {result.agent_name}")
    print(f"        {result.rationale}")
    if result.metrics:
        print("\nMETRICS:")
        for k, v in result.metrics.items():
            print(f"  {k:<32} {v:.3f}" if isinstance(v, float) else f"  {k:<32} {v}")

    print(f"\nREQUESTS ({len(result.requests)}):" if result.requests
          else "\nREQUESTS: none")
    for r in result.requests:
        print(f"  mode {r.mode:<12} {'WANT' if r.active else 'DROP':<4}  {r.reason}")

    print(f"\nBLOCKS ({len(result.blocks)}):" if result.blocks else "\nBLOCKS: none")
    for b in result.blocks:
        print(f"  mode {b.mode:<12} FORBID  {b.reason}")

    print(f"\nBOOKINGS ({len(result.bookings)}):" if result.bookings
          else "\nBOOKINGS: none")
    for b in sorted(result.bookings, key=lambda x: x.start):
        print(f"  [{_time_tag(b.start, now)}]  mode {b.mode:<12} "
              f"until {b.end.strftime('%m-%d %H:%M')}  {b.reason}")

    print(f"\nCOMMANDS ({len(result.commands)}):" if result.commands
          else "\nCOMMANDS: none")
    for c in result.commands:
        print(f"  {c.actuator}={'ON' if c.on else 'OFF':<4}  {c.reason}")

    if result.inhibit:
        print(f"\nINHIBIT: {result.inhibit}")
    print(sep)


def _mode_windows(result, now, horizon_end) -> tuple:
    """(mode, [(start, end), ...]) — the intervals this agent wants its mode in.

    The mode is read off the agent's own output rather than declared here, so an
    agent that plans `ac_solar` needs no entry in this file. Bookings carry their
    interval; a live `ModeRequest` carries no end — it means "now, until I say
    otherwise" — so it is drawn to the horizon.
    """
    modes = ([b.mode for b in result.bookings]
             + [r.mode for r in result.requests if r.active])
    if not modes:
        return None, []
    mode = modes[0]
    windows = [(b.start, b.end) for b in result.bookings if b.mode == mode]
    if any(r.mode == mode and r.active for r in result.requests):
        windows.append((now, horizon_end))
    return mode, sorted(windows)


def _extra_load_fn(windows, wallbox_w: float, efficiency: float):
    wallbox_dc_w = wallbox_w / efficiency

    def extra_load(t) -> float:
        return wallbox_dc_w if any(s <= t < e for s, e in windows) else 0.0

    return extra_load


def _plot(state, forecast, projection, projection_scheduled, result, config, args,
          mode, windows, now) -> None:
    try:
        import matplotlib
        if args.save:
            matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        print("WARNING: matplotlib not available — skipping plot.")
        print("  Install with: uv pip install matplotlib")
        return

    step_times = [s.time for s in projection.steps]
    step_socs = [s.projected_soc * 100 for s in projection.steps]
    step_solar = [s.solar_w for s in projection.steps]
    sched_socs = [s.projected_soc * 100 for s in projection_scheduled.steps]

    x_end = step_times[-1] if step_times else now + timedelta(hours=config.horizon_hours)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 7), sharex=True)
    fig.suptitle(
        f"Agent: {result.agent_name}  |  "
        f"SOC={state.soc:.1%}  solar={state.solar_power_w:.0f}W  "
        f"load_est={config.estimated_load_w:.0f}W  "
        f"[{now.strftime('%Y-%m-%d %H:%M')}]",
        fontsize=10, fontweight="bold",
    )

    # -- Panel 1: solar ----------------------------------------------------------
    if forecast is not None:
        fc_times = [e.time for e in forecast.entries]
        fc_vals = [e.total_w for e in forecast.entries]
        ax1.bar(fc_times, fc_vals, width=timedelta(hours=0.85),
                color="gold", alpha=0.65, label="Forecast (hourly)")
        fc_now = forecast.get_power(now)
        bias = min(3.0, max(0.1, state.solar_power_w / fc_now)) if fc_now > 50 else 1.0
        bias_end = now + timedelta(hours=2)
        bt = [e.time for e in forecast.entries if now <= e.time <= bias_end]
        bv = [e.total_w * bias for e in forecast.entries if now <= e.time <= bias_end]
        if bt:
            ax1.plot(bt, bv, "b--", lw=1.5, label=f"Bias-adjusted ({bias:.2f}x)")
        # The balcony share — the only part of the forecast the Deye can harvest, so
        # it is what `forecast_ac_solar` thresholds against.
        ax1.plot(fc_times, [e.mppt_AC_w for e in forecast.entries],
                 color="seagreen", lw=1.2, alpha=0.9, label="Balcony (AC) forecast")
    threshold = getattr(getattr(config.agents, args.agent), "min_yield_w", None)
    if threshold:
        ax1.axhline(threshold, color="seagreen", ls="--", lw=1.2,
                    label=f"Threshold ({threshold:.0f} W)")
    ax1.plot(step_times, step_solar, color="darkorange", lw=1, alpha=0.8,
             label="Interp. solar (15 min)")
    ax1.scatter([now], [state.solar_power_w], color="red", zorder=5, s=70,
                label=f"Actual now ({state.solar_power_w:.0f} W)")
    ax1.axvline(now, color="gray", lw=1, ls=":")
    for ws, we in windows:
        ax1.axvspan(ws, we, color="green", alpha=0.12)
    ax1.set_ylabel("Solar power (W)")
    ax1.legend(loc="upper right", fontsize=8)
    ax1.grid(True, alpha=0.3)

    # -- Panel 2: SOC projection -------------------------------------------------
    ax2.plot(step_times, step_socs, color="steelblue", lw=1.5, ls="--", alpha=0.5,
             label="SOC (unloaded)")
    ax2.plot(step_times, sched_socs, color="steelblue", lw=2,
             label="SOC (with the plan)")
    ax2.axhline(config.battery.min_soc * 100, color="red", ls="--", lw=1.2,
                label=f"Min SOC ({config.battery.min_soc:.0%})")
    ax2.scatter([now], [state.soc * 100], color="orange", zorder=5, s=70,
                label=f"Current SOC ({state.soc:.1%})")
    ax2.axvline(now, color="gray", lw=1, ls=":")
    for i, (ws, we) in enumerate(windows):
        ax2.axvspan(ws, we, color="green", alpha=0.20,
                    label=f"Mode {mode} wanted" if i == 0 else None)
    ax2.set_ylabel("Battery SOC (%)")
    ax2.set_ylim(0, 105)
    ax2.legend(loc="upper right", fontsize=8)
    ax2.grid(True, alpha=0.3)

    ax2.set_xlim(now, x_end)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d\n%H:%M"))
    ax2.xaxis.set_major_locator(mdates.HourLocator(interval=4))
    fig.autofmt_xdate(rotation=0, ha="center")
    plt.tight_layout()

    if args.save:
        plt.savefig(args.save, dpi=150, bbox_inches="tight")
        print(f"\n[plot] saved to {args.save}")
    else:
        plt.show()


def _build_agent(name, data_dir):
    if name == "forecast_wallbox":
        from ecowhen_power_system.control.agents.forecast_wallbox import ForecastWallboxAgent
        return ForecastWallboxAgent()
    if name == "soc_wallbox_charge":
        from ecowhen_power_system.control.agents.soc_wallbox_charge import SocWallboxChargeAgent
        return SocWallboxChargeAgent()
    if name == "wallbox_optimal_charge":
        from ecowhen_power_system.control.agents.wallbox_optimal_charge import WallboxOptimalChargeAgent
        return WallboxOptimalChargeAgent(data_dir)
    if name == "ac_solar_scheduler":
        from ecowhen_power_system.control.agents.forecast_ac_solar import AC_SolarScheduler
        return AC_SolarScheduler()
    from ecowhen_power_system.control.agents.system_safety import SystemSafetyAgent
    return SystemSafetyAgent()


def main() -> None:
    args = _parse_args()

    data_dir = _SYNC_DIR / "data"
    runtime_dir = _SYNC_DIR / "runtime"
    # paths.py reads these at import, so they have to be set before anything in the
    # package is imported — which is why the imports below are inside main().
    os.environ["ECOPS_DATA_DIR"] = str(data_dir)
    os.environ["ECOPS_RUNTIME_DIR"] = str(runtime_dir)
    sys.path.insert(0, str(_ROOT))

    if not args.no_sync:
        _rsync(_REMOTE_DATA, data_dir)
        _rsync(_REMOTE_RUNTIME, runtime_dir)

    from dotenv import load_dotenv
    load_dotenv()

    from ecowhen_power_system import paths
    from ecowhen_power_system.control.config import ControlConfig, now
    from ecowhen_power_system.control.forecast import SolarForecastProvider
    from ecowhen_power_system.control.projection import BatteryProjector
    from ecowhen_power_system.control.state import (StateUnavailableError,
                                                    read_current_state)

    config = ControlConfig.load_or_default(paths.CONTROL_CONFIG_PATH)
    _apply_overrides(config, args)

    # -- state -------------------------------------------------------------------
    print("[state] reading ...")
    try:
        # A synced state.json is stale by definition; the age check is the live
        # process's safety net, not this one's.
        state = read_current_state(max_age_seconds=float("inf"))
    except StateUnavailableError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    if args.soc is not None:
        state = dataclasses.replace(state, soc=args.soc)
        print(f"[state] SOC overridden -> {args.soc:.1%}")

    print(f"[state] SOC={state.soc:.1%}  solar={state.solar_power_w:.0f}W  "
          f"load={state.ac_load_w:.0f}W  temp={state.battery_temp:.1f}C  "
          f"ts={state.timestamp.strftime('%m-%d %H:%M:%S')} "
          f"({state.age_seconds:.0f}s old)")

    # -- forecast ----------------------------------------------------------------
    print("[forecast] fetching ...")
    forecast = SolarForecastProvider(config.forecast).get()
    t_now = now()
    if forecast is None:
        print("[forecast] unavailable — projection will use zero solar")
    else:
        up = [e for e in forecast.entries if e.time >= t_now][:4]
        print(f"[forecast] fetched {forecast.fetched_at.strftime('%H:%M')}  "
              + "  ".join(f"{e.time.strftime('%H:%M')}={e.total_w:.0f}W" for e in up))

    # -- projection --------------------------------------------------------------
    print("[projection] computing ...")
    projector = BatteryProjector(config)
    projection = projector.project(state, forecast)
    print(f"[projection] {len(projection.steps)} steps x 15 min = "
          f"{config.horizon_hours}h horizon")

    # -- agent -------------------------------------------------------------------
    print(f"[agent] running {args.agent} ...")
    result = _build_agent(args.agent, data_dir).run(projection, config)

    horizon_end = (projection.steps[-1].time if projection.steps
                   else t_now + timedelta(hours=config.horizon_hours))
    mode, windows = _mode_windows(result, t_now, horizon_end)

    cfg = getattr(config.agents, args.agent)
    wallbox_w = getattr(cfg, "wallbox_power_w", None)
    if windows and wallbox_w:
        print("[projection] re-computing with the wallbox load ...")
        projection_scheduled = projector.project(
            state, forecast,
            _extra_load_fn(windows, wallbox_w, cfg.inverter_efficiency))
    else:
        # ponytail: only the wallbox agents declare a load to price. The Deye's draw
        # is not configured anywhere and its yield lands on the AC side, so
        # forecast_ac_solar's effect on SOC is not something this can honestly draw.
        projection_scheduled = projection

    _print_summary(result, t_now)
    _plot(state, forecast, projection, projection_scheduled, result, config, args,
          mode, windows, t_now)


if __name__ == "__main__":
    main()
