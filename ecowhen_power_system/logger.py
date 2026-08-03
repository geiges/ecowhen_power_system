"""③ Logger — `state.json` becomes the time series.

A pure sink. Nothing in the system reads what this writes, which is the point: the
predecessor had Control globbing `sim_*.csv` for `minutes_at_full_soc` and the
simulator restoring itself from its own CSV output, so the log format was load-bearing
and could not be changed. Both of those now come out of `state.json` and
`soc_state.json`, so everything here is free to be a storage decision.

Three files per day, split by where the numbers came from:

    log_<date>.csv   exactly what the bus said, plus the packed state code
    sim_<date>.csv   what nothing measures — the derived clamp values and the SOC estimate
    aux_<date>.csv   the HTTP devices

The split comes from `system_configuration.yaml`: the poll maps travel with the
topology contract precisely so this process never has to re-discover anything.
"""
import csv
import shutil
from datetime import datetime
from pathlib import Path

# Keys that exist in the state contract for Control's benefit and are not part of the
# time series. Everything in state.json that is not a poll-map variable, not an aux
# variable and not one of these is a simulator output and lands in sim_<date>.csv —
# so a new estimator output gets a column without anyone editing a list here.
STATE_ONLY_KEYS = frozenset({
    "timestamp",                    # the ISO stamp; the CSVs carry `time` instead
    "time",                         # written from `timestamp`, not copied
    "running_since",
    "soc_above_threshold_since",    # Control plumbing (decision #7)
    "minutes_at_full_soc",
    "system/power_yield",           # sums over components; the parts are logged
    "system/total_yield",
})


def split_state(state: dict, system_config: dict) -> dict:
    """Sort one state into the three column sets, keyed by file kind."""
    log_keys = set(system_config.get("variables_to_log", {})) | {"state"}
    aux_keys = {
        var
        for aux in system_config.get("aux_components", {}).values()
        for var in aux.get("variables", [])
    }

    rows = {"log": {}, "aux": {}, "sim": {}}
    for key, value in state.items():
        if key in STATE_ONLY_KEYS:
            continue
        if key in log_keys:
            rows["log"][key] = value
        elif key in aux_keys:
            rows["aux"][key] = value
        else:
            rows["sim"][key] = value
    return rows


def round_row(row: dict, digits: int) -> dict:
    """Round the floats. Anything else — None, the state code — passes through.

    Not cosmetic: unrounded floats never repeat, so without this the dedup below
    could never fire on the simulator's output.
    """
    return {
        k: round(v, digits) if isinstance(v, float) else v
        for k, v in row.items()
    }


class CsvLogger:
    """One rotating CSV: a new file per day, deduplicated, with growable columns.

    Harvested in behaviour from the predecessor's `File_Logger`, with its three
    latent faults fixed — see the module history in REFACTOR_PLAN A18.
    """

    def __init__(self, path_template, date_format="%y-%m-%d",
                 time_format="%H:%M:%S", skip_no_changes=True):
        self.path_template = str(path_template)
        self.date_format = date_format
        self.time_format = time_format
        self.skip_no_changes = skip_no_changes

        self.fieldnames = None
        self.old_data = None
        self.old_date_str = None
        self.cached = None          # the last unchanged row, held back — see log_step

    def path_for(self, t_now: datetime) -> Path:
        return Path(self.path_template.format(date_str=t_now.strftime(self.date_format)))

    def _open_file(self, path: Path, data: dict) -> None:
        """Work out this file's header, growing it if new columns have appeared.

        A component coming online mid-day adds keys. The existing rows are still
        valid, they just lack those columns, so the file is rewritten under the union
        header rather than started again or silently written with a mismatched one.
        """
        wanted = ["time"] + [k for k in data if k != "time"]

        if not path.exists():
            self.fieldnames = wanted
            return

        with open(path, newline="") as f:
            existing = csv.DictReader(f).fieldnames or []

        if set(wanted).issubset(existing):
            self.fieldnames = existing
            return

        merged = ["time"] + sorted(set(wanted) | set(existing) - {"time"})
        backup = path.with_name(path.name + "_previous_columns")
        shutil.move(path, backup)
        with open(backup, newline="") as src, open(path, "w", newline="") as dst:
            writer = csv.DictWriter(dst, merged)
            writer.writeheader()
            writer.writerows(csv.DictReader(src))
        print(f"[logger] {path.name}: columns grew to {len(merged)}, file rewritten")
        self.fieldnames = merged

    def _append(self, path: Path, row: dict, write_header: bool) -> None:
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, self.fieldnames, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerow(row)

    def log_step(self, t_now: datetime, data: dict) -> bool:
        """Write one sample. True if it reached the file.

        Consecutive identical samples collapse, but the *last* one before a change is
        written too — held in `cached` and flushed when the change arrives. That is
        deliberate: without it a plateau records when it started and never when it
        ended, and a reader cannot tell a steady value from a dead logger.
        """
        date_str = t_now.strftime(self.date_format)
        is_new_day = self.old_date_str != date_str
        path = self.path_for(t_now)

        columns_grew = (
            self.fieldnames is not None
            and not (set(data) | {"time"}).issubset(self.fieldnames)
        )
        if is_new_day or columns_grew:
            self._open_file(path, data)

        row = {"time": t_now.strftime(self.time_format), **data}
        changed = data != self.old_data
        header_needed = is_new_day and self._is_empty(path)

        self.old_data = data
        self.old_date_str = date_str

        if self.skip_no_changes and not changed and not is_new_day:
            self.cached = (path, row)
            return False

        if self.cached is not None:
            cached_path, cached_row = self.cached
            self._append(cached_path, cached_row, write_header=False)
            self.cached = None

        self._append(path, row, write_header=header_needed)
        return True

    @staticmethod
    def _is_empty(path: Path) -> bool:
        return not path.exists() or path.stat().st_size == 0


class StateLogger:
    """The three per-day CSVs, driven by one state."""

    def __init__(self, data_dir: Path, config):
        self.round_digits = config.round_digits
        self.loggers = {
            kind: CsvLogger(
                data_dir / f"{kind}_{{date_str}}.csv",
                date_format=config.date_format,
                time_format=config.time_format,
                skip_no_changes=config.logger_skip_no_changes,
            )
            for kind in ("log", "sim", "aux")
        }

    def log_state(self, state: dict, system_config: dict) -> datetime:
        """Write one poll to all three files. Returns the state's own timestamp."""
        t_now = datetime.fromisoformat(state["timestamp"])
        rows = split_state(state, system_config)
        for kind, logger in self.loggers.items():
            if rows[kind]:
                logger.log_step(t_now, round_row(rows[kind], self.round_digits))
        return t_now
