"""What this installation actually has: seven services, six modes.

The spike's `service.py` / `mode.py` are abstract on purpose — they own the state
machines and know nothing about hardware. Everything hardware-shaped is here, and it
is thin, because the Gateway already resolved the hard part.

**Every actuator is a service; a mode is a set of services.** The two layers are still
deliberately named differently, and now the rule is explicit: a *service* is named for
the hardware it switches, a *mode* for what the user wants (A8):

    service            the actuator it drives     the hardware
    ---------------    ----------------------     ---------------------------------
    multiplus_ac       multiplus_mode             the Multiplus II inverter
    mppt100_dc_load    mppt100_load               the MPPT's DC load — the fans
    mppt150_charger    mppt150_mode               the MPPT 150/35 solar charger
    mppt100_charger    mppt100_mode               the MPPT 100/20 solar charger
    wallbox_charger    wallbox_relay              the wallbox charging plug
    deye_inverter      ac_inverter_relay          mains to the Deye (the plug has no
                                                  function of its own)
    other_ac_supply    ac_other_relay             the spare AC socket

    mode        primary service    prerequisites
    --------    ---------------    -----------------------------
    wallbox     wallbox_charger    multiplus_ac
    ac_solar    deye_inverter      multiplus_ac
    ac_other    other_ac_supply    multiplus_ac  (blocked_by wallbox)
    cooling     —                  mppt100_dc_load
    main_ac     —                  multiplus_ac
    dc_solar    —                  mppt150_charger, mppt100_charger

A mode's *primary* service is the one it holds only once its prerequisites are
satisfied, and lets go of first on the way down. That is the two-level ordering, and
it is the whole reason a plug is not simply another prerequisite: parallel would
switch the wallbox on before the inverter had mains.

What this bought over commanding the plug from the mode directly: the plugs now have
a retry budget, a `failed` phase and readback verification, which `apply_primary`
never had — it re-POSTed forever and never failed.

There is still no way to *forbid* a service. Safety blocks the mode; the primary is
released by the state machine below and the service reverts on its own count.
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
        """The Gateway's remembered reading — the hardware only when it must be.

        It answers from memory, refreshed by every write, which is why a settled
        system touches no hardware at all. The one case memory cannot resolve is an
        actuator that read *unknown* while something is waiting on it: nothing will
        write it (a service never drives blind), so nothing would ever refresh the
        entry and the service would wait forever on a device that may have come back.
        """
        on = self.gateway.read(self.actuator)
        if on is None and self.required_by:
            return self.gateway.read(self.actuator, fresh=True)
        return on

    def drive(self, on, config):
        return self.gateway.command(self.actuator, on)


class ServiceMode(Mode):
    """A mode is its services: prerequisites, plus at most one primary.

    `cooling` and `main_ac` have no primary — they *are* their requirements, and
    reference counting is then the whole feature: whichever of cooling and wallbox
    lets go of the fans last is the one that switches them off.
    """

    def __init__(self, name, services, required_services, primary=None,
                 blocked_by=None):
        super().__init__(name=name, required_services=required_services,
                         blocked_by=blocked_by)
        self.services = services
        self.primary = primary

    def declare_requirements(self, services):
        """Prerequisites via the base class; the primary level-triggered on top.

        The primary is required and released *here*, in the declare pass, and not in
        `apply_primary` during the mode reconcile — the coordinator runs
        declare → services → modes, so a release deferred to the reconcile would come
        one pass too late. The tick where safety cuts the Deye plug by direct command
        and blocks `ac_solar` would then find the service still targeting `on` with
        the hardware off, and switch it straight back on.
        """
        super().declare_requirements(services)
        if self.primary is None:
            return
        held = self.state in ("activating", "active") and all(
            s.satisfied for s in self._mine(services))
        primary = services[self.primary]
        if held:
            primary.require(self.name)
        else:
            primary.release(self.name)

    def apply_primary(self, on, config):
        return True                      # declare_requirements already said so

    def primary_satisfied(self, on, config):
        if self.primary is None:
            return True
        service = self.services[self.primary]
        # "idle" and not just "satisfied": a service nobody requires, sitting at its
        # default, is settled off — that is what the teardown is waiting to see.
        return service.target_on == on and service.phase in ("satisfied", "idle")

    def status(self):
        status = super().status()
        if self.primary:
            # Everything the mode drives, not only what it waits on. `_mine` still
            # sees the prerequisites alone, which is what gates the primary.
            status.required_services = status.required_services + [self.primary]
        return status


def build(gateway, config):
    """The services and modes of this installation. Returns (services, modes)."""
    services = {}
    for service in (
        ActuatorService("multiplus_ac", "multiplus_mode", gateway,
                        default_on=config.services.multiplus_ac_default_on),
        ActuatorService("mppt100_dc_load", "mppt100_load", gateway,
                        default_on=config.services.mppt100_dc_load_default_on),
        # The two solar chargers. Their default is *on*: a charger nobody has an
        # opinion about should be charging, which is why the knob exists at all.
        ActuatorService("mppt150_charger", "mppt150_mode", gateway,
                        default_on=config.services.mppt_charger_default_on),
        ActuatorService("mppt100_charger", "mppt100_mode", gateway,
                        default_on=config.services.mppt_charger_default_on),
        # The three plugs. No default_on knob: a socket nobody asked for is off.
        ActuatorService("wallbox_charger", "wallbox_relay", gateway),
        ActuatorService("deye_inverter", "ac_inverter_relay", gateway),
        ActuatorService("other_ac_supply", "ac_other_relay", gateway),
    ):
        services[service.name] = service

    modes = {
        m.name: m for m in (
            # Exactly the predecessor's wallbox_on step list — inverter, then fan,
            # then plug — except that here it is a declaration, and the teardown
            # falls out of it instead of being written backwards in a second file.
            # ponytail: mppt100_dc_load dropped from the prerequisites while the
            # MPPT's load output is broken — cooling is wired to the AC inverter
            # now, so it comes up with multiplus_ac. Put it back when the load
            # switch works again.
            ServiceMode("wallbox", services, primary="wallbox_charger",
                        required_services=["multiplus_ac"]),
            # Both plugs hang off the Multiplus's AC output, so mains has to be there
            # before switching one on is worth anything.
            ServiceMode("ac_solar", services, primary="deye_inverter",
                        required_services=["multiplus_ac"]),
            # Both sockets on one inverter would sit over the AC limit, so the spare
            # yields to the charger. Declared here and only here: the mode that gives
            # way is the one that says so. `wallbox` knows nothing about it.
            ServiceMode("ac_other", services, primary="other_ac_supply",
                        required_services=["multiplus_ac"], blocked_by=["wallbox"]),
            ServiceMode("cooling", services,
                        required_services=["mppt100_dc_load"]),
            # The user's way to say "I want mains".
            ServiceMode("main_ac", services, required_services=["multiplus_ac"]),
            # Both chargers under one switch. No primary: they *are* the mode, and
            # neither has to come up before the other. While
            # `mppt_charger_default_on` stays True this can only ever switch them on
            # — including back on after somebody switched one off by hand. Set it
            # False and dc_solar becomes what decides whether the array charges.
            ServiceMode("dc_solar", services,
                        required_services=["mppt150_charger", "mppt100_charger"]),
        )
    }
    return services, modes
