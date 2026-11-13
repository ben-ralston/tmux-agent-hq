#!/usr/bin/env python3
"""Run the agent-hq CLI against the demo environment.

    uv run python demo/hq.py --list
    uv run python demo/hq.py web --bind 127.0.0.1 --port 9911
    uv run python demo/hq.py --dashboard
    uv run python demo/hq.py --print-isolation    # show the paths in effect, then exit

Same arguments as ``agent-hq``. This file only hands over to
``demo/bin/agent-hq``, the demo's stand-in for the installed command; see it
and ``demo/_isolate.py`` for what is redirected and what is refused.
"""

from __future__ import annotations

import os
import sys

from _isolate import DEMO_DIR, demo_root


def main() -> None:
    os.environ.setdefault("AGENT_HQ_DEMO_ROOT", str(demo_root()))
    os.environ["AGENT_HQ_DEMO_PYTHON"] = sys.executable
    launcher = str(DEMO_DIR / "bin" / "agent-hq")
    os.execv(sys.executable, [sys.executable, launcher, *sys.argv[1:]])


if __name__ == "__main__":
    main()
