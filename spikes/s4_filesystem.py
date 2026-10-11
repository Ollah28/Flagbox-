"""M0.3 spike S4: does the pinned Filesystem MCP server run on Windows, and is it safe?

Starts the server with `node ...dist/index.js ./filed` (never npx), lists its tools, writes a
file inside filed/, writes it a second time to see whether write_file overwrites, then tries
to write outside filed/ with a `../` path and an absolute path. No model is involved.

Run from the repo root:  uv run python spikes/s4_filesystem.py
"""

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent

ROOT = Path(__file__).resolve().parent.parent
FILED = ROOT / "filed"
PACKAGE = ROOT / "node_modules" / "@modelcontextprotocol" / "server-filesystem"
SERVER_JS = PACKAGE / "dist" / "index.js"
PROBE = "s4_probe.txt"
ESCAPE_NAME = "s4_escape.txt"


def text_of(result: CallToolResult) -> str:
    return " ".join(c.text for c in result.content if isinstance(c, TextContent)).strip()


async def try_write(session: ClientSession, path: str, content: str) -> tuple[bool, str]:
    """Return (accepted, message). A refusal may come back as isError or as an exception."""
    try:
        result = await session.call_tool("write_file", {"path": path, "content": content})
    except Exception as exc:  # the server may reject at protocol level
        return False, f"exception: {exc}"
    return (not result.isError), text_of(result)


async def main() -> int:
    if not SERVER_JS.exists():
        print(f"Server not found at {SERVER_JS}. Run `npm ci` first.")
        return 2
    FILED.mkdir(exist_ok=True)
    params = StdioServerParameters(command="node", args=[str(SERVER_JS), "./filed"], cwd=str(ROOT))

    outside_relative = ROOT / ESCAPE_NAME  # what "../s4_escape.txt" resolves to from filed/
    problems: list[str] = []

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        tools = (await session.list_tools()).tools
        print(f"Tools ({len(tools)}): {', '.join(t.name for t in tools)}")

        allowed = await session.call_tool("list_allowed_directories", {})
        print(f"Allowed directories: {text_of(allowed)}")

        ok, msg = await try_write(session, f"{FILED}/{PROBE}", "first write\n")
        print(f"1. write inside filed/        -> accepted={ok}  {msg[:100]}")
        if not ok or (FILED / PROBE).read_text() != "first write\n":
            problems.append("write inside filed/ did not work")

        ok2, msg2 = await try_write(session, f"{FILED}/{PROBE}", "second write\n")
        overwrote = (FILED / PROBE).read_text() == "second write\n"
        print(f"2. write same file again      -> accepted={ok2}  overwrote={overwrote}")

        ok3, msg3 = await try_write(session, f"{FILED}/../{ESCAPE_NAME}", "escape\n")
        print(f"3. write via filed/../x       -> accepted={ok3}  {msg3[:100]}")
        if ok3 or outside_relative.exists():
            problems.append("a ../ write was NOT refused")

        ok4, msg4 = await try_write(session, str(outside_relative), "escape\n")
        print(f"4. write absolute outside     -> accepted={ok4}  {msg4[:100]}")
        if ok4 or outside_relative.exists():
            problems.append("an absolute write outside filed/ was NOT refused")

        listing = await session.call_tool("list_directory", {"path": str(FILED)})
        print(f"5. list_directory(filed)      -> {text_of(listing)!r}")

    for leftover in (FILED / PROBE, outside_relative):
        leftover.unlink(missing_ok=True)

    print()
    if problems:
        print("S4 FAILED:", "; ".join(problems))
        return 1
    print(f"S4 PASSED. write_file overwrites an existing file: {overwrote}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
