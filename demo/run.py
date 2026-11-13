#!/usr/bin/env python3
"""Run a Python script inside the demo environment.

    uv run python demo/run.py path/to/script.py [args...]

The script sees the demo's home, tmux server, runtime directory and stand-in
commands instead of the real ones (see ``demo/_isolate.py``).
"""

from __future__ import annotations

import runpy
import sys

from _isolate import isolate


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    isolate()
    sys.argv = sys.argv[1:]
    runpy.run_path(sys.argv[0], run_name="__main__")


if __name__ == "__main__":
    main()
