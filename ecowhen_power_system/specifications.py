#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Thu Jul 16 08:38:34 2026

@author: and
"""

SPECS_BATTERY_CELL = {
    
         "lifepo4":
             {"nominal_voltage" : 3.2, # in V
              "max_voltage" : 3.60,   # abs max 3.65
              "min_voltage" : 3.0,    # abs in 2.8
              "max_charge_rate" : 0.5,  # in C
              "max_temp": 45,         # in °C
              "min_temp" : 5}         # in °C
     }
 