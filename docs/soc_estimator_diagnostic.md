# SOC estimator — diagnostic against 37 days of recorded data

**Date:** 2026-07-31 · **Scope:** the Kalman filter's current-sign convention, plus what the
same replay revealed about pack capacity and the Thevenin model.

Findings, in order of how much they cost:

| # | Finding | Status |
|---|---|---|
| 1 | The EKF's prediction step was fed current in the **inverted sign convention** | **fixed** |
| 2 | Configured capacity was **210 Ah**; the data measures **~288 Ah** | 310 Ah in the working tree, supported |
| 3 | `R1` is **~4× too large** — now the dominant model error | open |

---

## Method

The predecessor's `sim_*.csv` already carry the derived clamp voltage and current, which are exactly
the estimator's inputs — so only the filter had to be re-run, not the whole pipeline.

```
source   victron_system_monitor/data/sim_26-06-*.csv, sim_26-07-*.csv
span     2026-06-01 00:00:04 .. 2026-07-07 01:04:27
rows     538 603 (median 5 s cadence, 9 gaps > 300 s, skipped not counted across)
current  -101.5 .. +79.2 A
harness  tools/replay.py
```

**Harness fidelity.** The variant runner subclasses the real `ExtendedKalmanFilter`, copying `update()`
verbatim with only the two noise-gate thresholds parameterised. At stock thresholds it is asserted
identical to `kalman.py` — Δ = 0.0 across the full span — so no result below is an artefact of the
harness.

**Shipped-code fidelity.** After the fix, `Battery_Simulation.update()` driven end to end over two days
reproduces harness variant B to **0.000e+00** across 27 603 rows. What was measured is what ships.

### The metrics, and which of them mean anything

| metric | what it is | weight |
|---|---|---|
| **innov mean** | mean of `z − h(x)`. A consistent filter has zero-mean innovations; a persistent bias means the prediction and the measurement disagree systematically | **decisive** |
| **innov RMS** | spread of the same residual — how well the model predicts its own measurement | strong |
| **d(innov)/dI** | slope of innovation on current: the resistance the model still owes after its own R0/RC terms | diagnostic (→ finding 3) |
| **Kf−count RMSE** | disagreement between the two SOC estimators | weak — neither is ground truth |
| **anchor RMSE** | error against SOC from OCV inversion at rest points | **discount entirely, see below** |

**Why anchor RMSE is not evidence.** Anchors are by construction low-current points. There the
prediction term contributes almost nothing and every variant relaxes to the OCV-implied SOC — which is
precisely how the anchor is computed. The comparison is near-circular. All three variants land within
0.0007 of each other, and that number should not be read as favouring any of them. It is tabulated so
it can be seen to be uninformative, not to be weighed.

---

## Finding 1 — the current sign was inverted (fixed)

### The clash

Two pieces of code disagreed about what a positive current means, and they were wired together.

- **Victron's convention, used everywhere else in the package:** positive = charging. `count_charge`
  follows it (`actual_capacity += current * time_delta`).
- **`kalman.py`'s convention, stated nowhere:** positive = discharging.

`battery_simulation.py` fed the first into the second.

### Two independent derivations, same conclusion

**SOC row.** `kalman.py:94-95`:

```python
B = np.matrix([[-time_delta/(self.battery_sim.total_capacity)],
               [ self.battery_sim.R1*(1-exp_coeff)]])
```

with `self._x = F * self._x + B * u` (line 169), so `SOC_new = SOC − Δt·u / Q_tot`. The minus sign is
the whole story: for SOC to *rise* while charging, `u` must be negative while charging.

**RC row.** The second state relaxes toward `R1·u`. What should it hold? Work back from the measurement
model (`kalman.py:77`, `h(x) = OCV(SOC) − x₁`) and the measurement itself
(`OCV_est = voltage − R0·current`). Physically `V = OCV + R0·I + V_RC`, so `OCV_est = OCV + V_RC`, and
setting `h(x) = OCV_est` gives **`x₁ = −V_RC`**. But prediction drove it to `+R1·u`. Sign flipped again,
same root cause.

Measured, same +30 A charging input for one hour:

```
EKF predict step : SOC 0.5 → 0.4032   (RC state +1.20 V, wrong sign against h(x))
coulomb counting : SOC 0.5 → 0.5968
```

### Why it survived the port

The measurement half was correct. `OCV_est = voltage − R0*current` removes the ohmic drop properly under
the charge-positive convention, so every update pulled the estimate back toward what the voltage implied
and undid the prediction's wrong-way push. The filter never diverged — it degenerated into a lagged OCV
lookup. Plausible at all times.

It mattered because `SOC_Kf` gates the reset of the *counted* SOC to 0.0/1.0, and because LiFePO4's OCV
curve is flat through the middle (≈15.6 mV per 1 % SOC at 50 %, against `R_var = 0.5²`), so coulomb
counting is precisely what is supposed to carry the estimate between the knees. That was the inverted
half.

### The decision that needed data

Negating `u` also moves the noise gate (`kalman.py:104`, `if u>10 or u<-3`), which is asymmetric and so
means a different thing under each convention:

| | inflates measurement noise when |
|---|---|
| A — as-is | charging above 10 A, or discharging above 3 A |
| B — sign fixed, gate follows | discharging above 10 A, or charging above 3 A |
| C — sign fixed, gate meaning preserved (`u < -10 or u > 3`) | charging above 10 A, or discharging above 3 A |

Not answerable from first principles — this installation's MPPTs deliver ~35 A of charge while the
Multiplus can pull >100 A discharging, so the two readings behave very differently in practice.

### Result

**capacity 210 Ah** (as configured when the data was recorded):

| variant | anchor RMSE | innov mean | innov RMS | d(innov)/dI | Kf−count RMSE |
|---|---|---|---|---|---|
| A  as-is | 0.0150 | **−0.08660** | 0.78889 | +0.04016 | 0.1386 |
| B  sign fixed, gate follows | 0.0154 | **+0.00266** | 0.60399 | −0.03093 | **0.1233** |
| C  sign fixed, gate preserved | 0.0157 | +0.02180 | 0.60331 | −0.03088 | 0.1240 |

**capacity 310 Ah** (as configured now):

| variant | anchor RMSE | innov mean | innov RMS | d(innov)/dI | Kf−count RMSE |
|---|---|---|---|---|---|
| A  as-is | 0.0152 | **−0.09237** | 0.75311 | +0.03791 | 0.1234 |
| B  sign fixed, gate follows | 0.0156 | **−0.00907** | 0.58176 | −0.02922 | 0.1419 |
| C  sign fixed, gate preserved | 0.0160 | +0.01079 | 0.57925 | −0.02903 | 0.1545 |

### Recommendation — B, applied

**A → B is the decisive step.** Mean innovation falls **32×** at 210 Ah (−87 mV → +2.7 mV) and **10×**
at 310 Ah (−92 mV → −9.1 mV). Innovation RMS drops **23 %** at both capacities. That is the sign bug,
and the margin is not close.

**B → C is a much finer call.** B has the smaller mean innovation at both capacities, but the margin
collapses from 8× at 210 Ah to 1.2× at 310 Ah (9.1 mV vs 10.8 mV); innovation RMS is a wash, favouring C
by <0.5 %. B wins Kf−count RMSE against C at both (0.1233 vs 0.1240; 0.1419 vs 0.1545), though at
310 Ah both sit above A's 0.1234 — that column compares two imperfect estimators and is weak evidence
either way. So B is chosen on a
consistent-but-narrow edge plus the fact that it is the smaller change — letting the gate follow the
sign rather than restating its thresholds. **If the gate is ever re-tuned, C is not ruled out by this
data** and the choice should be revisited together with finding 3, since both concern how much the model
should distrust the voltage under load.

The A → B ranking is stable across both capacities, so it does not depend on finding 2 being settled
first.

Applied in `battery_simulation.py`, naming the convention rather than burying a minus sign:

```python
# kalman.py runs in the OPPOSITE sign convention to the rest of this package …
discharge_positive = -current
self.Kf.predict(time_delta=time_delta, u=discharge_positive)
self.Kf.update(OCV_est, discharge_positive)
```

Two regression tests pin it: `test_the_filter_is_fed_discharge_positive_current` (the handoff) and
`test_prediction_and_counting_move_soc_the_same_way` (the physics). Nothing about the output looks wrong
when this regresses, which is why it needs a test rather than review.

---

## Finding 2 — the pack measures ~288 Ah

Independent of the filter. Between two genuine rest points the SOC change is known by inverting the OCV
curve at each end, and the charge that moved is ∫I dt, so their ratio is a measured capacity:

```
Q [Ah] = (integral I dt) / (SOC_end − SOC_start) / 3600
```

58 clean rest anchors over the span, 27 pairs swinging more than 10 % SOC:

```
implied capacity   pooled 288 Ah   median 280 Ah   IQR 247-313 Ah   n=27
```

`config_default` declared **210 Ah** throughout the recorded period, so coulomb counting was overstating
every SOC swing by roughly a third — a large part of why `SOC_Kf` and `SOC_counted` sit ~12 points apart
in these logs. The working-tree change to **310 Ah** moves decisively toward the measurement and is
supported by it. 288 Ah would be marginally better, but the difference sits inside the spread.

**Caveats.** The spread is wide because the mid-range OCV curve is flat — 10 mV of noise is ±0.6 % SOC
per anchor at 50 %, and short swings divide two noisy anchors by each other. The pooled figure weights
by swing size and is the one to trust. This is an *effective* capacity at these temperatures and rates,
not a nameplate.

**A trap worth keeping.** Low current alone does not mean "at rest". During absorption the charger holds
the pack at 28.3 V while current tapers below 1 A — above the top of the OCV curve, where the inversion
clamps to 100 % and yields nonsense (one such pair implied 97 Ah). An anchor is valid only when its
voltage lands strictly inside the curve's invertible range. Five periods were rejected this way.

### OCV curve sensitivity (8 cells) — the precision floor on every anchor

```
SOC 10%:  250.8 mV per 1% SOC     10 mV of noise = 0.04% SOC
SOC 30%:   16.3 mV per 1% SOC     10 mV of noise = 0.61% SOC
SOC 50%:   15.6 mV per 1% SOC     10 mV of noise = 0.64% SOC
SOC 70%:   13.4 mV per 1% SOC     10 mV of noise = 0.75% SOC
SOC 90%:   62.3 mV per 1% SOC     10 mV of noise = 0.16% SOC
```

This is also why voltage-based SOC is weak between the knees and why the coulomb-counting path matters.

---

## Finding 3 — `R1` is ~4× too large (open)

`d(innov)/dI` is the resistance the model still owes after its own R0 and RC terms are removed. Post-fix
it is **−0.029 to −0.031 Ω**: the model moves predicted voltage far more per amp than the pack actually
does. Solving the two variants' slopes for the pack's effective resistance gives **≈0.010–0.019 Ω**
against the model's `R0 + R1 = 0.05 Ω`.

So the RC branch is oversized by roughly 3–4×, and it is now the **dominant remaining error** — on
innovation RMS it is larger than the sign bug was.

**Not acted on.** Unlike the sign, this has no correct answer derivable from first principles; it is
genuine calibration. Re-tuning `R1`/`C1` — and plausibly `R_var`, which at 0.5 V² assumes far more
voltage noise than the data shows — is a fitting exercise, and `tools/replay.py` is the harness it
should be scored against. Worth doing before Phase 4 leans on this model.

---

## Reproducing

```bash
# variant comparison over the full span
uv run --project . python tools/replay.py "sim_26-0[67]-*.csv"

# capacity from rest anchors
uv run --project . python tools/capacity.py "sim_26-0[67]-*.csv"

# shipped code == measured variant B
uv run --project . python tools/shipped.py "sim_26-07-0[1-2].csv"
```

The harness reads `victron_system_monitor/data/` directly and writes nothing.
