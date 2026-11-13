"""Minimal per-harness adapter registry (#205 Stage 1B).

Stage 2 (#206) extends it with transcript capabilities:
``transcript_phase`` / ``transcript_summary`` dispatch the resolver's
phase read and enrichment's summary fallback to the right reader —
Claude's projects-JSONL primitives (``transcript.py``/``phase.py``) or
the Codex rollout reader (``codex_transcript.py``). ``stable_id`` is the
harness's session identity: Claude ignores it (the transcript path
already encodes the session id); Codex verifies the rollout's
``session_meta`` against it so a stray rollout never binds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .codex_transcript import extract_last_codex_assistant_text, extract_last_codex_phase_tag
from .models import AgentType, DevPhase, normalize_phase_value
from .phase import detect_transcript_phase
from .transcript import extract_last_assistant_text


@dataclass(frozen=True)
class ResumeSpec:
    cwd: str
    argv: tuple[str, ...]


class HarnessAdapter(Protocol):
    kind: AgentType
    chat_efforts: frozenset[str]

    def launch_args(self, model: str | None, effort: str | None) -> list[str]: ...
    def repl_command(
        self, permission_mode: str, *, model: str | None = None, effort: str | None = None
    ) -> list[str]: ...
    def chat_command(
        self,
        *,
        model: str | None = None,
        effort: str | None = None,
        prompt: str | None = None,
        name: str | None = None,
    ) -> list[str]: ...
    def matches_process(self, command: str) -> bool: ...
    def resume_spec(self, record: dict) -> ResumeSpec | None: ...
    def transcript_phase(self, path: str, stable_id: str | None) -> DevPhase | None: ...
    def transcript_summary(self, path: str, stable_id: str | None) -> str | None: ...


def _record_stable_id(record: dict) -> str | None:
    return record.get("stable_id") or record.get("session_id")


class _Base:
    kind: AgentType

    def matches_process(self, command: str) -> bool:
        return self.kind.value in command.lower().split("/")[-1]

    def launch_args(self, model: str | None, effort: str | None) -> list[str]:
        args: list[str] = ["--harness", self.kind.value]
        if model:
            args += ["--model", model]
        if effort:
            args += ["--effort", effort]
        return args


class ClaudeAdapter(_Base):
    kind = AgentType.CLAUDE
    # What ``claude --effort`` accepts (the shared VALID_EFFORTS union is wider).
    chat_efforts = frozenset({"low", "medium", "high", "xhigh", "max"})

    def launch_args(self, model: str | None, effort: str | None) -> list[str]:
        # claude is /worktree-create's implicit default: emit nothing unless
        # a model/effort override needs the harness-forwarding path engaged.
        if not model and not effort:
            return []
        return super().launch_args(model, effort)

    def repl_command(
        self, permission_mode: str, *, model: str | None = None, effort: str | None = None
    ) -> list[str]:
        # ``model``/``effort`` are the dispatch-controller pins (#232); callers
        # that pass neither get the byte-identical pre-#232 line. The bare chat
        # window doesn't come through here at all -- ``open_chat_window`` uses
        # ``chat_command`` (no permission mode).
        cmd = ["claude", "--permission-mode", permission_mode]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        return cmd

    def chat_command(
        self,
        *,
        model: str | None = None,
        effort: str | None = None,
        prompt: str | None = None,
        name: str | None = None,
    ) -> list[str]:
        # The bare chat window (#169) -- no permission mode. ``model``/
        # ``effort``/``prompt`` come only from the MCP ``open_chat_session``
        # tool (#290); passing none yields the pre-#290 flagless ``claude``.
        # The prompt rides argv, so claude submits it itself at startup.
        cmd = ["claude"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        if name:
            # #429: claude's own display name, so the session is @name in
            # list_agents. Only the remote start_session passes one.
            cmd += ["--name", name]
        if prompt:
            # ``--`` keeps an option- or subcommand-shaped prompt a message.
            cmd += ["--", prompt]
        return cmd

    def resume_spec(self, record: dict) -> ResumeSpec | None:
        sid = _record_stable_id(record)
        if not sid or not record.get("cwd"):
            return None
        return ResumeSpec(cwd=record["cwd"], argv=("claude", "--resume", sid))

    def transcript_phase(self, path: str, stable_id: str | None) -> DevPhase | None:
        return detect_transcript_phase(path)

    def transcript_summary(self, path: str, stable_id: str | None) -> str | None:
        return extract_last_assistant_text(path)


class CodexAdapter(_Base):
    kind = AgentType.CODEX
    # codex ``model_reasoning_effort`` values.
    chat_efforts = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})

    def repl_command(
        self, permission_mode: str, *, model: str | None = None, effort: str | None = None
    ) -> list[str]:
        # codex's interactive REPL takes no permission-mode flag, and dispatch
        # never pins a model/effort on it (#232 is claude-only): bare command.
        return ["agent-hq", "codex", "--"]

    def chat_command(
        self,
        *,
        model: str | None = None,
        effort: str | None = None,
        prompt: str | None = None,
        name: str | None = None,
    ) -> list[str]:
        # ``name`` is ignored: codex has no session-name flag.
        # codex has no --effort flag: reasoning effort is a config key, and
        # the wrapper forwards explicit -c overrides to frontend and app
        # server alike. The prompt is codex's positional initial prompt.
        cmd = ["agent-hq", "codex", "--"]
        # Model and effort both ride -c: the wrapper forwards -c overrides to
        # the frontend AND the app server, while --model reaches only the
        # frontend (#290 review).
        if model:
            cmd += ["-c", f"model={model}"]
        if effort:
            cmd += ["-c", f"model_reasoning_effort={effort}"]
        if prompt:
            # ``--`` keeps an option- or subcommand-shaped prompt a message.
            cmd += ["--", prompt]
        return cmd

    def resume_spec(self, record: dict) -> ResumeSpec | None:
        sid = _record_stable_id(record)
        if not sid or not record.get("cwd"):
            return None
        return ResumeSpec(cwd=record["cwd"], argv=("agent-hq", "codex", "--", "resume", sid))

    def transcript_phase(self, path: str, stable_id: str | None) -> DevPhase | None:
        tag = extract_last_codex_phase_tag(path, stable_id)
        if tag is None:
            return None
        try:
            return DevPhase(normalize_phase_value(tag))
        except ValueError:
            return None

    def transcript_summary(self, path: str, stable_id: str | None) -> str | None:
        return extract_last_codex_assistant_text(path, stable_id)


_ADAPTERS: dict[AgentType, HarnessAdapter] = {
    AgentType.CLAUDE: ClaudeAdapter(),
    AgentType.CODEX: CodexAdapter(),
}


def get_adapter(kind: AgentType | str) -> HarnessAdapter | None:
    if isinstance(kind, str):
        try:
            kind = AgentType(kind)
        except ValueError:
            return None
    return _ADAPTERS.get(kind)
