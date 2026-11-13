"""`agent-hq idea` subcommand: post free-form text to the local web app.

Reads its bearer token from the same file the web app uses
(`~/.config/agent-hq/web-token`) and posts to a configured host/port (default
`http://127.0.0.1:8765`). Prints the issue URL on stdout; warnings (if any)
go to stderr.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_TOKEN_FILE = Path.home() / ".config" / "agent-hq" / "web-token"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _read_text(arg_text: str | None) -> str:
    """Resolve idea text from arg, stdin, or $EDITOR."""
    if arg_text:
        return arg_text.strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return _from_editor()


def _from_editor() -> str:
    editor = os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile(suffix=".md", mode="w+", delete=False) as f:
        path = f.name
        f.write("# Type your idea below; save and quit to file.\n\n")
    try:
        subprocess.call([editor, path])
        with open(path) as f:
            content = f.read()
    finally:
        os.unlink(path)
    # Strip the comment header
    lines = [line for line in content.splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(lines).strip()


def run_idea(
    *,
    text: str | None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token_file: Path | None = None,
    out: object = None,
    err: object = None,
) -> int:
    """Run one idea-submit. Returns exit code (0 on success).

    ``token_file`` defaults to ``DEFAULT_TOKEN_FILE``, resolved at call
    time (not as a def-time default) so the test suite's isolation
    redirect applies (#133).
    """
    out = out or sys.stdout
    err = err or sys.stderr
    token_file = token_file or DEFAULT_TOKEN_FILE

    body_text = _read_text(text)
    if not body_text:
        print("error: no text provided (arg, stdin, or editor)", file=err)
        return 1

    try:
        token = token_file.read_text().strip()
    except OSError as e:
        print(f"error: cannot read token file {token_file}: {e}", file=err)
        return 1
    if not token:
        print(f"error: token file {token_file} is empty", file=err)
        return 1

    # Derive the idempotency key from the text so re-running the same idea
    # within the server's cache TTL dedupes instead of filing a duplicate. A
    # fresh per-invocation uuid could never share a key across retries (#181).
    idempotency_key = hashlib.sha256(body_text.encode()).hexdigest()
    payload = json.dumps({"text": body_text, "idempotency_key": idempotency_key}).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/api/idea",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
            msg = err_body.get("error") or err_body.get("detail") or str(e)
        except Exception:
            msg = str(e)
        print(f"error: HTTP {e.code}: {msg}", file=err)
        return 1
    except urllib.error.URLError as e:
        print(
            f"error: cannot reach {host}:{port} — is `agent-hq web` running? ({e.reason})",
            file=err,
        )
        return 1

    print(body.get("issue_url", ""), file=out)
    if body.get("needs_triage"):
        print("(filed with needs-triage label)", file=err)
    for warning in body.get("warnings", []):
        print(f"warning: {warning}", file=err)
    return 0
