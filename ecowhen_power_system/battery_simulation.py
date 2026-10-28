#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The battery: its model, its derived clamp values, and the SOC estimator.

Ex-`battery.py` + ex-`simulation.py`, merged. The Thevenin model, the coulomb counter
and the Kalman filter all describe the same battery and were only ever separated by
file; the filter now takes `self` as its battery model.

**Nothing measures the battery.** There is no shunt at the clamps and no BMS on the
bus — every DC component reports the voltage at *its own* terminals and the current
down *its own* cable. The clamp values are therefore *derived*, not "corrected": the
error of any individual sensor is unknown and beside the point. What the cable
constants describe is the path from a source to the clamps, which is why they are
declared on the battery (`Generic_Battery.init_derrived_measurements`) rather than on
the component that happens to sit at the far end.
"""
import numpy as np

from .kalman import ExtendedKalmanFilter
from .specifications import SPECS_BATTERY_CELL

# The GX aggregates the DC sources into one battery current. Nothing else in this
# module knows a D-Bus name.
BATTERY_CURRENT_VAR = "system/battery_current"

# Below this current the clamp voltage is close enough to OCV to invert the OCV
# curve for a starting SOC.
OCV_INIT_MAX_CURRENT = 2.0      # A
# The filter is only allowed to pull the counted SOC onto a rail when little current
# flows: under load the model error alone is enough to rail it falsely.
SOC_RAIL_MAX_CURRENT = 5.0      # A
OCV_FIT_DEGREE = 5


class Battery_Simulation:
    """Coulomb counting + an extended Kalman filter over the derived clamp voltage."""

    default_init_SOC = 0.5
    default_init_RC_voltage = 0.0

    def __init__(self, battery, debug=False):
        self.debug = debug
        self.n_cells = battery.n_cells
        self.charge_efficiency = battery.charge_efficiency
        self.low_battery_SOC = battery.SOC_low

        # Capacity in As throughout; the config declares Ah.
        self.capacity_wh = battery.capacity_wh
        self.total_capacity = battery.Q_tot * 3600
        self.actual_capacity = self.total_capacity
        self._current = 0.0                     # charge positive, as Victron reports

        # Thevenin model: OCV + R0 + R1||C1. ExtendedKalmanFilter reads R1, C1,
        # total_capacity and OCV_model straight off this object.
        p = battery.model_parameter
        self.R0, self.R1, self.C1 = p.R0, p.R1, p.C1
        self.R_var, self.Q_soc, self.Q_rc = p.R_var, p.Q_soc, p.Q_rc

        # How the clamp values are derived. Declared once, in config_default.
        self.voltage_sources = battery.indirect_voltage_measurements
        self.current_sources = battery.indirect_current_measurements

        cell_curve = SPECS_BATTERY_CELL[battery.cell_type]["ocv_curve"]
        self._rep_SOC = np.linspace(0, 1, len(cell_curve))
        self._rep_V = [v * self.n_cells for v in cell_curve]
        self._OCV_model = np.poly1d(
            np.polyfit(self._rep_SOC, self._rep_V, OCV_FIT_DEGREE)
        )
        self._OCV_model.deriv = self._OCV_model.deriv()

        self.Kf = None
        self.initilized = False
        self.t_previous = None
        
        self.voltage_full = SPECS_BATTERY_CELL[battery.cell_type]['full_voltage'] * battery.n_cells
        print('Voltage of the battery when full is {self.voltage_full}')

    # -- the battery model ---------------------------------------------------

    @property
    def OCV_model(self):
        return self._OCV_model

    @property
    def OCV(self):
        return self._OCV_model(self.state_of_charge)

    @property
    def state_of_charge(self):
        return self.actual_capacity / self.total_capacity

    def set_state_of_charge(self, SOC):
        self.actual_capacity = self.total_capacity * SOC

    @property
    def current(self):
        return self._current

    def plot_SOCV_relation(self):
        import matplotlib.pyplot as plt
        plt.figure('battery soc to V')
        plt.clf()
        plt.scatter(self._rep_SOC, self._rep_V, marker='x')
        plt.plot(self._rep_SOC, [self._OCV_model(x) for x in self._rep_SOC])

    def count_charge(self, time_delta, current):
        """Coulomb counting. `time_delta` in seconds, `current` charge-positive."""
        self._current = current
        # Efficiency is a charging loss; a discharge of 1 A really does remove 1 A.
        efficiency = self.charge_efficiency if current > 0 else 1.0
        self.actual_capacity += current * time_delta * efficiency

        if self.state_of_charge > 1.0:
            print('Warning, SOC exceed 100%, correcting down to 100%')
            self.set_state_of_charge(1.0)

    def estimate_initial_SOC(self, voltage):
        """Invert the OCV curve: the SOC whose OCV is closest to `voltage`.

        Only meaningful near rest — under load the clamp voltage is not the OCV.
        """
        soc_values = np.linspace(0, 1, 1000)
        errors = (self.OCV_model(soc_values) - voltage) ** 2
        return float(soc_values[np.argmin(errors)])

    def set_state(self, SOC, t_now, RC_voltage=0):
        print(f'Setting state to SOC={SOC} and RC_voltage={RC_voltage}')
        self.set_state_of_charge(SOC)

        print('setting up Kalman filter')
        self.Kf = ExtendedKalmanFilter(self.R_var, self.Q_soc, self.Q_rc, self)
        self.Kf.set_state(SOC, RC_voltage)
        self.t_previous = t_now

    def time_to_low_battery(self):
        """Seconds until the low-battery SOC at the present current, or None.

        Straight linear extrapolation: it assumes the discharge current holds. None
        means the question does not apply — charging, resting, or already below.
        """
        current = self.current
        SOC = self.state_of_charge
        if current >= -0.5 or (self.low_battery_SOC > SOC):
            return None
        charge_left = (SOC - self.low_battery_SOC) * self.total_capacity   # in As
        return charge_left / -current                                      # in s

    # -- the derivation ------------------------------------------------------

    def derive_clamp_measurements(self, raw_data, psystem):
        """Voltage and current at the battery clamps. Neither is measured.

        Each source sees the clamps through its own cable, so its terminal voltage is
        the clamp voltage plus the drop its own current causes:

            V_clamp = V_source + offset_source - R_source * I_source

        (current charge-positive: a source pushing current in sits above the clamps).
        One estimate per source, averaged. Their spread is reported alongside because
        it is the only signal that a cable or a constant has drifted — sources that
        agree on the clamps to within a few mV are right; sources that disagree are
        not, and nothing else in the system would ever say so.

        The current is the figure the GX already aggregates across the DC sources,
        less each source's own idle draw, which its DC current does not include.
        """
        estimates = {}
        for name, spec in self.voltage_sources.items():
            voltage = raw_data.get(f"{name}/{spec['variable']}")
            current = raw_data.get(f"{name}/{self.current_sources[name]['variable']}")
            if voltage is None or current is None:
                continue          # on the bus but not reporting this poll — see A11
            estimates[name] = (
                voltage + spec['voltage_offset'] - spec['connector_R0'] * current
            )

        if not estimates:
            raise RuntimeError(
                f"nothing can see the battery clamps: none of "
                f"{sorted(self.voltage_sources)} reported both a voltage and a "
                f"current this poll, so the clamp voltage cannot be derived"
            )
        if BATTERY_CURRENT_VAR not in raw_data:
            raise RuntimeError(
                f"{BATTERY_CURRENT_VAR} is missing from this poll, so the battery "
                f"current cannot be derived"
            )

        idle_draw = sum(
            psystem[name].const_consumption for name in self.current_sources
        )
        return {
            "battery/voltage": sum(estimates.values()) / len(estimates),
            "battery/current": raw_data[BATTERY_CURRENT_VAR] - idle_draw,
            "battery/voltage_spread": max(estimates.values()) - min(estimates.values()),
        }

    # -- one poll ------------------------------------------------------------

    def update(self, raw_data, t_now, psystem):
        """Derive the clamp values, then advance the SOC estimate. Returns the row."""
        sim_data = self.derive_clamp_measurements(raw_data, psystem)
        voltage = sim_data["battery/voltage"]
        current = sim_data["battery/current"]

        # OCV with the ohmic drop taken out — what the filter measures against.
        OCV_est = voltage - self.R0 * current

        if not self.initilized:
            if abs(current) < OCV_INIT_MAX_CURRENT:
                initial_SOC = self.estimate_initial_SOC(voltage)
                print(f'Estimated initial SOC from OCV={voltage:.2f}V: {initial_SOC:.2%}')
            else:
                initial_SOC = self.default_init_SOC
                print(f'Current too high ({current:.1f}A) for OCV-based init, '
                      f'using default SOC={initial_SOC}')
            self.set_state(initial_SOC, t_now=t_now,
                           RC_voltage=self.default_init_RC_voltage)
            self.initilized = True
        else:
            time_delta = (t_now - self.t_previous).total_seconds()

            self.count_charge(time_delta, current)

            # kalman.py runs in the OPPOSITE sign convention to the rest of this
            # package. Its control matrix (kalman.py:94) is
            #     B = [[-Δt/Q_tot], [R1(1-exp)]]
            # so SOC only rises when u is negative — i.e. u is discharge-positive,
            # while Victron, count_charge above and every other current in this
            # package are charge-positive. Fed charge-positive current the prediction
            # step walked SOC *down* while the counter walked it up, and the
            # measurement update spent every step undoing it. See A13.
            discharge_positive = -current
            self.Kf.predict(time_delta=time_delta, u=discharge_positive)
            self.Kf.update(OCV_est, discharge_positive)

            # The filter can see a rail the counter cannot: at rest, an estimate that
            # has left [0, 1] means the count has drifted off the end of the battery.
            if abs(current) < SOC_RAIL_MAX_CURRENT:
                if self.Kf.x[0, 0] > 1.0:
                    self.set_state_of_charge(1.0)
                elif self.Kf.x[0, 0] < 0.0:
                    self.set_state_of_charge(0.0)

            self.t_previous = t_now
            
       
            

        sim_data["OCV_est"] = OCV_est
        if voltage > self.voltage_full:
            print('Setting SOC to 100%')
            sim_data["SOC_Kf"] = 1.0
            sim_data["SOC_counted"] = 1.0

        else:
            sim_data["SOC_Kf"] = float(self.Kf.x[0, 0])
            sim_data["SOC_counted"] = self.state_of_charge

        print(f"SOC estimated: {sim_data['SOC_Kf']} / "
              f"counted: {sim_data['SOC_counted']}", end=' - ')
        return sim_data
