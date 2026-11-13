"""Ensure GitHub labels exist on a repo, caching successes.

Used by the Idea Inbox handler so the very first idea filed in a repo doesn't
fail just because `idea-inbox` or `needs-triage` haven't been created yet.

Cache shape: {"labels": {"owner/name": ["idea-inbox", ...]}}
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

DEFAULT_CACHE_PATH = Path.home() / ".cache" / "agent-hq" / "idea-inbox.json"


def ensure_labels(repo: str, labels: list[str], cache_path: Path) -> set[str]:
    """Create any of `labels` not already cached as ensured for `repo`.

    Best-effort: never raises. A label is cached only when `gh label create`
    succeeds OR when gh reports it already exists.

    Returns the set of labels confirmed present for `repo` — callers that need
    a hard guarantee (e.g. reroute, so the moved issue keeps `idea-inbox`) can
    check membership and warn when a label couldn't be ensured (#181).
    """
    cache = _load_cache(cache_path)
    ensured = set(cache.setdefault("labels", {}).setdefault(repo, []))

    for label in labels:
        if label in ensured:
            continue
        if _try_create_label(repo, label):
            ensured.add(label)

    cache["labels"][repo] = sorted(ensured)
    _save_cache(cache_path, cache)
    return ensured


def _try_create_label(repo: str, label: str) -> bool:
    cmd = ["gh", "label", "create", label, "--repo", repo]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        return True
    except FileNotFoundError:
        return False
    except subprocess.CalledProcessError as e:
        # `gh label create` exits non-zero when the label already exists; treat
        # that as success so we cache and skip on subsequent calls.
        if "already exists" in (e.stderr or "").lower():
            return True
        return False


def _load_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


def _save_cache(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True))
    except OSError:
        pass
