"""Solar forecast, fetched from the `ecowhen_data_api` instance on computepi.

Harvested from `control/forecast.py`. One change: parsed timestamps are localised to
the configured timezone. The CSV carries naive local times, `state.json` carries
tz-aware ones, and the projection subtracts one from the other — mixing the two
raises rather than answering, so the naive half is fixed at the parse boundary.

This is a *different* service from the Gateway (a data API on another host), so it
keeps its own key: `ECOWHEN_DATA_API_KEY`, not `ECOPS_API_KEY`.
"""
import csv
import io
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

from .config import TZ, now

_FORECAST_TIME_FMT = "%Y-%m-%d %H:%M:%S"


@dataclass
class HourlyEntry:
    time: datetime
    mppt150_w: float
    mppt100_w: float
    mppt_AC_w: float = 0.0

    @property
    def total_w(self) -> float:
        return self.mppt150_w + self.mppt100_w + self.mppt_AC_w


@dataclass
class SolarForecast:
    fetched_at: datetime
    entries: list          # list[HourlyEntry], sorted by time

    def get_hour(self, t: datetime) -> float:
        """Total solar W for the hourly slot containing t."""
        for entry in self.entries:
            if (entry.time.year == t.year and entry.time.month == t.month
                    and entry.time.day == t.day and entry.time.hour == t.hour):
                return entry.total_w
        return 0.0

    def get_power(self, t: datetime) -> float:
        """Total solar W at t, linearly interpolated between hourly entries."""
        if not self.entries:
            return 0.0
        before = None
        after = None
        for entry in self.entries:
            if entry.time <= t:
                before = entry
            else:
                after = entry
                break
        if before is None:
            return after.total_w
        if after is None:
            return before.total_w
        span = (after.time - before.time).total_seconds()
        offset = (t - before.time).total_seconds()
        return before.total_w + (offset / span) * (after.total_w - before.total_w)


def _parse_csv(text: str) -> dict:
    """Parse a forecast CSV (columns: time, 0) into {datetime: float}."""
    result = {}
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        raw = row.get("0", "").strip()
        if not raw:
            continue
        try:
            t = TZ.localize(datetime.strptime(row["time"], _FORECAST_TIME_FMT))
            result[t] = float(raw)
        except (ValueError, KeyError):
            continue
    return result


def _merge(mppt150: dict, mppt100: dict, mppt_AC: dict) -> list:
    times = sorted(set(mppt150) | set(mppt100) | set(mppt_AC))
    return [
        HourlyEntry(
            time=t,
            mppt150_w=mppt150.get(t, 0.0),
            mppt100_w=mppt100.get(t, 0.0),
            mppt_AC_w=mppt_AC.get(t, 0.0),
        )
        for t in times
    ]


class SolarForecastProvider:
    """Hourly-cached solar forecast, with its own clock on the failures too.

    A successful fetch is held for `cache_minutes`. A *failed* one used to hold
    nothing, so the next cycle tried all three files again — three `requests.get` at
    10 s each. That is survivable on a one-minute loop and not on a ten-second one:
    while computepi is down the control loop would do nothing but time out. So the
    attempt is what is timed, not the result, and a failure is retried on its own
    shorter interval.
    """

    def __init__(self, config):
        self._cfg = config
        self._cache = None
        self._last_attempt = None

    def get(self):
        if self._cache is not None:
            age_min = (now() - self._cache.fetched_at).total_seconds() / 60
            if age_min < self._cfg.cache_minutes:
                return self._cache

        if self._last_attempt is not None:
            since_min = (now() - self._last_attempt).total_seconds() / 60
            if since_min < self._cfg.retry_minutes:
                return self._stale_or_none()

        self._last_attempt = now()
        try:
            fresh = self._fetch()
        except Exception as exc:
            print(f"[forecast] fetch failed: {exc}")
            return self._stale_or_none()

        if not self._is_recent_enough(fresh):
            print("[forecast] fetched data is too old, treating as unavailable")
            return None

        self._cache = fresh
        return self._cache

    def _fetch(self) -> SolarForecast:
        base = self._cfg.computepi_base_url.rstrip("/")
        ep = self._cfg.endpoint_id

        texts = [
            self._get_file(f"{base}/files/{ep}/{name}")
            for name in (self._cfg.mppt150_file, self._cfg.mppt100_file,
                         self._cfg.mppt_AC_file)
        ]
        return SolarForecast(fetched_at=now(),
                             entries=_merge(*(_parse_csv(t) for t in texts)))

    def _get_file(self, url: str) -> str:
        headers = {}
        api_key = os.environ.get("ECOWHEN_DATA_API_KEY", "")
        if api_key:
            headers["X-API-Key"] = api_key
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.text

    def _is_recent_enough(self, forecast: SolarForecast) -> bool:
        if not forecast.entries:
            return False
        latest = max(e.time for e in forecast.entries)
        return latest >= now() - timedelta(hours=self._cfg.max_age_hours)

    def _stale_or_none(self):
        if self._cache is None:
            return None
        age_h = (now() - self._cache.fetched_at).total_seconds() / 3600
        return None if age_h > self._cfg.max_age_hours else self._cache
