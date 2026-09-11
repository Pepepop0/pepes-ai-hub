"""The explicit entry point. Nothing starts this server implicitly.

    python -m local_ai_stack.test_api --profile pepe

The token comes from the environment, never from an argument, so it does not
land in shell history or in a process listing. The bind address defaults to
loopback and a non-loopback value is refused rather than accepted with a
warning. The application uses controlled providers; local-real is unsupported.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from ..test_application import create_test_api
from .auth import ENV_TOKEN, ENV_TOKEN_FILE, TokenError, token_from_environment
from .binding import LOOPBACK_HOST, BindRefused
from .server import ShutdownTimeout, TestApiConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m local_ai_stack.test_api",
        description="Start the opt-in local test control server (dev/test only).",
    )
    parser.add_argument("--host", default=LOOPBACK_HOST, help="loopback address to bind")
    parser.add_argument("--port", type=int, default=0, help="port, or 0 to be assigned one")
    parser.add_argument(
        "--profile",
        action="append",
        required=True,
        metavar="ID",
        help="a profile id a run may name; repeat for more",
    )
    parser.add_argument("--allow-real-providers", action="store_true")
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--allow-gpu", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        token = token_from_environment()
    except TokenError as error:
        print(f"{error}", file=sys.stderr)
        print(f"set {ENV_TOKEN} or {ENV_TOKEN_FILE} and try again.", file=sys.stderr)
        return 2

    config = TestApiConfig(
        token=token,
        known_profiles=frozenset(arguments.profile),
        host=arguments.host,
        port=arguments.port,
        allow_real_providers=arguments.allow_real_providers,
        allow_network=arguments.allow_network,
        allow_gpu=arguments.allow_gpu,
    )
    try:
        api = create_test_api(config)
        api.start()
    except (BindRefused, ValueError) as error:
        print(f"{error}", file=sys.stderr)
        return 2

    print(f"test control API listening on {api.url}", file=sys.stderr)
    print("authenticate with a bearer token; it is not printed here.", file=sys.stderr)
    try:
        api.wait()
    except KeyboardInterrupt:
        pass
    try:
        api.stop(timeout=30)
    except ShutdownTimeout as error:
        print(f"{error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
