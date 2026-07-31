"""Does the code as shipped actually reproduce measured variant B?

The replay harness drives ExtendedKalmanFilter directly. The fix went into
Battery_Simulation.update(), which negates internally. These must agree, or the thing
that was measured is not the thing that ships.
"""
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from replay import MAX_GAP_S, load, run
from ecowhen_power_system import config_default as cfg
from ecowhen_power_system.battery_simulation import Battery_Simulation
from ecowhen_power_system.power_system import Power_system

d = load(sys.argv[1] if len(sys.argv) > 1 else "sim_26-07-0[1-2].csv")
ps = Power_system(cfg.system_components)
idle = sum(ps[n].const_consumption for n in ("mppt150", "mppt100", "multiplus"))

# harness, variant B
harness = run(d, cfg.battery.Q_tot, -1, 10, -3)[0]

# the shipped path: Battery_Simulation.update() end to end
from datetime import datetime, timedelta

sim = Battery_Simulation(cfg.battery)
shipped = []
t_prev = None
for k in range(len(d)):
    t = datetime.fromtimestamp(d.t[k])
    if k and (d.dt[k] <= 0 or d.dt[k] > MAX_GAP_S):
        shipped.append(float(sim.Kf.x[0, 0]) if sim.Kf else np.nan)
        continue
    # one source carries the whole clamp voltage; the others are absent this poll, so
    # derive_clamp_measurements averages over the one that reported
    row = {"time": t.strftime("%H:%M:%S"),
           "system/battery_current": d.I[k] + idle,
           "mppt150/DC_0_voltage": d.V[k] + 0.011 * 0.0 + 0.1,
           "mppt150/DC_0_current": 0.0}
    sim.update(row, t, ps)
    shipped.append(float(sim.Kf.x[0, 0]))

shipped = np.array(shipped)
ok = ~np.isnan(shipped) & ~np.isnan(harness)
print(f"\nshipped Battery_Simulation.update() vs harness variant B")
print(f"  rows compared : {ok.sum()}")
print(f"  worst |diff|  : {np.max(np.abs(shipped[ok] - harness[ok])):.3e}")
print(f"  final SOC_Kf  : shipped {shipped[-1]:.6f}   harness {harness[-1]:.6f}")
