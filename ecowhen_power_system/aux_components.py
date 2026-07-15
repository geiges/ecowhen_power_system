#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from typing import NamedTuple, Optional
import re
import requests


class AuxVariableType(NamedTuple):
    basename: str
    unit: str


class BaseAuxComponent:
    """
    Base class for auxiliary (non-D-Bus) data sources.

    Subclasses must implement fetch() and declare component_variables.
    """
    protocol: str = 'base'
    component_variables: list = []

    def __init__(self, short_name: str):
        self.short_name = short_name
        self.variable_list = [
            f"{self.short_name}/{var.basename}" for var in self.component_variables
        ]

    def fetch(self) -> dict:
        """
        Retrieve current values from the device.

        Returns a flat dict mapping basename -> value for each variable.
        Raise an exception on connection failure so the caller can skip.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement fetch()"
        )

    def get_labeled_data(self) -> dict:
        """Return fetch() output with keys prefixed by short_name."""
        return {
            f"{self.short_name}/{k}": v for k, v in self.fetch().items()
        }

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(short_name={self.short_name!r})"


class DummyAuxDevice(BaseAuxComponent):
    """
    Stub device that returns fixed values. Used during development and testing
    before a real protocol driver is wired in.
    """
    protocol = 'dummy'
    component_variables = [
        AuxVariableType(basename='value_a', unit=''),
        AuxVariableType(basename='value_b', unit=''),
    ]

    def __init__(self, short_name: str, dummy_values: Optional[dict] = None):
        super().__init__(short_name)
        if dummy_values is not None:
            self._values = dummy_values
            self.component_variables = [
                AuxVariableType(basename=k, unit='') for k in dummy_values
            ]
            self.variable_list = [
                f"{short_name}/{k}" for k in dummy_values
            ]
        else:
            self._values = {var.basename: 0.0 for var in self.component_variables}

    def fetch(self) -> dict:
        return self._values.copy()


class TasmotaSmartPlug(BaseAuxComponent):
    """
    Tasmota-flashed smart plug polled via its HTTP/JSON Status 8 endpoint.

    Returns instantaneous power (W) and accumulated energy for today (kWh).
    On connection failure both values are None so the CSV columns stay present.

    Parameters
    ----------
    short_name : str
        Column prefix used in the CSV (e.g. 'wallbox').
    url : str
        Primary base URL, e.g. 'http://tasmota-158A57-2647' (mDNS hostname).
    fallback_url : Optional[str]
        Static-IP URL tried when the primary URL fails at the network level,
        e.g. 'http://192.168.1.185'. Useful on hosts without mDNS support.
    power_scale : float
        Multiplicative calibration factor applied to both Power and Today
        readings. Defaults to 1.0.
    timeout : float
        HTTP request timeout in seconds. Defaults to 3.
    """
    protocol = 'http_tasmota'
    component_variables = [
        AuxVariableType(basename='power_w',    unit='W'),
        AuxVariableType(basename='today_kwh',  unit='kWh'),
    ]

    def __init__(self, short_name: str, url: str,
                 fallback_url: Optional[str] = None,
                 power_scale: float = 1.0, timeout: float = 3.0):
        super().__init__(short_name)
        self.url = url.rstrip('/')
        self.fallback_url = fallback_url.rstrip('/') if fallback_url else None
        self.power_scale = power_scale
        self.timeout = timeout

    def fetch(self) -> dict:
        result = {var.basename: None for var in self.component_variables}
        urls = [u for u in (self.url, self.fallback_url) if u]
        for url in urls:
            try:
                resp = requests.get(
                    f"{url}/cm?cmnd=Status%208",
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                energy = resp.json()["StatusSNS"]["ENERGY"]
                result['power_w']   = float(energy["Power"]) * self.power_scale
                result['today_kwh'] = float(energy["Today"]) * self.power_scale
                return result
            except requests.exceptions.RequestException:
                pass
        print(f"Warning: {self!r} unreachable at all URLs {urls}")
        return result


class DeyeSunInverter(BaseAuxComponent):
    """
    DEYE Sun inverter polled by scraping its status.html web UI.

    The page exposes readings as JavaScript variables of the form
    ``var webdata_<field> = "value"``, which are extracted via regex.

    Parameters
    ----------
    short_name : str
        Column prefix used in the CSV (e.g. 'ac_mppt').
    url : str
        Full URL to status.html, including credentials if required,
        e.g. 'http://admin:admin@192.168.1.165/status.html'.
    timeout : float
        HTTP request timeout in seconds. Defaults to 3.
    """
    protocol = 'http_deye'
    component_variables = [
        AuxVariableType(basename='power_w',   unit='W'),
        AuxVariableType(basename='today_kwh', unit='kWh'),
        AuxVariableType(basename='total_kwh', unit='kWh'),
    ]
    _webdata_fields = {
        'now_p':   'power_w',
        'today_e': 'today_kwh',
        'total_e': 'total_kwh',
    }

    def __init__(self, short_name: str, url: str, timeout: float = 3.0):
        super().__init__(short_name)
        self.url = url
        self.timeout = timeout

    def fetch(self) -> dict:
        result = {var.basename: None for var in self.component_variables}
        try:
            resp = requests.get(self.url, timeout=self.timeout)
            resp.raise_for_status()
            for field, basename in self._webdata_fields.items():
                m = re.search(
                    rf'var webdata_{field}\s*=\s*"?([0-9.]+)"?',
                    resp.text,
                )
                if m:
                    result[basename] = float(m.group(1))
        except Exception:
            print(f"Warning: {self!r} unreachable at {self.url}")
        return result
