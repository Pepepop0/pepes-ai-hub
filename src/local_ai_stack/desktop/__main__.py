"""Explicit browser mode for exercising the same production desktop API."""

import argparse

from . import run_desktop


def main() -> int:
    parser = argparse.ArgumentParser(description="Pepito's AI Hub desktop")
    parser.add_argument(
        "--browser", action="store_true", help="Abrir no navegador e imprimir a URL local"
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Com --browser: só imprimir a URL, sem abrir o navegador (para agentes)",
    )
    args = parser.parse_args()
    if args.no_open and not args.browser:
        parser.error("--no-open requer --browser")
    return run_desktop(browser=args.browser, open_browser=not args.no_open)


if __name__ == "__main__":
    raise SystemExit(main())
