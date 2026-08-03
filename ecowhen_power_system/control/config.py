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


@dataclass
class ForecastConfig(_FromDict):
    computepi_base_url: str = "http://192.168.1.93:5100"
    endpoint_id: str = "homesolar"
    mppt150_file: str = "forecast_solar_yield_5312439980_mppt150.csv"
    mppt100_file: str = "forecast_solar_yield_5312439980_mppt100.csv"
    mppt_AC_file: str = "forecast_solar_yield_5312439980_balcony.csv"
    cache_minutes: int = 60
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
    """
    ac_inverter_default_on: bool = False
    fan_default_on: bool = False


@dataclass
class SystemSafetyConfig(_FromDict):
    enabled: bool = True
    confirmed_disable: bool = False   # both flags required to actually disable


@dataclass
class SocWallboxChargeConfig(_FromDict):
    enabled: bool = False
    soc_on_threshold: float = 0.99    # SOC considered "full"
    soc_on_minutes: int = 30          # minutes at full before requesting the wallbox
    soc_off_threshold: float = 0.25   # SOC below which the wallbox is released


@dataclass
class ForecastWallboxConfig(_FromDict):
    enabled: bool = False
    wallbox_power_w: float = 1600.0
    max_periods_per_day: int = 2
    inverter_efficiency: float = 0.93
    min_period_minutes: int = 30
    merge_gap_minutes: int = 30
    min_solar_fraction: float = 0.5


@dataclass
class WallboxOptimalChargeConfig(_FromDict):
    enabled: bool = False
    wallbox_power_w: float = 1600.0
    inverter_efficiency: float = 0.93
    horizon_days: int = 4
    min_storage_fraction: float = 0.25


@dataclass
class AgentsConfig:
    system_safety: SystemSafetyConfig = field(default_factory=SystemSafetyConfig)
    soc_wallbox_charge: SocWallboxChargeConfig = field(default_factory=SocWallboxChargeConfig)
    forecast_wallbox: ForecastWallboxConfig = field(default_factory=ForecastWallboxConfig)
    wallbox_optimal_charge: WallboxOptimalChargeConfig = field(
        default_factory=WallboxOptimalChargeConfig)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentsConfig":
        d = d or {}
        return cls(**{
            f.name: f.type.from_dict(d.get(f.name, {}))
            for f in dataclasses.fields(cls)
        })


@dataclass
class ControlConfig:
    safety_interval_seconds: int = 60
    control_interval_seconds: int = 300
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
