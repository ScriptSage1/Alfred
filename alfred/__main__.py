"""Entry point: ``python -m alfred`` opens Alfred; ``--startup`` (used at sign-in) starts hidden in the tray."""
import sys


def main():
    from .ui import run
    return run()


if __name__ == '__main__':
    sys.exit(main())
