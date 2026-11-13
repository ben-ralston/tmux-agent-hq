#!/bin/bash
# Has notes-graph changed the OAuth files agent-hq vendors (#396)?
# Compares the pinned commit (agent_hq/remote/vendor.py UPSTREAM_COMMIT) with
# notes-graph origin/main. Exit 0 = up to date, 1 = upstream moved (re-vendor),
# 2 = could not check. notes-graph's own tests also fail when these files change.
#
# The upstream repository may not be public. Without a checkout of it this
# script exits 2 and there is nothing to re-vendor from: describe the change
# you need in an issue or pull request instead of editing the vendored files.
# tests/test_remote_vendor.py does not need the checkout and still runs.
set -u
repo="${NOTES_GRAPH_DIR:-$HOME/src/notes-graph}"
here="$(cd "$(dirname "$0")/.." && pwd)"
pinned="$(sed -n 's/^UPSTREAM_COMMIT = "\(.*\)"$/\1/p' "$here/agent_hq/remote/vendor.py")"
files=(src/apple_notes_mcp/remote/auth.py src/apple_notes_mcp/remote/request_log.py)
if [ -z "$pinned" ] || ! git -C "$repo" fetch -q origin 2>/dev/null; then
  echo "cannot check: no pinned commit or notes-graph checkout at $repo" >&2
  echo "(set NOTES_GRAPH_DIR; if you have no access to the upstream, see the note at the top of this script)" >&2
  exit 2
fi
if git -C "$repo" diff --quiet "$pinned" origin/main -- "${files[@]}"; then
  echo "up to date: notes-graph origin/main matches $pinned for the vendored files"
  exit 0
fi
echo "upstream moved since $pinned - re-vendor into agent_hq/remote/:"
git -C "$repo" diff --stat "$pinned" origin/main -- "${files[@]}"
exit 1
