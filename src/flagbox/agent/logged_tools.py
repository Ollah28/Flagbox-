"""The client-side logging wrapper (CLAUDE.md hard rule 3, SPEC decision D15).

Agent code never holds a raw MCP session. It holds a LoggedSession, whose `call_tool`:
  1. assigns a tool_call_id ("tc-0001", "tc-0002", ...);
  2. calls the real server;
  3. appends one record to the run's hash-chained log: tool, arguments, result (or error),
     duration;
  4. hands back the result with the tool_call_id added, so the model can see which call produced
     what, and later findings can cite it.

It sits on the client side so one wrapper covers both our server and the borrowed Filesystem
server. `server` is just a label in the log ("flagbox", "filesystem").
"""

import json
import time
from typing import Any

from mcp.types import CallToolResult, ContentBlock, TextContent

from flagbox.agent.runlog import RunLog


def result_payload(result: CallToolResult) -> dict[str, Any]:
    """What to store in the log: the structured result if there is one, else the text blocks."""
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    return {
        "text": [block.text for block in result.content if isinstance(block, TextContent)],
    }


def with_tool_call_id(result: CallToolResult, tool_call_id: str) -> CallToolResult:
    """Return a copy of the result that carries the tool_call_id in what the model reads."""
    if isinstance(result.structuredContent, dict):
        structured = {**result.structuredContent, "tool_call_id": tool_call_id}
        content: list[ContentBlock] = [
            TextContent(type="text", text=json.dumps(structured, ensure_ascii=False))
        ]
        return result.model_copy(update={"structuredContent": structured, "content": content})
    note = TextContent(type="text", text=json.dumps({"tool_call_id": tool_call_id}))
    return result.model_copy(update={"content": [*result.content, note]})


class LoggedSession:
    """Wraps an MCP ClientSession (or anything with the same call_tool/list_tools).

    It deliberately offers nothing else (no passthrough of other session methods), so agent code
    has no unlogged route to a tool.
    """

    def __init__(self, session: Any, log: RunLog, server: str) -> None:
        self._session = session
        self._log = log
        self._server = server

    @property
    def log(self) -> RunLog:
        return self._log

    async def list_tools(self, *args: Any, **kwargs: Any) -> Any:
        """Listing tools is not a tool call, so it is passed through unlogged."""
        return await self._session.list_tools(*args, **kwargs)

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, *args: Any, **kwargs: Any
    ) -> CallToolResult:
        tool_call_id = self._log.next_tool_call_id()
        started = time.perf_counter()
        try:
            result: CallToolResult = await self._session.call_tool(name, arguments, *args, **kwargs)
        except Exception as error:
            self._log.append(
                "tool_call",
                tool_call_id=tool_call_id,
                server=self._server,
                tool=name,
                arguments=arguments or {},
                duration_ms=round((time.perf_counter() - started) * 1000),
                is_error=True,
                error=f"{type(error).__name__}: {error}",
            )
            raise
        self._log.append(
            "tool_call",
            tool_call_id=tool_call_id,
            server=self._server,
            tool=name,
            arguments=arguments or {},
            duration_ms=round((time.perf_counter() - started) * 1000),
            is_error=bool(result.isError),
            result=result_payload(result),
        )
        return with_tool_call_id(result, tool_call_id)
