"""``python -m yawn_sandbox [serve|healthcheck]``。"""

from __future__ import annotations

import sys

from .server import healthcheck, serve


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "serve"
    if command == "serve":
        serve()
        return 0
    if command == "healthcheck":
        return healthcheck()
    print("usage: python -m yawn_sandbox [serve|healthcheck]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
