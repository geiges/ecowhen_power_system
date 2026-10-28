#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Thu Feb 12 17:04:18 2026

@author: and
"""
from dataclasses import dataclass

from .specifications import SPECS_BATTERY_CELL
from .manufacturer.victron import (VictronMultiplusII, VictronSolarMPPT,
                                  VictronSolarMPPTWithDCLoad, VictronSystem,
                                  VictronPhoenix24_800, VictronBatteryMonitor,
                                  DBusComponent)
# Not on the bus, but components like any other — one `system_components` list.
from .manufacturer.tasmota import TasmotaSmartPlug
from .manufacturer.deye import DeyeSunInverter

class Generic_DS18B20_Temperature_Sensor(DBusComponent):
    """
    DS18B20 Temperature Sensor connected to the DBUS of victron via
    github.com/Rikkert-RS/VenusOS-TemperatureService/blob/main/setup
    """
    
    from .manufacturer.victron import VariableType, StateType
    
    component_variables =[
        VariableType(basename = "temperature", subaddress = "/Temperature", unit='°C'),
        VariableType(basename = "", subaddress = "", unit='-'), 
        VariableType(basename = "", subaddress = "Connected", unit='-')
        ]
    component_states = [
        StateType(basename = 'status', subaddress='/Status', mapping= {0 : 'no error'} ),
        StateType(basename = 'connected', subaddress='/Connected', mapping= {0: "disconnected", 1: "connected"}),
        ]
    
    
    def __init__(self, product_name, short_name, const_consumption=0.0):
 
        # Root string to identify available components on dbus
        self.product_name = product_name
        self.short_name = short_name
        self.component_type = 'com.victronenergy.temperature'
        self.const_consumption = const_consumption
        
        self.variable_list = [
            f"{self.short_name}/{var.basename}" for var in self.component_variables
            ]
        
        

 
class Generic_Battery(object):
    Q_tot : float
    n_cells : int
    cell_type : str
    nominal_voltage : float
    min_safety_voltage : float      # in V
    max_safety_voltage : float      # in V
    min_safety_temperature : float  # in °C
    max_safety_temperature : float  # in °C
    max_safety_load_watt : float    # in Watt
    hardware = None
    component_actuators = []        # nothing to switch, but the registry still asks
    capacity_wh: float

    def __init__(self,
                 short_name,
                 cell_type,
                 n_cells,
                 capacity_ah, 
                 SOC_low,
                 charge_efficiency=1.0):
        
        spec = SPECS_BATTERY_CELL[cell_type]
        self.short_name = short_name
        self.cell_type = cell_type
        # Nothing is derived until init_derrived_measurements() says how. An empty
        # declaration is caught at startup by discovery.check_voltage_calibration.
        self.indirect_voltage_measurements = {}
        self.indirect_current_measurements = {}
        self.charge_efficiency = charge_efficiency
        self.Q_tot = capacity_ah
        self.n_cells = n_cells
        self.SOC_low = SOC_low
        self.nominal_voltage = spec['nominal_voltage'] * n_cells
        self.min_safety_voltage = spec['min_voltage'] * n_cells
        self.max_safety_voltage = spec['max_voltage'] * n_cells
        self.min_safety_temperature = spec['min_temp']
        self.max_safety_temperature = spec['max_temp']
        self.max_safety_load_watt = spec['max_charge_rate'] * self.nominal_voltage * self.Q_tot
        self.capacity_wh = self.nominal_voltage * self.Q_tot
        
    def init_simulation_model(self,
                              R0,
                              R1,
                              C1, 
                              R_var, 
                              Q_soc,
                              Q_rc):
        @dataclass
        class ModelParameter:
            R0: float
            R1: float
            C1: float 
            R_var: float # measurement noise variance (V²)
            Q_soc: float # process noise for SOC state
            Q_rc: float # process noise for RC voltage state
            
        self.model_parameter = ModelParameter(R0, R1, C1, R_var, Q_soc,Q_rc)

    def init_derrived_measurements(self,
                                   indirect_voltage_measurements,
                                   indirect_current_measurements):
        """Declare how the battery's own voltage and current are derived.

        Neither is measured: no sensor sits at the clamps. Each entry names a
        component that sees them indirectly, the variable it reports, and — for
        voltage — the cable in between: `connector_R0` is the drop that the source's
        own current causes, `voltage_offset` the residual difference.

        These describe the *path* from a source to the clamps, not the source, which
        is why they are declared here and not on the component. A voltage source
        needs a current source to go with it, or its cable drop is unknowable.
        """
        missing_current = (
            set(indirect_voltage_measurements) - set(indirect_current_measurements)
        )
        if missing_current:
            raise ValueError(
                f"{self.short_name}: {sorted(missing_current)} declare an indirect "
                f"voltage but no indirect current, so the cable drop between them "
                f"and the clamps cannot be derived"
            )
        self.indirect_voltage_measurements = indirect_voltage_measurements
        self.indirect_current_measurements = indirect_current_measurements
