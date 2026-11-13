"""Tests for `agent-hq idea` CLI subcommand."""

import hashlib
import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

from agent_hq.idea.cli import run_idea


def _token_file(tmp_path, value: str = "secret-token"):
    f = tmp_path / "web-token"
    f.write_text(value)
    return f


def _mock_http_response(payload: dict):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestRunIdea:
    @patch("agent_hq.idea.cli.urllib.request.urlopen")
    def test_posts_text_with_bearer_and_prints_url(self, mock_urlopen, tmp_path):
        token_file = _token_file(tmp_path)
        mock_urlopen.return_value = _mock_http_response(
            {
                "issue_url": "https://github.com/o/r/issues/42",
                "needs_triage": False,
                "warnings": [],
            }
        )
        out = io.StringIO()
        err = io.StringIO()
        rc = run_idea(text="my idea", token_file=token_file, out=out, err=err)
        assert rc == 0
        assert out.getvalue().strip() == "https://github.com/o/r/issues/42"
        # Posted with bearer header
        sent_req = mock_urlopen.call_args.args[0]
        assert sent_req.method == "POST"
        assert sent_req.headers["Authorization"] == "Bearer secret-token"
        body = json.loads(sent_req.data.decode())
        assert body["text"] == "my idea"
        assert body["idempotency_key"]

    @patch("agent_hq.idea.cli.urllib.request.urlopen")
    def test_idempotency_key_is_content_hash_and_stable(self, mock_urlopen, tmp_path):
        """AHQ-005: the CLI derives its idempotency key from the text (sha256)
        so re-running the same text within the server TTL dedupes instead of
        minting a fresh uuid that can never share a key."""
        token_file = _token_file(tmp_path)
        mock_urlopen.return_value = _mock_http_response(
            {"issue_url": "https://github.com/o/r/issues/7", "needs_triage": False, "warnings": []}
        )
        text = "the same idea"
        expected = hashlib.sha256(text.encode()).hexdigest()

        keys = []
        for _ in range(2):
            run_idea(text=text, token_file=token_file, out=io.StringIO(), err=io.StringIO())
            body = json.loads(mock_urlopen.call_args.args[0].data.decode())
            keys.append(body["idempotency_key"])

        assert keys[0] == expected
        assert keys[0] == keys[1]  # stable across invocations of the same text

    @patch("agent_hq.idea.cli.urllib.request.urlopen")
    def test_writes_warnings_to_stderr(self, mock_urlopen, tmp_path):
        token_file = _token_file(tmp_path)
        mock_urlopen.return_value = _mock_http_response(
            {
                "issue_url": "https://github.com/o/r/issues/1",
                "needs_triage": True,
                "warnings": ["project-add: HTTP 502"],
            }
        )
        out = io.StringIO()
        err = io.StringIO()
        rc = run_idea(text="idea", token_file=token_file, out=out, err=err)
        assert rc == 0
        assert "https://github.com/o/r/issues/1" in out.getvalue()
        assert "needs-triage" in err.getvalue()
        assert "project-add" in err.getvalue()

    @patch("agent_hq.idea.cli.urllib.request.urlopen")
    def test_http_error_returns_nonzero(self, mock_urlopen, tmp_path):
        token_file = _token_file(tmp_path)
        e = urllib.error.HTTPError(
            "url",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(json.dumps({"error": "MY_KEY missing"}).encode()),
        )
        mock_urlopen.side_effect = e
        out = io.StringIO()
        err = io.StringIO()
        rc = run_idea(text="x", token_file=token_file, out=out, err=err)
        assert rc == 1
        assert "503" in err.getvalue()
        assert "MY_KEY" in err.getvalue()

    @patch("agent_hq.idea.cli.urllib.request.urlopen")
    def test_connection_refused_returns_nonzero(self, mock_urlopen, tmp_path):
        token_file = _token_file(tmp_path)
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        out = io.StringIO()
        err = io.StringIO()
        rc = run_idea(text="x", token_file=token_file, out=out, err=err)
        assert rc == 1
        assert "is `agent-hq web` running" in err.getvalue()

    def test_missing_token_file_returns_nonzero(self, tmp_path):
        out = io.StringIO()
        err = io.StringIO()
        rc = run_idea(
            text="x",
            token_file=tmp_path / "nonexistent",
            out=out,
            err=err,
        )
        assert rc == 1
        assert "token file" in err.getvalue()

    def test_empty_token_returns_nonzero(self, tmp_path):
        token_file = _token_file(tmp_path, value="")
        out = io.StringIO()
        err = io.StringIO()
        rc = run_idea(text="x", token_file=token_file, out=out, err=err)
        assert rc == 1

    def test_no_text_returns_nonzero(self, tmp_path):
        """No arg, no stdin, no editor — should error."""
        token_file = _token_file(tmp_path)
        out = io.StringIO()
        err = io.StringIO()
        with patch("agent_hq.idea.cli.sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            mock_stdin.read.return_value = ""
            rc = run_idea(text=None, token_file=token_file, out=out, err=err)
        assert rc == 1
        assert "no text" in err.getvalue()

    @patch("agent_hq.idea.cli.urllib.request.urlopen")
    def test_reads_text_from_stdin(self, mock_urlopen, tmp_path):
        token_file = _token_file(tmp_path)
        mock_urlopen.return_value = _mock_http_response(
            {"issue_url": "https://github.com/o/r/issues/9", "needs_triage": False, "warnings": []}
        )
        out = io.StringIO()
        err = io.StringIO()
        with patch("agent_hq.idea.cli.sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            mock_stdin.read.return_value = "from stdin"
            rc = run_idea(text=None, token_file=token_file, out=out, err=err)
        assert rc == 0
        sent_req = mock_urlopen.call_args.args[0]
        body = json.loads(sent_req.data.decode())
        assert body["text"] == "from stdin"
