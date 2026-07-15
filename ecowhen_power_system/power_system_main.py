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
from . import discovery, paths, poll

# Past this age a persisted SOC estimate is discarded and SOC is re-derived from
# open-circuit voltage: coulomb counting cannot account for a gap it did not see.
MAX_SOC_STATE_AGE = timedelta(hours=1)


def get_bus():
    """Import pydbus lazily so the package stays importable off-device.

    pydbus needs the system gobject bindings, which only exist on the Venus OS box.
    Everything else here takes `bus` as an argument, so the tests pass a fake and
    never reach this function.
    """
    import os

    if os.environ.get("VICTRON_TEST_SESSION_BUS"):
        from pydbus import SessionBus
        return SessionBus()
    from pydbus import SystemBus
    return SystemBus()


def setup(bus, tz, debug=False):
    """Discover the topology, publish it, and prepare the estimator."""
    paths.ensure_dirs()

    system_config, psystem = discovery.discover(bus, config)
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

    if config.simulate_system:
        from . import simulation

        simulator = simulation.System_Simulation(config.batt_config_V1, debug)
        soc_state = poll.load_soc_state(paths.SOC_STATE_PATH)
        if poll.restore_simulator(simulator, soc_state, MAX_SOC_STATE_AGE, t_now):
            soc_tracker.since = soc_state["soc_above_threshold_since"]
            print(f"[power_system] restored SOC={soc_state['soc']:.1%} "
                  f"from {paths.SOC_STATE_PATH}")

    return system_config, psystem, simulator, soc_tracker


def run_loop(bus, system_config, psystem, simulator, soc_tracker, tz, debug=False):
    running_since = datetime.now(tz=tz)

    while True:
        t_now = datetime.now(tz=tz)

        state = poll.build_state(
            bus, psystem, system_config, config, simulator, soc_tracker,
            t_now, running_since, debug,
        )

        if state is not None:
            poll.save_state(paths.STATE_PATH, state)
            if simulator is not None:
                poll.save_soc_state(
                    paths.SOC_STATE_PATH,
                    soc=state["SOC_counted"],
                    rc_voltage=float(simulator.Kf.x[1, 0]),
                    t_now=t_now,
                    soc_above_threshold_since=soc_tracker.since,
                )

        elapsed = (datetime.now(tz=tz) - t_now).total_seconds()
        time.sleep(max(0, config.log_interval - elapsed))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Power System — the system model")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--once", action="store_true",
        help="discover, poll once, write both contracts, exit",
    )
    args = parser.parse_args(argv)

    tz = pytz.timezone(config.tz)
    bus = get_bus()

    system_config, psystem, simulator, soc_tracker = setup(bus, tz, args.debug)

    if args.once:
        t_now = datetime.now(tz=tz)
        state = poll.build_state(
            bus, psystem, system_config, config, simulator, soc_tracker,
            t_now, t_now, args.debug,
        )
        if state is None:
            print("[power_system] poll failed")
            return 1
        poll.save_state(paths.STATE_PATH, state)
        print(f"[power_system] wrote {paths.STATE_PATH}")
        return 0

    print(f"[power_system] polling every {config.log_interval}s")
    run_loop(bus, system_config, psystem, simulator, soc_tracker, tz, args.debug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
