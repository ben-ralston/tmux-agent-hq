"""The vendored notes-graph OAuth stack matches its pinned upstream (#396).

The copy may differ from upstream only by ``vendor.SUBSTITUTIONS``. Reversing
them must give back the exact bytes notes-graph had at ``UPSTREAM_COMMIT``, so a
hand edit here fails CI. This cannot see upstream moving on; notes-graph pins
the same hashes on its side and ``scripts/check-remote-vendor.sh`` diffs it.
"""

import hashlib
from pathlib import Path

import pytest

from agent_hq import remote
from agent_hq.remote import vendor

REMOTE_DIR = Path(remote.__file__).parent


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("name", sorted(vendor.FILES))
def test_vendored_files_match_pinned_upstream(name):
    _upstream_path, upstream_sha = vendor.FILES[name]
    text = (REMOTE_DIR / name).read_text(encoding="utf-8")
    assert _sha(vendor.restore_upstream(name, text)) == upstream_sha, (
        f"agent_hq/remote/{name} no longer matches notes-graph {vendor.UPSTREAM_COMMIT} "
        "plus vendor.SUBSTITUTIONS. Don't hand-edit vendored files: re-vendor instead."
    )


@pytest.mark.parametrize("name", sorted(vendor.FILES))
def test_each_substitution_applies_exactly_once(name):
    text = (REMOTE_DIR / name).read_text(encoding="utf-8")
    for _upstream, ours in vendor.SUBSTITUTIONS[name]:
        assert text.count(ours) == 1, ours


def test_restore_upstream_detects_hand_edit():
    text = (REMOTE_DIR / "auth.py").read_text(encoding="utf-8")
    edited = text.replace("AUTH_CODE_TTL = 300", "AUTH_CODE_TTL = 301")
    assert edited != text
    assert _sha(vendor.restore_upstream("auth.py", edited)) != vendor.FILES["auth.py"][1]
