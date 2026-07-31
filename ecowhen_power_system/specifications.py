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
              "min_temp" : 5,         # in °C
              # Open-circuit voltage of one cell over SOC, evenly spaced 0 → 1.
              # A chemistry property, so it belongs here; battery_simulation fits a
              # polynomial through it and scales by n_cells. (battery.py used to
              # carry two of these lists, one of them dead — this is the live one.)
              "ocv_curve" : [2.5, 3.0, 3.19, 3.22, 3.25,
                             3.26, 3.27, 3.3, 3.32, 3.35, 3.5],
              }
     }
 