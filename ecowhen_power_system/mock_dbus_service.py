#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mock D-Bus service for testing dbus_logger.py without Venus OS hardware.

Registers Victron component interfaces on the session bus and replays
values from a CSV log file, advancing one row per poll cycle.

Each service gets its own private D-Bus connection so that overlapping
object paths (e.g. /Dc/0/Voltage on both solarcharger and vebus) work.

Usage:
    uv run mock_dbus_service.py <path_to_csv>

Example:
    uv run mock_dbus_service.py data/log_26-02-13.csv
"""
import os
import sys
import csv
from gi.repository import GLib, Gio


# ---------------------------------------------------------------------------
# Column name -> (bus_name, object_path) mapping
# ---------------------------------------------------------------------------
COLUMN_MAP = {
    "system/battery_voltage":       ("com.victronenergy.system",                  "/Dc/Battery/Voltage"),
    "system/battery_current":       ("com.victronenergy.system",                  "/Dc/Battery/Current"),
    "system/battery_temperature":   ("com.victronenergy.system",                  "/Dc/Battery/Temperature"),
    "mppt150/power_yield":          ("com.victronenergy.solarcharger.ttyUSB0",    "/Yield/Power"),
    "mppt150/DC_0_voltage":         ("com.victronenergy.solarcharger.ttyUSB0",    "/Dc/0/Voltage"),
    "mppt150/DC_0_current":         ("com.victronenergy.solarcharger.ttyUSB0",    "/Dc/0/Current"),
    "mppt150/total_yield":          ("com.victronenergy.solarcharger.ttyUSB0",    "/Yield/System"),
    "multiplus/AC_power_output":    ("com.victronenergy.vebus.ttyUSB1",           "/Ac/Out/P"),
    "multiplus/DC_0_voltage":       ("com.victronenergy.vebus.ttyUSB1",           "/Dc/0/Voltage"),
    "multiplus/DC_0_current":       ("com.victronenergy.vebus.ttyUSB1",           "/Dc/0/Current"),
    "multiplus/alarm_temperature":  ("com.victronenergy.vebus.ttyUSB1",           "/Alarms/TemperatureSensor"),
    "multiplus/alarm_low_battery":  ("com.victronenergy.vebus.ttyUSB1",           "/Alarms/LowBattery"),
    "multiplus/alarm_overload":     ("com.victronenergy.vebus.ttyUSB1",           "/Alarms/Overload"),
}

PRODUCT_NAMES = {
    "com.victronenergy.system":               "Victron System",
    "com.victronenergy.solarcharger.ttyUSB0": "SmartSolar Charger MPPT 150/35",
    "com.victronenergy.vebus.ttyUSB1":        "MultiPlus-II 24/3000/70-32",
}

BUSITEM_IFACE_XML = """
<node>
  <interface name='com.victronenergy.BusItem'>
    <method name='GetValue'>
      <arg type='v' name='value' direction='out'/>
    </method>
  </interface>
</node>
"""

# Parse the interface XML once
_node_info = Gio.DBusNodeInfo.new_for_xml(BUSITEM_IFACE_XML)
_iface_info = _node_info.interfaces[0]


class ServiceConnection:
    """A private D-Bus connection that owns one bus name and exports objects."""

    def __init__(self, bus_address, bus_name):
        flags = (
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        )
        self.con = Gio.DBusConnection.new_for_address_sync(
            bus_address, flags, None, None
        )
        # Request the well-known name
        result = Gio.bus_own_name_on_connection(
            self.con, bus_name,
            Gio.BusNameOwnerFlags.NONE,
            None, None
        )
        self.bus_name = bus_name
        self._values = {}       # obj_path -> current value
        self._reg_ids = []

    def export_object(self, obj_path, initial_value=0.0):
        """Register an object at obj_path with a GetValue method."""
        self._values[obj_path] = initial_value
        reg_id = self.con.register_object(
            obj_path, _iface_info, self._handle_method_call, None, None
        )
        self._reg_ids.append(reg_id)

    def set_value(self, obj_path, value):
        self._values[obj_path] = value

    def _handle_method_call(self, connection, sender, object_path,
                            interface_name, method_name, parameters,
                            invocation):
        if method_name == "GetValue":
            val = self._values.get(object_path, 0.0)
            if isinstance(val, float):
                variant = GLib.Variant("v", GLib.Variant("d", val))
            elif isinstance(val, int):
                variant = GLib.Variant("v", GLib.Variant("i", val))
            else:
                variant = GLib.Variant("v", GLib.Variant("s", str(val)))
            invocation.return_value(GLib.Variant.new_tuple(variant))


def load_csv(path):
    """Load CSV and return list of row dicts."""
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def parse_value(raw):
    """Convert a CSV string to a float, falling back to the raw string."""
    try:
        return float(raw)
    except (ValueError, TypeError):
        return raw


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <csv_file>")
        sys.exit(1)

    csv_path = sys.argv[1]
    rows = load_csv(csv_path)
    if not rows:
        print("CSV file is empty")
        sys.exit(1)

    print(f"Loaded {len(rows)} rows from {csv_path}")

    bus_address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if not bus_address:
        print("Error: DBUS_SESSION_BUS_ADDRESS not set. Is a session bus running?")
        sys.exit(1)

    # Determine which bus names we need
    all_bus_names = set()
    for _, (bus_name, _) in COLUMN_MAP.items():
        all_bus_names.add(bus_name)
    for bus_name in PRODUCT_NAMES:
        all_bus_names.add(bus_name)

    # Create a separate connection per bus name
    services = {}
    for bus_name in sorted(all_bus_names):
        svc = ServiceConnection(bus_address, bus_name)
        services[bus_name] = svc
        print(f"Registered {bus_name}")

    # Export data objects
    for col, (bus_name, obj_path) in COLUMN_MAP.items():
        services[bus_name].export_object(obj_path, 0.0)
        print(f"  {bus_name}: {obj_path}")

    # Export ProductName objects
    for bus_name, product_name in PRODUCT_NAMES.items():
        services[bus_name].export_object("/ProductName", product_name)
        print(f"  {bus_name}: /ProductName = {product_name!r}")

    # Row advancement state
    row_index = [0]

    def apply_row(idx):
        row = rows[idx]
        for col, (bus_name, obj_path) in COLUMN_MAP.items():
            if col in row and row[col] != "":
                services[bus_name].set_value(obj_path, parse_value(row[col]))

    apply_row(0)
    print(f"\nServing row 0/{len(rows)-1} (time={rows[0].get('time', '?')})")

    def advance_row():
        row_index[0] = (row_index[0] + 1) % len(rows)
        apply_row(row_index[0])
        t = rows[row_index[0]].get("time", "?")
        print(f"Serving row {row_index[0]}/{len(rows)-1} (time={t})")
        return True  # keep the timeout active

    # Advance every 5 seconds (matches default log_interval)
    GLib.timeout_add_seconds(5, advance_row)

    print("\nMock D-Bus service running. Press Ctrl+C to stop.\n")
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
