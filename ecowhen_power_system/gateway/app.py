"""Flask application factory for the Gateway.

Harvested in shape from `ecowhen_data_api/app.py` and `auth.py`, minus its config
loader: the actuator registry arrives fully resolved in `system_configuration.yaml`
and the file roots come from `paths.py`, so there is nothing left for a config file
to say that would not just be a second place to get it wrong.
"""
import logging
import os

from flask import Flask, jsonify, request

from .. import discovery, paths
from .actuators import actuators_bp
from .control import control_bp
from .files import default_endpoints, files_bp

API_KEY_ENV = "ECOPS_API_KEY"


class ActuatorRegistry:
    """The actuator half of the topology contract, re-read when it changes.

    The Power System rewrites `system_configuration.yaml` at every start, and it is
    supposed to start before this process — but it also gets restarted on its own,
    and a Gateway holding addresses from a previous boot would fail every write with
    a confusing message. Stat-per-request is cheap next to a D-Bus round trip.

    It also carries `state`, what each actuator last read — see `actuators.py`. It
    lives here rather than in a module global so it is per-app (a test client gets an
    empty one) and so a topology change clears it: new addresses, new hardware, and
    nothing worth remembering about the old ones.
    """

    def __init__(self, path):
        self.path = path
        self._mtime = None
        self._actuators = {}
        self.state = {}             # name -> (body, http_status, monotonic)

    def current(self) -> dict:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return self._actuators      # not published yet, or gone; routes say so
        if mtime != self._mtime:
            config = discovery.load_system_configuration(self.path)
            self._actuators = config.get("actuators", {})
            self._mtime = mtime
            self.state.clear()
            logging.info("gateway: loaded %d actuators from %s",
                         len(self._actuators), self.path)
        return self._actuators


def check_api_key():
    """Validate X-API-Key on every request except /health.

    Registered as a before_request hook so every route is protected without
    per-route decoration. Fails closed: an unset key is a 503, never open access.
    """
    if request.path == "/health":
        return None

    expected = os.environ.get(API_KEY_ENV)
    if not expected:
        return jsonify({"error": "API key not configured on server"}), 503

    if request.headers.get("X-API-Key", "") != expected:
        return jsonify({"error": "unauthorized"}), 401

    return None


def create_app(bus=None, system_config_path=None, read_only=False) -> Flask:
    """Build the Gateway.

    `bus` is injected — the same seam every other D-Bus entry point in this package
    uses, which is what lets the whole write path be tested off-device against the
    fake bus. Left None, it connects to the real system bus lazily on first use.
    """
    app = Flask(__name__)

    app.config["BUS"] = bus if bus is not None else _LazyBus()
    app.config["ACTUATOR_REGISTRY"] = ActuatorRegistry(
        system_config_path or paths.SYSTEM_CONFIG_PATH
    )
    app.config["FILE_ENDPOINTS"] = default_endpoints()
    app.config["READ_ONLY"] = read_only

    app.before_request(check_api_key)

    @app.get("/health")
    def health():
        """Unauthenticated liveness check."""
        return jsonify({"status": "ok"})

    app.register_blueprint(actuators_bp, url_prefix="/actuators")
    app.register_blueprint(files_bp, url_prefix="/files")
    app.register_blueprint(control_bp, url_prefix="/control")

    return app


class _LazyBus:
    """Connect to D-Bus on first use, not at import or app-creation time.

    The Gateway must be constructible off-device (that is how it is tested) and must
    survive being started before the bus is ready, so the connection is deferred to
    the first actuator call and then kept.
    """

    def __init__(self):
        self._bus = None

    def _connect(self):
        if self._bus is None:
            from ..manufacturer.victron import get_bus
            self._bus = get_bus()
        return self._bus

    def get(self, service, path="/"):
        return self._connect().get(service, path)

    @property
    def dbus(self):
        return self._connect().dbus
