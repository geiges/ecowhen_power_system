"""The safety agent: limits, and what to switch off when one is crossed.

The metric tables below hold no numbers of their own: every limit and every warn
threshold comes from `config.battery`, because they are calibration rather than code
and the warn thresholds in particular are now load-bearing — `warn_max` is where the
veto on `ac_solar` arms and releases, so tuning that deadband must not be a code
edit. What changed from the predecessor:

* **Bug #2 is gone.** `is_enabled` was a bare `True` with no `return`, so it answered
  `None`, and the runner never called it anyway (`control_runner.py:156`). The
  documented "both `enabled=False` and `confirmed_disable=True` required" protection
  did not exist in any form. It exists here, and the runner calls it.
* **A breach inhibits the coordinator** as well as commanding the actuators. Cutting
  the inverter while the wallbox mode still wants it would have the two fight every
  tick: the mode machinery must stand down, not be out-written.
* **A high reading vetoes `ac_solar` as well as cutting the plug.** The command is
  the hard cut; the veto stops the mode graph putting it straight back on, and it
  releases on the warn threshold rather than the limit so the relay does not cycle
  on the boundary. Safety may forbid a mode; it does not get to drive somebody
  else's actuator on.
* **Cooling is a mode request, not a write.** The fan is a shared service — the
  wallbox wants it too — so whoever lets go last switches it off. That is reference
  counting doing its job, and it is why cooling is not simply "write mppt100_load on".
* `heat_memory` was a *class* attribute, shared by every instance ever constructed.
  Now per-instance.
"""
from ..coordinator import ModeBlock, ModeRequest
from .base import AgentResult, BaseAgent, DirectCommand

# ponytail: one policy for every metric today — a low breach cuts what drains the
# battery (AC inverter mode + DC load), a high breach hard-cuts mains AC to the Deye
# via the smart plug. Give a metric its own "action" list when it needs to diverge.
_MIN_ACTIONS = [("multiplus_mode", False), ("mppt100_load", False)]
_MAX_ACTIONS = [("ac_inverter_relay", False)]
_AC_OVERLOAD_ACTIONS = [("multiplus_mode", False)]

# The mode that owns the actuator a max breach cuts. The command switches the plug
# off; this stops the mode graph switching it straight back on, and it is a veto
# rather than a second command because only the owner may drive an actuator.
#
# It arms at the *warn* threshold, not the limit, and that asymmetry is the whole
# point: cutting solar at the limit lowers the voltage, which clears the breach,
# which would re-arm the solar — a relay cycling on the boundary. Holding the veto
# until the value is back under `warn_max` is the deadband, and the numbers for it
# were already in the table below.
_MAX_VETOES = ["ac_solar"]


class SystemSafetyAgent(BaseAgent):
    name = "system_safety"
    fast_cycle = True

    def __init__(self):
        self.heat_memory = {"cooling_AC_load": 0}

    def is_enabled(self, config) -> bool:
        """Two flags, both required — this is the one agent that is on by default.

        A single `enabled: false` is not enough and never was meant to be: the REST
        surface refuses it without `confirmed_disable`, and so does this.
        """
        cfg = config.agents.system_safety
        return not (cfg.enabled is False and cfg.confirmed_disable is True)

    @staticmethod
    def _safety_metrics(bcfg):
        return {
            "voltage": {
                "value": lambda s: s.battery_voltage,
                "min": bcfg.min_voltage,
                "max": bcfg.max_voltage,
                "warn_min": bcfg.warn_min_voltage,
                "warn_max": bcfg.warn_max_voltage,
                "label": "voltage",
                "fmt": ".2f",
                "unit": "V",
                "action": {"min": _MIN_ACTIONS, "max": _MAX_ACTIONS},
                "veto": {"max": _MAX_VETOES},
            },
            "temp": {
                "value": lambda s: s.battery_temp,
                "min": bcfg.min_temp,
                "max": bcfg.max_temp,
                "warn_min": bcfg.warn_min_temp,
                "warn_max": bcfg.warn_max_temp,
                "label": "temperature",
                "fmt": ".1f",
                "unit": "°C",
                "action": {"min": _MIN_ACTIONS, "max": _MAX_ACTIONS},
                "veto": {"max": _MAX_VETOES},
            },
            "ac_load": {
                "value": lambda s: s.ac_load_w,
                "min": None,
                "max": bcfg.max_ac_load_w,
                "warn_min": None,
                "warn_max": bcfg.warn_max_ac_load_w,
                "label": "AC load output",
                "fmt": ".1f",
                "unit": "W",
                "action": {"max": _AC_OVERLOAD_ACTIONS},
            },
        }

    @staticmethod
    def _cooling_metrics(bcfg):
        return {
            "cooling_AC_load": {
                "value": lambda s: s.ac_load_w,
                "max": 1000,
                "label": "AC cooling load",
                "fmt": ".0f",
                "unit": "W",
                "n_req": 1,
            },
        }

    def run(self, projection, config) -> AgentResult:
        current = projection.current
        breaches = []
        notes = []
        ok_parts = []
        metrics = {}
        actions = []
        vetoes = {}                 # mode -> why it is being held off

        for key, spec in self._safety_metrics(config.battery).items():
            value = spec["value"](current)
            fmt, unit = spec["fmt"], spec.get("unit", "")
            margins = []

            metrics[f"{key}_value"] = value
            if spec["warn_min"] is not None:
                metrics[f"{key}_warn_min"] = spec["warn_min"]
            if spec["warn_max"] is not None:
                metrics[f"{key}_warn_max"] = spec["warn_max"]

            if spec["min"] is not None:
                margin = value - spec["min"]
                margins.append(margin)
                metrics[f"min_{key}_margin"] = round(margin, 4)
                if margin < 0:
                    breaches.append(
                        f"{spec['label']} {value:{fmt}}{unit} below limit "
                        f"{spec['min']:{fmt}}{unit}"
                    )
                    actions += spec["action"].get("min", [])

            if spec["max"] is not None:
                margin = spec["max"] - value
                margins.append(margin)
                metrics[f"max_{key}_margin"] = round(margin, 4)
                if margin < 0:
                    breaches.append(
                        f"{spec['label']} {value:{fmt}}{unit} above limit "
                        f"{spec['max']:{fmt}}{unit}"
                    )
                    actions += spec["action"].get("max", [])

            # Deliberately outside the breach test, and against the warn threshold:
            # this is the deadband. Once the value is above `warn_max` the mode
            # stays vetoed all the way through the breach and back down again, so
            # the plug does not cycle around the limit.
            if spec["warn_max"] is not None and value > spec["warn_max"]:
                for mode in spec.get("veto", {}).get("max", []):
                    vetoes.setdefault(
                        mode,
                        f"{spec['label']} {value:{fmt}}{unit} above "
                        f"{spec['warn_max']:{fmt}}{unit}",
                    )

            ok_parts.append(
                f"{spec['label']} {value:{fmt}}{unit} (margin {min(margins):+{fmt}}{unit})"
            )

        # Cooling — a want, not a breach. It asks for a mode and never writes.
        cooling_wanted = False
        for key, spec in self._cooling_metrics(config.battery).items():
            value = spec["value"](current)
            margin = spec["max"] - value
            metrics[f"{key}_margin"] = round(margin, 4)
            if margin < 0:
                self.heat_memory[key] = min(self.heat_memory[key] + 1, spec["n_req"])
            else:
                self.heat_memory[key] = max(self.heat_memory[key] - 1, 0)

            if self.heat_memory[key] >= spec["n_req"]:
                cooling_wanted = True
                notes.append(f"{spec['label']} heat limit reached")
            elif self.heat_memory[key] > 0:
                notes.append(
                    f"{spec['label']} at cooling level "
                    f"{self.heat_memory[key]}/{spec['n_req']}"
                )

        reason = "; ".join(breaches)

        # De-duplicate by actuator: two breaches wanting the same thing off is one
        # command, and the first one named wins.
        commands = []
        seen = set()
        for actuator, on in actions:
            if actuator in seen:
                continue
            seen.add(actuator)
            commands.append(DirectCommand(actuator=actuator, on=on, reason=reason))

        blocks = [
            ModeBlock(mode=mode, agent=self.name, reason=why)
            for mode, why in sorted(vetoes.items())
        ]

        requests = []
        if cooling_wanted and not breaches:
            # Not while a limit is breached: the inhibit that follows would drop the
            # request on the same tick anyway, and a min-breach is busy switching the
            # very same fan off.
            requests.append(ModeRequest(mode="cooling", active=True, agent=self.name,
                                        reason="; ".join(notes)))

        if vetoes:
            # After the cooling request is built, so it does not end up quoted as
            # the reason somebody wanted the fan.
            notes.append("holding off " + ", ".join(sorted(vetoes)))

        if breaches:
            rationale = "SAFETY ACTION: " + reason
        else:
            rationale = "OK — " + ", ".join(ok_parts + notes)

        return AgentResult(
            agent_name=self.name,
            rationale=rationale,
            metrics=metrics,
            requests=requests,
            blocks=blocks,
            commands=commands,
            inhibit=reason,
        )
