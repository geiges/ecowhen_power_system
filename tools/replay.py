"""Replay the EKF over the old system's recorded sim_*.csv, three sign/gate variants.

The sim CSVs already carry the derived clamp voltage and current (the old pipeline's
output, verified bit-identical to the new one), so they are exactly the estimator's
inputs. Only the filter is re-run.

Variants
  A  as-is          predict(u=+I_charge), gate fires on charge>10 A or discharge>3 A
  B  sign fixed     predict(u=-I_charge), gate meaning flips with it:
                                                discharge>10 A or charge>3 A
  C  sign fixed,    predict(u=-I_charge), gate thresholds swapped so its *physical*
     gate preserved              meaning stays today's: charge>10 A or discharge>3 A
"""
import csv
import glob
import os
import pathlib
import sys
from datetime import datetime

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from ecowhen_power_system import config_default as cfg
from ecowhen_power_system.battery_simulation import Battery_Simulation
from ecowhen_power_system.kalman import ExtendedKalmanFilter

DATA = "/home/and/python/victron_system_monitor/data"
REST_CURRENT = 1.0          # A — "at rest" for an OCV anchor
REST_MINUTES = 15           # RC time constant is R1*C1 = 80 s, so 15 min is ~11 tau
MAX_GAP_S = 300             # beyond this the logger was down; do not count across it


class GatedEKF(ExtendedKalmanFilter):
    """The real filter with the noise gate's thresholds exposed.

    update() is copied verbatim from kalman.py:99-126 with only the two threshold
    literals parameterised. test_copy_is_faithful() below proves the copy matches.
    """

    def __init__(self, *a, hi=10, lo=-3, **kw):
        super().__init__(*a, **kw)
        self.hi, self.lo = hi, lo

    def update(self, z, u):
        P = self._P
        if u > self.hi or u < self.lo:
            nfactor = np.abs(u)
        else:
            nfactor = 1
        R = self._R_var * nfactor
        x = self._x
        H = self._HJacobian(x)
        S = H * P * H.T + R
        K = P * H.T * S.I
        self._K = K
        hx = self._Hx(x)
        y = np.subtract(z, hx)
        self._x = x + K * y
        KH = K * H
        I_KH = np.identity((KH).shape[1]) - KH
        self._P = I_KH * P * I_KH.T + K * R * K.T


class Rows:
    """The columns the replay needs, as plain arrays. (pandas in this environment is
    a broken conda build; numpy is already a project dependency.)"""

    def __init__(self, t, V, I):
        self.t, self.V, self.I = np.array(t), np.array(V), np.array(I)
        self.dt = np.diff(self.t, prepend=self.t[0]) 

    def __len__(self):
        return len(self.t)


def load(pattern):
    t, V, I = [], [], []
    for path in sorted(glob.glob(os.path.join(DATA, pattern))):
        day = os.path.basename(path)[4:-4]          # sim_26-06-01.csv -> 26-06-01
        with open(path) as f:
            for row in csv.DictReader(f):
                try:
                    stamp = datetime.strptime(day + " " + row["time"],
                                              "%y-%m-%d %H:%M:%S")
                    v = float(row["system/battery_voltage"])
                    i = float(row["system/battery_current"])
                except (ValueError, KeyError, TypeError):
                    continue
                t.append(stamp.timestamp()); V.append(v); I.append(i)
    order = np.argsort(t)
    return Rows(np.array(t)[order], np.array(V)[order], np.array(I)[order])


def run(d, capacity_ah, sign, hi, lo, rail=True):
    """One pass. Returns per-row SOC_Kf, unrailed coulomb count, and innovations."""
    cfg.battery.Q_tot = capacity_ah
    sim = Battery_Simulation(cfg.battery)
    R0 = sim.R0

    v0, i0 = d.V[0], d.I[0]
    soc0 = sim.estimate_initial_SOC(v0) if abs(i0) < 2.0 else 0.5
    sim.set_state(soc0, t_now=d.t[0], RC_voltage=0.0)
    kf = GatedEKF(sim.R_var, sim.Q_soc, sim.Q_rc, sim, hi=hi, lo=lo)
    kf.set_state(soc0, 0.0)

    count = soc0                      # pure coulomb count, never railed
    soc_kf, soc_count, innov, gaps = [], [], [], 0

    for dt, V, I in zip(d.dt, d.V, d.I):
        if not (dt > 0) or dt > MAX_GAP_S:
            gaps += 1
            soc_kf.append(float(kf.x[0, 0])); soc_count.append(count)
            innov.append(np.nan)
            continue

        count += I * dt / sim.total_capacity
        sim.count_charge(dt, I)
        kf.predict(time_delta=dt, u=sign * I)

        z = V - R0 * I
        hx = sim.OCV_model(kf.x[0, 0]) - kf.x[1, 0]      # before the update
        innov.append(z - hx)
        kf.update(z, sign * I)

        if rail and abs(I) < 5.0:
            if kf.x[0, 0] > 1.0:
                sim.set_state_of_charge(1.0)
            elif kf.x[0, 0] < 0.0:
                sim.set_state_of_charge(0.0)

        soc_kf.append(float(kf.x[0, 0]))
        soc_count.append(sim.state_of_charge)

    return (np.array(soc_kf), np.array(soc_count), np.array(innov),
            np.array([count]), gaps)


def rest_anchors(d, ocv_model):
    """Independent SOC references: the OCV curve inverted at genuine rest points.

    A run of >=REST_MINUTES with |I| < REST_CURRENT means the RC transient has long
    decayed, so the clamp voltage is the OCV. The last sample of each run is taken.

    Low current is NOT sufficient on its own: during absorption the charger holds the
    pack well above its OCV while the current tapers below 1 A. Those points sit off
    the top of the OCV curve, where the inversion just clamps to 100%. An anchor is
    kept only if its voltage lands strictly inside the curve's invertible range.
    """
    grid = np.linspace(0, 1, 2001)
    curve = ocv_model(grid)
    lo_v, hi_v = float(ocv_model(0.02)), float(ocv_model(0.98))
    at_rest = np.abs(d.I) < REST_CURRENT

    anchors, start, held = [], None, 0
    for k in range(len(d) + 1):
        resting = k < len(d) and at_rest[k]
        if resting and start is None:
            start = k
        elif not resting and start is not None:
            end = k - 1
            if d.t[end] - d.t[start] >= REST_MINUTES * 60:
                v = d.V[end]
                if lo_v < v < hi_v:
                    anchors.append((end, float(grid[np.argmin((curve - v) ** 2)]), v))
                else:
                    held += 1        # charger holding the pack, not rest
            start = None
    if held:
        print(f"  ({held} low-current periods rejected: voltage outside the OCV "
              f"curve's {lo_v:.2f}..{hi_v:.2f} V range - charger holding, not rest)")
    return anchors


def test_copy_is_faithful(d):
    """GatedEKF at the stock thresholds must equal the real ExtendedKalmanFilter."""
    sub = run(d, 210, +1, 10, -3)[0]
    cfg.battery.Q_tot = 210
    sim = Battery_Simulation(cfg.battery)
    v0, i0 = d.V[0], d.I[0]
    soc0 = sim.estimate_initial_SOC(v0) if abs(i0) < 2.0 else 0.5
    sim.set_state(soc0, t_now=d.t[0], RC_voltage=0.0)
    kf = ExtendedKalmanFilter(sim.R_var, sim.Q_soc, sim.Q_rc, sim)   # the real one
    kf.set_state(soc0, 0.0)
    out = []
    for dt, V, I in zip(d.dt, d.V, d.I):
        if not (dt > 0) or dt > MAX_GAP_S:
            out.append(float(kf.x[0, 0])); continue
        sim.count_charge(dt, I)
        kf.predict(time_delta=dt, u=I)
        kf.update(V - sim.R0 * I, I)
        if abs(I) < 5.0:
            if kf.x[0, 0] > 1.0:
                sim.set_state_of_charge(1.0)
            elif kf.x[0, 0] < 0.0:
                sim.set_state_of_charge(0.0)
        out.append(float(kf.x[0, 0]))
    worst = np.nanmax(np.abs(sub - np.array(out)))
    assert worst == 0.0, f"GatedEKF diverges from kalman.py by {worst}"
    print(f"  harness check: GatedEKF == kalman.ExtendedKalmanFilter exactly (Δ={worst})")


if __name__ == "__main__":
    pattern = sys.argv[1] if len(sys.argv) > 1 else "sim_26-0[67]-*.csv"
    d = load(pattern)
    print(f"{len(d)} rows, {datetime.fromtimestamp(d.t[0])} .. "
          f"{datetime.fromtimestamp(d.t[-1])}")
    print(f"dt: median {np.median(d.dt[1:]):.0f}s, "
          f"{(d.dt > MAX_GAP_S).sum()} gaps > {MAX_GAP_S}s")
    print(f"current: {d.I.min():.1f} .. {d.I.max():.1f} A")

    print("\nharness fidelity")
    test_copy_is_faithful(d)

    cfg.battery.Q_tot = 210
    anchors = rest_anchors(d, Battery_Simulation(cfg.battery).OCV_model)
    print(f"\n{len(anchors)} rest anchors (|I|<{REST_CURRENT} A for >{REST_MINUTES} min)")

    VARIANTS = [
        ("A  as-is          ", +1, 10, -3),
        ("B  sign fixed     ", -1, 10, -3),
        ("C  sign fixed+gate", -1, 3, -10),
    ]

    for capacity in (210, 310):
        print(f"\n{'='*78}\ncapacity {capacity} Ah\n{'='*78}")
        print(f"{'variant':20} {'anchorRMSE':>11} {'innov mean':>11} {'innov RMS':>10} "
              f"{'d(innov)/dI':>12} {'Kf-count RMSE':>14}")
        for name, sign, hi, lo in VARIANTS:
            kf_soc, counted, innov, _, _ = run(d, capacity, sign, hi, lo)
            idx = [i for i, _, _ in anchors]
            err = kf_soc[idx] - np.array([s for _, s, _ in anchors])
            ok = ~np.isnan(innov)
            # slope of innovation on current: the resistance the model still owes
            slope = np.polyfit(d.I[ok], innov[ok], 1)[0]
            print(f"{name:20} {np.sqrt((err**2).mean()):11.4f} "
                  f"{np.nanmean(innov):+11.5f} {np.sqrt(np.nanmean(innov**2)):10.5f} "
                  f"{slope:+12.5f} {np.sqrt(((kf_soc - counted)**2).mean()):14.4f}")
