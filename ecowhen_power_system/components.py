#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Thu Feb 12 17:04:18 2026

@author: and
"""
from typing import NamedTuple

class VariableType(NamedTuple):
    basename :str
    subaddress : str
    unit : str

class StateType(NamedTuple):
    basename: str
    subaddress: str
    mapping: dict
    toggle_values: list = []

class VeDirectSetType(NamedTuple):
    basename: str
    register: int               # VE.Direct HEX register address, e.g. 0xEDAB
    allowed_values: list = []   # empty = any value accepted
    readable: bool = True       # also emit a vedirect_get read command

class BaseComponent(object):
    """
    Base class for system components to provide some common functions
    """
    component_states = []
    vedirect_sets = []
    
    def __init__(self, 
                 product_name, 
                 short_name, 
                 component_type, 
                 const_consumption):
        
        # Root string to identify available components on dbus
        self.product_name = product_name
        self.short_name = short_name
        self.component_type = component_type
        self.const_consumption = const_consumption
        
        self.variable_list = [
            f"{self.short_name}/{var.basename}" for var in self.component_variables
            ]
        
    def init_connected_PV(self, 
                lon,
                lat,
                azimuth,
                tilt,
                PV_peak,
                P_limit):
        """
        Set the data for the PV system connected to Victron device

        Parameters
        ----------
        lon : TYPE
            DESCRIPTION.
        lat : TYPE
            DESCRIPTION.
        azimuth : TYPE
            DESCRIPTION.
        tilt : TYPE
            DESCRIPTION.
        PV_peak : TYPE
            DESCRIPTION.
        P_limit : TYPE
            DESCRIPTION.

        Returns
        -------
        None.

        """
        
        self. PV_system = dict(lon=lon, lat=lat, azimuth=azimuth, tilt=tilt, PV_peak = PV_peak, P_limit=P_limit)
            
            
        
    def _components_on_bus(self, dbus):
        """
        Check is type of device is available on bus and returns instances
        """    
        return [x for x in dbus.dbus.ListNames() if x.startswith(self.component_type)]
    
   
    
    def get_interface(self, dbus):
        """
        Get the interface for actual product. Return None if not available
        """
        
        for interface in self._components_on_bus(dbus):
            
            comp_product_name = dbus.get(
                interface, 
                '/ProductName'
                ).GetValue()
            
            if (self.product_name is None) or (comp_product_name == self.product_name):
                
                return interface
        else:
            return None
        
    def is_avaiable_on_bus(self, dbus):
     
        if self.get_interface(dbus) is None:
            return False
        else:
            return True
        
    def get_device_variables(self,dbus):
        """
        Returns a dictionary of all implemented variables for this device
        """
        interface_address = self.get_interface(dbus)
        variables = {}
        if interface_address is None:
            return variables
        else:
            for variable in self.component_variables:
                
                variables.update({
                    f"{self.short_name}/{variable.basename}": {
                        "dbus_device" : interface_address,
                        "address" : variable.subaddress,
                        'unit' : variable.unit},
                    })
            return variables
    
    def get_device_states(self,dbus):
        """
        Returns a dictionary of all implemented variables for this device
        """
        interface_address = self.get_interface(dbus)
        variables = {}
        if interface_address is None:
            return variables
        else:
            for variable in self.component_states:
                
                variables.update({
                    f"{self.short_name}/{variable.basename}": {
                        "dbus_device" : interface_address,
                        "address" : variable.subaddress,
                        'mapping' : variable.mapping},
                    })
            return variables        
    
    def init_measurement_correction(self, connector_R0, voltage_offset):
        """
        Set connector resistance of cable connection and voltage offset in order
        to process raw voltage measurements.
        """
        self.connector_R0 = connector_R0
        self.voltage_offset = voltage_offset
        
    def voltage_measurement(self, raw_voltage_value, current):
        """
        Process raw voltage measurement from component. Processing does consider
        the voltage drop/rise due to cable resistance for current flow and can
        consider and additive offset.

        Parameters
        ----------
        raw_voltage_value : float
            
        current : float
            

        Returns
        -------
        voltage : float

        """
        if self.connector_R0 is None:
            raise(Exception('Connector resisitance not set'))
        else:
             voltage = raw_voltage_value - (self.connector_R0 * current) + self.voltage_offset
        return voltage
            

class VictronSystem(BaseComponent):
    """
    Victron solar charger component
    """
    component_variables =[
        VariableType(basename = "battery_voltage", subaddress = "/Dc/Battery/Voltage", unit='V'),
        VariableType(basename = "battery_current", subaddress = "/Dc/Battery/Current", unit='A'),
        VariableType(basename = "battery_temperature", subaddress = "/Dc/Battery/Temperature", unit='°C'),
        ]
    
    def __init__(self, product_name, short_name, const_consumption=0.0):

        component_type =  'com.victronenergy.system'
        super().__init__(product_name, short_name, component_type, const_consumption) 
        #for measurements
        
        
    
class VictronSolarCharger(BaseComponent):
    """
    Victron solar charger component
    """
    component_variables =[
        VariableType(basename = "power_yield", subaddress = "/Yield/Power", unit='W'),
        VariableType(basename = "DC_0_voltage", subaddress = "/Dc/0/Voltage", unit='V'), 
        VariableType(basename = "DC_0_current", subaddress = "/Dc/0/Current", unit='A'),
        VariableType(basename = "total_yield", subaddress = "/Yield/System", unit='kWh'),
        ]
    component_states = [
        StateType(basename = 'tracking_state', subaddress='/State', mapping= {0 : 'off', 3: "bulk", 4: "absorbtion", 5: "float"}),
        StateType(basename = 'mppt_mode', subaddress='/Mode', mapping= {0: "ideal", 1: "working", 4: "off"}, toggle_values= [1,4]),
        ]
    
    
    def __init__(self, product_name, short_name, const_consumption=0.0, connected_PV=None):
         
        component_type = 'com.victronenergy.solarcharger'
        super().__init__(product_name, short_name, component_type, const_consumption) 
        self.connector_R0 = None
        
        if connected_PV is not None:
            self.init_connected_PV(**connected_PV)
        
class VictronSolarChargerWithDCLoad(BaseComponent):
    """
    Victron solar charger component
    """
    component_variables =[
        VariableType(basename = "power_yield", subaddress = "/Yield/Power", unit='W'),
        VariableType(basename = "DC_0_voltage", subaddress = "/Dc/0/Voltage", unit='V'),
        VariableType(basename = "DC_0_current", subaddress = "/Dc/0/Current", unit='A'),
        VariableType(basename = "DC_load_current", subaddress = "/Load/I", unit='A'),
        VariableType(basename = "total_yield", subaddress = "/Yield/System", unit='kWh'),
        ]
    component_states = [
        StateType(basename = 'tracking_state', subaddress='/State', mapping= {0 : 'off', 3: "bulk", 4: "absorbtion", 5: "float"}),
        StateType(basename = 'mppt_mode', subaddress='/Mode', mapping= {0: "ideal", 1: "working", 4: "off"}, toggle_values =[1,4]),
        StateType(basename = 'load_state', subaddress='/Load/State', mapping= {1: "off", 4: "on", 5: "USER"}),
        ]
    vedirect_sets = [
        VeDirectSetType(basename='load_control', register=0xEDAB, allowed_values=[0, 1, 4, 5]),
    ]
    
    def __init__(self, product_name, short_name, const_consumption=0.0, connected_PV=None):
         
        component_type = 'com.victronenergy.solarcharger'
        super().__init__(product_name, short_name, component_type, const_consumption) 
        self.connector_R0 = None
        
class VictronMultiplusII(BaseComponent):
    """
    Victron solar charger component
    """
    component_variables =[
        VariableType(basename = "AC_power_output", subaddress = "/Ac/Out/P", unit='W'),
        VariableType(basename = "AC_frequency", subaddress = "/Ac/Out/L1/F", unit='Hz'), 
        VariableType(basename = "DC_0_voltage", subaddress = "/Dc/0/Voltage", unit='V'), 
        VariableType(basename = "DC_0_current", subaddress = "/Dc/0/Current", unit='A'),
        VariableType(basename = "alarm_temperature", subaddress="/Alarms/TemperatureSensor", unit=''),
        VariableType(basename = "alarm_low_battery", subaddress="/Alarms/LowBattery", unit=''),
        VariableType(basename = "alarm_overload", subaddress="/Alarms/Overload", unit='')
        ]
    
    component_states = [
        StateType(basename = 'inverter_mode', subaddress='/Mode', mapping= {3: "on", 4: "off"}, toggle_values= [3,4]),
        ]
    
    
    def __init__(self, product_name, short_name, const_consumption=0.0, connected_PV=None):

        component_type ='com.victronenergy.vebus'
        super().__init__(product_name, short_name, component_type, const_consumption) 
        self.connector_R0 = None
        
        if connected_PV is not None:
            self.init_connected_PV(**connected_PV)
        
class VictronPhoenix24_800(BaseComponent):
    """
    Victron solar charger component
    """
    component_variables =[
        VariableType(basename = "AC_power_output", subaddress = "/Ac/Out/P", unit='W'),
        VariableType(basename = "AC_frequency", subaddress = "/Ac/Out/L1/F", unit='Hz'), 
        VariableType(basename = "DC_0_voltage", subaddress = "/Dc/0/Voltage", unit='V'), 
        VariableType(basename = "DC_0_current", subaddress = "/Dc/0/Current", unit='A'),
        VariableType(basename = "alarm_high_temperature", subaddress="/Alarms/HighTemperature", unit=''),
        VariableType(basename = "alarm_low_battery", subaddress="/Alarms/LowBattery", unit=''),
        VariableType(basename = "alarm_overload", subaddress="/Alarms/Overload", unit='')
        ]
    
    def __init__(self, product_name, short_name, const_consumption=0.0):

        component_type ='com.victronenergy.inverter'
        super().__init__(product_name, short_name, component_type, const_consumption) 
        self.connector_R0 = None
        

class VictronBatteryMonitor(BaseComponent):
    pass # not implemented
        

            
            
if __name__ == "__main__":
    #testing
    mppt = VictronSolarCharger('SmartSolar Charger MPPT 150/35', short_name='mppt150')
    inverter = VictronMultiplusII('MultiPlus-II 24/3000/70-32', short_name='multiplus')
    inverter2 = VictronPhoenix24_800('Phoenix Inverter 24V 800VA 230V', short_name='phoenix')
    system = VictronSystem('-', short_name='system')
