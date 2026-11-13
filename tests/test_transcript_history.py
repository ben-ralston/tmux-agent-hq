"""Tests for ``agent_hq.transcript_history`` — transcript-local scrollback (#171).

The history pager turns a Claude Code transcript JSONL into renderable
conversation turns, paged newest-first with byte-offset cursors. Pure
reads only: no clock, no writes, tolerant of malformed input.
"""

from __future__ import annotations

import json

from agent_hq.transcript_history import (
    HistoryPage,
    TranscriptTurn,
    read_history_page,
)


def _write_jsonl(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _user(text, **extra):
    return {
        "type": "user",
        "message": {"content": text},
        "timestamp": "2026-07-14T00:00:00Z",
        **extra,
    }


def _assistant(blocks, **extra):
    return {
        "type": "assistant",
        "message": {"content": blocks},
        "timestamp": "2026-07-14T00:00:01Z",
        **extra,
    }


def _text(t):
    return {"type": "text", "text": t}


def _tool(name, tool_input):
    return {"type": "tool_use", "name": name, "input": tool_input}


class TestTurnExtraction:
    def test_orders_turns_oldest_first_with_block_order(self, tmp_path):
        t = tmp_path / "s.jsonl"
        _write_jsonl(
            t,
            [
                _user("fix the bug"),
                _assistant([_text("Looking."), _tool("Bash", {"command": "git status"})]),
            ],
        )
        page = read_history_page(str(t))
        assert isinstance(page, HistoryPage)
        assert [(turn.kind, turn.text) for turn in page.turns] == [
            ("user", "fix the bug"),
            ("assistant", "Looking."),
            ("tool", "Bash(git status)"),
        ]
        assert page.has_more is False
        assert page.next_cursor is None

    def test_user_record_with_text_blocks(self, tmp_path):
        t = tmp_path / "s.jsonl"
        _write_jsonl(t, [{"type": "user", "message": {"content": [_text("hello there")]}}])
        page = read_history_page(str(t))
        assert [(turn.kind, turn.text) for turn in page.turns] == [("user", "hello there")]

    def test_assistant_text_preserves_paragraphs(self, tmp_path):
        t = tmp_path / "s.jsonl"
        _write_jsonl(t, [_assistant([_text("para one.\n\npara two.")])])
        page = read_history_page(str(t))
        assert page.turns[0].text == "para one.\n\npara two."

    def test_timestamp_carried_onto_turns(self, tmp_path):
        t = tmp_path / "s.jsonl"
        _write_jsonl(t, [_user("hi")])
        assert read_history_page(str(t)).turns[0].timestamp == "2026-07-14T00:00:00Z"

    def test_tool_one_liner_arg_selection(self, tmp_path):
        t = tmp_path / "s.jsonl"
        _write_jsonl(
            t,
            [
                # Text blocks between tools keep each run below the
                # collapse threshold so the one-liners stay visible.
                _assistant(
                    [
                        _tool("Bash", {"command": "git log", "description": "ignored"}),
                        _tool("Edit", {"file_path": "/tmp/x.py", "old_string": "a"}),
                        _text("…"),
                        _tool("Grep", {"pattern": "foo.*bar"}),
                        _tool("Task", {"description": "explore the repo"}),
                        _text("…"),
                        _tool("Weird", {"count": 3}),
                    ]
                )
            ],
        )
        texts = [turn.text for turn in read_history_page(str(t)).turns]
        assert texts == [
            "Bash(git log)",
            "Edit(/tmp/x.py)",
            "…",
            "Grep(foo.*bar)",
            "Task(explore the repo)",
            "…",
            "Weird()",
        ]

    def test_tool_arg_truncated(self, tmp_path):
        t = tmp_path / "s.jsonl"
        _write_jsonl(t, [_assistant([_tool("Bash", {"command": "x" * 300})])])
        text = read_history_page(str(t)).turns[0].text
        assert len(text) <= 90
        assert text.startswith("Bash(xxx")
        assert text.endswith("…)")

    def test_skips_noise_records_and_blocks(self, tmp_path):
        t = tmp_path / "s.jsonl"
        records = [
            _user("real prompt"),
            _assistant([{"type": "thinking", "thinking": "hmm"}, _text("visible")]),
            # tool_result-only user record (huge in real transcripts)
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "big dump"}]},
            },
            # sidechain (subagent) traffic
            _assistant([_text("subagent text")], isSidechain=True),
            _user("sidechain prompt", isSidechain=True),
            # non-message record types
            {"type": "ai-title", "title": "t"},
            {"type": "queue-operation", "op": "x"},
            {"type": "system", "content": "<phase>done</phase>"},
            {"type": "summary", "summary": "s"},
        ]
        _write_jsonl(t, records)
        raw = t.read_text() + "{not json}\n" + '{"type": "assistant"}\n'
        t.write_text(raw)
        page = read_history_page(str(t))
        assert [(turn.kind, turn.text) for turn in page.turns] == [
            ("user", "real prompt"),
            ("assistant", "visible"),
        ]


class TestInjectedRecordClassification:
    """Injected records must not render as user prompts (#171 follow-up).

    Claude Code's live UI shows these as dimmed transient notices or not at
    all; history mirrors that by skipping, reclassifying as ``system``, or
    condensing. Live-transcript audit 2026-07-17: 33-68% of rendered "user"
    turns were injected content before this classification existed.
    """

    def _turns(self, tmp_path, record):
        t = tmp_path / "s.jsonl"
        _write_jsonl(t, [record])
        return read_history_page(str(t)).turns

    def test_meta_records_are_skipped(self, tmp_path):
        # Skill/command expansions, hook feedback, local-command caveats.
        turns = self._turns(tmp_path, _user("Base directory for this skill: /x", isMeta=True))
        assert turns == []

    def test_compact_summary_becomes_divider(self, tmp_path):
        record = _user(
            "This session is being continued from a previous conversation…",
            isCompactSummary=True,
            isVisibleInTranscriptOnly=True,
        )
        turns = self._turns(tmp_path, record)
        assert [(t.kind, t.text) for t in turns] == [("system", "— conversation compacted —")]

    def test_command_record_condenses_to_command_line(self, tmp_path):
        raw = (
            "<command-name>/model</command-name>\n"
            "<command-message>model</command-message>\n"
            "<command-args>opus</command-args>"
        )
        turns = self._turns(tmp_path, _user(raw))
        assert [(t.kind, t.text) for t in turns] == [("user", "/model opus")]

    def test_command_record_with_message_tag_first(self, tmp_path):
        # Live transcripts sometimes order <command-message> before
        # <command-name> — classification keys on any <command-*> prefix.
        raw = "<command-message>doctor</command-message>\n<command-name>/doctor</command-name>"
        turns = self._turns(tmp_path, _user(raw))
        assert [(t.kind, t.text) for t in turns] == [("user", "/doctor")]

    def test_command_record_without_args(self, tmp_path):
        raw = (
            "<command-name>/compact</command-name>\n"
            "<command-message>compact</command-message>\n"
            "<command-args></command-args>"
        )
        turns = self._turns(tmp_path, _user(raw))
        assert [(t.kind, t.text) for t in turns] == [("user", "/compact")]

    def test_local_command_stdout_is_system_with_ansi_stripped(self, tmp_path):
        raw = "<local-command-stdout>Set model to \x1b[1mOpus\x1b[22m</local-command-stdout>"
        turns = self._turns(tmp_path, _user(raw))
        assert [(t.kind, t.text) for t in turns] == [("system", "Set model to Opus")]

    def test_empty_local_command_stdout_is_skipped(self, tmp_path):
        turns = self._turns(tmp_path, _user("<local-command-stdout></local-command-stdout>"))
        assert turns == []

    def test_task_notification_condenses_to_summary(self, tmp_path):
        raw = (
            "<task-notification>\n<task-id>abc</task-id>\n"
            "<output-file>/tmp/x</output-file>\n<status>completed</status>\n"
            '<summary>Background command "poll" completed (exit code 0)</summary>\n'
            "</task-notification>"
        )
        turns = self._turns(tmp_path, _user(raw))
        assert [(t.kind, t.text) for t in turns] == [
            ("system", 'Background command "poll" completed (exit code 0)')
        ]

    def test_teammate_message_condenses_to_id_and_summary(self, tmp_path):
        raw = (
            "Another Claude session sent a message:\n"
            '<teammate-message teammate_id="toolkit-review" color="green"'
            ' summary="Review clean, 4 nits">\n'
            '{"type":"result"}\n'
            "</teammate-message>\n\nThis came from another Claude session."
        )
        turns = self._turns(tmp_path, _user(raw))
        assert [(t.kind, t.text) for t in turns] == [
            ("system", "teammate toolkit-review: Review clean, 4 nits")
        ]

    def test_teammate_message_without_summary_uses_body_first_line(self, tmp_path):
        raw = (
            "Another Claude session sent a message:\n"
            '<teammate-message teammate_id="codex-review" color="blue">\n'
            '{"type":"idle_notification","from":"codex-review"}\n'
            "</teammate-message>"
        )
        turns = self._turns(tmp_path, _user(raw))
        assert turns[0].kind == "system"
        assert turns[0].text.startswith("teammate codex-review: ")
        assert "idle_notification" in turns[0].text

    def test_interrupt_marker_is_system(self, tmp_path):
        for raw in (
            "[Request interrupted by user]",
            "[Request interrupted by user for tool use]",
        ):
            turns = self._turns(tmp_path, _user(raw))
            assert [(t.kind, t.text) for t in turns] == [("system", "Interrupted by user")]

    def test_plain_prompts_still_render_as_user(self, tmp_path):
        turns = self._turns(tmp_path, _user("please rebase onto main"))
        assert [(t.kind, t.text) for t in turns] == [("user", "please rebase onto main")]


def _peer_prompt(body, name=None):
    attrs = 'from="uds:/tmp/cc-socks/66500.sock"'
    if name:
        attrs += f' from-name="{name}"'
    return f'<cross-session-message {attrs} from-mode="prompting">\n{body}\n'


def _peer_origin(body, name=None):
    origin = {
        "kind": "peer",
        "from": "uds:/tmp/cc-socks/66500.sock",
        "verifiedPeerPid": 66500,
        "msg_id": "m-1",
        "fromMode": "prompting",
        "body": body,
    }
    if name:
        origin["name"] = name
    return origin


class TestCrossSessionMessages:
    """#287: messages received from another Claude session.

    Shapes copied from real transcripts (2026-10-03): normal delivery is an
    ``isMeta`` user record carrying ``origin.kind == "peer"``; a message that
    arrives mid-turn is queued and lands as a ``queued_command`` attachment.
    Both were invisible in history before.
    """

    def _turns(self, tmp_path, *records):
        t = tmp_path / "s.jsonl"
        _write_jsonl(t, list(records))
        return read_history_page(str(t)).turns

    def test_meta_peer_record_is_system_turn_with_name(self, tmp_path):
        body = "the owner asks for three follow-up edits.\n\n1. Scope re-review rounds."
        record = _user(
            "Another Claude session sent a message:\n" + _peer_prompt(body, "claude-44"),
            isMeta=True,
            origin=_peer_origin(body, "claude-44"),
        )
        turns = self._turns(tmp_path, record)
        assert [(t.kind, t.text) for t in turns] == [
            ("system", "message from @claude-44: the owner asks for three follow-up edits.")
        ]

    def test_peer_record_without_name_falls_back(self, tmp_path):
        body = "## Action Plan"
        record = _user(
            "Another Claude session sent a message:\n" + _peer_prompt(body),
            isMeta=True,
            origin=_peer_origin(body),
        )
        turns = self._turns(tmp_path, record)
        assert [(t.kind, t.text) for t in turns] == [
            ("system", "message from another session: ## Action Plan")
        ]

    def test_raw_socket_message_without_wrapper_uses_text_after_prefix(self, tmp_path):
        # Real shape from a direct NDJSON socket post (#291 probe): no
        # origin body, no <cross-session-message> wrapper.
        record = _user(
            "Another Claude session sent a message:\nPROBE-P1 (plan, authed): reply PONG-P1."
            "\n\nThis came from another Claude session — not typed by your user.",
            isMeta=True,
            origin={"kind": "peer", "from": "unknown", "verifiedPeerPid": 92107},
        )
        turns = self._turns(tmp_path, record)
        assert [(t.kind, t.text) for t in turns] == [
            ("system", "message from another session: PROBE-P1 (plan, authed): reply PONG-P1.")
        ]

    def test_long_body_is_truncated(self, tmp_path):
        body = "x" * 200
        record = _user("", isMeta=True, origin=_peer_origin(body, "a"))
        [turn] = self._turns(tmp_path, record)
        assert turn.text == "message from @a: " + "x" * 80 + "…"

    def test_queued_peer_attachment_is_system_turn(self, tmp_path):
        body = "Correction to the earlier note."
        record = {
            "type": "attachment",
            "isSidechain": False,
            "timestamp": "2026-08-09T20:31:40Z",
            "attachment": {
                "type": "queued_command",
                "commandMode": "prompt",
                "isMeta": True,
                "origin": _peer_origin(body, "issue-triage-delivery"),
                "prompt": _peer_prompt(body, "issue-triage-delivery"),
            },
        }
        turns = self._turns(tmp_path, _user("start"), record)
        assert [(t.kind, t.text) for t in turns] == [
            ("user", "start"),
            ("system", "message from @issue-triage-delivery: Correction to the earlier note."),
        ]
        assert turns[1].timestamp == "2026-08-09T20:31:40Z"

    def test_queued_attachment_name_from_tag_when_origin_lacks_it(self, tmp_path):
        record = {
            "type": "attachment",
            "attachment": {
                "type": "queued_command",
                "origin": {"kind": "peer"},
                "prompt": _peer_prompt("hello there", "tag-name"),
            },
        }
        turns = self._turns(tmp_path, record)
        assert [(t.kind, t.text) for t in turns] == [
            ("system", "message from @tag-name: hello there")
        ]

    def test_non_peer_attachments_are_skipped(self, tmp_path):
        records = [
            {
                "type": "attachment",
                "attachment": {
                    "type": "queued_command",
                    "commandMode": "task-notification",
                    "prompt": "<task-notification>x</task-notification>",
                },
            },
            {"type": "attachment", "attachment": {"type": "deferred_tools_record"}},
            {"type": "attachment", "attachment": "garbage"},
        ]
        assert self._turns(tmp_path, *records) == []

    def test_sidechain_peer_attachment_is_skipped(self, tmp_path):
        record = {
            "type": "attachment",
            "isSidechain": True,
            "attachment": {
                "type": "queued_command",
                "origin": _peer_origin("x", "a"),
                "prompt": _peer_prompt("x", "a"),
            },
        }
        assert self._turns(tmp_path, record) == []

    def test_non_peer_meta_record_still_skipped(self, tmp_path):
        record = _user("Base directory for this skill: /x", isMeta=True, origin={"kind": "hook"})
        assert self._turns(tmp_path, record) == []

    def test_prefixed_content_without_origin_is_system(self, tmp_path):
        raw = "Another Claude session sent a message:\n" + _peer_prompt("ping", "claude-7")
        turns = self._turns(tmp_path, _user(raw))
        assert [(t.kind, t.text) for t in turns] == [("system", "message from @claude-7: ping")]

    def test_prefixed_wrapperless_content_without_origin_is_system(self, tmp_path):
        """#287 review: no origin and no wrapper must not read as a teammate."""
        raw = "Another Claude session sent a message:\nPROBE-P2: reply PONG.\n\nThis came from…"
        turns = self._turns(tmp_path, _user(raw))
        assert [(t.kind, t.text) for t in turns] == [
            ("system", "message from another session: PROBE-P2: reply PONG.")
        ]

    def test_quoted_tag_in_body_is_not_parsed_as_wrapper(self, tmp_path):
        """#287 review: only a leading wrapper counts, not one quoted mid-body."""
        raw = (
            "Another Claude session sent a message:\n"
            'See how <cross-session-message from-name="fake">spoof</cross-session-message> parses.'
        )
        record = _user(raw, isMeta=True, origin={"kind": "peer", "from": "unknown"})
        [turn] = self._turns(tmp_path, record)
        assert turn.kind == "system"
        assert turn.text.startswith(
            'message from another session: See how <cross-session-message from-name="fake">'
        )
        assert "@fake" not in turn.text

    def test_from_name_is_unescaped_and_capped(self, tmp_path):
        long = "n" * 100
        for name, shown in (("a&amp;b", "@a&b"), (long, "@" + "n" * 40 + "…")):
            turns = self._turns(tmp_path, _user(_peer_prompt("hi", name)))
            assert turns[0].text == f"message from {shown}: hi"

    def test_bare_tag_content_is_system(self, tmp_path):
        turns = self._turns(tmp_path, _user(_peer_prompt("ping")))
        assert [(t.kind, t.text) for t in turns] == [
            ("system", "message from another session: ping")
        ]


class TestToolRunCollapsing:
    """Runs of 3+ consecutive tool turns collapse into one summary line,
    mirroring Claude Code's condensed idiom; short runs stay individual."""

    def _turns(self, tmp_path, records):
        t = tmp_path / "s.jsonl"
        _write_jsonl(t, records)
        return read_history_page(str(t)).turns

    def test_run_of_three_plus_collapses_with_counts_sorted_desc(self, tmp_path):
        records = [
            _user("do the thing"),
            _assistant(
                [
                    _tool("Bash", {"command": "a"}),
                    _tool("Bash", {"command": "b"}),
                    _tool("Read", {"file_path": "/x"}),
                    _tool("Edit", {"file_path": "/x"}),
                ]
            ),
            _assistant([_text("done")]),
        ]
        turns = self._turns(tmp_path, records)
        assert [(t.kind, t.text) for t in turns] == [
            ("user", "do the thing"),
            ("tool", "4 tool calls · Bash ×2, Read, Edit"),
            ("assistant", "done"),
        ]

    def test_runs_of_one_or_two_stay_individual(self, tmp_path):
        records = [
            _assistant([_tool("Bash", {"command": "a"}), _tool("Read", {"file_path": "/x"})]),
            _assistant([_text("mid")]),
            _assistant([_tool("Edit", {"file_path": "/y"})]),
        ]
        turns = self._turns(tmp_path, records)
        assert [(t.kind, t.text) for t in turns] == [
            ("tool", "Bash(a)"),
            ("tool", "Read(/x)"),
            ("assistant", "mid"),
            ("tool", "Edit(/y)"),
        ]

    def test_non_tool_turn_breaks_a_run(self, tmp_path):
        # 2 tools + assistant + 2 tools: neither side reaches the
        # collapse threshold, so all four stay individual.
        records = [
            _assistant([_tool("Bash", {"command": "a"}), _tool("Bash", {"command": "b"})]),
            _assistant([_text("mid")]),
            _assistant([_tool("Bash", {"command": "c"}), _tool("Bash", {"command": "d"})]),
        ]
        turns = self._turns(tmp_path, records)
        assert [t.kind for t in turns] == ["tool", "tool", "assistant", "tool", "tool"]
        assert all("tool calls" not in t.text for t in turns)

    def test_run_spanning_records_collapses(self, tmp_path):
        # Consecutive-ness is about rendered order, not record boundaries.
        records = [
            _assistant([_tool("Bash", {"command": "a"})]),
            _assistant([_tool("Bash", {"command": "b"})]),
            _assistant([_tool("Bash", {"command": "c"})]),
        ]
        turns = self._turns(tmp_path, records)
        assert [(t.kind, t.text) for t in turns] == [("tool", "3 tool calls · Bash ×3")]


class TestPagination:
    def test_pages_walk_backwards_without_overlap(self, tmp_path):
        t = tmp_path / "s.jsonl"
        _write_jsonl(t, [_user(f"msg {i}") for i in range(10)])
        page1 = read_history_page(str(t), max_turns=4)
        assert [turn.text for turn in page1.turns] == [f"msg {i}" for i in range(6, 10)]
        assert page1.has_more is True
        assert isinstance(page1.next_cursor, int)

        page2 = read_history_page(str(t), before_offset=page1.next_cursor, max_turns=4)
        assert [turn.text for turn in page2.turns] == [f"msg {i}" for i in range(2, 6)]
        assert page2.has_more is True

        page3 = read_history_page(str(t), before_offset=page2.next_cursor, max_turns=4)
        assert [turn.text for turn in page3.turns] == ["msg 0", "msg 1"]
        assert page3.has_more is False
        assert page3.next_cursor is None

    def test_cursor_stable_while_file_grows(self, tmp_path):
        t = tmp_path / "s.jsonl"
        _write_jsonl(t, [_user(f"old {i}") for i in range(6)])
        page1 = read_history_page(str(t), max_turns=3)
        assert [turn.text for turn in page1.turns] == ["old 3", "old 4", "old 5"]
        # transcript grows between pages — append-only, offsets stay valid
        with open(t, "a") as f:
            f.write(json.dumps(_user("new arrival")) + "\n")
        page2 = read_history_page(str(t), before_offset=page1.next_cursor, max_turns=3)
        assert [turn.text for turn in page2.turns] == ["old 0", "old 1", "old 2"]
        assert page2.has_more is False

    def test_record_never_split_across_pages(self, tmp_path):
        t = tmp_path / "s.jsonl"
        _write_jsonl(
            t,
            [
                _user("earlier"),
                _assistant(
                    [_text("a"), _tool("Bash", {"command": "b"}), _tool("Bash", {"command": "c"})]
                ),
            ],
        )
        page = read_history_page(str(t), max_turns=2)
        # the assistant record yields 3 turns; it is included whole
        assert [turn.text for turn in page.turns] == ["a", "Bash(b)", "Bash(c)"]
        assert page.has_more is True
        page2 = read_history_page(str(t), before_offset=page.next_cursor)
        assert [turn.text for turn in page2.turns] == ["earlier"]

    def test_record_larger_than_reverse_chunk(self, tmp_path):
        t = tmp_path / "s.jsonl"
        big = "y" * (150 * 1024)  # single line spans multiple 64 KiB chunks
        _write_jsonl(t, [_user("before"), _user(big), _user("after")])
        page = read_history_page(str(t), max_turns=1)
        assert [turn.text for turn in page.turns] == ["after"]
        page2 = read_history_page(str(t), before_offset=page.next_cursor, max_turns=1)
        assert page2.turns[0].text == big
        page3 = read_history_page(str(t), before_offset=page2.next_cursor, max_turns=1)
        assert [turn.text for turn in page3.turns] == ["before"]
        assert page3.has_more is False


class TestLocateTranscript:
    """``locate_transcript`` — the stat-side gate shared by the web
    routes and the TUI (candidates from ``transcript_path_for``)."""

    def test_returns_first_existing_candidate(self):
        from pathlib import Path

        from agent_hq.transcript import transcript_path_for
        from agent_hq.transcript_history import locate_transcript

        path = Path(transcript_path_for("/proj", "sid-1")[0])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        assert locate_transcript("/proj", "sid-1") == str(path)

    def test_none_when_no_candidate_exists(self):
        from agent_hq.transcript_history import locate_transcript

        assert locate_transcript("/proj", "no-such-session") is None
        assert locate_transcript("/proj", None) is None


class TestDegenerateInputs:
    def test_missing_file_returns_empty_page(self):
        page = read_history_page("/no/such/file.jsonl")
        assert page.turns == []
        assert page.has_more is False
        assert page.next_cursor is None

    def test_empty_file(self, tmp_path):
        t = tmp_path / "s.jsonl"
        t.write_text("")
        page = read_history_page(str(t))
        assert page.turns == []
        assert page.has_more is False

    def test_turn_dataclass_shape(self):
        turn = TranscriptTurn(kind="user", text="x", timestamp=None)
        assert (turn.kind, turn.text, turn.timestamp) == ("user", "x", None)
