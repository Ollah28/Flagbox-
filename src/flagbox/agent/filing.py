"""Filing an approved memo (CLAUDE.md hard rule 4, SPEC decisions D16 and D33).

Only code calls the Filesystem server's `write_file`, and only after an approval. Before writing,
this module re-hashes the draft on disk and refuses if it is not the exact memo that was
approved, then asks the server's `list_directory` whether the filename is taken, because
`write_file` would silently overwrite (measured in spike S4). After writing it reads the file
back and checks the hash again.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.types import TextContent

from flagbox.agent.logged_tools import LoggedSession
from flagbox.agent.runlog import parse_run_id

FILED = "filed"
DRAFT_CHANGED = "draft_changed"
DUPLICATE = "duplicate_filename"
LISTING_FAILED = "listing_failed"
WRITE_FAILED = "write_failed"
WRITTEN_DIFFERENTLY = "written_differently"
NO_FILESYSTEM = "filesystem_unavailable"
LOG_UNVERIFIABLE = "log_unverifiable"
BAD_DRAFT = "bad_draft"


@dataclass(frozen=True)
class FilingResult:
    status: str  # FILED or one of the refusal codes above
    reason: str = ""
    path: str | None = None
    sha256: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == FILED


def _text(result: Any) -> str:
    """The server's own text: its first text block.

    LoggedSession appends a further block carrying the tool_call_id to results that are not
    structured. Joining all blocks would glue that onto the last filename in a listing and make
    the duplicate check miss it (found by a test), so only the first block is used."""
    for block in result.content:
        if isinstance(block, TextContent):
            return block.text.strip()
    return ""


def listed_names(listing: str) -> set[str]:
    """Names from the server's list_directory text, lines like '[FILE] name.md' or '[DIR] x'."""
    names: set[str] = set()
    for line in listing.splitlines():
        line = line.strip()
        if line.startswith(("[FILE] ", "[DIR] ")):
            names.add(line.split(" ", 1)[1].strip())
    return names


async def file_memo(
    fs: LoggedSession,
    run_id: str,
    draft_path: Path,
    approved_sha256: str,
    filed_dir: Path,
) -> FilingResult:
    """Write the approved memo to filed/<run_id>.md, or refuse and say why. Never raises for a
    refusal; a refusal leaves filed/ exactly as it was."""
    run_id = parse_run_id(run_id)
    if draft_path.name != f"{run_id}.md":
        return FilingResult(BAD_DRAFT, "The draft file is not named for this run.")
    try:
        data = draft_path.read_bytes()  # read once: these exact bytes are hashed and written
    except OSError:
        return FilingResult(DRAFT_CHANGED, "The draft file can no longer be read.")
    actual = hashlib.sha256(data).hexdigest()
    if actual != approved_sha256:
        return FilingResult(
            DRAFT_CHANGED,
            "The draft on disk is not the memo that was approved (its hash is different).",
            sha256=actual,
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return FilingResult(BAD_DRAFT, "The draft is not valid UTF-8.")

    filed_dir = filed_dir.resolve()  # the server resolves relative paths against its own root
    target = filed_dir / f"{run_id}.md"
    listing = await fs.call_tool("list_directory", {"path": str(filed_dir)})
    if listing.isError:
        return FilingResult(LISTING_FAILED, _text(listing)[:200] or "list_directory failed.")
    if target.name in listed_names(_text(listing)):
        return FilingResult(
            DUPLICATE, f"{target.name} already exists in the filing folder; nothing was written."
        )

    written = await fs.call_tool("write_file", {"path": str(target), "content": text})
    if written.isError:
        return FilingResult(WRITE_FAILED, _text(written)[:200] or "write_file failed.")

    try:
        on_disk = hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return FilingResult(WRITTEN_DIFFERENTLY, "The filed file could not be read back.")
    if on_disk != approved_sha256:
        return FilingResult(
            WRITTEN_DIFFERENTLY,
            "The filed file does not match the approved memo. Do not rely on it.",
            path=str(target),
            sha256=on_disk,
        )
    return FilingResult(FILED, path=str(target), sha256=on_disk)
