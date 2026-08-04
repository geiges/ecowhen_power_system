"""What this installation actually has: two services, five modes.

The spike's `service.py` / `mode.py` are abstract on purpose — they own the state
machines and know nothing about hardware. Everything hardware-shaped is here, and it
is thin, because the Gateway already resolved the hard part.

Names live on two different layers, and they are deliberately not the same (A8):

    service / mode name     the actuator it drives
    --------------------    ---------------------
    ac_inverter             multiplus_mode
    fan                     mppt100_load
    wallbox (mode)          wallbox_relay
    ac_solar (mode)         ac_inverter_relay
    ac_other (mode)         ac_other_relay
    cooling (mode)          — nothing of its own; it only wants the fan
    ac_on (mode)            — nothing of its own; it only wants ac_inverter

`ac_inverter_relay` (the Tasmota feeding mains to the Deye) is still deliberately
*not a service*: it has exactly one owner, the `ac_solar` mode, so it is never
reference-counted and no second requester can hold it on. The safety agent still
cuts it as a direct command at the top priority, and its inhibit stands `ac_solar`
down for as long as the breach lasts — but when the breach clears and the mode is
still wanted, the mode switches it back on. That is new: it used to take a human.
"""
from .mode import Mode
from .service import SystemService


class ActuatorService(SystemService):
    """A service that is exactly one Gateway actuator."""

    def __init__(self, name, actuator, gateway, default_on=False, **kw):
        super().__init__(name=name, default_on=default_on, **kw)
        self.actuator = actuator
        self.gateway = gateway

    def read_actual_on(self, config):
        return self.gateway.read(self.actuator)

    def drive(self, on, config):
        return self.gateway.command(self.actuator, on)


class ActuatorMode(Mode):
    """A mode whose own effect is one Gateway actuator, gated on its services."""

    def __init__(self, name, actuator, gateway, required_services, blocked_by=None):
        super().__init__(name=name, required_services=required_services,
                         blocked_by=blocked_by)
        self.actuator = actuator
        self.gateway = gateway

    def apply_primary(self, on, config):
        return self.gateway.command(self.actuator, on)

    def primary_satisfied(self, on, config):
        return self.gateway.read(self.actuator) is on


class ServiceOnlyMode(Mode):
    """A mode that *is* its requirements — it has no actuator of its own.

    `cooling` is this: the safety agent asks for the fan when the AC load has been
    high for long enough, and the fan is already a shared service because the wallbox
    needs it too. Reference counting is then the whole feature — whichever of the two
    lets go last is the one that switches the fan off.
    """

    def apply_primary(self, on, config):
        return True

    def primary_satisfied(self, on, config):
        return True


def build(gateway, config):
    """The services and modes of this installation. Returns (services, modes)."""
    services = {
        s.name: s for s in (
            ActuatorService("ac_inverter", "multiplus_mode", gateway,
                            default_on=config.services.ac_inverter_default_on),
            ActuatorService("fan", "mppt100_load", gateway,
                            default_on=config.services.fan_default_on),
        )
    }
    modes = {
        m.name: m for m in (
            # The wallbox needs mains from the inverter, and the compartment fan for
            # the hours of DC load that follow. Exactly the predecessor's wallbox_on
            # step list — inverter, then fan, then plug — except that here it is a
            # declaration, and the teardown falls out of it instead of being written
            # backwards in a second file.
            ActuatorMode("wallbox", "wallbox_relay", gateway,
                         required_services=["ac_inverter", "fan"]),
            # Both plugs hang off the Multiplus's AC output, so mains has to be
            # there before switching one on is worth anything — hence the same
            # requirement, and hence a mode each rather than a bare actuator POST.
            # `ac_solar` re-energises the Deye; `ac_other` is the spare socket.
            ActuatorMode("ac_solar", "ac_inverter_relay", gateway,
                         required_services=["ac_inverter"]),
            # Both sockets on one inverter would sit over the AC limit, so the
            # spare yields to the charger. Declared here and only here: the mode
            # that gives way is the one that says so, which is the whole priority
            # rule. `wallbox` knows nothing about it.
            ActuatorMode("ac_other", "ac_other_relay", gateway,
                         required_services=["ac_inverter"],
                         blocked_by=["wallbox"]),
            ServiceOnlyMode(name="cooling", required_services=["fan"]),
            # The user's way to say "I want mains". Same shape as cooling: no
            # actuator of its own, it just holds a reference on ac_inverter, so
            # it composes with wallbox instead of fighting it.
            ServiceOnlyMode(name="ac_on", required_services=["ac_inverter"]),
        )
    }
    return services, modes
