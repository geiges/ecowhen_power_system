#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Jan 23 19:48:12 2026

@author: and
"""

# import sys
# import os
# import subprocess
# import matplotlib.pyplot as plt
import numpy as np
from .battery import Battery
from .kalman import ExtendedKalmanFilter


class System_Simulation():

    default_init_SOC = .5
    default_init_RV  = 0.0
    
    def __init__(self, battery, sim_config, debug=False):

        # Kalman-filter tuning
        self.R_var = sim_config['R_var']
        self.Q_soc = sim_config['Q_soc']
        self.Q_rc = sim_config['Q_rc']
        self.low_battery_SOC = sim_config["low_battery_SOC"]

        # Battery properties (from the Generic_Battery component)
        self.R0 = battery.R0
        self.R1 = battery.R1
        self.C1 = battery.C1
        self.ncells = battery.n_cells
        self.Q_tot = battery.Q_tot  # in Ah
        self.charge_efficiency = battery.charge_efficiency


        self.battery_simulation = Battery(self.Q_tot,
                                          self.R0,
                                          self.R1,
                                          self.C1,
                                          self.ncells,
                                          self.charge_efficiency)
        self.initilized = False
        self.t_previous = None
        # self.set_state(SOC, RC_voltage)

    def estimate_initial_SOC(self, voltage):
        """
        Estimate initial SOC from an open-circuit voltage reading.
        Uses the inverse of the OCV-SOC polynomial by finding the SOC
        that minimizes |OCV(SOC) - voltage| within [0, 1].
        Best used when battery current is near zero.
        """
        soc_values = np.linspace(0, 1, 1000)
        errors = (self.battery_simulation.OCV_model(soc_values) - voltage) ** 2
        best_idx = np.argmin(errors)
        return float(soc_values[best_idx])

    def set_state(self, SOC, t_now, RC_voltage= 0):
        print(f'Setting state to SOC={SOC} and RC_voltage={RC_voltage}')
        self.battery_simulation.actual_capacity =  SOC* self.battery_simulation.total_capacity

        print('setting up Kalman filter')
        self.Kf = ExtendedKalmanFilter(
                          self.R_var,
                          self.Q_soc,
                          self.Q_rc,
                          self.battery_simulation)

        self.Kf.set_state(SOC, RC_voltage)
        self.t_previous = t_now


    def time_to_low_battery(self):
        """
        Linear extrapolation of the remaining time to the low battery state
        assuming the discarge current remains the same.

        Returns
        -------
        time_to_empty : float
            Remaining time in seconds

        """
        
        current = self.battery_simulation.current
        SOC = self.battery_simulation.state_of_charge
        if current >= -0.5 or (self.low_battery_SOC > SOC):
            return None
        else:   
            full_amphours =  self.battery_simulation.total_capacity
            
            amphours_left = (
                 SOC* full_amphours - self.low_battery_SOC * full_amphours
                 )
            time_to_empty = amphours_left / -current # in s
            return time_to_empty
        
        

        
    def update(self,
               raw_data,
               t_now,
               psystem):
        
       
        

        battery_current_var = 'system/battery_current'
        battery_voltage_var = 'system/battery_voltage'

        ## coulomb counting
        sim_data = dict(time=raw_data['time'])

        # Simulate currents
        # reduce by const consumption for each component
        total_const_consumption = 0.

        non_system_variables = [x for x in raw_data.keys() if  not x.startswith('system')]

        for var in [x for x in non_system_variables if x.endswith('DC_0_current')]:
            # get compenent name and comp from varible name
            comp = psystem[var.split('/')[0]]
            sim_data[var] = raw_data[var] - comp.const_consumption
            total_const_consumption +=  comp.const_consumption


        # total battery current
        sim_data[battery_current_var] = raw_data[battery_current_var] - total_const_consumption



        # Simulate voltages
        voltages_to_average = []
        for var in [x for x in non_system_variables if x.endswith('DC_0_voltage')]:

            # get compenent name and comp from varible name
            comp = psystem[var.split('/')[0]]

            # current variable that relates to the voltage variable
            curr_var = var.replace('voltage', 'current')

            current_value = raw_data[curr_var] if curr_var in raw_data.keys() else 0

            # use component method to correct for cable losses and offsets
            sim_data[var] = comp.voltage_measurement(
                raw_data[var],
                current_value)

            voltages_to_average.append(sim_data[var])

        sim_data[battery_voltage_var] = sum(voltages_to_average) / len(voltages_to_average)
        
        if not self.initilized:
            
            # initialize SOC             
            voltage = sim_data['system/battery_voltage']
            current = sim_data['system/battery_current']
            
            if abs(current) < 2.0:
                initial_soc = self.estimate_initial_SOC(voltage)
                print(f'Estimated initial SOC from OCV={voltage:.2f}V: {initial_soc:.2%}')
                self.set_state(initial_soc, t_now=t_now, RC_voltage=0.)
            else:
                print(f'Current too high ({current:.1f}A) for OCV-based init, using default SOC=0.5')
                self.set_state(self.default_init_SOC, t_now, self.default_init_RV)
                    
            self.initilized = True
            OCV_est = sim_data[battery_voltage_var] - self.R0 * sim_data[battery_current_var]
            
        else:
            
            # simulate SOC
            time_delta = (t_now - self.t_previous).total_seconds()
            
            if time_delta is not None:
                self.battery_simulation.update(-time_delta, sim_data[battery_current_var])

    
            # ENKF updating
            if time_delta is not None:
                self.Kf.predict(time_delta=time_delta,
                                u=sim_data[battery_current_var])
    
            OCV_est = sim_data[battery_voltage_var] - self.R0 * sim_data[battery_current_var]
            if time_delta is not None:
                self.Kf.update(OCV_est, sim_data[battery_current_var])
    
            # updating counted SOC if Kf updated indicated full battery
            # and battery current is below 5A
            if (self.Kf.x[0,0] > 1.0) and  (abs(sim_data[battery_current_var]) < 5.):
                self.battery_simulation.set_state_of_charge(SOC=1.0)
    
            # updating counted SOC if Kf updated indicated empty battery
            # and battery current is below 5A
            if (self.Kf.x[0,0] < 0.0) and  (abs(sim_data[battery_current_var]) < 5.):
                self.battery_simulation.set_state_of_charge(SOC=0.0)
    
            self.t_previous = t_now
        estimated_SOC = float(self.Kf.x[0,0])
        SOC_counted = self.battery_simulation.state_of_charge
        sim_data['OCV_est'] = OCV_est
        sim_data['SOC_Kf'] = estimated_SOC
        sim_data['SOC_counted'] = SOC_counted
        
        
        print(f"SOC estimated: {estimated_SOC} / counted: {SOC_counted}", end = ' - ' )
        return sim_data
