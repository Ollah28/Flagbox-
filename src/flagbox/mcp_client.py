"""How Flagbox starts and talks to its own MCP server (stdio).

The server is launched as a child process with the same Python that runs Flagbox, so there is
no `uv` or `npx` in the middle. Paths are made absolute so the server finds the same data and
config no matter where it is started from.
"""

import sys
from pathlib import Path

from mcp import StdioServerParameters
from mcp.types import Tool

DEFAULT_DATA_DIR = Path("data")
DEFAULT_CONFIG = Path("config/indicators.yaml")
DEFAULT_LOGS_DIR = Path("logs")


def server_parameters(
    data_dir: Path = DEFAULT_DATA_DIR,
    config_path: Path = DEFAULT_CONFIG,
    run_id: str | None = None,
    logs_dir: Path = DEFAULT_LOGS_DIR,
) -> StdioServerParameters:
    """With a run_id the server can record findings for that run (it checks them against the
    run's log). Without one, record_finding refuses every call."""
    env = {
        "FLAGBOX_DATA_DIR": str(data_dir.resolve()),
        "FLAGBOX_CONFIG": str(config_path.resolve()),
        "FASTMCP_LOG_LEVEL": "WARNING",  # keep the server's per-request chatter off the terminal
    }
    if run_id:
        env["FLAGBOX_RUN_ID"] = run_id
        env["FLAGBOX_LOGS_DIR"] = str(logs_dir.resolve())
    return StdioServerParameters(
        command=sys.executable, args=["-m", "flagbox.server.server"], env=env
    )


async def list_server_tools(params: StdioServerParameters) -> list[Tool]:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        return (await session.list_tools()).tools
