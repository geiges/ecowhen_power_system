#!/usr/bin/env python3
"""Diff the new `state.json` against the predecessor's, on the device.

This is the Phase 1 verification the plan has never been able to run: off a fake bus
only the *key names* could be checked, and key names are the easy half. Here both
systems are polling the same hardware at the same time, so the values can be compared
too.

    ./deploy/compare.py                 # one shot
    ./deploy/compare.py --watch 30      # every 30 s, until Ctrl-C

Read the two caveats before reading the numbers:

1. **The two samples are not simultaneous.** Each system polls on its own clock, so
   anything that is actually moving will differ by however far it moved in between.
   The age gap is printed first for that reason — judge a difference against it, and
   prefer a quiet moment (no sun, no load) for the strict comparison.

2. **A12 changed what some keys mean, not just what they are called.** The old
   `system/battery_voltage` was overwritten by the simulator with a derived value;
   the new contract keeps the bus reading under that name and puts the derived one
   under `battery/voltage`. Those pairs are compared across the rename; the keys
   whose meaning changed in place are listed separately and are *expected* to differ.
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

OLD_STATE = Path("/data/python/victron_system_monitor/data/state.json")

# Same quantity, new name (A12). These must still agree.
RENAMED = {
    "system/battery_voltage": "battery/voltage",
    "system/battery_current": "battery/current",
}

# Same name, different meaning. A difference here is the point, not a regression.
MEANING_CHANGED = {
    key: "A12: was the simulator's derived value, is now the raw bus reading"
    for key in (
        "mppt150/DC_0_voltage", "mppt100/DC_0_voltage", "multiplus/DC_0_voltage",
        "mppt150/DC_0_current", "mppt100/DC_0_current", "multiplus/DC_0_current",
        "system/battery_voltage", "system/battery_current",
    )
}
# The state code is one digit per logged state, in the contract's order. A10 added
# three DS18B20 sensors, so the new string is longer and every position after the
# first has moved. Compare `states_to_log` in system_configuration.yaml, not this.
MEANING_CHANGED["state"] = "A10: positional, and three sensors were added"

# Not values: formats, clocks, and keys that only one side ever had.
IGNORED = {"time", "timestamp", "running_since"}

TOLERANCE = 1e-6


def load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return None


def age_gap(old, new):
    """How far apart the two samples are, in seconds, or None if unknowable."""
    try:
        new_t = datetime.fromisoformat(new["timestamp"])
    except (KeyError, ValueError):
        return None
    old_time = old.get("time")          # the predecessor logs "HH:MM:SS", no date
    if not old_time:
        return None
    try:
        hh, mm, ss = (int(x) for x in old_time.split(":"))
    except ValueError:
        return None
    old_t = new_t.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    return abs((new_t - old_t).total_seconds())


def compare(old, new):
    gap = age_gap(old, new)
    print(f"old sample: {old.get('time', '?')}   new sample: {new.get('timestamp', '?')}")
    if gap is not None:
        print(f"the two samples are {gap:.0f} s apart — moving values will differ by "
              f"however far they moved")
    print()

    old_keys, new_keys = set(old) - IGNORED, set(new) - IGNORED
    missing = sorted(k for k in old_keys - new_keys if k not in RENAMED)
    added = sorted(k for k in new_keys - old_keys if k not in RENAMED.values())

    if missing:
        print(f"MISSING from the new contract ({len(missing)}):")
        for key in missing:
            print(f"    {key}")
    else:
        print("MISSING from the new contract: none — it is a strict superset")
    print(f"\nADDED by the new contract ({len(added)}): {', '.join(added) or 'none'}")

    pairs = [(k, k) for k in sorted(old_keys & new_keys)]
    pairs += [(o, n) for o, n in RENAMED.items() if o in old and n in new]

    same, moved, changed_meaning = [], [], []
    for old_key, new_key in pairs:
        o, n = old[old_key], new[new_key]
        label = old_key if old_key == new_key else f"{old_key} -> {new_key}"
        expected = old_key == new_key and old_key in MEANING_CHANGED
        if isinstance(o, (int, float)) and isinstance(n, (int, float)) \
                and not isinstance(o, bool):
            delta = abs(float(n) - float(o))
            row = (label, float(o), float(n), delta)
        else:
            row = (label, o, n, 0.0 if str(o) == str(n) else None)
        if expected:
            changed_meaning.append(row)
        elif row[3] is not None and row[3] <= TOLERANCE:
            same.append(row)
        else:
            moved.append(row)

    def table(title, rows):
        if not rows:
            return
        print(f"\n{title} ({len(rows)}):")
        print(f"    {'key':42} {'old':>14} {'new':>14} {'delta':>12}")
        for label, o, n, delta in sorted(
            rows, key=lambda r: -(r[3] or 0) if isinstance(r[3], float) else 0
        ):
            d = f"{delta:12.6f}" if isinstance(delta, float) else f"{'':>12}"
            print(f"    {label:42} {str(o)[:14]:>14} {str(n)[:14]:>14} {d}")

    table("IDENTICAL", same)
    table("DIFFERENT — check against the sample gap above", moved)
    table("EXPECTED TO DIFFER — the contract changed here, see the reasons below",
          changed_meaning)
    if changed_meaning:
        for key, reason in sorted(MEANING_CHANGED.items()):
            if any(r[0] == key for r in changed_meaning):
                print(f"    {key:42} {reason}")

    print(f"\nsummary: {len(same)} identical, {len(moved)} different, "
          f"{len(changed_meaning)} expected-different, {len(missing)} missing")
    return not missing


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--old", default=OLD_STATE, help=f"default {OLD_STATE}")
    parser.add_argument("--new", default=None,
                        help="default $ECOPS_RUNTIME_DIR/state.json")
    parser.add_argument("--watch", type=float, metavar="SECONDS",
                        help="repeat forever at this interval")
    args = parser.parse_args(argv)

    if args.new is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from ecowhen_power_system import paths
        args.new = paths.STATE_PATH

    while True:
        old, new = load(args.old), load(args.new)
        if old is None or new is None:
            return 1
        print("=" * 88)
        ok = compare(old, new)
        if not args.watch:
            return 0 if ok else 1
        time.sleep(args.watch)


if __name__ == "__main__":
    sys.exit(main())
