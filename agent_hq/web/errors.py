"""Wrappers that translate `False`/`None` returns from core actions into
structured ActionError exceptions that routes can surface as toasts."""

from dataclasses import dataclass

from agent_hq.actions import kill_agent, rename_session
from agent_hq.state import load_inactive_state, toggle_inactive


@dataclass
class ActionError(Exception):
    """Raised when a core action returns a failure sentinel."""

    message: str
    pane_id: str | None = None

    def __str__(self) -> str:
        if self.pane_id:
            return f"{self.message} (pane {self.pane_id})"
        return self.message


def safe_kill(pane_id: str) -> None:
    if not kill_agent(pane_id):
        raise ActionError("Could not kill agent", pane_id=pane_id)


def safe_rename(pane_id: str, new_name: str | None = None) -> None:
    if not rename_session(pane_id, new_name):
        raise ActionError("Could not rename session", pane_id=pane_id)


def safe_mute(pane_id: str, fingerprint: str) -> bool:
    """Toggle mute; returns True if now muted, False if now active.

    Consistent call surface with the other safe_* helpers even though
    `toggle_inactive` has no failure mode to translate.
    """
    return toggle_inactive(pane_id, fingerprint)


def safe_set_mute(pane_id: str, *, muted: bool, fingerprint: str) -> bool:
    """Idempotent set-mute used by the web POST.

    Unlike ``safe_mute``, the caller passes an explicit desired state
    rather than toggling — so retrying the same request never flips the
    mute back on. Returns True if the agent is now muted, False otherwise.
    """
    is_muted = pane_id in load_inactive_state()
    if is_muted == muted:
        return is_muted
    return toggle_inactive(pane_id, fingerprint)
