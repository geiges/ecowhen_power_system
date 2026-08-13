"""③ Logger — the process.

Watches `state.json` and turns every new poll into three CSV rows, plus one daily
aggregate row per completed day.

It reads only the two contracts and writes only into DATA_DIR. Nothing reads what it
writes, so it can fall behind, be restarted, or be switched off entirely without any
other process noticing — which is exactly what "pure sink" buys.

    ecops-logger [--once]
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta

import pytz

from . import aggregation, config_default as config
from . import discovery, health, logger, paths

# How far behind state.json may fall before it is worth saying so out loud. The
# Power System republishes every log_interval; several missed in a row means it died,
# and the Logger would otherwise sit there silently writing nothing.
STALE_AFTER = timedelta(seconds=60)


def read_state(path):
    """The current state, or None if it is absent or half-written."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


class LoggerProcess:
    def __init__(self, data_dir=None, runtime_state_path=None,
                 system_config_path=None):
        data_dir = data_dir or paths.DATA_DIR
        self.state_path = runtime_state_path or paths.STATE_PATH
        self.system_config_path = system_config_path or paths.SYSTEM_CONFIG_PATH

        self.state_logger = logger.StateLogger(data_dir, config)
        self.aggregator = aggregation.DailyAggregator(data_dir, config)
        self.last_timestamp = None
        self.last_date_str = None
        self._system_config = {}
        self._system_config_mtime = None
        self._stale_warned = False

    def system_config(self) -> dict:
        """The topology contract, re-read when the Power System rewrites it.

        The column split comes from here, so a component appearing after a Power
        System restart has to reach this process without restarting it too.
        """
        try:
            mtime = self.system_config_path.stat().st_mtime
        except OSError:
            return self._system_config
        if mtime != self._system_config_mtime:
            self._system_config = discovery.load_system_configuration(
                self.system_config_path
            )
            self._system_config_mtime = mtime
        return self._system_config

    def step(self, t_now: datetime) -> bool:
        """One look at state.json. True if a new poll was logged."""
        state = read_state(self.state_path)
        if state is None or "timestamp" not in state:
            return False

        if state["timestamp"] == self.last_timestamp:
            stamp = datetime.fromisoformat(state["timestamp"])
            if t_now - stamp > STALE_AFTER and not self._stale_warned:
                print(f"[logger] state.json has not moved since {stamp} — "
                      f"is the Power System running?")
                self._stale_warned = True
            return False
        self._stale_warned = False

        system_config = self.system_config()
        if not system_config:
            print(f"[logger] no {self.system_config_path.name} yet — waiting for the "
                  f"Power System to publish the topology")
            return False

        # Aggregate before the first row of a new day lands, so yesterday's file is
        # complete and closed when it is read back.
        date_str = datetime.fromisoformat(state["timestamp"]).strftime(
            config.date_format
        )
        if date_str != self.last_date_str:
            self.aggregator.update(t_now)
            self.last_date_str = date_str

        self.state_logger.log_state(state, system_config)
        self.last_timestamp = state["timestamp"]
        return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Logger — state.json to CSVs")
    parser.add_argument("--once", action="store_true",
                        help="log a single state and exit")
    args = parser.parse_args(argv)

    paths.ensure_dirs()
    tz = pytz.timezone(config.tz)
    process = LoggerProcess()

    if args.once:
        if not process.step(datetime.now(tz=tz)):
            print("[logger] nothing new in state.json")
            return 1
        print(f"[logger] wrote one row per file into {paths.DATA_DIR}")
        return 0

    print(f"[logger] watching {paths.STATE_PATH} every {config.log_interval}s")
    while True:
        t_now = datetime.now(tz=tz)
        process.step(t_now)
        # Unconditional: the heartbeat says this loop is turning, not that there was
        # anything to log. A quiet sink with nothing new is healthy — it is the
        # *Power System* that has gone quiet, and that shows up as its own heartbeat.
        health.beat("logger", config.log_interval, datetime.now(tz=tz))
        elapsed = (datetime.now(tz=tz) - t_now).total_seconds()
        time.sleep(max(0, config.log_interval - elapsed))


if __name__ == "__main__":
    sys.exit(main())
