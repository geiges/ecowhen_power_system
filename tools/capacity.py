"""What the recorded data says about the battery's actual capacity.

Between two genuine rest points the SOC change is known independently — invert the
OCV curve at each end — and the charge that moved between them is the integral of the
clamp current. Their ratio is the capacity, measured rather than declared:

    Q [Ah] = (integral I dt) / (SOC_end - SOC_start) / 3600

Only pairs with a large enough SOC swing are used: the OCV curve is flat through
LiFePO4's mid-range, so a small swing divides two noisy anchors by each other.
"""
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from replay import MAX_GAP_S, load, rest_anchors
from ecowhen_power_system import config_default as cfg
from ecowhen_power_system.battery_simulation import Battery_Simulation

MIN_SWING = 0.10        # SOC — below this the anchor noise dominates the ratio


def ocv_sensitivity(model):
    """How many volts one SOC point is worth, across the range. The precision floor
    on every anchor, and the reason the mid-range ones are weak."""
    grid = np.linspace(0.05, 0.95, 19)
    return grid, np.gradient(model(grid), grid) / 100.0     # V per 1% SOC


if __name__ == "__main__":
    d = load(sys.argv[1] if len(sys.argv) > 1 else "sim_26-0[67]-*.csv")
    sim = Battery_Simulation(cfg.battery)

    print("OCV curve sensitivity (8 cells) — the anchors' precision floor")
    grid, sens = ocv_sensitivity(sim.OCV_model)
    for s in (0.1, 0.3, 0.5, 0.7, 0.9):
        v = np.interp(s, grid, sens)
        print(f"  SOC {s:.0%}: {v*1000:6.1f} mV per 1% SOC"
              f"   → 10 mV of noise = {10/(v*1000):5.1f}% SOC")

    anchors = rest_anchors(d, sim.OCV_model)
    print(f"\n{len(anchors)} rest anchors over the span")

    # cumulative charge in As, so any pair's transfer is one subtraction
    dt = np.clip(d.dt, 0, MAX_GAP_S)
    dt[d.dt > MAX_GAP_S] = 0.0
    charge = np.concatenate([[0.0], np.cumsum((d.I * dt)[1:])])

    print(f"\n{'from':>6} {'to':>6} {'V_a':>7} {'V_b':>7} {'dSOC':>8} {'charge Ah':>10} "
          f"{'implied Q':>10}")
    implied, ah_all, dsoc_all = [], [], []
    for (i_a, soc_a, v_a), (i_b, soc_b, v_b) in zip(anchors, anchors[1:]):
        d_soc = soc_b - soc_a
        ah = (charge[i_b] - charge[i_a]) / 3600.0
        if abs(d_soc) < MIN_SWING:
            continue
        q = ah / d_soc
        implied.append(q); ah_all.append(ah); dsoc_all.append(d_soc)
        print(f"{soc_a:6.1%} {soc_b:6.1%} {v_a:7.3f} {v_b:7.3f} {d_soc:+8.1%} "
              f"{ah:10.1f} {q:10.1f}")

    if implied:
        implied = np.array(implied)
        # Pooled estimate: total charge moved / total SOC moved. Weights each pair by
        # its swing, so the big excursions (where the anchors are most trustworthy)
        # dominate and a 12% wobble between two flat-curve anchors does not.
        pooled = np.sum(np.abs(ah_all)) / np.sum(np.abs(dsoc_all))
        q1, q3 = np.percentile(implied, [25, 75])
        print(f"\nimplied capacity  pooled {pooled:.0f} Ah   "
              f"median {np.median(implied):.0f} Ah   IQR {q1:.0f}-{q3:.0f} Ah   "
              f"n={len(implied)}")
        print(f"configured: {cfg.battery.Q_tot} Ah (was 210 Ah when this data was "
              f"recorded)")
    else:
        print(f"\nno anchor pair swung more than {MIN_SWING:.0%} SOC — "
              f"cannot measure capacity from this span")
