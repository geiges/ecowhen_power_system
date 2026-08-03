"""② Gateway — the process that owns hardware writes.

Starts after the Power System, which publishes the actuator registry it serves.
Nothing else in the system may write D-Bus, VE.Direct or a Tasmota plug: Control
asks for a logical actuator by name over REST, and the value semantics were settled
once, by the component that declares them.

    ecops-gateway [--host H] [--port P]
"""
import argparse
import logging
import os
import sys

from . import paths
from .gateway.app import API_KEY_ENV, create_app

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5100


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Gateway — the sole hardware writer")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--read-only", action="store_true",
        help="serve reads but refuse every actuation with 403. Required while "
             "another system is still driving this hardware.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    from dotenv import load_dotenv
    load_dotenv()

    app = create_app(read_only=args.read_only)
    if args.read_only:
        print("[gateway] READ-ONLY — actuation disabled")

    if not os.environ.get(API_KEY_ENV):
        # Requests already fail closed with a 503; this just says why at startup
        # instead of at 3am from the other end of the wire.
        print(f"[gateway] {API_KEY_ENV} is not set — every request will be refused")

    registry = app.config["ACTUATOR_REGISTRY"].current()
    if not registry:
        # Not fatal: the registry is re-read when it appears, so a Gateway started
        # ahead of the Power System recovers on its own rather than needing a restart
        # in the right order.
        print(f"[gateway] no actuators yet — {paths.SYSTEM_CONFIG_PATH} is not there. "
              f"Is the Power System running? Serving anyway; it is picked up when it "
              f"appears.")
    else:
        print(f"[gateway] actuators: {', '.join(sorted(registry))}")

    print(f"[gateway] listening on {args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
