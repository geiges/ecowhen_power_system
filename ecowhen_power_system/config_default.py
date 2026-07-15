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
    
    
        
        

#system setup
system_components = [
    components.VictronSystem(None, short_name='system'),
    components.VictronSolarCharger('SmartSolar Charger MPPT 150/35',
                                   short_name='mppt150',
                                   const_consumption=0.05,
                                   connected_PV = PV_componentes['large_array']
                                   ),
    components.VictronSolarChargerWithDCLoad('SmartSolar Charger MPPT 100/20 48V',
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
                                    )
    ]

# system connectors (relevant for measurements)
measurement_components = {
    "mppt150": {
        'connector_R0' :  0.011,
        'voltage_offset' :  -0.1},
    "mppt100": {
        'connector_R0' :  0.015,
        'voltage_offset' :  -0.1},
    "multiplus": {
        'connector_R0' :  0.0035,
        'voltage_offset' :  -0.16},
    }

# Battery Simulation configuration
batt_config_V1 = {
    "Q_tot" : 210,
    "R0" : 0.01,
    "R1" : 0.04,
    "C1" : 2000,
    "ncells" : 8,
    "R_var" : 0.5**2,   # measurement noise variance (V²)
    "Q_soc" : 1e-6,     # process noise for SOC state
    "Q_rc"  : 1e-6,     # process noise for RC voltage state
    "charge_efficiency" : 1.0,
    "version" : 'V1',
    "low_battery_SOC" : 0.2,
    "min_safety_voltage" : 24.3,
    "max_safety_voltage" : 28.9,
    "min_safety_temperature" : 5,
    "max_safety_temperature" : 42.5,
    "max_safety_ac_load_w" : 2500,
}

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
# Actuator registry
# ----------------------------------------------------------------------------
# The logical actuators Control is allowed to command, and how the Gateway reaches
# each one. These names are the *whole* vocabulary Control speaks — it says
# command("multiplus_mode", on=True) and never learns that "on" is the integer 3.
#
# Declared once, here. Discovery resolves the concrete addresses off the live bus
# and writes them into system_configuration.yaml; the Gateway is the only consumer.
# Declaring the Tasmota URLs in exactly one place (they come from the aux component
# below) is what stops the predecessor's bug where the reading URL and the writing
# URL drifted apart and the AC hard-cut silently stopped firing.
#
# `on`/`off` are omitted wherever they can be derived from a component state's
# `mapping` (e.g. the Multiplus declares {3: "on", 4: "off"} already).
actuators = {
    'multiplus_mode': dict(
        description='Multiplus II inverter on/off',
        component='multiplus',
        write=dict(transport='dbus', state='inverter_mode'),
        read=dict(transport='dbus', state='inverter_mode'),
    ),
    'mppt100_load': dict(
        description='DC load output on the mppt100 — the battery-compartment fan',
        component='mppt100',
        # ASYMMETRIC, and deliberately so: /Load/State is read-only on this model,
        # so writes go out over VE.Direct HEX while reads come back over D-Bus.
        # The two use *different value spaces*: writing off is register value 0,
        # but reading off comes back as /Load/State == 1. A reconcile loop that
        # assumed one number for "off" would write 0, read 1, call it a mismatch
        # and flap the fan until it exhausted its retries.
        write=dict(transport='vedirect', vedirect_set='load_control', on=4, off=0),
        read=dict(transport='dbus', state='load_state'),
    ),
    'wallbox_charge': dict(
        description='Wallbox charging plug',
        aux_component='wallbox',
        write=dict(transport='tasmota'),
        read=dict(transport='tasmota'),
    ),
    'ac_inverter_plug': dict(
        description='Hard-cuts mains AC to the ac_mppt (Deye) inverter',
        aux_component='ac_inverter',
        write=dict(transport='tasmota'),
        read=dict(transport='tasmota'),
    ),
}

# ponytail: the `from config import *` override hook was dropped in the port —
# no config.py ever existed, and any stray one on sys.path would have silently
# replaced the calibration constants above. Reinstate a *named* override
# (e.g. an env-pointed YAML) only if a second installation actually needs one.
