"""`flagbox run`: one screening run, start to finish (SPEC section 2).

Order of events:
  1. check Ollama and the data (exit code 2 if either is missing);
  2. open the run's log and start our MCP server for this run;
  3. find the buyer with `find_buyer` (a logged tool call) and let the officer pick if unclear;
  4. run the graph, printing one line per tool call, until the memo is drafted;
  5. print the memo and its SHA-256, and ask a named person to approve or reject it;
  6. on approval, file exactly those bytes (code, not the model); on rejection, file nothing;
  7. close the log.
"""

import asyncio
import hashlib
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, closing
from datetime import date
from pathlib import Path
from typing import Any

from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from flagbox.agent import prompts
from flagbox.agent.gate import collect_decision
from flagbox.agent.graph import AgentState, Deps, build_graph, progress_line
from flagbox.agent.logged_tools import LoggedSession
from flagbox.agent.model import (
    ModelSettings,
    ModelUnavailable,
    check_ollama,
    make_llm,
    settings_from_env,
)
from flagbox.agent.runlog import RunLog
from flagbox.data.ingest import DB_NAME
from flagbox.data.redact import normalise_name
from flagbox.mcp_client import (
    DEFAULT_DRAFTS_DIR,
    DEFAULT_FILED_DIR,
    filesystem_parameters,
    server_parameters,
)
from flagbox.server import runstate

RUN_HARD_STOP_SECONDS = 30 * 60  # SPEC section 7 (the human's time at the gate is not counted)
FILING_SECONDS = 120
CHECKPOINT_DB = "checkpoints.sqlite"

# Outcomes that are a normal end to a run, as opposed to something that went wrong.
CLEAN_OUTCOMES = {"filed", "rejected", "not_decided"}


def choose_buyer(
    payload: dict[str, Any], query: str, ask: Callable[[str], str], say: Callable[[str], None]
) -> tuple[dict[str, Any] | None, str]:
    """Pick one buyer from find_buyer's result. Returns (candidate or None, how it was chosen)."""
    candidates: list[dict[str, Any]] = payload.get("candidates", [])
    if not candidates:
        return None, "none"
    wanted = normalise_name(query)
    exact = [c for c in candidates if normalise_name(c["name"]) == wanted]
    if len(exact) == 1:
        return exact[0], "exact_name"
    if payload.get("match") == "all_words" and len(candidates) == 1:
        return candidates[0], "single_match"

    say(f"No single clear match for '{query}'. Closest buyers:")
    for number, c in enumerate(candidates, start=1):
        span = f"{c['first_close']} to {c['last_close']}" if c.get("first_close") else "no dates"
        say(f"  {number}. {c['name']}  ({c['processes']} processes, {c['awards']} awards, {span})")
    answer = ask("Pick a number, or press Enter to cancel: ").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(candidates):
        return candidates[int(answer) - 1], "officer_pick"
    return None, "cancelled"


def _show_memo(say: Callable[[str], None], memo: dict[str, Any]) -> bool:
    """Print the memo exactly as it is on disk, with its hash. Returns False if the file's hash
    is not the hash it was drafted with, in which case nobody should be asked to approve it."""
    data = Path(memo["path"]).read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    say("")
    say("=" * 78)
    say("MEMO FOR REVIEW (nothing has been approved or filed yet)")
    say("=" * 78)
    say(data.decode("utf-8").rstrip("\n"))
    say("=" * 78)
    say(f"Draft file: {memo['path']}")
    say(f"SHA-256:    {actual}")
    by_model = memo["summary_source"] == "model"
    say(
        "Summary:    "
        + ("written by the model, checked by rules" if by_model else "fixed template")
    )
    if actual != memo["sha256"]:
        say("The draft on disk is not the file that was drafted (its hash changed).")
        return False
    return True


def _print_outcome(say: Callable[[str], None], state: dict[str, Any]) -> None:
    outcome = state.get("outcome", "")
    approval = state.get("approval") or {}
    filing = state.get("filing") or {}
    say("")
    if outcome == "filed":
        say(f"FILED: {filing['path']}")
        say(f"SHA-256 of the filed file: {filing['sha256']} (matches the approved memo)")
        say(f"Approved by {approval['approver_name']} (user {approval['os_user']}).")
    elif outcome == "rejected":
        say(
            f"REJECTED by {approval['approver_name']} (user {approval['os_user']}). "
            "Nothing was filed."
        )
        if approval.get("note"):
            say(f"Note: {approval['note']}")
    elif outcome == "not_decided":
        say(
            "No decision was recorded. Nothing was filed. "
            "The draft memo is still in the drafts folder."
        )
    elif outcome.startswith("filing_"):
        say(f"NOT FILED: {filing.get('reason') or outcome}")
        say(
            f"Approved by {approval.get('approver_name', '?')}, "
            "but the safety checks refused to file."
        )
    elif outcome in ("decision_invalid", "already_decided"):
        say(f"NOT FILED: the decision was not accepted ({approval.get('problem') or outcome}).")
    elif outcome == "draft_changed":
        say("NOT FILED: the draft changed after it was drafted. Run again.")
    else:
        say("The memo could not be drafted. See the run log for the reason.")


@asynccontextmanager
async def _filesystem(log: RunLog, filed_dir: Path) -> AsyncIterator[LoggedSession]:
    """Start the borrowed Filesystem server for filing only, logged like every other server."""
    with open(os.devnull, "w") as quiet:  # the server prints a banner to stderr on start-up
        async with stdio_client(filesystem_parameters(filed_dir), errlog=quiet) as (read, write):
            async with ClientSession(read, write) as raw:
                await raw.initialize()
                yield LoggedSession(raw, log, server="filesystem")


async def run(
    buyer_query: str,
    date_from: str,
    date_to: str,
    data_dir: Path,
    logs_dir: Path,
    config_path: Path,
    say: Callable[[str], None] = lambda text: print(text, flush=True),
    ask: Callable[[str], str] = input,
    settings: ModelSettings | None = None,
    drafts_dir: Path = DEFAULT_DRAFTS_DIR,
    filed_dir: Path = DEFAULT_FILED_DIR,
    check_model: Callable[[ModelSettings], None] = check_ollama,
    llm_factory: Callable[[ModelSettings], Any] = make_llm,
) -> int:
    settings = settings or settings_from_env()
    drafts_dir, filed_dir = drafts_dir.resolve(), filed_dir.resolve()
    try:
        start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
        if start > end:
            raise ValueError
    except ValueError:
        say("Dates must look like 2023-01-01, and --from must not be after --to.")
        return 2
    if not (data_dir / DB_NAME).exists():
        say(f"No snapshot at {data_dir / DB_NAME}. Run `flagbox setup-data` then `flagbox ingest`.")
        return 2
    try:
        check_model(settings)
    except ModelUnavailable as error:
        say(str(error))
        return 2

    log = RunLog.create(
        logs_dir,
        buyer_query=buyer_query,
        date_from=date_from,
        date_to=date_to,
        model=settings.model,
        num_ctx=settings.num_ctx,
        prompt_version=prompts.PROMPT_VERSION,
    )
    with closing(runstate.connect(data_dir)) as conn:
        runstate.start_run(
            conn,
            log.run_id,
            date_from,
            date_to,
            settings.model,
            settings.num_ctx,
            prompts.PROMPT_VERSION,
        )
    say(f"Run {log.run_id}  model {settings.model}  prompts {prompts.PROMPT_VERSION}")

    status, exit_code = "failed", 2
    deps: Deps | None = None
    params = server_parameters(
        data_dir, config_path, run_id=log.run_id, logs_dir=logs_dir, drafts_dir=drafts_dir
    )
    try:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as raw:
            await raw.initialize()
            session = LoggedSession(raw, log, server="flagbox")

            started = time.monotonic()
            found = await session.call_tool("find_buyer", {"name": buyer_query, "limit": 5})
            payload: dict[str, Any] = found.structuredContent or {}
            temp = Deps(session, log, None, [], data_dir, say)
            say(progress_line(temp, "find_buyer", payload, time.monotonic() - started))
            buyer, how = choose_buyer(payload, buyer_query, ask, say)
            if buyer is None:
                if how == "none":
                    names = (
                        ", ".join(c["name"] for c in payload.get("candidates", [])) or "none found"
                    )
                    say(f"No buyer matches '{buyer_query}'. Closest names: {names}")
                status, exit_code = ("buyer_not_found" if how == "none" else "cancelled"), 1
            else:
                log.append(
                    "buyer_selected", buyer_key=buyer["buyer_key"], name=buyer["name"], how=how
                )
                with closing(runstate.connect(data_dir)) as conn:
                    runstate.set_run_buyer(conn, log.run_id, buyer["buyer_key"], buyer["name"])
                say(f"Buyer: {buyer['name']} (key {buyer['buyer_key']}, chosen by {how})")

                deps = Deps(
                    session=session,
                    log=log,
                    llm=llm_factory(settings),
                    base_tools=await load_mcp_tools(session),  # type: ignore[arg-type]
                    data_dir=data_dir,
                    say=say,
                    filesystem=lambda: _filesystem(log, filed_dir),
                    filed_dir=filed_dir,
                )
                deps.tool_calls = 1  # the find_buyer call above
                initial: AgentState = {
                    "run_id": log.run_id,
                    "buyer_key": buyer["buyer_key"],
                    "buyer_name": buyer["name"],
                    "date_from": date_from,
                    "date_to": date_to,
                }
                config: Any = {"configurable": {"thread_id": log.run_id}}
                async with AsyncSqliteSaver.from_conn_string(
                    str(data_dir / CHECKPOINT_DB)
                ) as saver:
                    graph = build_graph(deps, saver)
                    async with asyncio.timeout(RUN_HARD_STOP_SECONDS):
                        result = await graph.ainvoke(initial, config)
                    state: dict[str, Any] = {**initial, **result}

                    if result.get("__interrupt__"):  # the memo is ready and the gate is waiting
                        if not _show_memo(say, state["memo"]):
                            state["outcome"] = "draft_changed"
                        else:
                            decision = collect_decision(ask, say, state["memo"]["sha256"])
                            if decision is None:
                                state["outcome"] = "not_decided"
                                log.append("not_decided", reason="the reviewer did not decide")
                            else:
                                async with asyncio.timeout(FILING_SECONDS):
                                    result = await graph.ainvoke(Command(resume=decision), config)
                                state = {**state, **result}

                _print_outcome(say, state)
                status = state.get("outcome") or "completed"
                exit_code = 0 if status in CLEAN_OUTCOMES else 2
                with closing(runstate.connect(data_dir)) as conn:
                    refused = runstate.count_rejected(conn, log.run_id)
                say(
                    f"Model calls {deps.llm_calls}, tool calls {deps.tool_calls}, findings "
                    f"refused by the verifier {refused}, tokens in/out "
                    f"{deps.tokens_in}/{deps.tokens_out}, run time {deps.clock()}."
                )
                say(f"Check the log has not been edited:  flagbox verify-log {log.run_id}")
    except ModelUnavailable as error:
        say(f"The model could not be reached: {error}")
        status, exit_code = "model_unavailable", 2
    except TimeoutError:
        say("The run hit its time limit and was cancelled.")
        status, exit_code = "timeout", 2
    finally:
        counts = {
            "tool_calls": deps.tool_calls if deps else 0,
            "llm_calls": deps.llm_calls if deps else 0,
            "tokens_in": deps.tokens_in if deps else 0,
            "tokens_out": deps.tokens_out if deps else 0,
        }
        log.finish(status, **counts, gaps=deps.gaps if deps else [])
        with closing(runstate.connect(data_dir)) as conn:
            runstate.finish_run(conn, log.run_id, status, **counts)
    return exit_code
