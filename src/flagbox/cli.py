"""Command-line entry point for Flagbox.

Each later milestone adds a subcommand here (setup-data, run, demo, ...).
Kept on the standard library's argparse so there is nothing extra to learn.
"""

import argparse

from flagbox import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flagbox",
        description="Sourced red flags for public tenders. The committee decides.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version", help="Print the Flagbox version.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        print(f"flagbox {__version__}")
        return 0
    return 1  # unreachable: argparse rejects unknown commands


if __name__ == "__main__":
    raise SystemExit(main())
