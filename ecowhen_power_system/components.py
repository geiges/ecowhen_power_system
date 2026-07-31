#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Thu Feb 12 17:04:18 2026

@author: and
"""

from .specifications import SPECS_BATTERY_CELL
from .manufacturer.victron import (VictronMultiplusII, VictronSolarMPPT, 
                                  VictronSolarMPPTWithDCLoad, VictronSystem,
                                  VictronPhoenix24_800, VictronBatteryMonitor,
                                  DBusComponent)

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
    R0 : float = None # optional
    R1 : float = None # optional
    C1 : float = None # optional
    
    hardware = None
    
    def __init__(self,
                 short_name,
                 cell_type,
                 n_cells,
                 capacity_ah, 
                 charge_efficiency=1.0):
        
        spec = SPECS_BATTERY_CELL[cell_type]
        self.short_name = short_name
        self.cell_type = cell_type
        self.charge_efficiency = charge_efficiency
        self.Q_tot = capacity_ah
        self.n_cells = n_cells
        self.nominal_voltage = spec['nominal_voltage'] * n_cells
        self.min_safety_voltage = spec['min_voltage'] * n_cells
        self.max_safety_voltage = spec['max_voltage'] * n_cells
        self.min_safety_temperature = spec['min_temp']
        self.max_safety_temperature = spec['max_temp']
        self.max_safety_load_watt = spec['max_charge_rate'] * self.nominal_voltage * self.Q_tot
        
    def init_equivalent_circuit_model(self,
                                      R0,
                                      R1,
                                      C1):
        self.R0 = R0
        self.R1 = R1
        self.C1 = C1
        