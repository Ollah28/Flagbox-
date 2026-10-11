"""Command-line entry point for Flagbox.

Each later milestone adds a subcommand here (run, demo, ...).
Kept on the standard library's argparse so there is nothing extra to learn.
"""

import argparse
import asyncio
from pathlib import Path

from flagbox import __version__
from flagbox.agent.runlog import log_path, verify_log
from flagbox.data.download import setup_data
from flagbox.data.ingest import DB_NAME, ingest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flagbox",
        description="Sourced red flags for public tenders. The committee decides.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version", help="Print the Flagbox version.")
    setup = commands.add_parser(
        "setup-data", help="Download the Kenya PPRA OCDS files (2023-2025) into data/raw/."
    )
    setup.add_argument("--data-dir", type=Path, default=Path("data"), help="Default: ./data")
    load = commands.add_parser(
        "ingest", help="Build the redacted, live-tender-filtered SQLite snapshot from data/raw/."
    )
    load.add_argument("--data-dir", type=Path, default=Path("data"), help="Default: ./data")
    listing = commands.add_parser(
        "tools", help="List the tools our MCP server offers, using a real MCP client."
    )
    listing.add_argument("--data-dir", type=Path, default=Path("data"), help="Default: ./data")
    runner = commands.add_parser(
        "run", help="Screen one buyer for patterns that warrant review (needs Ollama)."
    )
    runner.add_argument(
        "--buyer", required=True, help='Part of a buyer name, e.g. "Kenya Literature Bureau"'
    )
    runner.add_argument(
        "--from", dest="date_from", required=True, help="First closing date, YYYY-MM-DD"
    )
    runner.add_argument("--to", dest="date_to", required=True, help="Last closing date, YYYY-MM-DD")
    runner.add_argument("--data-dir", type=Path, default=Path("data"), help="Default: ./data")
    runner.add_argument("--logs-dir", type=Path, default=Path("logs"), help="Default: ./logs")
    runner.add_argument(
        "--config", type=Path, default=Path("config/indicators.yaml"), help="Indicator settings"
    )
    verify = commands.add_parser(
        "verify-log", help="Check that a run's log has not been edited (hash chain)."
    )
    verify.add_argument("run_id", help="The run's UUID.")
    verify.add_argument("--logs-dir", type=Path, default=Path("logs"), help="Default: ./logs")
    return parser


def _run_verify_log(run_id: str, logs_dir: Path) -> int:
    try:
        path = log_path(logs_dir, run_id)
    except ValueError as error:
        print(error)
        return 2
    if not path.exists():
        print(f"No log for run {run_id} in {logs_dir}.")
        return 2
    result = verify_log(path)
    if not result.ok:
        print(f"FAILED  line {result.line}: {result.error}")
        return 1
    state = "complete" if result.complete else "INCOMPLETE (no run_end record)"
    print(f"OK  {result.records} records, {result.tool_calls} tool calls, {state}")
    print(f"Head hash: {result.head_hash}")
    return 0


def _run_setup_data(data_dir: Path) -> int:
    for result in setup_data(data_dir):
        print(
            f"{result.status:<10} {result.name}  {result.line_count:>7} lines  "
            f"sha256 {result.sha256[:16]}..."
        )
    print(f"Manifest: {data_dir / 'MANIFEST.json'}")
    return 0


def _run_ingest(data_dir: Path) -> int:
    try:
        report = ingest(data_dir)
    except (FileNotFoundError, ValueError) as error:
        print(error)
        return 2
    print(f"Live-tender filter date: {report.today} (decision D20)")
    print(f"Read {report.lines_read} processes, kept {report.kept}, excluded:")
    for reason, count in sorted(report.excluded.items()):
        print(f"  {reason:<22} {count:>7}")
    print("Rows written:")
    for table, count in report.rows.items():
        print(f"  {table:<22} {count:>7}")
    if report.suppliers_skipped:
        print(f"Award suppliers skipped (no ID and no name): {report.suppliers_skipped}")
    print(f"Done in {report.seconds:.0f}s -> {data_dir / DB_NAME}")
    return 0


def _run_agent(args: argparse.Namespace) -> int:
    from flagbox.agent.run import run

    try:
        return asyncio.run(
            run(
                args.buyer,
                args.date_from,
                args.date_to,
                args.data_dir,
                args.logs_dir,
                args.config,
            )
        )
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.")
        return 1


def _run_tools(data_dir: Path) -> int:
    from flagbox.mcp_client import list_server_tools, server_parameters

    if not (data_dir / DB_NAME).exists():
        print(f"No snapshot at {data_dir / DB_NAME}. Run `flagbox ingest` first.")
        return 2
    found = asyncio.run(list_server_tools(server_parameters(data_dir)))
    print(f"{len(found)} tools from the Flagbox MCP server:")
    for tool in found:
        arguments = ", ".join(tool.inputSchema.get("properties", {}))
        summary = " ".join((tool.description or "").split()).split(". ")[0].rstrip(".")
        print(f"  {tool.name}({arguments})")
        print(f"      {summary}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        print(f"flagbox {__version__}")
        return 0
    if args.command == "setup-data":
        return _run_setup_data(args.data_dir)
    if args.command == "ingest":
        return _run_ingest(args.data_dir)
    if args.command == "tools":
        return _run_tools(args.data_dir)
    if args.command == "run":
        return _run_agent(args)
    if args.command == "verify-log":
        return _run_verify_log(args.run_id, args.logs_dir)
    return 1  # unreachable: argparse rejects unknown commands


if __name__ == "__main__":
    raise SystemExit(main())
