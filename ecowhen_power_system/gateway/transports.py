"""The three ways to reach a piece of hardware.

Each transport has exactly two operations — read one value, write one value — and
both take a resolved endpoint dict straight out of the actuator registry in
`system_configuration.yaml`. Nothing here knows what an actuator *means*, which
value counts as "on", or which component it belongs to: discovery settled all of
that. That is why adding a transport is a dispatch entry and two functions.

Every operation returns `(value, error)` with exactly one of them set. Errors are
strings meant for a human reading a 3am log, not for branching on.
"""
import subprocess

import requests

from ..manufacturer.victron import read_number, write_number

TIMEOUT = 10        # s, for the two subprocess transports
HTTP_TIMEOUT = 3.0  # s, matching manufacturer.tasmota.TasmotaSmartPlug


# ---------------------------------------------------------------------------
# D-Bus — via pydbus, the same injected-bus seam the Power System uses
# ---------------------------------------------------------------------------

def dbus_read(bus, ep):
    value = read_number(bus, ep["service"], ep["path"])
    if value is None:
        # read_number returns None for Victron's empty-array "invalid" as well as
        # for an absent service — the device is there but cannot answer. Either way
        # the caller must not guess (A7: an unknown readback waits, it does not drive).
        return None, f"{ep['service']}{ep['path']} is unreadable"
    return int(value), None


def dbus_read_measurement(bus, service, path):
    """Read one analogue value. Float, deliberately — `dbus_read` truncates to int
    because registry states are integers, and 0.1 A of fan current truncates to 0."""
    value = read_number(bus, service, path)
    if value is None:
        return None, f"{service}{path} is unreadable"
    return float(value), None


def dbus_write(bus, ep, value):
    if write_number(bus, ep["service"], ep["path"], value):
        return value, None
    return None, f"the bus refused {ep['service']}{ep['path']} = {value}"


# ---------------------------------------------------------------------------
# VE.Direct — a thin shell over vregd, which speaks the HEX protocol for us
# ---------------------------------------------------------------------------

def _vregd(args):
    try:
        result = subprocess.run(
            ["vregd"] + args, capture_output=True, text=True, timeout=TIMEOUT,
        )
    except FileNotFoundError:
        return None, "vregd executable not found"
    except subprocess.TimeoutExpired:
        return None, "vregd timed out"
    if result.returncode != 0:
        return None, f"vregd failed: {result.stderr.strip() or result.stdout.strip()}"
    return result, None


def _register(ep):
    return f"0x{ep['register']:04X}"


def vedirect_read(bus, ep):
    result, err = _vregd(["-c", ep["port"], "-g", _register(ep)])
    if err:
        return None, err
    # vregd prints the value as the last whitespace-separated token, in hex or
    # decimal depending on the register. Anything else means the tool changed
    # under us, which is worth saying rather than papering over.
    token = result.stdout.split()[-1] if result.stdout.split() else ""
    try:
        return int(token, 0), None
    except ValueError:
        return None, f"cannot parse vregd output: {result.stdout!r}"


def vedirect_write(bus, ep, value):
    _, err = _vregd(
        ["-c", ep["port"], "-s", _register(ep), f"{ep['value_type']}:{value}"]
    )
    if err:
        return None, err
    return value, None


# ---------------------------------------------------------------------------
# Tasmota — HTTP, with the mDNS name first and the static IP as a fallback
# ---------------------------------------------------------------------------

_TASMOTA_STATE = {"ON": 1, "OFF": 0}


def _tasmota(ep, cmnd):
    """Run one Tasmota command, trying the primary URL then the fallback."""
    urls = [u for u in (ep.get("url"), ep.get("fallback_url")) if u]
    for url in urls:
        try:
            resp = requests.get(f"{url}/cm", params={"cmnd": cmnd},
                                timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            state = resp.json().get("POWER")
        except (requests.exceptions.RequestException, ValueError):
            continue
        if state not in _TASMOTA_STATE:
            return None, f"unexpected Tasmota answer: {state!r}"
        return _TASMOTA_STATE[state], None
    return None, f"unreachable at {urls}"


def tasmota_read(bus, ep):
    return _tasmota(ep, "Power")


def tasmota_write(bus, ep, value):
    # Tasmota answers a Power write with the resulting state, so the value coming
    # back is the device's own word for what it did — not a readback of a later
    # moment, which is why it is safe to report here.
    return _tasmota(ep, f"Power {int(value)}")


READERS = {"dbus": dbus_read, "vedirect": vedirect_read, "tasmota": tasmota_read}
WRITERS = {"dbus": dbus_write, "vedirect": vedirect_write, "tasmota": tasmota_write}


def read(bus, ep):
    """Read the current raw value of one resolved endpoint."""
    reader = READERS.get(ep["transport"])
    if reader is None:
        return None, f"unknown transport {ep['transport']!r}"
    return reader(bus, ep)


def write(bus, ep, value):
    """Write one raw value to one resolved endpoint."""
    writer = WRITERS.get(ep["transport"])
    if writer is None:
        return None, f"unknown transport {ep['transport']!r}"
    return writer(bus, ep, value)
