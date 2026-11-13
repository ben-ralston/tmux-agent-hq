"""Durable log of Idea-Inbox sent messages. #140

Append-only JSONL at ``~/.config/agent-hq/inbox-sent.jsonl`` recording every
non-empty Inbox submission and its outcome, so a failed or interrupted send
survives a page reload instead of vanishing with the client's Alpine state.

Every line is a **full self-contained record** — outcome lines re-carry
``text``/``created_at`` — so the size cap (which truncates raw lines) can
never orphan a record's content. Reads fold lines by ``id``: the latest line
wins, except ``dismissed`` which is terminal (a zombie request thread writing
``filed`` after the user dismissed an interrupted row must not resurrect it).

Mirrors ``snapshots.SnapshotStore`` conventions: ``__init__(path or
_DEFAULT)``, tolerant reads, a flock (``_locking.file_lock``) around each
read-modify-write, atomic temp-file rename, and mutating methods that log and
swallow their own I/O errors — a logging failure must never break filing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agent_hq._locking import file_lock

log = logging.getLogger(__name__)

_DEFAULT_SENT_FILE = Path.home() / ".config" / "agent-hq" / "inbox-sent.jsonl"
MAX_RECORDS = 500

_STATUSES = {"pending", "filed", "error", "dismissed"}


@dataclass
class SentRecord:
    """One folded sent-message record (the latest state of one submission)."""

    id: str
    text: str
    created_at: str
    status: str  # pending | filed | error | dismissed
    destination: str = ""  # "github" | "" while pending/error ("personal" on pre-#345 records)
    issue_url: str = ""
    issue_number: int = 0
    repo: str = ""
    project: str = ""
    title: str = ""
    warnings: list[str] = field(default_factory=list)
    confidence: float | None = None
    needs_triage: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _record_from_dict(obj: dict) -> SentRecord | None:
    if not isinstance(obj, dict):
        return None
    rec_id = obj.get("id")
    status = obj.get("status")
    if not isinstance(rec_id, str) or status not in _STATUSES:
        return None
    try:
        return SentRecord(
            id=rec_id,
            text=str(obj.get("text", "")),
            created_at=str(obj.get("created_at", "")),
            status=status,
            destination=str(obj.get("destination", "")),
            issue_url=str(obj.get("issue_url", "")),
            issue_number=int(obj.get("issue_number") or 0),
            repo=str(obj.get("repo", "")),
            project=str(obj.get("project", "")),
            title=str(obj.get("title", "")),
            warnings=[str(w) for w in obj.get("warnings") or []],
            confidence=obj.get("confidence"),
            needs_triage=bool(obj.get("needs_triage", False)),
            error=str(obj.get("error", "")),
        )
    except (TypeError, ValueError):
        # Valid JSON with junk field types — honor the tolerant-read
        # contract and skip the line rather than raise from list()/dismiss().
        return None


class SentMessageStore:
    """Append-only, folded, size-bounded log of Inbox sent messages."""

    def __init__(self, path: Path | None = None, max_records: int = MAX_RECORDS) -> None:
        self._path = path or _DEFAULT_SENT_FILE
        self._lock = self._path.with_suffix(self._path.suffix + ".lock")
        self._max_records = max_records

    # --- reads -------------------------------------------------------------

    def _read_lines(self) -> list[dict]:
        try:
            text = self._path.read_text()
        except (FileNotFoundError, OSError):
            return []
        lines: list[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                lines.append(obj)
        return lines

    def _fold(self) -> tuple[dict[str, SentRecord], set[str]]:
        """Fold raw lines into latest-per-id records + the dismissed id set.

        The dict preserves first-occurrence order, so iteration is
        oldest-submission-first regardless of later outcome lines.
        """
        records: dict[str, SentRecord] = {}
        dismissed: set[str] = set()
        for obj in self._read_lines():
            rec = _record_from_dict(obj)
            if rec is None:
                continue
            if rec.status == "dismissed":
                dismissed.add(rec.id)
            # Re-assignment keeps the id's original insertion position, so
            # iteration stays oldest-submission-first.
            records[rec.id] = rec
        return records, dismissed

    def list(self) -> list[SentRecord]:
        """Folded records oldest-first, excluding dismissed. Never raises."""
        records, dismissed = self._fold()
        return [rec for rec_id, rec in records.items() if rec_id not in dismissed]

    # --- writes ------------------------------------------------------------

    def record_pending(self, text: str, *, sent_id: str) -> SentRecord | None:
        """Append a pending record for a new submission. Never raises."""
        record = SentRecord(
            id=sent_id,
            text=text,
            created_at=datetime.now(timezone.utc).isoformat(),
            status="pending",
        )
        if self._append(record.to_dict()):
            return record
        return None

    def record_outcome(self, sent_id: str, *, status: str, **fields) -> bool:
        """Append a self-contained outcome line for ``sent_id``.

        Carries ``text``/``created_at`` forward from the existing record so
        the line stands alone if the cap later drops the pending line.
        Unknown ids are a no-op. Never raises.
        """
        records, dismissed = self._fold()
        existing = records.get(sent_id)
        # Dismissed is terminal: a zombie thread's late outcome must not
        # append — the line cap could eventually drop the dismiss tombstone
        # and resurrect the row.
        if existing is None or sent_id in dismissed:
            return False
        merged = {**existing.to_dict(), **fields, "status": status}
        return self._append(merged)

    def dismiss(self, sent_id: str) -> bool:
        """Terminally dismiss ``sent_id``. Returns False for unknown/dismissed ids."""
        records, dismissed = self._fold()
        if sent_id not in records or sent_id in dismissed:
            return False
        merged = {**records[sent_id].to_dict(), "status": "dismissed"}
        return self._append(merged)

    def _append(self, obj: dict) -> bool:
        """Flock-guarded append + cap-to-newest-lines atomic rewrite."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with file_lock(self._lock):
                lines = self._read_lines()
                lines.append(obj)
                if len(lines) > self._max_records:
                    lines = lines[-self._max_records :]
                tmp = self._path.with_suffix(self._path.suffix + ".tmp")
                tmp.write_text("".join(json.dumps(rec) + "\n" for rec in lines))
                tmp.replace(self._path)
            return True
        except Exception as exc:
            log.warning("sent-store append failed (%s): %s", self._path, exc)
            return False
