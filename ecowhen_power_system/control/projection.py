"""Forward projection: where SOC goes if the forecast is right.

Harvested from `control/projection.py`. The one substantive change is which battery
it projects with: `Battery_Simulation` (A12) rather than the deleted `Battery`, built
from `ControlConfig.battery` so that editing the capacity through the REST config
actually moves the projection.

This is the half of the estimator that is *not* the Power System's: PS says where the
battery is now, Control asks where it would be in four days. Same model, different
question — which is why both processes carry it.
"""
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..battery_simulation import Battery_Simulation
from ..components import Generic_Battery
from .. import config_default

STEP_MINUTES = 15


@dataclass
class ProjectedStep:
    time: datetime
    solar_w: float
    estimated_load_w: float      # base load only
    projected_soc: float
    extra_load_w: float = 0.0    # e.g. the wallbox, from a candidate schedule


@dataclass
class SystemProjection:
    current: object              # CurrentState
    forecast: object             # SolarForecast or None
    horizon_hours: int
    steps: list                  # list[ProjectedStep]


def make_battery(cfg) -> Battery_Simulation:
    """A battery model configured from `ControlConfig`.

    The Kalman noise terms come from `config_default` rather than the control config:
    they are filter tuning, and the filter runs in the Power System. Nothing here
    runs it — projection only needs the OCV curve, the capacity and the efficiency.
    """
    b = cfg.battery
    battery = Generic_Battery(
        short_name="battery",
        cell_type=b.cell_type,
        n_cells=b.n_cells,
        capacity_ah=b.capacity_ah,
        SOC_low=b.min_soc,
        charge_efficiency=b.charge_efficiency,
    )
    p = config_default.battery.model_parameter
    battery.init_simulation_model(R0=b.r0, R1=b.r1, C1=b.c1,
                                  R_var=p.R_var, Q_soc=p.Q_soc, Q_rc=p.Q_rc)
    return Battery_Simulation(battery)


def _ceil_to_quarter(t: datetime) -> datetime:
    """The first 15-minute wall-clock boundary at or after t."""
    t_min = t.replace(second=0, microsecond=0)
    remainder = t_min.minute % 15
    if remainder == 0:
        return t_min
    return t_min + timedelta(minutes=(15 - remainder))


def step_soc(battery, solar_w: float, load_w: float, dt_seconds: float) -> float:
    """Advance SOC by dt_seconds under a net power flow. Charge-positive.

    Efficiency is a charging loss only — a 1 A discharge really does remove 1 A —
    which is the same asymmetry `Battery_Simulation.count_charge` applies (A12).
    """
    ocv = battery.OCV
    net_current = (solar_w - load_w) / ocv if ocv > 0 else 0.0
    efficiency = battery.charge_efficiency if net_current > 0 else 1.0
    delta_as = net_current * dt_seconds * efficiency
    battery.actual_capacity = max(
        0.0, min(battery.total_capacity, battery.actual_capacity + delta_as)
    )
    return battery.state_of_charge


class BatteryProjector:
    def __init__(self, config):
        self._config = config

    def project(self, current, forecast, extra_load_fn=None) -> SystemProjection:
        """Project SOC forward over the configured horizon.

        `extra_load_fn(t) -> watts` adds a candidate load (the wallbox) on top of the
        base estimate, which is how an agent prices a plan before requesting it.
        """
        cfg = self._config
        battery = make_battery(cfg)
        battery.set_state_of_charge(current.soc)

        dt_seconds = STEP_MINUTES * 60
        n_steps = cfg.horizon_hours * (60 // STEP_MINUTES)
        t_first = _ceil_to_quarter(current.timestamp)

        steps = []
        for i in range(n_steps):
            t = t_first + timedelta(minutes=i * STEP_MINUTES)
            solar_w = forecast.get_power(t) if forecast is not None else 0.0
            extra_w = extra_load_fn(t) if extra_load_fn is not None else 0.0
            soc = step_soc(battery, solar_w, cfg.estimated_load_w + extra_w, dt_seconds)
            steps.append(ProjectedStep(
                time=t,
                solar_w=solar_w,
                estimated_load_w=cfg.estimated_load_w,
                extra_load_w=extra_w,
                projected_soc=soc,
            ))

        return SystemProjection(current=current, forecast=forecast,
                                horizon_hours=cfg.horizon_hours, steps=steps)

    def summarise(self, projection: SystemProjection) -> str:
        socs = [s.projected_soc for s in projection.steps]
        if not socs:
            return "[projection] no steps"
        t0 = projection.current.timestamp
        lo, hi = min(socs), max(socs)
        lo_h = (projection.steps[socs.index(lo)].time - t0).total_seconds() / 3600
        hi_h = (projection.steps[socs.index(hi)].time - t0).total_seconds() / 3600
        return (f"[projection] base load {self._config.estimated_load_w} W: "
                f"min SOC={lo:.1%} (in {lo_h:.2f}h), max SOC={hi:.1%} (in {hi_h:.2f}h)")


def save_projection_csv(projection: SystemProjection, path: Path) -> None:
    """Overwrite the projected steps, for display. Nothing reads this back."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time", "solar_w", "estimated_load_w", "extra_load_w",
                         "projected_soc"])
        for s in projection.steps:
            writer.writerow([
                s.time.strftime("%Y-%m-%d %H:%M"),
                round(s.solar_w, 1),
                round(s.estimated_load_w, 1),
                round(s.extra_load_w, 1),
                round(s.projected_soc, 4),
            ])
