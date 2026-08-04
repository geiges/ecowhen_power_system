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

    def http_components(self):
        """The components polled over HTTP instead of the bus.

        `hardware is None` already keeps them out of every bus walk, but it is not
        what identifies them: the battery is hardware-less too and is polled by
        nothing. Being able to hand back labelled data is.
        """
        return [c for c in self.values() if hasattr(c, "get_labeled_data")]


    def get_variables_to_log(self, dbus):
        """
        Return a dictionary with all information of available variabless of the current system components. Second
        output is a list of components that are registered, but not found currently.
        """
        variables_to_log = dict()
        missing_components = list()
        
        #loop over configures system components
        for component in self.values():
            
            if component.hardware is None:
                continue
            
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
            
            if component.hardware is None:
                continue
            
            if component.is_avaiable_on_bus(dbus):
                # component is currently connected
                states_to_log.update(component.get_device_states(dbus))
            else:
                missing_components.append(component)
                
        if len(missing_components)> 0:
            print(f'The following components are unresponsive: {missing_components}')

        return states_to_log, missing_components

# init_power_system() is gone with the measurement corrections it used to install on
# each component — the cable constants now belong to the battery, so constructing the
# system is just Power_system(config.system_components).