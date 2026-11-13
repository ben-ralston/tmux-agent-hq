"""Carries the demo's isolation into every Python process the demo starts.

``demo/_isolate.py`` puts this directory on ``PYTHONPATH``. Python imports a
module named ``sitecustomize`` at startup, so any interpreter started from the
demo, including one that runs an installed ``agent-hq``, gets the demo's
runtime paths and signal guard before it can import ``agent_hq``. It does
nothing unless ``AGENT_HQ_DEMO_ROOT`` is set.
"""

import importlib.util
import os
import sys

if os.environ.get("AGENT_HQ_DEMO_ROOT"):
    _path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_isolate.py")
    try:
        _spec = importlib.util.spec_from_file_location("_agent_hq_demo_isolate", _path)
        _module = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_module)
        _module.install_hooks(_module.demo_root())
    except Exception as exc:  # a broken hook must be loud, never silent
        sys.stderr.write("demo isolation hook failed: %r\n" % (exc,))
        raise SystemExit(70)
