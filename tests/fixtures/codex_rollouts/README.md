# Codex rollout fixtures

These JSONL files are sanitized reductions of rollouts produced by Codex CLI
0.144.5 during the issue #204 Stage 0 probes. They preserve record shapes,
ordering, duplicate event/message representations, compaction boundaries, and
parent/child identity fields while replacing local paths, IDs, and encrypted
content.

- `plain.jsonl`: one ordinary turn, including the duplicate `event_msg` view.
- `resume.jsonl`: two turns appended to one rollout before and after resume.
- `compaction.jsonl`: a manual compaction record followed by another turn.
- `subagent-parent.jsonl` and `subagent-child.jsonl`: a linked parent/child pair.

`<phase>` tag values are canonicalized to valid `DevPhase` values (the Stage 0
probe prompt authored arbitrary tag text); the surrounding record shapes are
the pinned ground truth.
