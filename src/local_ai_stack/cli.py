import argparse

from .app import run_tui
from .doctor import doctor_text


def main() -> None:
    p = argparse.ArgumentParser(prog="local-ai-stack")
    p.add_argument("--doctor", action="store_true")
    a = p.parse_args()
    if a.doctor:
        print(doctor_text())
        return
    run_tui()


if __name__ == "__main__":
    main()
