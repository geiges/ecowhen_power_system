"""① Power System — the system model.

Starts first. Discovers the topology once, then polls forever, publishing both
contracts:

    system_configuration.yaml   what is out there and how to reach it
    state.json                  what it is doing right now

Reads D-Bus and the aux HTTP devices. Writes neither — actuation is the Gateway's
job, and this process holds no path to it. That is deliberate: exactly one process
writes hardware.
"""
import argparse
import sys
import time
from datetime import datetime, timedelta

import pytz

from . import config_default as config
from . import discovery, health, paths, poll
from .manufacturer.victron import get_bus

# Past this age a persisted SOC estimate is discarded and SOC is re-derived from
# open-circuit voltage: coulomb counting cannot account for a gap it did not see.
MAX_SOC_STATE_AGE = timedelta(hours=1)


def setup(bus, tz, debug=False, run_startup_sequence=True):
    """Discover the topology, publish it, and prepare the estimator."""
    paths.ensure_dirs()

    system_config, psystem = discovery.discover(bus, config, run_startup_sequence)
    discovery.save_system_configuration(system_config, paths.SYSTEM_CONFIG_PATH)

    unavailable = [
        name for name, c in system_config["components"].items() if not c["available"]
    ]
    if unavailable:
        print(f"[power_system] not on the bus: {', '.join(unavailable)}")
    print(f"[power_system] wrote {paths.SYSTEM_CONFIG_PATH}")

    t_now = datetime.now(tz=tz)
    simulator = None
    soc_tracker = poll.SocTracker()
    state_averager = poll.Averager(config)

    if config.simulate_system:
        from . import battery_simulation

        simulator = battery_simulation.Battery_Simulation(config.battery, debug)
        soc_state = poll.load_soc_state(paths.SOC_STATE_PATH)
        if poll.restore_simulator(simulator, soc_state, MAX_SOC_STATE_AGE, t_now):
            soc_tracker.above_threshold_since = soc_state["soc_above_threshold_since"]
            print(f"[power_system] restored SOC={soc_state['soc']:.1%} "
                  f"from {paths.SOC_STATE_PATH}")

    return system_config, psystem, simulator, soc_tracker, state_averager


def run_loop(bus, system_config, psystem, simulator, soc_tracker, state_averager, 
             tz, debug=False):
    running_since = datetime.now(tz=tz)

    while True:
        t_now = datetime.now(tz=tz)

        state = poll.build_state(
            bus, psystem, system_config, config, simulator, soc_tracker,
            state_averager, t_now, running_since, debug,
        )

        if state is not None:
            poll.save_state(paths.STATE_PATH, state)
            if simulator is not None:
                poll.save_soc_state(
                    paths.SOC_STATE_PATH,
                    soc=state["SOC_counted"],
                    rc_voltage=float(simulator.Kf.x[1, 0]),
                    t_now=t_now,
                    soc_above_threshold_since=soc_tracker.above_threshold_since,
                )

        # A poll that produced no state is `error`, not silence: the process is
        # alive and state.json is simply not being refreshed, which is the failure
        # every other process is about to trip over.
        health.beat("power_system", config.log_interval, datetime.now(tz=tz),
                    ok=state is not None,
                    detail="" if state is not None else "poll produced no state")

        elapsed = (datetime.now(tz=tz) - t_now).total_seconds()
        time.sleep(max(0, config.log_interval - elapsed))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Power System — the system model")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--once", action="store_true",
        help="discover, poll once, write both contracts, exit",
    )
    parser.add_argument(
        "--no-startup-sequence", action="store_true",
        help="never write D-Bus — skip the Multiplus measurement wake-up (A11). "
             "Required while another system is live on this bus.",
    )
    args = parser.parse_args(argv)

    tz = pytz.timezone(config.tz)
    bus = get_bus()

    if args.no_startup_sequence:
        print("[power_system] startup sequence disabled — this process will not "
              "write D-Bus at all")

    system_config, psystem, simulator, soc_tracker, state_averager = setup(
        bus, tz, args.debug, run_startup_sequence=not args.no_startup_sequence
    )

    if args.once:
        t_now = datetime.now(tz=tz)
        state = poll.build_state(
            bus, psystem, system_config, config, simulator, soc_tracker,
            state_averager, t_now, t_now, args.debug,
        )
        if state is None:
            print("[power_system] poll failed")
            return 1
        poll.save_state(paths.STATE_PATH, state)
        print(f"[power_system] wrote {paths.STATE_PATH}")
        return 0

    print(f"[power_system] polling every {config.log_interval}s")
    run_loop(bus, system_config, psystem, simulator, soc_tracker, state_averager, 
             tz, args.debug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
    