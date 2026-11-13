"""Tests for dispatch-related data models."""

from agent_hq.models import Complexity, Issue, Priority


class TestPriority:
    def test_need_value(self):
        assert Priority.NEED.value == "Need"

    def test_want_value(self):
        assert Priority.WANT.value == "Want"

    def test_idea_value(self):
        assert Priority.IDEA.value == "Idea"

    def test_no_pri_value(self):
        assert Priority.NO_PRI.value == ""

    def test_sort_order(self):
        members = list(Priority)
        assert members == [Priority.NEED, Priority.WANT, Priority.IDEA, Priority.NO_PRI]


class TestComplexity:
    def test_trivial_value(self):
        assert Complexity.TRIVIAL.value == "trivial"

    def test_straightforward_value(self):
        assert Complexity.STRAIGHTFORWARD.value == "straightforward"

    def test_complex_value(self):
        assert Complexity.COMPLEX.value == "complex"

    def test_exploratory_value(self):
        assert Complexity.EXPLORATORY.value == "exploratory"


class TestIssue:
    def test_key_format(self):
        issue = Issue(
            number=42,
            title="Test",
            repo="ben-ralston/agent-hq",
            status="Todo",
            priority=Priority.NEED,
        )
        assert issue.key == "ben-ralston/agent-hq#42"

    def test_repo_short(self):
        issue = Issue(
            number=1,
            title="Test",
            repo="ben-ralston/agent-hq",
            status=None,
            priority=Priority.NO_PRI,
        )
        assert issue.repo_short == "agent-hq"

    def test_repo_short_no_slash(self):
        issue = Issue(
            number=1, title="Test", repo="agent-hq", status=None, priority=Priority.NO_PRI
        )
        assert issue.repo_short == "agent-hq"

    def test_default_fields(self):
        issue = Issue(number=1, title="T", repo="o/r", status=None, priority=Priority.NO_PRI)
        assert issue.labels == []
        assert issue.body == ""
