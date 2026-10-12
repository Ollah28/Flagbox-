"""The human gate and filing: reject files nothing, a changed draft is refused, a duplicate name
is refused, and only code ever touches the Filesystem server."""

import asyncio
import hashlib
import re
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any

import pytest
from helpers import CONFIG_PATH
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent
from test_graph import GOOD_SUMMARY, ScriptedLLM, happy_steps, with_summaries

from flagbox.agent import filing
from flagbox.agent.gate import check_name, collect_decision, parse_decision
from flagbox.agent.graph import Deps, build_graph
from flagbox.agent.logged_tools import LoggedSession
from flagbox.agent.run import run as run_command
from flagbox.agent.runlog import RunLog, iter_records, verify_log
from flagbox.mcp_client import FILESYSTEM_SERVER_JS
from flagbox.server import runstate, tools
from flagbox.server.server import build_server

ROOT = Path(__file__).resolve().parent.parent
REAL_FS = (ROOT / FILESYSTEM_SERVER_JS).exists()
needs_node = pytest.mark.skipif(not REAL_FS, reason="run `npm ci` to install the Filesystem server")
SHA = "a" * 64


def decision(sha: str, verdict: str = "approve", name: str = "Wanjiru Kamau", note: str = ""):
    return {"approver_name": name, "decision": verdict, "note": note, "memo_sha256": sha}


# ---- the decision itself ----------


@pytest.mark.parametrize("name", ["Wanjiru Kamau", "Li", "A. O. Otieno-Smith", "Zoë Müller"])
def test_ordinary_names_are_accepted(name):
    assert check_name(name) is None


@pytest.mark.parametrize(
    "name", ["", " ", "A", "12345", "x" * 81, "Bad\x00Name", "Line\nBreak", None, 7]
)
def test_unusable_names_are_refused(name):
    assert check_name(name) is not None


def test_parse_decision_accepts_a_good_decision_and_tidies_it():
    parsed, problem = parse_decision(decision(SHA, "APPROVE", "  Wanjiru Kamau ", "  all   good  "))
    assert problem is None and parsed is not None
    assert (parsed.approver_name, parsed.decision, parsed.note) == (
        "Wanjiru Kamau",
        "approve",
        "all good",
    )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "approve",
        {},
        decision(SHA, "maybe"),
        decision(SHA, "approve", name=""),
        decision("not-a-hash"),
        decision("A" * 63),
        decision(SHA, note="n" * 501),
    ],
)
def test_parse_decision_refuses_anything_else(raw):
    parsed, problem = parse_decision(raw)
    assert parsed is None and problem


def scripted(*answers: str) -> Callable[[str], str]:
    queue = list(answers)

    def ask(prompt: str) -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    return ask


def test_the_terminal_flow_returns_a_decision_tied_to_the_shown_hash():
    said: list[str] = []
    out = collect_decision(scripted("Wanjiru Kamau", "a", "looks fine"), said.append, SHA)
    assert out == decision(SHA, "approve", note="looks fine")
    assert any(SHA in line for line in said)  # the person is told which hash they approve


def test_leaving_the_name_empty_means_no_decision():
    assert collect_decision(scripted(""), lambda t: None, SHA) is None


def test_the_terminal_flow_reprompts_and_gives_up_after_three_tries():
    out = collect_decision(scripted("Wanjiru Kamau", "x", "r", ""), lambda t: None, SHA)
    assert out is not None and out["decision"] == "reject"
    assert collect_decision(scripted("W K", "x", "y", "z"), lambda t: None, SHA) is None
    assert collect_decision(scripted("1", "22", "333"), lambda t: None, SHA) is None


# ---- filing: the checks around write_file ----------


class FakeFilesystem:
    """Behaves like the Filesystem server in the ways that matter (write_file overwrites
    silently, as measured in spike S4), with switches to make it misbehave."""

    def __init__(self, root: Path, corrupt: bool = False, fail_listing: bool = False):
        self.root, self.corrupt, self.fail_listing = root, corrupt, fail_listing
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, *a: Any, **k: Any
    ):
        args = arguments or {}
        self.calls.append((name, args))
        if name == "list_directory":
            if self.fail_listing:
                return CallToolResult(content=[TextContent(type="text", text="no")], isError=True)
            lines = [f"[FILE] {p.name}" for p in sorted(self.root.iterdir())]
            return CallToolResult(content=[TextContent(type="text", text="\n".join(lines))])
        if name == "write_file":
            body = "tampered" if self.corrupt else args["content"]
            Path(args["path"]).write_bytes(body.encode("utf-8"))
            return CallToolResult(content=[TextContent(type="text", text="Successfully wrote")])
        raise AssertionError(f"unexpected tool {name}")

    async def list_tools(self, *a: Any, **k: Any):
        return None


def make_draft(tmp_path: Path, text: str = "# memo\nhello\n") -> tuple[Path, str, str]:
    run_id = str(uuid.uuid4())
    drafts = tmp_path / "drafts"
    drafts.mkdir(exist_ok=True)
    path = drafts / f"{run_id}.md"
    path.write_bytes(text.encode("utf-8"))
    return path, run_id, hashlib.sha256(text.encode("utf-8")).hexdigest()


def filing_setup(tmp_path: Path, **fs_options: Any):
    filed = tmp_path / "filed"
    filed.mkdir()
    fake = FakeFilesystem(filed, **fs_options)
    log = RunLog.create(tmp_path / "logs")
    return filed, fake, LoggedSession(fake, log, "filesystem"), log


def do_file(session, path, run_id, sha, filed):
    return asyncio.run(filing.file_memo(session, run_id, path, sha, filed))


def test_an_approved_memo_is_filed_byte_for_byte(tmp_path):
    path, run_id, sha = make_draft(tmp_path, "# memo\nünïcode and a pipe | ok\n")
    filed, fake, session, log = filing_setup(tmp_path)
    result = do_file(session, path, run_id, sha, filed)

    assert result.ok and result.sha256 == sha and result.path == str(filed / f"{run_id}.md")
    assert (filed / f"{run_id}.md").read_bytes() == path.read_bytes()
    assert [name for name, _ in fake.calls] == ["list_directory", "write_file"]  # check, then write
    assert [r["server"] for r in iter_records(log.path) if r["event"] == "tool_call"] == [
        "filesystem",
        "filesystem",
    ]


def test_a_changed_draft_is_refused_and_nothing_is_written(tmp_path):
    path, run_id, sha = make_draft(tmp_path)
    filed, fake, session, _ = filing_setup(tmp_path)
    path.write_bytes(b"# memo\nsomeone edited this after approval\n")

    result = do_file(session, path, run_id, sha, filed)
    assert result.status == filing.DRAFT_CHANGED and not result.ok
    assert list(filed.iterdir()) == [] and fake.calls == []  # the server was not even asked


def test_a_duplicate_filename_is_refused_and_the_existing_file_is_untouched(tmp_path):
    path, run_id, sha = make_draft(tmp_path)
    filed, fake, session, _ = filing_setup(tmp_path)
    (filed / f"{run_id}.md").write_bytes(b"ORIGINAL")

    result = do_file(session, path, run_id, sha, filed)
    assert result.status == filing.DUPLICATE
    assert (filed / f"{run_id}.md").read_bytes() == b"ORIGINAL"
    assert [name for name, _ in fake.calls] == ["list_directory"]  # write_file was never called


def test_the_fake_really_overwrites_so_the_duplicate_check_is_what_protects_us(tmp_path):
    """Why the list_directory check exists: write_file alone would replace the file."""
    path, run_id, _ = make_draft(tmp_path)
    filed, fake, _, _ = filing_setup(tmp_path)
    (filed / f"{run_id}.md").write_bytes(b"ORIGINAL")
    asyncio.run(
        fake.call_tool("write_file", {"path": str(filed / f"{run_id}.md"), "content": "NEW"})
    )
    assert (filed / f"{run_id}.md").read_bytes() == b"NEW"


def test_a_failed_listing_stops_before_writing(tmp_path):
    path, run_id, sha = make_draft(tmp_path)
    filed, fake, session, _ = filing_setup(tmp_path, fail_listing=True)
    result = do_file(session, path, run_id, sha, filed)
    assert result.status == filing.LISTING_FAILED and list(filed.iterdir()) == []


def test_a_file_that_does_not_match_after_writing_is_flagged_loudly(tmp_path):
    path, run_id, sha = make_draft(tmp_path)
    filed, _, session, _ = filing_setup(tmp_path, corrupt=True)
    result = do_file(session, path, run_id, sha, filed)
    assert result.status == filing.WRITTEN_DIFFERENTLY and result.path is not None
    assert "Do not rely on it" in result.reason


def test_the_draft_must_be_named_for_the_run_and_the_run_id_must_be_a_uuid(tmp_path):
    path, run_id, sha = make_draft(tmp_path)
    filed, _, session, _ = filing_setup(tmp_path)
    other = path.with_name("other.md")
    other.write_bytes(path.read_bytes())
    assert do_file(session, other, run_id, sha, filed).status == filing.BAD_DRAFT
    with pytest.raises(ValueError):
        do_file(session, path, "../../outside", sha, filed)  # no path tricks through the run id


def test_listed_names_reads_files_and_folders():
    assert filing.listed_names("[FILE] a.md\n[DIR] sub\nnoise") == {"a.md", "sub"}


# ---- the graph: pause, decide, file ----------


def fs_factory(log: RunLog, root: Path, calls: list[str], **options: Any):
    @asynccontextmanager
    async def open_filesystem():
        calls.append("opened")
        yield LoggedSession(FakeFilesystem(root, **options), log, "filesystem")

    return open_filesystem


async def drive(
    data_dir: Path,
    tmp_path: Path,
    llm: ScriptedLLM,
    verdict: Callable[[dict[str, Any]], Any] | None,
    before_resume: Callable[[dict[str, Any], RunLog, Path], None] | None = None,
    filesystem_options: dict[str, Any] | None = None,
    no_filesystem: bool = False,
):
    logs_dir, drafts_dir, filed = tmp_path / "logs", tmp_path / "drafts", tmp_path / "filed"
    filed.mkdir(exist_ok=True)
    log = RunLog.create(
        logs_dir, buyer_query="Buyer One", date_from="2024-01-01", date_to="2024-12-31"
    )
    ctx = tools.RunContext(log.run_id, logs_dir, data_dir, drafts_dir)
    opened: list[str] = []
    lines: list[str] = []
    async with create_connected_server_and_client_session(
        build_server(data_dir, CONFIG_PATH, ctx)
    ) as client:
        session = LoggedSession(client, log, "flagbox")
        deps = Deps(
            session=session,
            log=log,
            llm=llm,
            base_tools=await load_mcp_tools(session),  # type: ignore[arg-type]
            data_dir=data_dir,
            say=lines.append,
            filesystem=None
            if no_filesystem
            else fs_factory(log, filed, opened, **(filesystem_options or {})),
            filed_dir=filed,
            os_user=lambda: "test-os-user",
        )
        graph = build_graph(deps, InMemorySaver())
        config: Any = {"configurable": {"thread_id": log.run_id}}
        first = await graph.ainvoke(
            {
                "run_id": log.run_id,
                "buyer_key": "b1",
                "buyer_name": "Buyer One",
                "date_from": "2024-01-01",
                "date_to": "2024-12-31",
            },
            config,
        )
        assert first.get("__interrupt__"), "the run should pause at the gate"
        before = (llm.turns, llm.plan_calls, deps.tool_calls)
        if before_resume:
            before_resume(first, log, tmp_path)
        final = first
        if verdict is not None:
            final = await graph.ainvoke(Command(resume=verdict(first)), config)
        after = (llm.turns, llm.plan_calls, deps.tool_calls)
    return {**first, **final}, deps, log, filed, opened, before, after


def go(data_dir, tmp_path, verdict, **kwargs):
    llm = ScriptedLLM(["repeat_winner"], with_summaries(GOOD_SUMMARY))
    result = asyncio.run(drive(data_dir, tmp_path, llm, verdict, **kwargs))
    return (*result, llm)


def approve(state):
    return decision(state["memo"]["sha256"], "approve", note="reviewed")


def approval_row(data_dir: Path, run_id: str) -> sqlite3.Row | None:
    with closing(runstate.connect(data_dir)) as conn:
        return runstate.get_approval(conn, run_id)


def test_the_run_pauses_at_the_gate_with_the_memo_and_files_nothing(data_dir, tmp_path):
    state, deps, log, filed, opened, *_ = go(data_dir, tmp_path, None)
    assert state["__interrupt__"][0].value["memo"]["sha256"] == state["memo"]["sha256"]
    assert list(filed.iterdir()) == [] and opened == []
    assert approval_row(data_dir, log.run_id) is None


def test_approval_files_the_exact_bytes_and_records_who_approved(data_dir, tmp_path):
    state, deps, log, filed, opened, *_ = go(data_dir, tmp_path, approve)

    target = filed / f"{log.run_id}.md"
    assert state["outcome"] == "filed" and state["filing"]["path"] == str(target)
    assert target.read_bytes() == Path(state["memo"]["path"]).read_bytes()
    assert hashlib.sha256(target.read_bytes()).hexdigest() == state["memo"]["sha256"]

    row = approval_row(data_dir, log.run_id)
    assert row is not None
    assert (row["approver_name"], row["os_user"], row["decision"], row["note"]) == (
        "Wanjiru Kamau",
        "test-os-user",
        "approve",
        "reviewed",
    )
    assert row["memo_sha256"] == state["memo"]["sha256"] and row["filing_status"] == "filed"
    assert row["filed_path"] == str(target)

    events = {r["event"]: r for r in iter_records(log.path)}
    assert events["approval"]["memo_sha256"] == state["memo"]["sha256"]
    assert events["approval"]["log_head_hash"] == row["log_head_hash"]  # what the approver saw
    assert events["filing"]["status"] == "filed"
    fs_calls = [r["tool"] for r in iter_records(log.path) if r.get("server") == "filesystem"]
    assert fs_calls == ["list_directory", "write_file"]
    assert verify_log(log.path).ok


def test_rejection_files_nothing_and_never_starts_the_filesystem_server(data_dir, tmp_path):
    reject = lambda s: decision(s["memo"]["sha256"], "reject", note="not convincing")  # noqa: E731
    state, deps, log, filed, opened, *_ = go(data_dir, tmp_path, reject)

    assert state["outcome"] == "rejected"
    assert list(filed.iterdir()) == [] and opened == []
    row = approval_row(data_dir, log.run_id)
    assert row is not None and (row["decision"], row["note"]) == ("reject", "not convincing")
    assert row["filing_status"] is None
    assert [r["event"] for r in iter_records(log.path) if r["event"] in ("approval", "filing")] == [
        "approval"
    ]


def test_a_draft_edited_after_the_prompt_is_refused(data_dir, tmp_path):
    def tamper(state, log, base):
        path = Path(state["memo"]["path"])
        path.write_bytes(path.read_bytes() + b"\nAn extra sentence added after approval.\n")

    state, deps, log, filed, opened, *_ = go(data_dir, tmp_path, approve, before_resume=tamper)
    assert state["outcome"] == "filing_draft_changed" and list(filed.iterdir()) == []
    assert not [r for r in iter_records(log.path) if r.get("server") == "filesystem"]
    row = approval_row(data_dir, log.run_id)
    assert row is not None and row["filing_status"] == "draft_changed" and row["filed_path"] is None


def test_an_approval_of_a_different_hash_is_not_accepted(data_dir, tmp_path):
    wrong = lambda s: decision(SHA, "approve")  # noqa: E731
    state, deps, log, filed, opened, *_ = go(data_dir, tmp_path, wrong)
    assert state["outcome"] == "decision_invalid" and list(filed.iterdir()) == []
    assert approval_row(data_dir, log.run_id) is None  # nothing recorded as an approval
    assert any(r["event"] == "approval_refused" for r in iter_records(log.path))


@pytest.mark.parametrize(
    "bad",
    [
        lambda s: {"unexpected": "shape"},
        lambda s: "approve",
        lambda s: decision(s["memo"]["sha256"], "maybe"),
        lambda s: decision(s["memo"]["sha256"], "approve", name=""),
    ],
)
def test_malformed_decisions_file_nothing(data_dir, tmp_path, bad):
    state, deps, log, filed, opened, *_ = go(data_dir, tmp_path, bad)
    assert state["outcome"] == "decision_invalid" and list(filed.iterdir()) == []
    assert opened == [] and approval_row(data_dir, log.run_id) is None


def test_a_run_that_already_has_a_decision_cannot_get_another(data_dir, tmp_path):
    def earlier(state, log, base):
        with closing(runstate.connect(data_dir)) as conn:
            runstate.record_approval(conn, log.run_id, "Someone Else", "u", "reject", "", SHA, SHA)

    state, deps, log, filed, opened, *_ = go(data_dir, tmp_path, approve, before_resume=earlier)
    assert state["outcome"] == "already_decided" and list(filed.iterdir()) == []
    row = approval_row(data_dir, log.run_id)
    assert row is not None and row["approver_name"] == "Someone Else"  # the first decision stands


def test_a_duplicate_in_the_filing_folder_is_refused_through_the_graph(data_dir, tmp_path):
    def occupy(state, log, base):
        (base / "filed").mkdir(exist_ok=True)
        (base / "filed" / f"{log.run_id}.md").write_bytes(b"ORIGINAL")

    state, deps, log, filed, opened, *_ = go(data_dir, tmp_path, approve, before_resume=occupy)
    assert state["outcome"] == "filing_duplicate_filename"
    assert (filed / f"{log.run_id}.md").read_bytes() == b"ORIGINAL"


def test_a_tampered_log_blocks_filing(data_dir, tmp_path):
    def edit_log(state, log, base):
        log.path.write_text(
            log.path.read_text(encoding="utf-8").replace("repeat_winner", "something_else", 1),
            encoding="utf-8",
        )

    state, deps, log, filed, opened, *_ = go(data_dir, tmp_path, approve, before_resume=edit_log)
    assert state["outcome"] == "filing_log_unverifiable"
    assert list(filed.iterdir()) == [] and opened == []


def test_a_filesystem_server_that_cannot_start_is_reported_not_crashed(data_dir, tmp_path):
    state, deps, log, filed, opened, *_ = go(data_dir, tmp_path, approve, no_filesystem=True)
    assert state["outcome"] == "filing_filesystem_unavailable" and list(filed.iterdir()) == []


def test_a_bad_write_is_flagged_and_recorded(data_dir, tmp_path):
    state, deps, log, filed, opened, *_ = go(
        data_dir, tmp_path, approve, filesystem_options={"corrupt": True}
    )
    assert state["outcome"] == "filing_written_differently"
    row = approval_row(data_dir, log.run_id)
    assert row is not None and row["filing_status"] == "written_differently"


def test_resuming_runs_no_earlier_work_again(data_dir, tmp_path):
    state, deps, log, filed, opened, before, after, llm = go(data_dir, tmp_path, approve)
    assert before == after  # no model turn, plan call or server tool call was repeated
    names = [r["tool"] for r in iter_records(log.path) if r["event"] == "tool_call"]
    assert names.count("repeat_winner") == 1 and names.count("data_coverage") == 1


def test_the_model_is_never_offered_a_filesystem_tool(data_dir, tmp_path):
    *_, llm = go(data_dir, tmp_path, approve)
    offered = {name for menu in llm.menus for name in menu}
    assert offered <= {"repeat_winner", "record_finding"}
    assert not offered & {"write_file", "list_directory", "edit_file", "move_file"}


def test_our_server_exposes_no_way_to_write_into_the_filing_folder(data_dir, tmp_path):
    async def names() -> set[str]:
        ctx = tools.RunContext(str(uuid.uuid4()), tmp_path / "logs", data_dir, tmp_path / "drafts")
        async with create_connected_server_and_client_session(
            build_server(data_dir, CONFIG_PATH, ctx)
        ) as client:
            return {t.name for t in (await client.list_tools()).tools}

    exposed = asyncio.run(names())
    assert not exposed & {"write_file", "edit_file", "move_file", "list_directory"}


# ---- the whole command, with the real Filesystem server ----------


def run_command_with(data_dir: Path, tmp_path: Path, answers: list[str], llm: ScriptedLLM):
    lines: list[str] = []
    code = asyncio.run(
        run_command(
            "Buyer One",
            "2024-01-01",
            "2024-12-31",
            data_dir,
            tmp_path / "logs",
            CONFIG_PATH,
            say=lines.append,
            ask=scripted(*answers),
            drafts_dir=tmp_path / "drafts",
            filed_dir=tmp_path / "filed",
            check_model=lambda settings: None,
            llm_factory=lambda settings: llm,
        )
    )
    run_id = re.search(r"Run ([0-9a-f-]{36})", lines[0])
    assert run_id is not None
    return code, run_id.group(1), "\n".join(lines)


@needs_node
def test_approving_at_the_terminal_files_through_the_real_filesystem_server(data_dir, tmp_path):
    llm = ScriptedLLM(["repeat_winner"], with_summaries(GOOD_SUMMARY))
    code, run_id, output = run_command_with(data_dir, tmp_path, ["Wanjiru Kamau", "a", "ok"], llm)

    filed = tmp_path / "filed" / f"{run_id}.md"
    draft = tmp_path / "drafts" / f"{run_id}.md"
    assert code == 0 and filed.exists() and filed.read_bytes() == draft.read_bytes()
    assert "FILED:" in output and "Approved by Wanjiru Kamau" in output
    assert "MEMO FOR REVIEW (nothing has been approved or filed yet)" in output
    assert hashlib.sha256(filed.read_bytes()).hexdigest() in output

    log_path = tmp_path / "logs" / f"{run_id}.jsonl"
    result = verify_log(log_path)
    assert result.ok and result.complete
    servers = {r["server"] for r in iter_records(log_path) if r["event"] == "tool_call"}
    assert servers == {"flagbox", "filesystem"}
    last = [r for r in iter_records(log_path)][-1]
    assert last["event"] == "run_end" and last["status"] == "filed"
    assert (data_dir / "checkpoints.sqlite").exists()
    row = approval_row(data_dir, run_id)
    assert row is not None and row["filing_status"] == "filed"


@needs_node
def test_rejecting_at_the_terminal_leaves_the_filing_folder_empty(data_dir, tmp_path):
    llm = ScriptedLLM(["repeat_winner"], with_summaries(GOOD_SUMMARY))
    code, run_id, output = run_command_with(data_dir, tmp_path, ["Wanjiru Kamau", "r", "no"], llm)
    filed = tmp_path / "filed"
    assert code == 0 and (not filed.exists() or list(filed.iterdir()) == [])
    assert "REJECTED by Wanjiru Kamau" in output and "Nothing was filed" in output
    log_path = tmp_path / "logs" / f"{run_id}.jsonl"
    assert verify_log(log_path).ok
    assert not [r for r in iter_records(log_path) if r.get("server") == "filesystem"]


def test_deciding_later_files_nothing_and_ends_cleanly(data_dir, tmp_path):
    llm = ScriptedLLM(["repeat_winner"], with_summaries(GOOD_SUMMARY))
    code, run_id, output = run_command_with(data_dir, tmp_path, [""], llm)
    assert code == 0 and "No decision was recorded" in output
    assert not (tmp_path / "filed").exists() or list((tmp_path / "filed").iterdir()) == []
    assert approval_row(data_dir, run_id) is None
    log_path = tmp_path / "logs" / f"{run_id}.jsonl"
    assert (
        verify_log(log_path).ok
        and [r for r in iter_records(log_path)][-1]["status"] == "not_decided"
    )


def test_a_run_with_nothing_to_review_still_reaches_the_gate_and_can_be_rejected(
    data_dir, tmp_path
):
    llm = ScriptedLLM(
        ["repeat_winner"], [lambda m: __import__("test_graph").call("repeat_winner", {})]
    )
    lines: list[str] = []
    code = asyncio.run(
        run_command(
            "Buyer Two",
            "2024-01-01",
            "2024-12-31",
            data_dir,
            tmp_path / "logs",
            CONFIG_PATH,
            say=lines.append,
            ask=scripted("Wanjiru Kamau", "r", ""),
            drafts_dir=tmp_path / "drafts",
            filed_dir=tmp_path / "filed",
            check_model=lambda settings: None,
            llm_factory=lambda settings: llm,
        )
    )
    output = "\n".join(lines)
    assert code == 0 and "No finding was recorded." in output and "REJECTED" in output


def test_a_missing_model_is_reported_before_anything_starts(data_dir, tmp_path):
    from flagbox.agent.model import ModelUnavailable

    def down(settings):
        raise ModelUnavailable("Ollama not running at http://x. Start it.")

    lines: list[str] = []
    code = asyncio.run(
        run_command(
            "Buyer One", "2024-01-01", "2024-12-31", data_dir, tmp_path / "logs", CONFIG_PATH,
            say=lines.append, ask=scripted(), check_model=down,
        )
    )  # fmt: skip
    assert code == 2 and "Ollama not running" in lines[0] and not (tmp_path / "logs").exists()


def test_happy_steps_exist_for_reuse():
    assert len(happy_steps()) == 3


def test_an_injected_tool_call_id_block_cannot_hide_a_duplicate_name():
    """Regression: LoggedSession adds a text block to plain results; it must not be glued onto
    the last filename in a listing (that once let write_file overwrite an existing file)."""
    listing = CallToolResult(
        content=[
            TextContent(type="text", text="[FILE] taken.md"),
            TextContent(type="text", text='{"tool_call_id": "tc-0007"}'),
        ]
    )
    assert filing.listed_names(filing._text(listing)) == {"taken.md"}


@needs_node
def test_the_default_relative_folders_work_with_the_real_filesystem_server(
    data_dir, tmp_path, monkeypatch
):
    """Regression: the real server resolved a relative path against its own root (filed/filed),
    so filing failed with the default `--filed-dir filed`. Earlier tests all used absolute paths.
    Also runs from another folder, so the Node server must be found from the project root."""
    monkeypatch.chdir(tmp_path)
    llm = ScriptedLLM(["repeat_winner"], with_summaries(GOOD_SUMMARY))
    lines: list[str] = []
    code = asyncio.run(
        run_command(
            "Buyer One",
            "2024-01-01",
            "2024-12-31",
            data_dir,
            tmp_path / "logs",
            CONFIG_PATH,
            say=lines.append,
            ask=scripted("Wanjiru Kamau", "a", ""),
            drafts_dir=Path("drafts"),
            filed_dir=Path("filed"),
            check_model=lambda settings: None,
            llm_factory=lambda settings: llm,
        )
    )
    output = "\n".join(lines)
    filed = list((tmp_path / "filed").glob("*.md"))
    assert code == 0 and len(filed) == 1 and "FILED:" in output
    assert "Secure MCP Filesystem Server" not in output
