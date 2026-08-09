"""DEYE Sun micro-inverters."""
import re

import requests

from . import HttpComponent, HttpVariableType


class DeyeSunInverter(HttpComponent):
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
        HttpVariableType(basename='power_w',   unit='W'),
        HttpVariableType(basename='today_kwh', unit='kWh'),
        HttpVariableType(basename='total_kwh', unit='kWh'),
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
