import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from helpers import CONFIG_PATH
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent

from flagbox.agent.logged_tools import LoggedSession
from flagbox.agent.runlog import RunLog, iter_records, verify_log
from flagbox.server.server import build_server

ARGS = {"buyer_key": "b1", "date_from": "2024-01-01", "date_to": "2024-12-31"}


class FakeSession:
    """Stands in for an MCP session: returns a canned result or raises."""

    def __init__(
        self, result: CallToolResult | None = None, error: Exception | None = None
    ) -> None:
        self.result, self.error = result, error
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, *a: Any, **k: Any
    ):
        self.calls.append((name, arguments))
        if self.error:
            raise self.error
        assert self.result is not None
        return self.result

    async def list_tools(self, *a: Any, **k: Any) -> str:
        return "tools"


def text_result(text: str, is_error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=is_error)


def tool_calls(log: RunLog) -> list[dict[str, Any]]:
    return [r for r in iter_records(log.path) if r["event"] == "tool_call"]


# ---- against our real server, over the real protocol ----------


async def _real_calls(data_dir: Path, log: RunLog):
    server = build_server(data_dir, CONFIG_PATH)
    async with create_connected_server_and_client_session(server) as client:
        session = LoggedSession(client, log, server="flagbox")
        first = await session.call_tool("repeat_winner", ARGS)
        second = await session.call_tool("data_coverage", ARGS)
        bad = await session.call_tool("no_such_tool", {})
        return first, second, bad


def test_every_call_is_logged_with_an_id_and_the_id_reaches_the_model(
    data_dir: Path, tmp_path: Path
):
    log = RunLog.create(tmp_path / "logs")
    first, second, bad = asyncio.run(_real_calls(data_dir, log))

    # What the model reads carries the id, in both the structured and the text form.
    assert first.structuredContent is not None
    assert first.structuredContent["tool_call_id"] == "tc-0001"
    block = first.content[0]
    assert isinstance(block, TextContent)
    assert json.loads(block.text)["tool_call_id"] == "tc-0001"
    assert second.structuredContent is not None
    assert second.structuredContent["tool_call_id"] == "tc-0002"

    # The log has one record per call, with the original arguments and the full result.
    calls = tool_calls(log)
    assert [c["tool_call_id"] for c in calls] == ["tc-0001", "tc-0002", "tc-0003"]
    assert [c["tool"] for c in calls] == ["repeat_winner", "data_coverage", "no_such_tool"]
    assert calls[0]["arguments"] == ARGS and calls[0]["server"] == "flagbox"
    assert calls[0]["result"]["hits_total"] == 2
    assert calls[0]["result"]["hits"][0]["hit_id"].startswith("RW-")
    assert "tool_call_id" not in calls[0]["result"]  # the log keeps the server's own output
    assert isinstance(calls[0]["duration_ms"], int) and calls[0]["is_error"] is False

    # A call the server rejects is logged as an error, not lost.
    assert bad.isError is True and calls[2]["is_error"] is True
    log.finish("completed")
    assert verify_log(log.path).ok


# ---- with fakes: the edge cases ----------


def test_an_exception_is_logged_and_then_raised(tmp_path: Path):
    log = RunLog.create(tmp_path)
    session = LoggedSession(FakeSession(error=ConnectionError("server died")), log, "flagbox")
    with pytest.raises(ConnectionError):
        asyncio.run(session.call_tool("repeat_winner", ARGS))
    (call,) = tool_calls(log)
    assert call["is_error"] is True and "ConnectionError: server died" in call["error"]
    assert "result" not in call and verify_log(log.path).ok


def test_plain_text_results_from_other_servers_get_the_id_as_an_extra_block(tmp_path: Path):
    log = RunLog.create(tmp_path)
    session = LoggedSession(FakeSession(text_result("[FILE] a.md")), log, "filesystem")
    result = asyncio.run(session.call_tool("list_directory", {"path": "filed"}))
    assert [b.text for b in result.content if isinstance(b, TextContent)][0] == "[FILE] a.md"
    assert json.loads(result.content[-1].text)["tool_call_id"] == "tc-0001"  # type: ignore[union-attr]
    (call,) = tool_calls(log)
    assert call["server"] == "filesystem" and call["result"] == {"text": ["[FILE] a.md"]}


def test_two_sessions_share_one_chain_and_one_numbering(tmp_path: Path):
    log = RunLog.create(tmp_path)
    ours = LoggedSession(FakeSession(text_result("{}")), log, "flagbox")
    borrowed = LoggedSession(FakeSession(text_result("ok")), log, "filesystem")

    async def go() -> None:
        await ours.call_tool("data_coverage", ARGS)
        await borrowed.call_tool("write_file", {"path": "x", "content": "y"})
        await ours.call_tool("repeat_winner", ARGS)

    asyncio.run(go())
    calls = tool_calls(log)
    assert [(c["tool_call_id"], c["server"]) for c in calls] == [
        ("tc-0001", "flagbox"),
        ("tc-0002", "filesystem"),
        ("tc-0003", "flagbox"),
    ]
    assert verify_log(log.path).ok


def test_the_wrapper_offers_no_unlogged_route_to_a_tool(tmp_path: Path):
    session = LoggedSession(FakeSession(text_result("x")), RunLog.create(tmp_path), "flagbox")
    for attribute in ("send_request", "send_notification", "_session_call", "initialize"):
        assert not hasattr(session, attribute)
    assert asyncio.run(session.list_tools()) == "tools"  # listing is allowed; it is not a tool call


def test_arguments_are_logged_as_given_even_if_odd(tmp_path: Path):
    log = RunLog.create(tmp_path)
    session = LoggedSession(FakeSession(text_result("{}")), log, "flagbox")
    asyncio.run(session.call_tool("find_buyer", {"name": "x'; DROP TABLE buyers; --", "limit": 5}))
    (call,) = tool_calls(log)
    assert call["arguments"] == {"name": "x'; DROP TABLE buyers; --", "limit": 5}
    assert verify_log(log.path).ok
