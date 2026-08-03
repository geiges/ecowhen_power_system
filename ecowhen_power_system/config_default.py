#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Jan  2 17:31:27 2026

@author: and
"""
from . import components
#%%

date_format = "%y-%m-%d"
time_format = "%H:%M:%S"
log_interval = 5 # seconds
round_digits = 4
tz = 'Europe/Berlin'

non_numeric_var = []
simulate_system = True
logger_skip_no_changes = True

PV_componentes = {
    'large_array' : dict(
        lon = 12.68,
        lat = 47.81,
        azimuth =180.0,
        tilt = 24,
        PV_peak = 1125,
        P_limit = 900,
        ),
    'small_array' : dict(
        lon = 12.68,
        lat =47.81,
        azimuth=180.0,
        tilt =76.0,
        PV_peak = 750.0,
        P_limit = 500.0
        )
    }
    
    
# 8-cell LiFePo4 battery, 310Ah. Not a D-Bus device, so it is kept out of
# system_components (the discovery list); it feeds the battery simulation.
battery = components.Generic_Battery("battery",
                           cell_type='lifepo4',
                           n_cells=8,
                           capacity_ah=310, 
                           SOC_low = 0.2)

# Battery simulation / Kalman-filter tuning. Physical battery parameters
battery.init_simulation_model(R0=0.01,
                              R1=0.04,
                              C1=2000,
                              R_var =0.5**2,   # measurement noise variance (V²)
                              Q_soc = 1e-6,     # process noise for SOC state
                              Q_rc  = 1e-6,     # process noise for RC voltage state
                              )
# Voltage and current at the battery clamps are not measured by anything — these
# declare how they are derived. connector_R0 is the resistance of the cable between
# that component and the clamps, voltage_offset the residual difference: *physical
# measurements of this installation*, pinned by test_port_fidelity. Measure them,
# never guess them.
battery.init_derrived_measurements(
    indirect_voltage_measurements = {
        "mppt150": {
            'variable' : 'DC_0_voltage',
            'connector_R0' :  0.011,
            'voltage_offset' :  -0.1},
        "mppt100": {
            'variable' : 'DC_0_voltage',
            'connector_R0' :  0.015,
            'voltage_offset' :  -0.1},
        "multiplus": {
            'variable' : 'DC_0_voltage',
            'connector_R0' :  0.0035,
            'voltage_offset' :  -0.16},
        },
    indirect_current_measurements = 
    {
        "mppt150": {
            'variable' : 'DC_0_current'},
        "mppt100": {
            'variable' : 'DC_0_current'},
        "multiplus": {
            'variable' : 'DC_0_current'},
        },
    )


#system setup
system_components = [
    components.VictronSystem(None, short_name='system'),
    battery,
    components.VictronSolarMPPT('SmartSolar Charger MPPT 150/35',
                                   short_name='mppt150',
                                   const_consumption=0.05,
                                   connected_PV = PV_componentes['large_array']
                                   ),
    components.VictronSolarMPPTWithDCLoad('SmartSolar Charger MPPT 100/20 48V',
                                   short_name='mppt100',
                                   const_consumption=0.05,
                                   connected_PV = PV_componentes['small_array']
                                   ),
    components.VictronMultiplusII('MultiPlus-II 24/3000/70-32',
                                  short_name='multiplus',
                                  const_consumption=0.05
                                  ),
    components.VictronPhoenix24_800('Phoenix Inverter 24V 800VA 230V', 
                                    short_name='phoenix',
                                    const_consumption=0.1
                                    ), # not currently available
    components.Generic_DS18B20_Temperature_Sensor(product_name='1Wire Sensor 28-000000c8e009', 
                                        short_name = 'DS18B20_DC_multiplus', 
                                        ),
    components.Generic_DS18B20_Temperature_Sensor(product_name='1Wire Sensor 28-0000002146e4', 
                                        short_name = 'DS18B20_shunt', 
                                        ),
    components.Generic_DS18B20_Temperature_Sensor(product_name='1Wire Sensor 28-000000228b7c', 
                                        short_name = 'DS18B20_inside', 
                                        ),
    ]

from . import aux_components as aux_comp

# Auxiliary (non-D-Bus) data sources. Polled by the Power System, same loop as D-Bus.
aux_components = [
    aux_comp.TasmotaSmartPlug(
        short_name='wallbox',
        url='http://tasmota-158A57-2647',
        fallback_url='http://192.168.1.185',
        power_scale=0.81,
    ),
    aux_comp.TasmotaSmartPlug(
        short_name='ac_inverter',
        url='http://tasmota-156ecf-3791',
        fallback_url='http://192.168.1.60',
    ),
    aux_comp.DeyeSunInverter(
        short_name='ac_mppt',
        url='http://admin:admin@192.168.1.165/status.html',
    ),
]

# ----------------------------------------------------------------------------
# Actuators
# ----------------------------------------------------------------------------
# Nothing to declare here. Each component class declares what it can switch
# (`component_actuators`), and discovery resolves those against the live bus into
# system_configuration.yaml, where the Gateway is the only consumer.
#
# The logical names come out as {short_name}_{basename}, and they are the *whole*
# vocabulary Control speaks: it says command("multiplus_mode", on=True) and never
# learns that "on" is the integer 3. The names this installation therefore has:
#
#   multiplus_mode        the Multiplus II inverter
#   mppt100_load          the mppt100's DC load output — wired to the battery-
#                         compartment fan
#   wallbox_relay         the wallbox charging plug
#   ac_inverter_relay     hard-cuts mains AC to the ac_mppt (Deye) inverter
#
# ponytail: the `from config import *` override hook was dropped in the port —
# no config.py ever existed, and any stray one on sys.path would have silently
# replaced the calibration constants above. Reinstate a *named* override
# (e.g. an env-pointed YAML) only if a second installation actually needs one.
