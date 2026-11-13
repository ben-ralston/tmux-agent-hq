"""Jump helper — tmux switch-client + optional AppleScript focus.

Desktop jumps do both; iOS-triggered jumps (#137) pass
``focus_terminal=False`` to switch the pane (so a phone terminal
attaching over SSH sees the right agent) without raising Ghostty on
the Mac. Both subprocess calls are blocking; the route wraps
``perform_jump`` in ``run_in_threadpool`` to keep them off the event
loop.

AppleScript focus is best-effort and targets Ghostty only — the
terminal that hosts our tmux sessions. ``activate`` *launches* Ghostty
if it's closed, which is what we want. The return value reflects only
whether ``tmux switch-client`` succeeded.
"""

from __future__ import annotations

import logging
import subprocess

from agent_hq.actions import jump_to_pane as _jump_to_pane

log = logging.getLogger(__name__)

# Ghostty is the only terminal we focus. We deliberately do NOT fall
# back to iTerm/Terminal: AppleScript's ``activate`` *launches* an app
# that isn't running (it does not no-op), so a fallback list would
# cold-launch terminals the user never opened. Activating Ghostty when
# it's closed is fine/desired — it hosts the tmux sessions we jump into.
_GHOSTTY_ACTIVATE = 'tell application "Ghostty" to activate'


def _focus_terminal() -> bool:
    """Bring Ghostty forward. Return True iff the AppleScript ran cleanly."""
    try:
        result = subprocess.run(
            ["osascript", "-e", _GHOSTTY_ACTIVATE],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.debug("osascript missing or timed out for Ghostty")
        return False
    return result.returncode == 0


def perform_jump(pane_id: str, focus_terminal: bool = True) -> bool:
    """Switch tmux client to the pane, then nudge the terminal forward.

    Returns ``True`` iff the tmux switch succeeded — terminal focus is
    best-effort. The route always reports success when this returns
    True even if focus failed silently. ``focus_terminal=False`` skips
    the Ghostty raise (iOS-triggered jumps, #137).
    """
    if not _jump_to_pane(pane_id):
        return False
    if focus_terminal:
        _focus_terminal()  # best-effort; ignore result
    return True
