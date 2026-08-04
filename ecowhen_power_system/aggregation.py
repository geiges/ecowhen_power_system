"""Daily aggregates — one row per completed day, appended to `daily.csv`.

Reads yesterday's `log_*.csv` and `aux_*.csv` back off disk rather than accumulating
in memory, so a Logger that was restarted mid-day still produces the right total and
a gap of days is filled in when it comes back.

Energies are time-weighted integrals of the logged power columns, which is the only
correct way to read them: the Logger deduplicates, so samples are *not* evenly
spaced, and counting rows would weight a fast-changing hour the same as a quiet one.
"""
import csv
from datetime import datetime
from pathlib import Path

# Powers integrated over the day into kWh. `DC_load_power` is not logged — it is
# derived per row as load current x terminal voltage, the only power the DC load
# output does not report directly.
POWER_COLUMNS = ["DC_load_power", "AC_power_output"]
AUX_POWER_COLUMNS = ["wallbox/power_w", "ac_inverter/power_w", "ac_other/power_w"]
MAX_COLUMNS = ["system/battery_temperature", "system/battery_voltage"]
MIN_COLUMNS = ["system/battery_voltage"]

FIELDNAMES = (
    ["date", "solar_total"]
    + POWER_COLUMNS
    + AUX_POWER_COLUMNS
    + [f"max_{v}" for v in MAX_COLUMNS]
    + [f"min_{v}" for v in MIN_COLUMNS]
)

WS_TO_KWH = 3600 * 1000


def _float(row, key):
    """A CSV cell as a float, or None when the device was not reporting."""
    value = row.get(key, "")
    if value in ("", None):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _derive_load_power(row) -> None:
    """Add `<component>/DC_load_power` for every logged DC load current."""
    for key in [k for k in row if k.endswith("DC_load_current")]:
        voltage = _float(row, key.replace("load_current", "0_voltage"))
        current = _float(row, key)
        if voltage is not None and current is not None:
            row[key.replace("current", "power")] = voltage * current


def _read_rows(path: Path):
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _integrate(rows, columns, time_format) -> dict:
    """Time-weighted integral of each column, in kWh. Missing columns stay 0."""
    totals = {c: 0.0 for c in columns}
    previous = None
    for row in rows:
        _derive_load_power(row)
        try:
            stamp = datetime.strptime(row.get("time") or "", time_format)
        except ValueError:
            # A power cut mid-write leaves a row of NUL bytes; the predecessor's real
            # logs have several. Losing one sample is not a reason to lose the day.
            continue
        if previous is not None:
            delta = (stamp - previous).total_seconds()
            for column in columns:
                # Sum every component's copy: "AC_power_output" matches both the
                # multiplus and the phoenix if the phoenix is ever present.
                watts = sum(
                    v for v in (_float(row, k) for k in row if k.endswith(column))
                    if v is not None
                )
                totals[column] += watts * delta
        previous = stamp
    return {c: total / WS_TO_KWH for c, total in totals.items()}


def _extremes(rows) -> dict:
    """Daily max/min of the watched columns. Absent for a day with no readings."""
    result = {}
    for column in MAX_COLUMNS:
        values = [v for v in (_float(r, column) for r in rows) if v is not None]
        result[f"max_{column}"] = max(values) if values else None
    for column in MIN_COLUMNS:
        values = [v for v in (_float(r, column) for r in rows) if v is not None]
        result[f"min_{column}"] = min(values) if values else None
    return result


def _solar_total(rows) -> float:
    """Yield over the day: each charger's meter at the end less at the start."""
    if not rows:
        return 0.0
    first, last = rows[0], rows[-1]
    total = 0.0
    for key in [k for k in first if k.endswith("/total_yield")]:
        start, end = _float(first, key), _float(last, key)
        if start is not None and end is not None:
            total += end - start
    return total


def day_aggregates(log_path: Path, aux_path: Path, date_str: str, config) -> dict:
    """Everything daily.csv records about one day."""
    log_rows = _read_rows(log_path)
    aux_rows = _read_rows(aux_path)

    row = {"date": date_str}
    row.update(_integrate(log_rows, POWER_COLUMNS, config.time_format))
    row.update(_integrate(aux_rows, AUX_POWER_COLUMNS, config.time_format))
    row.update(_extremes(log_rows))

    # The Deye's AC output is metered by its own plug, not by any charger's yield
    # counter, so it has to be added in or the day's solar is understated.
    row["solar_total"] = _solar_total(log_rows) + row.get("ac_inverter/power_w", 0.0)

    return {
        k: round(v, config.round_digits) if isinstance(v, float) else v
        for k, v in row.items()
    }


class DailyAggregator:
    """Appends one row per completed day. Idempotent: a day is written once."""

    def __init__(self, data_dir: Path, config, output_dir=None):
        self.data_dir = Path(data_dir)
        self.config = config
        self.output_dir = Path(output_dir or self.data_dir / "aggregations")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.output_dir / "daily.csv"
        self.logged_dates = self._existing_dates()

    def _existing_dates(self) -> set:
        if not self.path.exists():
            with open(self.path, "w", newline="") as f:
                csv.DictWriter(f, FIELDNAMES).writeheader()
            return set()
        with open(self.path, newline="") as f:
            return {row["date"] for row in csv.DictReader(f) if row.get("date")}

    def _paths_for(self, date_str: str):
        return (self.data_dir / f"log_{date_str}.csv",
                self.data_dir / f"aux_{date_str}.csv")

    def pending_dates(self, today: datetime) -> list:
        """Every day with a log file, already finished, not yet aggregated."""
        today_str = today.strftime(self.config.date_format)
        dates = sorted(
            path.name[len("log_"):-len(".csv")]
            for path in self.data_dir.glob("log_*.csv")
        )
        return [d for d in dates if d != today_str and d not in self.logged_dates]

    def update(self, today: datetime) -> list:
        """Aggregate whatever days are complete. Returns the dates written."""
        written = []
        for date_str in self.pending_dates(today):
            log_path, aux_path = self._paths_for(date_str)
            try:
                row = day_aggregates(log_path, aux_path, date_str, self.config)
            except Exception as exc:
                # One unreadable day must not stop the rest, and must not be retried
                # forever either — a half-written CSV from a power cut never heals.
                print(f"[logger] cannot aggregate {date_str}: {exc}")
                self.logged_dates.add(date_str)
                continue
            with open(self.path, "a", newline="") as f:
                csv.DictWriter(f, FIELDNAMES, extrasaction="ignore").writerow(row)
            self.logged_dates.add(date_str)
            written.append(date_str)
            print(f"[logger] aggregated {date_str}")
        return written
