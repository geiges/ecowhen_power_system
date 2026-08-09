"""The only way Control touches hardware: HTTP to the Gateway.

`grep -rn "dbus-send\\|pydbus" control/` must return nothing. This module is why.

Two calls, both in the vocabulary of decision #5 — a logical actuator name and a
boolean. The integers (`/Mode` 3/4, VE.Direct 4/0) were resolved by discovery from
the components' own declarations and live in the registry; Control never sees one.

`read` answers `None` for *unknown*, and unknown is not off: the Gateway reports
`on: null` when `/Load/State` says 5 ("USER"), and any transport failure lands here
too. A service that treated that as off would drive the actuator every single tick
against a device it cannot see (A7, A17).
"""
import os

import requests

API_KEY_ENV = "ECOPS_API_KEY"


class GatewayClient:
    def __init__(self, base_url: str, timeout: float = 5.0, api_key: str = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._api_key = api_key
        self._errors = {}

    def _headers(self) -> dict:
        key = self._api_key if self._api_key is not None else os.environ.get(API_KEY_ENV, "")
        return {"X-API-Key": key} if key else {}

    def read(self, actuator: str, fresh: bool = False):
        """True / False / None. None means unknown — wait, do not act.

        The Gateway answers from what it last read unless `fresh`, which asks it to
        go to the device. It is the only writer, so its memory is right about
        everything except a human at the hardware — and after every write it has no
        memory to answer from, which is what keeps convergence honest.
        """
        try:
            resp = requests.get(f"{self.base_url}/actuators/{actuator}",
                                params={"max_age": 0} if fresh else None,
                                headers=self._headers(), timeout=self.timeout)
            resp.raise_for_status()
            self._errors.pop(actuator, None)
            return resp.json().get("on")
        except Exception as exc:
            # Once per fault, not once per tick: the Gateway now answers an
            # unreachable device from memory, so the same 502 comes back every 10 s
            # and would bury everything else in the log.
            if str(exc) != self._errors.get(actuator):
                print(f"[gateway] read {actuator} failed: {exc}")
                self._errors[actuator] = str(exc)
            return None

    def command(self, actuator: str, on: bool) -> bool:
        """Drive the actuator. True if the Gateway accepted the write.

        Acceptance is not confirmation: a POST reports what it wrote and does not read
        back. Verification is the next `read`, one tick later, on purpose.
        """
        try:
            resp = requests.post(f"{self.base_url}/actuators/{actuator}",
                                 json={"on": bool(on)},
                                 headers=self._headers(), timeout=self.timeout)
            resp.raise_for_status()
            return True
        except Exception as exc:
            print(f"[gateway] command {actuator}={on} failed: {exc}")
            return False
