#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Feb 13 09:52:12 2026

@author: and
"""
from . import components

class Power_system(dict):
    """
    System class to facilitate simple component access
    """
    
    def __init__(self, system_components):
        """
        Setting up the system from a list of individual components. The property
        short_name is used as dict-like keys to manage the components access.

        Parameters
        ----------
        components : list of components (see components.py)
           

        Returns
        -------
        None.

        """
        
        # if not battery monitor is available, other victron devices will 
        # partly provide battery properties, yet not fully reliable. For simulation,
        # an addition estimation step is required to improve battery properties.
        self.has_battery_monitor = False
        for component in system_components:
            
            self[component.short_name] = component
            
            if isinstance(component, components.VictronBatteryMonitor):
                self.has_battery_monitor = True
            
    
    def get_components(self):
        return self.keys()
    
    def get_variables_to_log(self, dbus):
        """
        Return a dictionary with all information of available variabless of the current system components. Second
        output is a list of components that are registered, but not found currently.
        """
        variables_to_log = dict()
        missing_components = list()
        
        #loop over configures system components
        for component in self.values():
            
            if component.is_avaiable_on_bus(dbus):
                variables_of_device = component.get_device_variables(dbus)
                # component is currently connected
                variables_to_log.update(variables_of_device)
                print(f"The following variables are loged fro device {component}:")
                print(variables_of_device)
            else:
                missing_components.append(component)
                
        if len(missing_components)> 0:
            print(f'The following components are unresponsive: {missing_components}')
            
        return variables_to_log, missing_components
            
    def get_states_to_log(self, dbus):
        """
        Return a dictionary with all information of available states of the current system components. Second
        output is a list of components that are registered, but not found currently.
        """
        
        states_to_log = dict()
        missing_components = list()
        
        #loop over configures system components
        for component in self.values():
            
            if component.is_avaiable_on_bus(dbus):
                # component is currently connected
                states_to_log.update(component.get_device_states(dbus))
            else:
                missing_components.append(component)
                
        if len(missing_components)> 0:
            print(f'The following components are unresponsive: {missing_components}')
            
        return states_to_log, missing_components
            
def init_power_system(system_components,
                      measurement_components):
    """
    Return the power system instance including all initialised components and 
    available measurements components.
    """
    
    power_system = Power_system(system_components)

    for component, measurement_setup in measurement_components.items():
        power_system[component].init_measurement_correction(**measurement_setup)
    
    return power_system