"""Persistent state for tracking agent access times and inactive agents."""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from agent_hq._locking import file_lock

log = logging.getLogger(__name__)

# State directory following XDG conventions
STATE_DIR = Path.home() / ".cache" / "agent-hq"
ACCESS_FILE = STATE_DIR / "access.json"
INACTIVE_FILE = STATE_DIR / "inactive.json"


@dataclass
class InactiveRecord:
    """Record of an agent marked as inactive."""

    marked_at: float  # Unix timestamp when marked inactive
    fingerprint: str  # Content fingerprint at time of marking (informational; #303)


def _lock_path_for(file_path: Path) -> Path:
    """Sidecar lock path for ``file_path``, computed fresh at call time so
    tests that monkeypatch ``ACCESS_FILE``/``INACTIVE_FILE`` get an
    isolated lock too (mirrors ``overrides._lock_path_for``)."""
    return file_path.with_name(file_path.name + ".lock")


def _atomic_write_json(path: Path, payload) -> None:
    """Write ``payload`` as JSON via a sibling temp file + atomic rename —
    matches the pattern already used by ``pinned_store``/``overrides`` so a
    concurrent reader never observes a half-written file (#164)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def load_access_times() -> dict[str, float]:
    """
    Load pane access timestamps from state file.

    Returns:
        Dict mapping pane_id to Unix timestamp of last access.
        Returns empty dict if file doesn't exist or is invalid.
    """
    if not ACCESS_FILE.exists():
        return {}

    try:
        with open(ACCESS_FILE) as f:
            data = json.load(f)
            # Validate structure
            if isinstance(data, dict):
                return {k: float(v) for k, v in data.items()}
            return {}
    except (json.JSONDecodeError, ValueError, OSError):
        return {}


def _load_access_times_or_raise() -> dict[str, float]:
    """Strict counterpart of ``load_access_times`` — only a genuinely
    missing file is legitimately empty; anything else raises so the
    read-modify-write callers below skip the write instead of clobbering
    existing access times (#164)."""
    try:
        with open(ACCESS_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{ACCESS_FILE} does not contain a JSON object")
    return {k: float(v) for k, v in data.items()}


def save_access_times(access_times: dict[str, float]) -> None:
    """
    Save pane access timestamps to state file. Locked + atomic (#164).

    Args:
        access_times: Dict mapping pane_id to Unix timestamp
    """
    with file_lock(_lock_path_for(ACCESS_FILE)):
        try:
            _atomic_write_json(ACCESS_FILE, access_times)
        except OSError:
            pass  # Silently fail - not critical


def record_access(pane_id: str) -> None:
    """
    Record current time as last access for a pane. Locked read-modify-write
    (#164) — serializes against concurrent ``record_access``/
    ``cleanup_stale_entries`` calls from other processes.

    Args:
        pane_id: The tmux pane identifier (e.g., "%5")
    """
    with file_lock(_lock_path_for(ACCESS_FILE)):
        try:
            access_times = _load_access_times_or_raise()
        except (ValueError, TypeError, OSError) as exc:
            log.error("cannot read %s, skipping access-time write: %s", ACCESS_FILE, exc)
            return
        access_times[pane_id] = time.time()
        try:
            _atomic_write_json(ACCESS_FILE, access_times)
        except OSError:
            pass


def cleanup_stale_entries(valid_pane_ids: set[str]) -> None:
    """
    Remove entries for panes that no longer exist. Locked read-modify-write
    (#164).

    Args:
        valid_pane_ids: Set of currently valid pane IDs
    """
    with file_lock(_lock_path_for(ACCESS_FILE)):
        try:
            access_times = _load_access_times_or_raise()
        except (ValueError, TypeError, OSError) as exc:
            log.error("cannot read %s, skipping stale-entry cleanup: %s", ACCESS_FILE, exc)
            return
        cleaned = {k: v for k, v in access_times.items() if k in valid_pane_ids}
        if len(cleaned) != len(access_times):
            try:
                _atomic_write_json(ACCESS_FILE, cleaned)
            except OSError:
                pass


# --- Inactive state management ---


def load_inactive_state() -> dict[str, InactiveRecord]:
    """
    Load inactive agent records from state file.

    Returns:
        Dict mapping pane_id to InactiveRecord.
        Returns empty dict if file doesn't exist or is invalid.
    """
    if not INACTIVE_FILE.exists():
        return {}

    try:
        with open(INACTIVE_FILE) as f:
            data = json.load(f)
            if isinstance(data, dict):
                return {
                    k: InactiveRecord(marked_at=v["marked_at"], fingerprint=v["fingerprint"])
                    for k, v in data.items()
                    if isinstance(v, dict) and "marked_at" in v and "fingerprint" in v
                }
            return {}
    except (json.JSONDecodeError, ValueError, OSError, KeyError):
        return {}


def _load_inactive_state_or_raise() -> dict[str, InactiveRecord]:
    """Strict counterpart of ``load_inactive_state``. Only a genuinely
    missing file is legitimately empty; a torn read (mid-write) or corrupt
    file raises instead of degrading to ``{}``, so ``toggle_inactive``/
    ``cleanup_inactive_entries`` skip the write rather than clobbering
    previously-stored records (AHQ-001). Individual malformed entries
    within an otherwise-valid file are still tolerated (skipped), matching
    ``load_inactive_state``'s existing per-entry leniency."""
    try:
        with open(INACTIVE_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{INACTIVE_FILE} does not contain a JSON object")
    return {
        k: InactiveRecord(marked_at=v["marked_at"], fingerprint=v["fingerprint"])
        for k, v in data.items()
        if isinstance(v, dict) and "marked_at" in v and "fingerprint" in v
    }


def _inactive_state_payload(records: dict[str, InactiveRecord]) -> dict:
    return {k: {"marked_at": v.marked_at, "fingerprint": v.fingerprint} for k, v in records.items()}


def save_inactive_state(records: dict[str, InactiveRecord]) -> None:
    """
    Save inactive agent records to state file. Locked + atomic (#164).

    Args:
        records: Dict mapping pane_id to InactiveRecord
    """
    with file_lock(_lock_path_for(INACTIVE_FILE)):
        try:
            _atomic_write_json(INACTIVE_FILE, _inactive_state_payload(records))
        except OSError:
            pass  # Silently fail - not critical


def toggle_inactive(pane_id: str, fingerprint: str) -> bool:
    """
    Toggle inactive state for a pane. Locked read-modify-write (#164) —
    closes the blind check-then-toggle race between e.g. a web request and
    a TUI keypress (AHQ-001).

    If the pane is currently active, marks it inactive with the given fingerprint.
    If the pane is currently inactive, marks it active (removes from inactive list).

    Args:
        pane_id: The tmux pane identifier (e.g., "%5")
        fingerprint: Content fingerprint to store when marking inactive

    Returns:
        True if now inactive, False if now active. Also False (a no-op) if
        the on-disk state couldn't be read — we never guess at a state
        change we can't verify, and never let a read failure be treated as
        "empty" and clobber existing records.
    """
    with file_lock(_lock_path_for(INACTIVE_FILE)):
        try:
            records = _load_inactive_state_or_raise()
        except (ValueError, TypeError, OSError) as exc:
            log.error("cannot read %s, skipping toggle for %s: %s", INACTIVE_FILE, pane_id, exc)
            return False

        if pane_id in records:
            # Currently inactive -> make active
            del records[pane_id]
            try:
                _atomic_write_json(INACTIVE_FILE, _inactive_state_payload(records))
            except OSError:
                pass
            return False
        else:
            # Currently active -> make inactive
            records[pane_id] = InactiveRecord(
                marked_at=time.time(),
                fingerprint=fingerprint,
            )
            try:
                _atomic_write_json(INACTIVE_FILE, _inactive_state_payload(records))
            except OSError:
                pass
            return True


def set_inactive(pane_id: str, fingerprint: str, *, muted: bool) -> bool | None:
    """Mute (``muted=True``) or unmute a pane explicitly, under the same lock
    as ``toggle_inactive`` (#397). A no-op when the pane is already in the
    wanted state, so a repeated or racing mute never flips it back.

    Returns the pane's muted state afterwards, or None when the state file
    couldn't be read or written (nothing is guessed, nothing is clobbered).
    """
    with file_lock(_lock_path_for(INACTIVE_FILE)):
        try:
            records = _load_inactive_state_or_raise()
        except (ValueError, TypeError, OSError) as exc:
            log.error("cannot read %s, skipping set for %s: %s", INACTIVE_FILE, pane_id, exc)
            return None
        if (pane_id in records) == muted:
            return muted
        if muted:
            records[pane_id] = InactiveRecord(marked_at=time.time(), fingerprint=fingerprint)
        else:
            del records[pane_id]
        try:
            _atomic_write_json(INACTIVE_FILE, _inactive_state_payload(records))
        except OSError as exc:
            log.error("cannot write %s for %s: %s", INACTIVE_FILE, pane_id, exc)
            return None
        return muted


def cleanup_inactive_entries(valid_pane_ids: set[str]) -> None:
    """
    Remove inactive entries for panes that no longer exist. Locked
    read-modify-write (#164).

    Args:
        valid_pane_ids: Set of currently valid pane IDs
    """
    with file_lock(_lock_path_for(INACTIVE_FILE)):
        try:
            records = _load_inactive_state_or_raise()
        except (ValueError, TypeError, OSError) as exc:
            log.error("cannot read %s, skipping inactive-entries cleanup: %s", INACTIVE_FILE, exc)
            return
        cleaned = {k: v for k, v in records.items() if k in valid_pane_ids}
        if len(cleaned) != len(records):
            try:
                _atomic_write_json(INACTIVE_FILE, _inactive_state_payload(cleaned))
            except OSError:
                pass
