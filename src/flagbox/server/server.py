"""The Flagbox MCP server (stdio). Run with: python -m flagbox.server.server

Tools registered here: find_buyer, data_coverage, fetch_buyer_awards, and one tool per
indicator found in flagbox/server/indicators/. All are read-only.
"""

import functools
import os
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from flagbox.agent.runlog import parse_run_id
from flagbox.data.ingest import DB_NAME
from flagbox.server import db, tools
from flagbox.server.config import Config, load_config
from flagbox.server.indicators import Indicator, discover


def _structured_errors(func: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Turn a bad argument into {"error": code, "message": ...} the model can read and correct."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return func(*args, **kwargs)
        except db.ToolInputError as error:
            return {"error": error.code, "message": str(error)}

    return wrapper


def build_server(data_dir: Path, config_path: Path, run: tools.RunContext | None = None) -> FastMCP:
    cfg: Config = load_config(config_path)
    indicators: dict[str, Indicator] = discover()

    missing = sorted(set(indicators) - set(cfg.indicators))
    unknown = sorted(set(cfg.indicators) - set(indicators))
    if missing or unknown:
        raise ValueError(
            f"config/indicators.yaml and flagbox/server/indicators disagree "
            f"(no config for: {missing or 'none'}; no indicator for: {unknown or 'none'})"
        )
    params = {
        name: ind.params_model.model_validate(cfg.indicators[name])
        for name, ind in indicators.items()
    }
    db_path = data_dir / DB_NAME

    mcp = FastMCP("flagbox", log_level="WARNING")

    @mcp.tool()
    @_structured_errors
    def find_buyer(name: str, limit: int = 5) -> dict[str, Any]:
        """Look up a buyer (procuring entity) by part of its name. Returns candidates with their
        buyer_key, number of processes and awards, and the span of closing dates."""
        with closing(db.connect_readonly(db_path)) as conn:
            return tools.find_buyer(conn, cfg, name, limit)

    @mcp.tool()
    @_structured_errors
    def data_coverage(buyer_key: str, date_from: str, date_to: str) -> dict[str, Any]:
        """Report how complete the data is for a buyer and date range (YYYY-MM-DD), and which
        indicators can run on it."""
        with closing(db.connect_readonly(db_path)) as conn:
            return tools.data_coverage(conn, cfg, indicators, buyer_key, date_from, date_to)

    @mcp.tool()
    @_structured_errors
    def fetch_buyer_awards(
        buyer_key: str, date_from: str, date_to: str, page: int = 1, page_size: int = 25
    ) -> dict[str, Any]:
        """List a buyer's awards in a date range, one page at a time (page_size at most 50)."""
        with closing(db.connect_readonly(db_path)) as conn:
            return tools.fetch_buyer_awards(
                conn, cfg, buyer_key, date_from, date_to, page, page_size
            )

    @_structured_errors
    def record_finding(hit_id: str, severity: str, reason_code: str) -> dict[str, Any]:
        return tools.record_finding(run, indicators, hit_id, severity, reason_code)

    @mcp.tool()
    @_structured_errors
    def draft_memo(run_id: str, summary: str | None = None) -> dict[str, Any]:
        """Draft the screening memo for the current run from its stored findings and write it to
        the drafts folder. Returns the file path and its SHA-256. Optionally pass a summary
        paragraph; it is checked against the findings (no uncited supplier or OCID, no numbers
        that are not in a finding, no accusing words) and, if it fails, no file is written."""
        return tools.draft_memo(run, run_id, summary)

    menu = "\n".join(f"- {r.code}: {r.text}" for ind in indicators.values() for r in ind.reasons)
    mcp.add_tool(
        record_finding,
        name="record_finding",
        description=(
            "Record a finding for one hit you have decided to keep. Give the hit_id exactly as a "
            "tool returned it, a severity (low, medium or high), and a reason_code chosen from "
            "the reason_codes listed on that hit. You write no explanation: the system supplies "
            "the fixed wording for the code, and copies the facts from the hit. If a finding is "
            "refused you are told why and may try again. Reason codes:\n" + menu
        ),
    )

    for indicator in indicators.values():
        mcp.add_tool(
            _indicator_tool(indicator, params[indicator.name], cfg, db_path),
            name=indicator.name,
            description=indicator.description,
        )
    return mcp


def _indicator_tool(
    indicator: Indicator, params: Any, cfg: Config, db_path: Path
) -> Callable[..., dict[str, Any]]:
    """One tool per indicator. Its only arguments are the buyer and the range: no thresholds."""

    @_structured_errors
    def tool(buyer_key: str, date_from: str, date_to: str) -> dict[str, Any]:
        with closing(db.connect_readonly(db_path)) as conn:
            return tools.run_indicator(conn, cfg, indicator, params, buyer_key, date_from, date_to)

    return tool


def main() -> int:
    data_dir = Path(os.environ.get("FLAGBOX_DATA_DIR", "data")).resolve()
    config_path = Path(os.environ.get("FLAGBOX_CONFIG", "config/indicators.yaml")).resolve()
    if not (data_dir / DB_NAME).exists():
        print(f"No snapshot at {data_dir / DB_NAME}. Run `flagbox ingest` first.", file=sys.stderr)
        return 2
    run_id = os.environ.get("FLAGBOX_RUN_ID")
    run = (
        tools.RunContext(
            run_id=parse_run_id(run_id),
            logs_dir=Path(os.environ.get("FLAGBOX_LOGS_DIR", "logs")).resolve(),
            drafts_dir=Path(os.environ.get("FLAGBOX_DRAFTS_DIR", "drafts")).resolve(),
            data_dir=data_dir,
        )
        if run_id
        else None
    )
    # stdio: stdout carries the protocol, so nothing else may print to it
    build_server(data_dir, config_path, run).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
