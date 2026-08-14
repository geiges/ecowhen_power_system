# Deploying

This system has full control of the hardware. The predecessor
(`/data/python/victron_system_monitor`) is stopped and stays stopped — two writers
on one actuator is the one genuinely dangerous failure here. Reverting means killing
the `ecops` tmux session and restarting its `control_runner.py`.

Nothing collides while it is stopped, and nothing would if it were started by hand
for a look: separate project dir, port 5101 against its 5100, its own `data/`, and
tmux session `ecops` against `victron`.

## Doing it

**From the dev machine:**

```sh
./deploy/push.sh                     # rsync the working tree to the box
```

**On the device**, in `/data/python/ecowhen_power_system`:

```sh
./deploy/install.sh                  # venv + deps + generate an API key
./deploy/start.sh                    # tmux session 'ecops': 4 windows
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
across firmware updates.

## Checking it

`GET /health` is the first stop, then `GET /control/modes` and
`data/control_log.jsonl` for what the brain thinks it is doing.

## Proving a write transport by hand

Control drives every service back to target on each tick, so a hand-switched fan is
off again within a tick or two and the test tells you nothing. Start without it:

```sh
./deploy/start.sh --restart --no-control    # nothing arbitrates; what you switch stays
./deploy/start.sh --restart                 # Control back up
```

What to run, on `mppt100_load` and then `wallbox_relay`:

```sh
curl -s -H "X-API-Key: $ECOPS_API_KEY" -H 'Content-Type: application/json' \
     -d '{"on": true}' http://127.0.0.1:5101/actuators/mppt100_load
curl -s -H "X-API-Key: $ECOPS_API_KEY" http://127.0.0.1:5101/actuators/mppt100_load
```

**Judge it on the second call, not the first.** A POST reports the value it *wrote*;
a `vregd` that exits 0 without moving the fan looks identical to one that worked.
On the fan expect the register's `4` written and the bus's `1` read, with
`"measured"` at or above 0.1 A — that current is what proves the fan turns, not the
state. Switching off writes `0` and reads `0` with `measured` at 0.0. A 502 names the failure
— `vregd executable not found`, `vregd failed: …`, or `unreachable at […]`.
