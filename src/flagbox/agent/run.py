"""`flagbox run`: one screening run, start to finish (SPEC section 2, steps 1-3).

Order of events:
  1. check Ollama and the data (exit code 2 if either is missing);
  2. open the run's log and start our MCP server for this run;
  3. find the buyer with `find_buyer` (a logged tool call) and let the officer pick if unclear;
  4. run the graph, printing one line per tool call;
  5. print the screening result and close the log.
"""

import asyncio
import json
import time
from collections.abc import Callable
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

from langchain_mcp_adapters.tools import load_mcp_tools
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from flagbox.agent import prompts
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
from flagbox.mcp_client import server_parameters
from flagbox.server import runstate

RUN_HARD_STOP_SECONDS = 30 * 60  # SPEC section 7


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


def _print_report(
    say: Callable[[str], None], run_id: str, state: dict[str, Any], deps: Deps, data_dir: Path
) -> None:
    with closing(runstate.connect(data_dir)) as conn:
        findings = runstate.kept_findings(conn, run_id)
        refused = conn.execute(
            "SELECT COUNT(*) FROM findings WHERE run_id = ? AND status = 'rejected'", (run_id,)
        ).fetchone()[0]

    say("")
    say("=" * 78)
    say(f"SCREENING RESULT  {state['buyer_name']}, {state['date_from']} to {state['date_to']}")
    say("=" * 78)
    verification = state.get("verification", {})
    if findings:
        for row in findings:
            refs = json.loads(row["source_refs"])
            say(
                f"\nFINDING {row['finding_id']}  severity: {row['severity']}  "
                f"(from call {row['tool_call_id']})"
            )
            say(f"  {row['claim_text']}")
            say(f"  Why it warrants review: {row['rationale']}")
            for ref in refs:
                say(f"  Evidence: {ref['ocid']}  ({ref['file']} line {ref['line_no']})")
                if ref.get("portal_url"):
                    say(f"            {ref['portal_url']}")
    else:
        say(_justified_none(state, verification))
    if verification.get("undecided"):
        say(
            f"\n{len(verification['undecided'])} hits received no finding "
            "(the model did not record them)."
        )
    for gap in deps.gaps:
        say(f"Gap: {gap}")
    for check in verification.get("checks", []):
        say(
            f"Checked: {check['indicator']} -> {check['hits_total']} hits "
            f"among {check['awards_checked']} awards"
        )
    say(
        f"\nModel calls {deps.llm_calls}, tool calls {deps.tool_calls}, findings refused by the "
        f"verifier {refused}, tokens in/out {deps.tokens_in}/{deps.tokens_out}, "
        f"run time {deps.clock()}."
    )
    say(
        "This is a screening result for a human to review. "
        "No memo has been drafted and nothing filed."
    )
    say(f"Check the log has not been edited:  flagbox verify-log {run_id}")


def _justified_none(state: dict[str, Any], verification: dict[str, Any]) -> str:
    outcome = state.get("outcome", "")
    coverage = state.get("coverage", {})
    if outcome == "no_awards":
        return "No findings: " + coverage.get("message", "no awards in this range.")
    if outcome == "nothing_runnable":
        return (
            "No findings: none of the indicators has enough usable data for this buyer and range."
        )
    if outcome == "plan_empty":
        return "No findings: the plan ran no indicators."
    if outcome == "coverage_failed":
        return "No findings: the coverage check failed."
    checks = verification.get("checks", [])
    if checks and all(c["hits_total"] == 0 for c in checks):
        return "No findings: no indicator crossed its threshold. That is a valid result."
    return "No findings were recorded."


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
) -> int:
    settings = settings or settings_from_env()
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
        check_ollama(settings)
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
    state: dict[str, Any] = {}
    params = server_parameters(data_dir, config_path, run_id=log.run_id, logs_dir=logs_dir)
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
                    llm=make_llm(settings),
                    base_tools=await load_mcp_tools(session),  # type: ignore[arg-type]
                    data_dir=data_dir,
                    say=say,
                )
                deps.tool_calls = 1  # the find_buyer call above
                graph = build_graph(deps)
                initial: AgentState = {
                    "run_id": log.run_id,
                    "buyer_key": buyer["buyer_key"],
                    "buyer_name": buyer["name"],
                    "date_from": date_from,
                    "date_to": date_to,
                }
                async with asyncio.timeout(RUN_HARD_STOP_SECONDS):
                    state = await graph.ainvoke(initial)
                state = {**initial, **state}
                _print_report(say, log.run_id, state, deps, data_dir)
                status, exit_code = state.get("outcome") or "completed", 0
    except ModelUnavailable as error:
        say(f"The model could not be reached: {error}")
        status, exit_code = "model_unavailable", 2
    except TimeoutError:
        say("The run hit the 30-minute hard stop and was cancelled.")
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
