from pathlib import Path

from agent_hq.adapters import ResumeSpec, get_adapter
from agent_hq.models import DevPhase


def test_claude_launch_args_empty_by_default():
    assert get_adapter("claude").launch_args(None, None) == []


def test_claude_launch_args_with_model_names_harness():
    assert get_adapter("claude").launch_args("opus", None) == [
        "--harness",
        "claude",
        "--model",
        "opus",
    ]


def test_codex_launch_args_always_name_harness():
    assert get_adapter("codex").launch_args(None, None) == ["--harness", "codex"]
    assert get_adapter("codex").launch_args("gpt-5.6-sol", "high") == [
        "--harness",
        "codex",
        "--model",
        "gpt-5.6-sol",
        "--effort",
        "high",
    ]


def test_repl_commands():
    assert get_adapter("claude").repl_command("acceptEdits") == [
        "claude",
        "--permission-mode",
        "acceptEdits",
    ]
    assert get_adapter("codex").repl_command("acceptEdits") == ["agent-hq", "codex", "--"]


def test_claude_repl_command_carries_model_and_effort():
    """#232: the dispatch controller pins model/effort on the REPL invocation."""
    assert get_adapter("claude").repl_command("auto", model="opus", effort="high") == [
        "claude",
        "--permission-mode",
        "auto",
        "--model",
        "opus",
        "--effort",
        "high",
    ]


def test_codex_repl_command_ignores_model_and_effort():
    """codex's bare REPL launch takes neither flag -- passing them is a no-op."""
    assert get_adapter("codex").repl_command("auto", model="opus", effort="high") == [
        "agent-hq",
        "codex",
        "--",
    ]


def test_matches_process_leaf_only():
    codex = get_adapter("codex")
    assert codex.matches_process("/usr/local/bin/codex")
    assert not codex.matches_process("/home/codex/bin/claude")


def test_resume_specs():
    claude = get_adapter("claude").resume_spec({"tool": "claude", "stable_id": "s1", "cwd": "/w"})
    assert claude == ResumeSpec(cwd="/w", argv=("claude", "--resume", "s1"))
    codex = get_adapter("codex").resume_spec({"tool": "codex", "session_id": "u7", "cwd": "/w"})
    assert codex == ResumeSpec(cwd="/w", argv=("agent-hq", "codex", "--", "resume", "u7"))
    assert get_adapter("codex").resume_spec({"tool": "codex", "cwd": "/w"}) is None


def test_unknown_harness_returns_none():
    assert get_adapter("aider") is None


class TestTranscriptCapabilities:
    """Stage 2 adapter extension (#206): transcript_phase / transcript_summary."""

    FIXTURES = Path(__file__).parent / "fixtures" / "codex_rollouts"
    PLAIN_ID = "00000000-0000-7000-8000-000000000001"

    def test_claude_phase_delegates_to_transcript_reader(self, monkeypatch):
        monkeypatch.setattr("agent_hq.adapters.detect_transcript_phase", lambda path: DevPhase.QA)
        assert get_adapter("claude").transcript_phase("/t.jsonl", "ignored") is DevPhase.QA

    def test_claude_summary_delegates(self, monkeypatch):
        monkeypatch.setattr("agent_hq.adapters.extract_last_assistant_text", lambda path: "hi")
        assert get_adapter("claude").transcript_summary("/t.jsonl", None) == "hi"

    def test_codex_phase_from_fixture(self):
        phase = get_adapter("codex").transcript_phase(
            str(self.FIXTURES / "plain.jsonl"), self.PLAIN_ID
        )
        assert phase is DevPhase.IMPLEMENT

    def test_codex_phase_wrong_thread_id(self):
        assert (
            get_adapter("codex").transcript_phase(str(self.FIXTURES / "plain.jsonl"), "other")
            is None
        )

    def test_codex_summary_from_fixture(self):
        text = get_adapter("codex").transcript_summary(
            str(self.FIXTURES / "plain.jsonl"), self.PLAIN_ID
        )
        assert text == "<phase>implement</phase>\nFixture response."


def test_chat_command_bare_by_default():
    """#290: with nothing passed, the chat line is the pre-#290 bare REPL."""
    assert get_adapter("claude").chat_command() == ["claude"]
    assert get_adapter("codex").chat_command() == ["agent-hq", "codex", "--"]


def test_claude_chat_command_model_effort_prompt():
    cmd = get_adapter("claude").chat_command(
        model="opus", effort="high", prompt="/clean-gone --all"
    )
    assert cmd == ["claude", "--model", "opus", "--effort", "high", "--", "/clean-gone --all"]


def test_codex_chat_command_model_effort_prompt():
    """codex has no --effort flag: effort rides a -c override, which the
    agent-hq codex wrapper forwards to both frontend and app server."""
    cmd = get_adapter("codex").chat_command(model="gpt-5", effort="high", prompt="hi")
    assert cmd == [
        "agent-hq",
        "codex",
        "--",
        "-c",
        "model=gpt-5",
        "-c",
        "model_reasoning_effort=high",
        "--",
        "hi",
    ]


def test_option_like_prompt_stays_a_positional():
    """An option- or subcommand-shaped prompt must reach the harness as the
    message, never as a flag (Codex review, #290): ``--`` ends parsing."""
    assert get_adapter("claude").chat_command(prompt="--help")[-2:] == ["--", "--help"]
    assert get_adapter("codex").chat_command(prompt="resume")[-2:] == ["--", "resume"]


def test_chat_efforts_per_harness():
    """Claude's CLI rejects none/minimal; codex has no max."""
    claude_efforts = frozenset({"low", "medium", "high", "xhigh", "max"})
    assert get_adapter("claude").chat_efforts == claude_efforts
    assert get_adapter("codex").chat_efforts == frozenset(
        {"none", "minimal", "low", "medium", "high", "xhigh"}
    )


def test_chat_command_session_name_is_claude_only():
    """#429: a named chat session shows as @name in list_agents."""
    cmd = get_adapter("claude").chat_command(
        model="opus", prompt="/daily-review", name="daily-review"
    )
    assert cmd == ["claude", "--model", "opus", "--name", "daily-review", "--", "/daily-review"]
    assert "--name" not in get_adapter("codex").chat_command(prompt="hi", name="daily-review")
