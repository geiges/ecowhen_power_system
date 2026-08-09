"""Tasmota-flashed smart plugs."""
from typing import Optional

import requests

from . import ActuatorType, HttpComponent, HttpVariableType


class TasmotaSmartPlug(HttpComponent):
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
        HttpVariableType(basename='power_w',    unit='W'),
        HttpVariableType(basename='today_kwh',  unit='kWh'),
    ]
    component_actuators = [
        # The plug's own relay. Its URL comes from this instance, so the address used
        # to switch it is the same one used to read it — which is the point: the
        # predecessor declared the two in separate files and they drifted apart.
        ActuatorType(
            basename='relay',
            description='Tasmota relay output',
            write=dict(transport='tasmota'),
            read=dict(transport='tasmota'),
        ),
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
