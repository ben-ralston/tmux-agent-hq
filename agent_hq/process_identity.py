"""Bounded, uncached process-generation reads for runtime collection."""

import subprocess
import time

from .hooks import _parse_etime


def read_process_births(*, runner=None, now=None) -> dict[int, float]:
    """One process-table read avoids per-pane subprocess delays and PID caches."""
    run = runner or subprocess.run
    try:
        result = run(
            ["ps", "-ax", "-o", "pid=,etime="], capture_output=True, text=True, timeout=0.2
        )
        if result.returncode:
            return {}
    except (OSError, subprocess.SubprocessError):
        return {}
    observed = time.time() if now is None else now
    births = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        elapsed = _parse_etime(parts[1])
        if elapsed is not None:
            births[int(parts[0])] = observed - elapsed
    return births
