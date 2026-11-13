"""Cookie-backed dispatch state for the web Dispatch view.

Stores ``filters`` (chip selections + free-text query) and ``selection``
(checked issue keys + grouped batches) in a single signed cookie. The
HMAC key is ``TokenManager.signing_key()`` (token bytes + mtime, #82) —
distinct from the mtime-only rotation key — so cookie validity still
tracks token rotation (rotating the token invalidates outstanding
dispatch cookies for free) without conflating the two contracts.

Threat model: identical to the auth cookie. The server is on a tailnet,
so non-revocable signed cookies are acceptable; the upgrade path if that
ever changes is to bump the file's mtime (e.g. via ``touch``).
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import hmac
import json
from dataclasses import dataclass, field

from fastapi import Request, Response

from agent_hq.web.auth import TokenManager, is_secure_request

COOKIE_NAME = "dispatch_state"
# 90 days, same as the auth cookie. Filters + selection are user-facing
# state, not credentials, but matching the auth cookie's lifetime keeps
# the rotation story simple.
COOKIE_MAX_AGE = 60 * 60 * 24 * 90

# Top-level filter dimensions the chip rows expose. Anything else is
# silently ignored on toggle so a stale URL can't crash the route.
# "priority" left with the GH Need/Want/Idea taxonomy (#247) — effort and
# fitness chips over the triage store replace it. Old cookies carrying a
# ``priorities`` entry decode fine: from_dict simply never reads the key.
_FILTER_DIMENSIONS = {"repo", "effort", "fitness", "status", "sentinel", "autopilot"}
_FILTER_DIM_TO_ATTR = {
    "repo": "repos",
    "effort": "efforts",
    "fitness": "fitnesses",
    "status": "statuses",
    "sentinel": "sentinels",
    "autopilot": "autopilots",
}

# Date-sort fields whose buttons carry a direction (#156). "default" has none.
_DATE_SORTS = ("created", "updated")


@dataclass
class DispatchState:
    """All state the dispatch view needs across requests.

    Lists are JSON-friendly. ``groups`` is a list-of-lists rather than a
    list-of-sets so the cookie round-trips through JSON; semantics are
    set-like and we de-duplicate on toggle.
    """

    repos: list[str] = field(default_factory=list)
    efforts: list[str] = field(default_factory=list)
    fitnesses: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    sentinels: list[str] = field(default_factory=list)
    # #302: one chip, "on" — flagged-for-autopilot issues only.
    autopilots: list[str] = field(default_factory=list)
    query: str = ""
    selected: list[str] = field(default_factory=list)
    groups: list[list[str]] = field(default_factory=list)
    # Date sort field (#156): "" / "default" / "created" / "updated". A sort
    # preference like the filters — preserved across reloads, not cleared
    # with the selection. Empty reads as the default priority order.
    sort: str = ""
    # Per-field sort direction (#156): "" reads as "desc" (↓ newest first).
    # Each date field remembers its own direction across button switches.
    created_dir: str = ""
    updated_dir: str = ""

    # ---- filter mutations -------------------------------------------------

    def toggle_filter(self, dimension: str, value: str) -> None:
        """Toggle a chip on/off in one of the filter dimensions.

        Unknown dimensions are no-ops — the cookie is user-controllable
        and a malformed POST shouldn't 500 the route.
        """
        if dimension not in _FILTER_DIMENSIONS:
            return
        attr = _FILTER_DIM_TO_ATTR[dimension]
        current: list[str] = getattr(self, attr)
        if value in current:
            current.remove(value)
        else:
            current.append(value)

    def prune_filters(self, options: dict[str, list[dict]]) -> bool:
        """Drop filter values absent from ``options`` (the chip set from
        ``filters.available_filter_options``). A value goes stale when its
        repo leaves the config or its last matching issue disappears — the
        chip vanishes but the cookie keeps filtering, leaving an
        unexplained empty list with no in-UI way to clear it (#80).

        Returns True when anything was removed so callers know to
        re-write the cookie.
        """
        changed = False
        for options_key, attr in (
            ("repos", "repos"),
            ("efforts", "efforts"),
            ("fitnesses", "fitnesses"),
            ("statuses", "statuses"),
            ("sentinels", "sentinels"),
            ("autopilots", "autopilots"),
        ):
            known = {opt["id"] for opt in options.get(options_key, [])}
            current: list[str] = getattr(self, attr)
            kept = [v for v in current if v in known]
            if len(kept) != len(current):
                setattr(self, attr, kept)
                changed = True
        return changed

    # ---- selection mutations ---------------------------------------------

    def toggle_selection(self, key: str) -> None:
        """Toggle an issue key in/out of the selection set.

        On removal, also strip the key from any group it lives in —
        mirrors ``DispatchScreen.toggle_selection`` so the cookie can't
        end up with a phantom-grouped key the user already deselected.
        """
        if key in self.selected:
            self.selected.remove(key)
            self.groups = [[k for k in g if k != key] for g in self.groups]
        else:
            self.selected.append(key)

    def group_selected(self, keys: list[str]) -> None:
        """Bundle the given keys into a new group (preserving order)."""
        self.groups.append(list(keys))

    def ungroup(self, index: int) -> None:
        """Dissolve the group at ``index``. Out-of-range is a no-op."""
        if 0 <= index < len(self.groups):
            del self.groups[index]

    def clear_selection(self) -> None:
        """Drop selection + groups (run after a successful dispatch).

        Filters are intentionally preserved — most users want to grab
        the next batch without re-entering their search.
        """
        self.selected = []
        self.groups = []

    def ungrouped_keys(self) -> list[str]:
        """Selected keys that are not yet part of any group.

        Used to enable / disable the Group button and to build the
        implicit one-worktree-per-ungrouped-issue batch list at dispatch
        time.
        """
        grouped: set[str] = set()
        for g in self.groups:
            grouped.update(g)
        return [k for k in self.selected if k not in grouped]

    # ---- sort axis (#156) ------------------------------------------------

    def direction_for(self, field: str) -> str:
        """'desc' (default) or 'asc' for a date field's remembered direction."""
        raw = (
            self.created_dir
            if field == "created"
            else self.updated_dir
            if field == "updated"
            else ""
        )
        return raw if raw in ("desc", "asc") else "desc"

    def apply_sort(self, value: str) -> None:
        """Activate a sort button, or flip the active date field's own
        direction when its button is re-clicked. Caller validates ``value``
        against ``SORT_MODES``."""
        if value in _DATE_SORTS and self.sort == value:
            flipped = "asc" if self.direction_for(value) == "desc" else "desc"
            if value == "created":
                self.created_dir = flipped
            else:
                self.updated_dir = flipped
        else:
            self.sort = value

    def sort_params(self) -> tuple[str, bool]:
        """(sort_by, descending) for ``filters.sort_issues``. Returns
        ("default", True) when the active sort is not a date field."""
        field = self.sort or "default"
        if field not in _DATE_SORTS:
            return "default", True
        return field, self.direction_for(field) == "desc"

    # ---- serialization ---------------------------------------------------

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DispatchState":
        """Tolerate extra fields (forward-compat) but reject wrong types
        for the known fields by raising ValueError so the caller can fall
        back to the empty state."""
        sort = _as_str(data.get("sort", ""))
        created_dir = _as_str(data.get("created_dir", ""))
        updated_dir = _as_str(data.get("updated_dir", ""))
        # Migrate legacy #101 cookies (sort by updated_at via newest/oldest).
        if sort == "newest":
            sort, updated_dir = "updated", "desc"
        elif sort == "oldest":
            sort, updated_dir = "updated", "asc"
        return cls(
            repos=_as_str_list(data.get("repos", [])),
            efforts=_as_str_list(data.get("efforts", [])),
            fitnesses=_as_str_list(data.get("fitnesses", [])),
            statuses=_as_str_list(data.get("statuses", [])),
            sentinels=_as_str_list(data.get("sentinels", [])),
            autopilots=_as_str_list(data.get("autopilots", [])),
            query=_as_str(data.get("query", "")),
            selected=_as_str_list(data.get("selected", [])),
            groups=[_as_str_list(g) for g in _as_list(data.get("groups", []))],
            sort=sort,
            created_dir=created_dir,
            updated_dir=updated_dir,
        )


# ---- type-strict coercers (raise on shape mismatch) ----------------------


def _as_list(v: object) -> list:
    if not isinstance(v, list):
        raise ValueError("expected list")
    return v


def _as_str_list(v: object) -> list[str]:
    items = _as_list(v)
    if not all(isinstance(x, str) for x in items):
        raise ValueError("expected list[str]")
    return list(items)


def _as_str(v: object) -> str:
    if not isinstance(v, str):
        raise ValueError("expected str")
    return v


# ---- HMAC-signed cookie codec --------------------------------------------


def _sign(payload_b64: str, tm: TokenManager) -> str:
    return hmac.new(tm.signing_key(), payload_b64.encode(), hashlib.sha256).hexdigest()


def _encode(state: DispatchState, tm: TokenManager) -> str:
    raw = json.dumps(state.to_dict(), separators=(",", ":")).encode()
    payload_b64 = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    sig = _sign(payload_b64, tm)
    return f"{sig}.{payload_b64}"


def _decode(cookie: str, tm: TokenManager) -> DispatchState:
    if "." not in cookie:
        raise ValueError("missing signature separator")
    sig, payload_b64 = cookie.split(".", 1)
    expected = _sign(payload_b64, tm)
    # bytes, not str: compare_digest raises TypeError on non-ASCII str.
    if not hmac.compare_digest(sig.encode(), expected.encode()):
        raise ValueError("bad signature")
    # Re-pad b64 (urlsafe_b64encode strip on encode mirrored here).
    padding = "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + padding)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("bad b64 payload") from exc
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("payload must be a JSON object")
    return DispatchState.from_dict(data)


def read_dispatch_state(request: Request, tm: TokenManager) -> DispatchState:
    """Return the user's dispatch state, or an empty default on any
    decode failure (missing cookie, bad signature, malformed payload)."""
    cookie = request.cookies.get(COOKIE_NAME, "")
    if not cookie:
        return DispatchState()
    try:
        return _decode(cookie, tm)
    except (ValueError, json.JSONDecodeError):
        return DispatchState()


def write_dispatch_state(
    response: Response,
    state: DispatchState,
    tm: TokenManager,
    request: Request | None = None,
) -> None:
    """Set the signed dispatch_state cookie on ``response``.

    ``Secure`` is only set when the inbound request is HTTPS — under
    plain-HTTP local dev (127.0.0.1, Tailscale-without-serve) the
    browser silently drops Secure cookies, so we mirror auth.py's
    ``is_secure_request`` heuristic.
    """
    secure = is_secure_request(request) if request is not None else False
    response.set_cookie(
        key=COOKIE_NAME,
        value=_encode(state, tm),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
