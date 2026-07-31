#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Jul 31 10:44:39 2026

@author: and
"""
import time
from typing import NamedTuple

# Victron's com.victronenergy.BusItem.SetValue answers 0 on success, -1 on refusal.
DBUS_SET_OK = 0


def _read_number(dbus, service, path):
    """Read one numeric D-Bus value, or None if it is missing or invalid.

    Victron answers an *empty array* — not an error, not None — for a value the
    hardware cannot currently provide, which is exactly how a Multiplus that has not
    run since boot reports /Dc/0/Current. Hence the type check rather than a bare
    try/except.
    """
    try:
        value = dbus.get(service, path).GetValue()
    except Exception:
        return None
    return value if isinstance(value, (int, float)) else None


def _write_number(dbus, service, path, value) -> bool:
    """Write one D-Bus value. True if the bus accepted it.

    BusItem.SetValue takes a variant, so on a real bus the int has to be wrapped;
    gi only exists on the Venus box (same reason power_system_main.get_bus imports
    pydbus lazily), and off-device the fake bus takes the plain value.
    """
    try:
        from gi.repository import GLib
    except ImportError:
        GLib = None
    try:
        arg = int(value) if GLib is None else GLib.Variant("i", int(value))
        return dbus.get(service, path).SetValue(arg) == DBUS_SET_OK
    except Exception as exc:
        print(f"[victron] SetValue {service}{path}={value} failed: {exc}")
        return False


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

class DBusComponent(object):
    """ Contains all dbus-related methods shared bettween victron components and 
    other hardware that is integrated into the dbus
    """
    hardware = "dbus_generic"
    component_states = []
    vedirect_sets = []
    
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

class VictronBaseComponent(DBusComponent):
    """
    Base class for system components to provide some common functions
    """
    hardware = "victron"
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

    def init_connected_PV(self, lon, lat, azimuth, tilt, PV_peak, P_limit):
        """Set the data for the PV system connected to the Victron device."""
        self.PV_system = dict(lon=lon, lat=lat, azimuth=azimuth, tilt=tilt,
                              PV_peak=PV_peak, P_limit=P_limit)

    
    
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
            

class VictronSystem(VictronBaseComponent):
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
        
        
    
class VictronSolarMPPT(VictronBaseComponent):
    """
    Victron solar MPPT component
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

class VictronSolarMPPTWithDCLoad(VictronBaseComponent):
    """
    Victron solar MPPT component with controlable DC load output
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

class VictronMultiplusII(VictronBaseComponent):
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
            
    # Startup wake-up. These are wall-clock waits on real hardware — the 5 s is a
    # measured guess, tune it here if the box turns out to be slower.
    MEASUREMENT_VARIABLES = ("DC_0_voltage", "DC_0_current")
    MEASUREMENT_WAKE_TIMEOUT = 5.0      # s to wait after a start; attempt 2 waits 2x
    MEASUREMENT_POLL_INTERVAL = 0.5     # s between availability checks
    MEASUREMENT_WAKE_ATTEMPTS = 2

    def _inverter_mode(self):
        """The declared inverter_mode state: D-Bus path and {value: label} in one place."""
        for state in self.component_states:
            if state.basename == "inverter_mode":
                return state
        raise ValueError(f"{self.short_name}: no inverter_mode state declared")

    def _set_mode(self, dbus, label) -> bool:
        state = self._inverter_mode()
        value = {lbl: val for val, lbl in state.mapping.items()}[label]
        return _write_number(dbus, self.get_interface(dbus), state.subaddress, value)

    def manual_start(self, dbus):
        """
        Manual start of the Multiplus during startupby setting the dbus value
        directly

        Returns
        -------
        success.

        """
        return self._set_mode(dbus, "on")

    def manual_stop(self, dbus):
        """
        Manual stop of the Multiplus during startup by setting the dbus value
        directly

        Returns
        -------
        success.

        """
        return self._set_mode(dbus, "off")

    def _measurements_readable(self, dbus, service) -> bool:
        return all(
            _read_number(dbus, service, var.subaddress) is not None
            for var in self.component_variables
            if var.basename in self.MEASUREMENT_VARIABLES
        )

    def _wait_for_measurements(self, dbus, service, timeout) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(self.MEASUREMENT_POLL_INTERVAL)
            if self._measurements_readable(dbus, service):
                return True
        return False

    def ensure_measurements_available(self, dbus) -> bool:
        """Wake the DC measurement hardware if it is asleep. Startup only.

        A Multiplus that has not run since boot sits on the bus with /Dc/0/Voltage and
        /Dc/0/Current invalid. It is then discovered as present, contributes those two
        variables to the poll map, and every poll from then on drops them — so the
        battery-voltage average silently loses this component and SOC estimation runs
        on the MPPTs alone. (That is the failure the predecessor hit.)

        Running the inverter once initialises the measurement hardware. The mode it was
        found in is restored afterwards, so a deliberately-stopped Multiplus ends up
        stopped again — with a few seconds of AC output in between, which is the price
        of a correct battery voltage.

        Returns True if the measurements are readable when this returns.
        """
        service = self.get_interface(dbus)
        if service is None:
            return False                    # not on the bus at all: nothing to wake

        if self._measurements_readable(dbus, service):
            return True

        mode_path = self._inverter_mode().subaddress
        previous_mode = _read_number(dbus, service, mode_path)

        ready = False
        for attempt in range(1, self.MEASUREMENT_WAKE_ATTEMPTS + 1):
            print(f"[{self.short_name}] DC measurements unavailable — starting the "
                  f"inverter to initialise the measurement hardware "
                  f"(attempt {attempt}/{self.MEASUREMENT_WAKE_ATTEMPTS})")
            if not self.manual_start(dbus):
                print(f"[{self.short_name}] the bus refused the start — giving up")
                break
            ready = self._wait_for_measurements(
                dbus, service, self.MEASUREMENT_WAKE_TIMEOUT * attempt
            )
            if ready:
                break

        # Back to how we found it. An unreadable /Mode restores to 'off', which is the
        # safe guess: we only got here because the inverter was not running.
        if previous_mode is None:
            self.manual_stop(dbus)
        else:
            _write_number(dbus, service, mode_path, previous_mode)

        if ready:
            print(f"[{self.short_name}] DC measurements available")
        else:
            print(f"[{self.short_name}] DC measurements still unavailable after "
                  f"{self.MEASUREMENT_WAKE_ATTEMPTS} starts — running without them; "
                  f"the battery voltage average loses this component")
        return ready
        
class VictronPhoenix24_800(VictronBaseComponent):
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
        

class VictronBatteryMonitor(VictronBaseComponent):
    pass # not implemented
  