# Deploying phases 1–3 beside the live system

> **Following the procedure rather than reading about it?** `CUTOVER.md` in this
> directory is the ordered runbook, end to end. This file is the reference behind
> it — why parallel mode is an interlock, how the two halves of the D-Bus bindings
> fail differently, and what `.python-version` does to a venv.

The predecessor keeps running and keeps control of the hardware. This deployment
only *reads*, so both can sit on the same bus and every number can be compared
against a working reference.

```
                    Venus OS box
   /data/python/victron_system_monitor    ← still live, still in control
   /data/python/ecowhen_power_system      ← this, read-only
```

## What keeps it safe

Parallel mode is an interlock, not a habit. `ECOPS_PARALLEL=1` (the default in
`config.sh`) passes two flags:

| flag | effect |
|---|---|
| `ecops-power-system --no-startup-sequence` | skips the Multiplus measurement wake-up (A11) — **the only place the Power System writes D-Bus**. With it off, the process cannot write the bus at all |
| `ecops-gateway --read-only` | every `POST /actuators/...` is a 403. Reads still work |

Nothing else in phases 1–3 writes hardware. The Logger only writes CSVs.

Everything else that could collide is separated too:

| | old | new |
|---|---|---|
| project dir | `/data/python/victron_system_monitor` | `/data/python/ecowhen_power_system` |
| REST port | 5100 | **5101** |
| durable data | its own `data/` | `$ECOPS_REMOTE_DIR/data` |
| ephemeral | — | `/dev/shm/ecowhen_power_system` |
| tmux session | `victron` | `ecops` |

The one real cost of running both: the two Tasmota plugs and the Deye get polled
twice as often. They are HTTP GETs against devices the old system already polls
every 5 s, so this doubles a load that is already small.

## Doing it

**From the dev machine:**

```sh
./deploy/push.sh                     # rsync the working tree to the box
```

**On the device**, in `/data/python/ecowhen_power_system`:

```sh
./deploy/install.sh                  # venv + deps + generate an API key
./deploy/start.sh                    # tmux session 'ecops': 3 windows
tmux attach -t ecops                 # watch them
```

### Two halves of the D-Bus bindings

They come from different places and fail differently, which is worth knowing before
something goes wrong:

| | where from | how it gets there |
|---|---|---|
| **`pydbus`** | PyPI, pure Python (`py2.py3-none-any.whl`, no compiled parts) | `uv sync --extra device` |
| **`gi`** (PyGObject) | the OS — it is compiled against the system libraries | `uv venv --system-site-packages` |

`install.sh` does both and then checks them **separately**, because `import pydbus`
pulls in `gi` and a single check cannot tell the two failures apart:

- `No module named 'pydbus'` → the `device` extra was not installed. It is *optional*
  in this package so the test suite installs off-device (A3), so a plain `uv sync`
  leaves it out. The predecessor has pydbus as a hard dependency, which is why the
  same recipe works there and did not here.
- `No module named 'gi'` → the venv cannot see the OS bindings. Either PyGObject is
  not installed (`opkg install python3-pygobject`) or `.venv` was built without
  `--system-site-packages` — `rm -rf .venv && ./deploy/install.sh`.

Every `uv run` in `start.sh` also passes `--extra device`, so the requirement is
visible at the point of use and a deleted venv recovers by itself.

**The venv must be built on the same python the OS built PyGObject for**, which is why
`install.sh` passes `--python /usr/bin/python3` explicitly. Override with
`SYS_PYTHON=/usr/bin/python3.11 ./deploy/install.sh` if the box has several.

> **There must be no `.python-version` file in this repo.** `uv venv
> --system-site-packages --python /usr/bin/python3` does the right thing, and then
> `uv sync` **silently destroys and rebuilds the venv** on whatever interpreter
> `.python-version` names — losing `--system-site-packages`, and with it `gi`. It
> leaves no trace beyond a venv that cannot import `gi`. Reproduced exactly:
>
> ```
> with .python-version=3.13:  uv venv -> 3.12.3, system-site=true
>                             uv sync -> 3.13.1, system-site=false   -> no gi
> without it:                 uv venv -> 3.12.3, system-site=true
>                             uv sync -> 3.12.3, system-site=true    -> gi OK
> ```
>
> `UV_PYTHON_DOWNLOADS=never` does **not** save you — uv happily reuses a *cached*
> managed interpreter. Deleting the file is the fix; `requires-python = ">=3.12"` is
> the real constraint and is enough on its own. `install.sh` now fails loudly if the
> venv comes out of the sync without system site packages.

To see which half is broken, run the check by hand:

```sh
uv run --no-dev --extra device python -c "import gi; print('gi ok')"
uv run --no-dev --extra device python -c "import pydbus; print('pydbus ok')"
```

Add `--boot` to `install.sh` to also hook `/data/rc.local`, which Venus OS keeps
across firmware updates. Leave that off while validating.

## Checking it

```sh
./deploy/compare.py                  # one shot
./deploy/compare.py --watch 30       # keep looking
```

This is the Phase 1 verification the plan has never been able to run: off a fake bus
only key *names* could be checked. Here both systems poll the same hardware, so the
values can be compared too.

Two things to hold in mind while reading it:

1. **The samples are not simultaneous.** Each system polls on its own clock, so
   anything moving differs by however far it moved. The gap is printed first. For the
   strict comparison, pick a quiet moment — night, no load.
2. **Some keys changed meaning, not just name.** Those are listed separately with the
   reason. `system/battery_voltage` was the simulator's derived value and is now the
   raw bus reading (A12); the derived one moved to `battery/voltage` and *is* compared
   across that rename. The packed `state` code is positional and grew from 6 to 12
   digits when A10 added three temperature sensors — compare `states_to_log` in
   `system_configuration.yaml` instead.

What "good" looks like: **0 missing**, the renamed pair agreeing to a few mV at rest,
and every non-moving key identical.

For the CSVs, the aggregation differential has already been run off-device against 151
real days (A18); on-device, compare `data/log_*.csv` headers and spot-check a row.

## Going to full control (phase 4, later)

Stop the predecessor first — two writers on one actuator is the one genuinely
dangerous failure in this rebuild. Then, in two steps:

```sh
ECOPS_PARALLEL=0 ./deploy/start.sh --restart --no-control   # 1. prove the writes
ECOPS_PARALLEL=0 ./deploy/start.sh --restart                # 2. bring Control up
```

`start.sh` prints a warning and waits 5 s before starting in either mode.

**Why two steps.** The VE.Direct and Tasmota *write* transports have never run —
parallel mode 403s every POST — and they can only be proven by hand. They cannot be
proven while Control runs: its coordinator drives every service back to target on
each tick, so a hand-switched fan is off again within 60 s and the test tells you
nothing. Between the two commands, nothing arbitrates the hardware; whatever you
switch stays switched.

What to run in step 1, on `mppt100_load` and then `wallbox_relay`:

```sh
curl -s -H "X-API-Key: $ECOPS_API_KEY" -H 'Content-Type: application/json' \
     -d '{"on": true}' http://127.0.0.1:5101/actuators/mppt100_load
curl -s -H "X-API-Key: $ECOPS_API_KEY" http://127.0.0.1:5101/actuators/mppt100_load
```

**Judge it on the second call, not the first.** A POST reports the value it *wrote*;
a `vregd` that exits 0 without moving the fan looks identical to one that worked.
On the fan expect `4` written and `4` read; switching off writes `0` and reads `1`,
which is the write/read asymmetry surviving the round trip. A 502 names the failure
— `vregd executable not found`, `vregd failed: …`, or `unreachable at […]`.
