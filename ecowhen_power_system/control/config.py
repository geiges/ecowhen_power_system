"""Control's durable knobs, one dataclass tree, one YAML file.

Harvested from the predecessor's `control/config.py` with three changes:

* **The battery is not restated.** Every default comes from `config_default.battery`
  — the same object the Power System estimates with. A10 already made the safety
  limits derived (`n_cells x per-cell spec`); retyping them here would be a second
  place for them to drift.
* **`from_dict` carries no literal fallbacks.** The predecessor's disagreed with its
  own field defaults — `horizon_hours` 24 vs 96, `estimated_load_w` 200 vs 20 (bug
  #3) — which is what happens when a default is written down twice. `_FromDict`
  drops unknown keys and lets the dataclass supply every default, so there is one.
* **`ActuatorsConfig` is gone** (decision #5). Control names actuators and never
  learns their values; the on/off integers live in the registry the Gateway reads.
"""
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytz
import yaml

from .. import config_default

TZ = pytz.timezone(config_default.tz)

_batt = config_default.battery


def now() -> datetime:
    """Local wall-clock time, tz-aware.

    There is exactly one clock in Control and this is it. `state.json` carries
    tz-aware ISO timestamps; one naive `datetime.now()` anywhere in the chain and the
    comparison against them raises instead of answering.
    """
    return datetime.now(tz=TZ)


class _FromDict:
    """Build from a dict, ignoring keys the dataclass does not declare."""

    @classmethod
    def from_dict(cls, d):
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class BatteryConfig(_FromDict):
    """The battery, as Control's projection needs it. Defaults are the real one."""
    capacity_ah: float = _batt.Q_tot
    cell_type: str = _batt.cell_type
    n_cells: int = _batt.n_cells
    r0: float = _batt.model_parameter.R0
    r1: float = _batt.model_parameter.R1
    c1: float = _batt.model_parameter.C1
    charge_efficiency: float = _batt.charge_efficiency
    min_soc: float = _batt.SOC_low
    min_voltage: float = _batt.min_safety_voltage
    max_voltage: float = _batt.max_safety_voltage
    min_temp: float = _batt.min_safety_temperature
    max_temp: float = _batt.max_safety_temperature
    # Not a battery property: this is the Multiplus II 24/3000's AC output rating,
    # so it is declared rather than derived from the cell spec.
    max_ac_load_w: float = 2500.0

    # The warn thresholds. Not limits — nothing is cut here — but not decoration
    # either: `warn_max` is where the safety agent starts holding `ac_solar` off,
    # and it releases there too. The gap to the limit above it *is* the deadband,
    # and it has to be wide enough that cutting solar cannot drop the value back
    # under it in one tick, or the plug cycles on the boundary. Alongside the
    # limits because they are read together and edited together; a warn value on
    # the wrong side of its limit is caught at load.
    warn_min_voltage: float = 25.0
    warn_max_voltage: float = 28.4
    warn_min_temp: float = 10.0
    warn_max_temp: float = 40.0
    warn_max_ac_load_w: float = 2200.0

    def __post_init__(self):
        for lo, hi, label in (
            (self.min_voltage, self.warn_min_voltage, "min voltage"),
            (self.warn_max_voltage, self.max_voltage, "max voltage"),
            (self.min_temp, self.warn_min_temp, "min temperature"),
            (self.warn_max_temp, self.max_temp, "max temperature"),
            (self.warn_max_ac_load_w, self.max_ac_load_w, "max AC load"),
        ):
            if lo > hi:
                raise ValueError(
                    f"{label}: warn threshold is on the wrong side of the limit "
                    f"({lo} > {hi}). A warn_max above its limit never arms the "
                    f"veto; a warn_min below its limit never warns at all."
                )


@dataclass
class ForecastConfig(_FromDict):
    computepi_base_url: str = "http://192.168.1.93:5100"
    endpoint_id: str = "homesolar"
    mppt150_file: str = "forecast_solar_yield_5312439980_mppt150.csv"
    mppt100_file: str = "forecast_solar_yield_5312439980_mppt100.csv"
    mppt_AC_file: str = "forecast_solar_yield_5312439980_balcony.csv"
    cache_minutes: int = 60
    # How soon a *failed* fetch is retried. Shorter than cache_minutes so a
    # forecast that comes back is picked up promptly, long enough that three
    # 10 s HTTP timeouts cannot occupy a 10 s control loop.
    retry_minutes: int = 5
    max_age_hours: int = 6


@dataclass
class GatewayConfig(_FromDict):
    """How to reach the one process allowed to write hardware."""
    base_url: str = "http://127.0.0.1:5101"
    timeout_seconds: float = 5.0


@dataclass
class ServicesConfig(_FromDict):
    """What a service reverts to when no mode requires it.

    Off, for both: "nobody needs the inverter, so switch it off" *is* the declarative
    model — it is what the predecessor's `wallbox_off` sequence did by hand. It is
    also the one setting that changes what happens on an otherwise quiet system, so
    it is a knob rather than a constant.

    Only these two. The three plugs are services as well now, but a socket nobody
    asked for is off, and a knob saying otherwise is a knob to switch the wallbox on
    by accident.
    """
    multiplus_ac_default_on: bool = False
    mppt100_dc_load_default_on: bool = False


@dataclass
class _AgentConfig(_FromDict):
    """What every agent has: whether it runs, and how often.

    `interval_seconds` is `None` rather than a number so the agent class stays the
    one place its cadence is declared — a default written down twice is the bug
    `_FromDict` exists to avoid. Set it here only to overrule the agent.
    """
    enabled: bool = False
    interval_seconds: int = None      # None → the agent's own declaration


@dataclass
class SystemSafetyConfig(_FromDict):
    """Deliberately *not* an `_AgentConfig`: safety has no interval knob.

    It runs every tick, outside the agent loop, because its commands and its
    inhibit are applied synchronously. A settable interval here would be a knob
    that reads as configured and does nothing.
    """
    enabled: bool = True
    confirmed_disable: bool = False   # both flags required to actually disable


@dataclass
class SocWallboxChargeConfig(_AgentConfig):
    soc_on_threshold: float = 0.99    # SOC considered "full"
    soc_on_minutes: int = 30          # minutes at full before requesting the wallbox
    soc_off_threshold: float = 0.25   # SOC below which the wallbox is released


@dataclass
class ForecastWallboxConfig(_AgentConfig):
    wallbox_power_w: float = 1600.0
    max_periods_per_day: int = 2
    inverter_efficiency: float = 0.93
    min_period_minutes: int = 30
    merge_gap_minutes: int = 30
    min_solar_fraction: float = 0.5


@dataclass
class WallboxOptimalChargeConfig(_AgentConfig):
    wallbox_power_w: float = 1600.0
    inverter_efficiency: float = 0.93
    horizon_days: int = 4
    min_storage_fraction: float = 0.25


@dataclass
class ForecastAcSolarConfig(_AgentConfig):
    """When the balcony forecast is worth switching the Deye on for.

    Both numbers are guesses and both are meant to be tuned from REST: 100 W is
    roughly what having the inverter on costs, and two windows is a judgement about
    how much plug cycling a day is tolerable — not a measurement of anything.
    """
    min_yield_w: float = 100.0
    max_windows_per_day: int = 2


@dataclass
class AgentsConfig:
    system_safety: SystemSafetyConfig = field(default_factory=SystemSafetyConfig)
    soc_wallbox_charge: SocWallboxChargeConfig = field(default_factory=SocWallboxChargeConfig)
    forecast_wallbox: ForecastWallboxConfig = field(default_factory=ForecastWallboxConfig)
    wallbox_optimal_charge: WallboxOptimalChargeConfig = field(
        default_factory=WallboxOptimalChargeConfig)
    forecast_ac_solar: ForecastAcSolarConfig = field(
        default_factory=ForecastAcSolarConfig)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentsConfig":
        d = d or {}
        return cls(**{
            f.name: f.type.from_dict(d.get(f.name, {}))
            for f in dataclasses.fields(cls)
        })


@dataclass
class ControlConfig:
    # "auto"   — agents plan, modes converge, services drive hardware.
    # "manual" — only the safety agent runs; the mode graph is *paused* rather than
    #            torn down (no reconcile, so nothing is driven) and actuators are
    #            switched by hand through `POST /actuators/<name>`. Switching back to
    #            auto resumes from wherever the graph was, and the first tick
    #            re-reads every requester — so a plug somebody moved by hand in the
    #            meantime is driven back to what the modes want.
    control_mode: str = "auto"
    # The tick. Everything else here is a wall clock, so this only sets how quickly
    # the mode graph converges: bringing the wallbox up costs four ticks — declare,
    # prerequisites satisfied, primary required, plug confirmed — which was four
    # minutes at 60 s. Sustainable at 10 s because the loop's real cost is five
    # actuator reads and one projection, and because an agent that cannot afford
    # that cadence now says so itself (`interval_seconds`).
    safety_interval_seconds: int = 10
    # Only the projection CSV and its printed summary hang off this now; the agents
    # each keep their own interval.
    control_interval_seconds: int = 300
    # `control_log.jsonl` is durable and had no bound at all. At a 10 s tick it
    # grows by two entries a tick, so it needs one. Trimming is amortised — see
    # DecisionLog — and this is a scrollback, not an archive: the time series
    # lives in the CSVs.
    #
    # 200 rather than a round 30 because `GET /control/agents` finds each agent's
    # last result by scanning this file. Two entries a tick is ~16 minutes here,
    # comfortably past the 300 s planning interval; anything under ~60 and the
    # planners intermittently read as `last_result: null`.
    log_max_entries: int = 200
    horizon_hours: int = 96
    estimated_load_w: float = 20.0
    # Past this, `state.json` is not a description of the present. Control treats
    # that as a safety event: no measurements, no permission to run loads.
    state_max_age_seconds: float = 120.0
    battery: BatteryConfig = field(default_factory=BatteryConfig)
    forecast: ForecastConfig = field(default_factory=ForecastConfig)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    services: ServicesConfig = field(default_factory=ServicesConfig)
    agents: AgentsConfig = field(default_factory=AgentsConfig)

    def __post_init__(self):
        if self.control_mode not in ("auto", "manual"):
            raise ValueError(
                f"control_mode must be 'auto' or 'manual', not {self.control_mode!r}"
            )

    @property
    def manual(self) -> bool:
        return self.control_mode == "manual"

    @classmethod
    def from_dict(cls, d: dict) -> "ControlConfig":
        d = d or {}
        nested = {"battery", "forecast", "gateway", "services", "agents"}
        kwargs = {}
        for f in dataclasses.fields(cls):
            if f.name in nested:
                kwargs[f.name] = f.type.from_dict(d.get(f.name, {}))
            elif f.name in d:
                kwargs[f.name] = f.type(d[f.name])
        return cls(**kwargs)

    @classmethod
    def load(cls, path: Path) -> "ControlConfig":
        with open(path) as f:
            return cls.from_dict(yaml.safe_load(f) or {})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            yaml.dump(dataclasses.asdict(self), f,
                      default_flow_style=False, allow_unicode=True, sort_keys=False)

    @classmethod
    def load_or_default(cls, path: Path) -> "ControlConfig":
        if path.exists():
            return cls.load(path)
        cfg = cls()
        cfg.save(path)
        return cfg
