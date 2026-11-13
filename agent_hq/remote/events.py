"""Forward security-relevant request-log events to eventlog (#396).

The vendored ``RequestLog`` writes the local JSON-lines log; this subclass
also sends the few events the sentinel should see. Only the event kind and
its fixed reason/outcome travel - never the login, subject, path or client
name the local log line carries.
"""

from agent_hq import eventlog

from .request_log import RequestLog

IDENTITY_DENIED = "remote sign-in denied: identity not allowed"
TOKEN_REUSE = "remote token reuse detected"
TOOL_FAILED = "remote tool failed"


class EventingRequestLog(RequestLog):
    def event(self, event: str, **fields) -> None:
        super().event(event, **fields)
        try:
            self._forward(event, fields)
        except Exception:
            pass

    @staticmethod
    def _forward(event: str, fields: dict) -> None:
        decision = fields.get("decision")
        reason = str(fields.get("reason") or "")
        outcome = str(fields.get("outcome") or "")
        if event == "sign_in" and decision == "deny" and reason == "identity not allowed":
            eventlog.emit("warn", IDENTITY_DENIED, {"event": event, "reason": reason})
        elif event == "token" and decision == "deny" and reason.startswith("reuse detected"):
            # A replayed code or rotated refresh token: possibly a stolen
            # token. Error level, so the sentinel files an issue.
            eventlog.emit("error", TOKEN_REUSE, {"event": event, "reason": reason})
        elif event == "tool" and outcome.startswith("error:"):
            eventlog.emit(
                "warn",
                TOOL_FAILED,
                {"event": event, "tool": str(fields.get("tool") or "")[:100], "outcome": outcome},
            )
