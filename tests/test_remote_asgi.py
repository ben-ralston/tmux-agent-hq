"""RemoteFastMCP's error boundary (#396 review): Claude gets a fixed message
naming only the exception type - never paths, YAML lines or other text a
tool's exception carries - and bad arguments are not logged as faults."""

import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from agent_hq.remote.asgi import RemoteFastMCP
from agent_hq.remote.request_log import RequestLog


@pytest.fixture
def server(tmp_path):
    log_path = tmp_path / "requests.log"
    mcp = RemoteFastMCP("t", request_log=RequestLog(log_path))

    @mcp.tool()
    def list_agents(state: int = 0) -> str:
        """Fails with a path in its message."""
        raise OSError("[Errno 13] Permission denied: '/Users/dev/.config/agent-hq/x'")

    return mcp, log_path


def _log(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


async def test_tool_exception_text_never_reaches_claude(server):
    mcp, log_path = server
    with pytest.raises(ToolError) as info:
        await mcp.call_tool("list_agents", {})
    message = str(info.value)
    assert "/Users" not in message and "Permission denied" not in message
    assert message == (
        "list_agents failed (OSError). Details are in the agent-hq server log on the owner's Mac."
    )
    assert _log(log_path)[-1]["outcome"] == "error:OSError"


async def test_bad_arguments_are_reported_as_such_not_as_a_fault(server):
    mcp, log_path = server
    with pytest.raises(ToolError) as info:
        await mcp.call_tool("list_agents", {"state": "not-a-number"})
    assert str(info.value).startswith("Invalid arguments for list_agents")
    assert _log(log_path)[-1]["outcome"] == "invalid_arguments"
