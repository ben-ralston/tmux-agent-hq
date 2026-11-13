"""Tests for idea-inbox label ensure-and-cache helper."""

import json
import subprocess
from unittest.mock import MagicMock, patch

from agent_hq.idea.labels import ensure_labels


class TestEnsureLabels:
    @patch("agent_hq.idea.labels.subprocess.run")
    def test_creates_missing_labels_and_caches(self, mock_run, tmp_path):
        cache = tmp_path / "cache.json"
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        ensure_labels("ben-ralston/foo", ["idea-inbox", "needs-triage"], cache)

        assert mock_run.call_count == 2
        data = json.loads(cache.read_text())
        assert set(data["labels"]["ben-ralston/foo"]) == {"idea-inbox", "needs-triage"}

    @patch("agent_hq.idea.labels.subprocess.run")
    def test_returns_set_of_ensured_labels(self, mock_run, tmp_path):
        """reroute (#181) needs to know which labels were actually ensured so it
        can warn when `idea-inbox` couldn't be created in the destination."""
        cache = tmp_path / "cache.json"
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = ensure_labels("ben-ralston/foo", ["idea-inbox", "needs-triage"], cache)
        assert result == {"idea-inbox", "needs-triage"}

    @patch("agent_hq.idea.labels.subprocess.run")
    def test_return_excludes_labels_that_failed_to_create(self, mock_run, tmp_path):
        cache = tmp_path / "cache.json"
        # gh label create fails for a reason other than "already exists" (auth,
        # network) — the label is NOT ensured, so it's absent from the result.
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="HTTP 403: forbidden")
        result = ensure_labels("ben-ralston/foo", ["idea-inbox"], cache)
        assert result == set()

    @patch("agent_hq.idea.labels.subprocess.run")
    def test_skips_already_cached_labels(self, mock_run, tmp_path):
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps({"labels": {"ben-ralston/foo": ["idea-inbox"]}}))
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        ensure_labels("ben-ralston/foo", ["idea-inbox", "needs-triage"], cache)

        # Only needs-triage should be created — idea-inbox already cached
        assert mock_run.call_count == 1
        called_args = mock_run.call_args[0][0]
        assert "needs-triage" in called_args

    @patch("agent_hq.idea.labels.subprocess.run")
    def test_treats_already_exists_as_success(self, mock_run, tmp_path):
        """gh label create exits non-zero when the label exists; treat as success."""
        cache = tmp_path / "cache.json"
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="label idea-inbox already exists"
        )

        ensure_labels("ben-ralston/foo", ["idea-inbox"], cache)

        # Label is cached even though gh exited non-zero
        data = json.loads(cache.read_text())
        assert "idea-inbox" in data["labels"]["ben-ralston/foo"]

    @patch("agent_hq.idea.labels.subprocess.run")
    def test_does_not_cache_on_real_failure(self, mock_run, tmp_path):
        """Failures unrelated to 'already exists' must not poison the cache."""
        cache = tmp_path / "cache.json"
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="HTTP 403: forbidden")

        ensure_labels("ben-ralston/foo", ["idea-inbox"], cache)

        # Cache empty — next call will retry
        if cache.exists():
            data = json.loads(cache.read_text())
            assert "idea-inbox" not in data.get("labels", {}).get("ben-ralston/foo", [])

    def test_corrupt_cache_file_treated_as_empty(self, tmp_path):
        cache = tmp_path / "cache.json"
        cache.write_text("not json{{{")

        with patch("agent_hq.idea.labels.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            ensure_labels("ben-ralston/foo", ["idea-inbox"], cache)

        data = json.loads(cache.read_text())
        assert "idea-inbox" in data["labels"]["ben-ralston/foo"]

    @patch("agent_hq.idea.labels.subprocess.run")
    def test_handles_missing_gh_silently(self, mock_run, tmp_path):
        """Missing gh CLI must not raise — degrades to "labels not ensured"."""
        cache = tmp_path / "cache.json"
        mock_run.side_effect = FileNotFoundError()

        ensure_labels("ben-ralston/foo", ["idea-inbox"], cache)
        # No exception — and nothing cached, so retry path stays open.
