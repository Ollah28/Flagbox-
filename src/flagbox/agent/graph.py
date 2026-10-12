"""The agent graph (SPEC 3.1), minimal slice for M2.4:

    check_coverage (code) -> plan (model) -> investigate (model loop) -> verify (code)

The human gate and filing arrive in M3. Who decides what:
  - code: reading coverage, enforcing limits, re-checking every finding;
  - the model: which runnable indicators to run, and the severity and rationale of each finding.
The model never writes a claim, a number or a name (hard rule 2).
"""

import json
import time
from collections import defaultdict
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, create_model

from flagbox.agent import prompts
from flagbox.agent.bound_tools import bind
from flagbox.agent.logged_tools import LoggedSession
from flagbox.agent.model import invoke_with_retry, token_counts
from flagbox.agent.runlog import RunLog, iter_records, verify_log
from flagbox.agent.verify import check_rationale, verify_finding
from flagbox.server import runstate
from flagbox.server.indicators import discover
from flagbox.server.tools import reasons_lookup

MAX_TOOL_CALLS = 25  # SPEC section 7
INVESTIGATE_SECONDS = 15 * 60
MAX_REPAIRS = 2  # a finding may be retried twice after a refusal
MAX_STALL = 2  # two wasted tool calls in a row end the investigation


class RunIdentity(TypedDict):
    """What the officer chose before the agent started. Always present."""

    run_id: str
    buyer_key: str
    buyer_name: str
    date_from: str
    date_to: str


class AgentState(RunIdentity, total=False):
    coverage: dict[str, Any]
    runnable: list[str]
    plan: list[str]
    outcome: str  # why the run stopped early, or "investigated"
    verification: dict[str, Any]
    memo: dict[str, Any]


@dataclass
class Deps:
    """Everything the nodes need that is not part of the (serialisable) state."""

    session: LoggedSession
    log: RunLog
    llm: Any
    base_tools: list[BaseTool]
    data_dir: Path
    say: Callable[[str], None]
    started: float = field(default_factory=time.monotonic)
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: int = 0
    gaps: list[str] = field(default_factory=list)
    attempts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    stall: int = 0  # consecutive wasted turns (refused, repeated or invented calls)

    def clock(self) -> str:
        seconds = int(time.monotonic() - self.started)
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def count_llm(self, message: Any) -> None:
        self.llm_calls += 1
        tokens_in, tokens_out = token_counts(message)
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out


def summarise(payload: dict[str, Any]) -> str:
    """One short phrase describing a tool result, for the progress line."""
    if "error" in payload:
        return f"error: {payload['error']}"
    if "accepted" in payload:
        if payload["accepted"]:
            return f"finding {payload['finding_id']} recorded ({payload['severity']})"
        return "refused: " + ", ".join(v["rule"] for v in payload.get("violations", []))
    if "hits_total" in payload:
        return f"{payload['hits_total']} hits among {payload.get('eligible_awards', '?')} awards"
    if "runnable_indicators" in payload:
        names = [e["indicator"] for e in payload["runnable_indicators"] if e["runnable"]]
        return f"{payload['awards']} awards; runnable: {', '.join(names) or 'none'}"
    if "candidates" in payload:
        return f"{len(payload['candidates'])} candidate buyers ({payload['match']})"
    return "ok"


def progress_line(
    deps: Deps, tool: str, payload: dict[str, Any], seconds: float, tool_call_id: str | None = None
) -> str:
    call_id = tool_call_id or payload.get("tool_call_id", "-")
    return f"  [{call_id}] {tool:<20} {summarise(payload)}  ({seconds:.1f}s, run {deps.clock()})"


def _payload(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


class Skip(BaseModel):
    indicator: str
    reason: str


def build_graph(deps: Deps) -> Any:
    async def check_coverage(state: AgentState) -> dict[str, Any]:
        started = time.monotonic()
        args = {k: state[k] for k in ("buyer_key", "date_from", "date_to")}  # type: ignore[literal-required]
        result = await deps.session.call_tool("data_coverage", args)
        deps.tool_calls += 1
        payload: dict[str, Any] = result.structuredContent or {}
        deps.say(progress_line(deps, "data_coverage", payload, time.monotonic() - started))
        if result.isError or "error" in payload:
            return {"coverage": payload, "runnable": [], "outcome": "coverage_failed"}
        runnable = [e["indicator"] for e in payload["runnable_indicators"] if e["runnable"]]
        outcome = (
            "no_awards" if payload["awards"] == 0 else "nothing_runnable" if not runnable else ""
        )
        update: dict[str, Any] = {"coverage": payload, "runnable": runnable}
        if outcome:
            update["outcome"] = outcome
        return update

    async def plan(state: AgentState) -> dict[str, Any]:
        names = tuple(state.get("runnable", []))
        plan_model = create_model(
            "Plan",
            indicators=(list[Literal[names]], ...),  # type: ignore[valid-type]
            skipped=(list[Skip], ...),
        )
        structured = deps.llm.with_structured_output(
            plan_model, method="json_schema", include_raw=True
        )
        prompt = prompts.PLAN.format(
            buyer_name=state["buyer_name"],
            date_from=state["date_from"],
            date_to=state["date_to"],
            coverage=json.dumps(state.get("coverage", {}).get("runnable_indicators", [])),
        )
        started = time.monotonic()
        reply = await invoke_with_retry(
            structured, [SystemMessage(prompts.SYSTEM), HumanMessage(prompt)]
        )
        deps.count_llm(reply.get("raw"))
        parsed = reply.get("parsed")

        fallback = parsed is None
        chosen = [] if fallback else [n for n in dict.fromkeys(parsed.indicators) if n in names]
        if fallback:
            chosen = list(names)  # the model's plan was unusable: run everything runnable
        skipped = []
        for item in [] if fallback else parsed.skipped:
            wording = check_rationale(item.reason)
            skipped.append(
                {
                    "indicator": item.indicator,
                    "reason": "(reason withheld: it broke the wording rules)"
                    if wording
                    else item.reason,
                }
            )
        deps.log.append("plan", indicators=chosen, skipped=skipped, fallback=fallback)
        shown = ", ".join(chosen) or "none"
        note = " (model plan unusable, ran all runnable)" if fallback else ""
        deps.say(
            f"  plan: run {shown}{note}  ({time.monotonic() - started:.1f}s, run {deps.clock()})"
        )
        for item in skipped:
            deps.say(f"        skip {item['indicator']}: {item['reason']}")
        update: dict[str, Any] = {"plan": chosen}
        if not chosen:
            update["outcome"] = "plan_empty"
        return update

    async def investigate(state: AgentState) -> dict[str, Any]:
        fixed = {k: state[k] for k in ("buyer_key", "date_from", "date_to")}  # type: ignore[literal-required]
        wanted = {*state.get("plan", []), "record_finding"}
        tools = {t.name: bind(t, fixed) for t in deps.base_tools if t.name in wanted}
        ran: set[str] = set()  # indicators already run
        hits_seen = False
        messages: list[BaseMessage] = [
            SystemMessage(prompts.SYSTEM),
            HumanMessage(
                prompts.INVESTIGATE.format(
                    buyer_name=state["buyer_name"],
                    date_from=state["date_from"],
                    date_to=state["date_to"],
                    plan=", ".join(state.get("plan", [])),
                )
            ),
        ]

        def menu() -> dict[str, Any]:
            """The tools offered this turn. record_finding appears only once a tool has returned
            a hit, so a model cannot be tempted to record something that does not exist."""
            return {n: t for n, t in tools.items() if n != "record_finding" or hits_seen}

        def refusal(name: str, args: dict[str, Any]) -> str:
            offered = menu()
            if name not in offered:
                if name in tools:
                    return "record_finding is available only after a tool has returned hits."
                return f"Unknown tool. Use one of: {', '.join(offered)}."
            if name != "record_finding" and name in ran:
                return f"{name} has already been run and its result is above."
            if (
                name == "record_finding"
                and deps.attempts[str(args.get("hit_id", ""))] > MAX_REPAIRS
            ):
                deps.gaps.append(f"hit {str(args.get('hit_id', ''))[:40]}: repair limit reached")
                return "Repair limit reached for this hit. Leave it and move on."
            return ""

        async def run_tool(name: str, args: dict[str, Any]) -> str:
            nonlocal hits_seen
            reason = refusal(name, args)
            if reason:
                deps.stall += 1
                deps.log.append("refused_call", tool=name, arguments=args, reason=reason)
                deps.say(f"  [-] {name:<20} not run: {reason}")
                return json.dumps({"error": "not_run", "message": reason})
            tool = tools[name]
            if name == "record_finding":
                deps.attempts[str(args.get("hit_id", ""))] += 1
            for attempt in (1, 2):  # a failed call is retried once, then logged as a gap
                started = time.monotonic()
                try:
                    text = await tool.ainvoke(args)
                except Exception as error:
                    if attempt == 2:
                        deps.gaps.append(f"{name}: failed twice ({type(error).__name__})")
                        deps.say(f"  [-] {name:<20} failed twice: {type(error).__name__}")
                        deps.stall += 1
                        return json.dumps({"error": "tool_failed", "message": "The tool failed."})
                    continue
                deps.tool_calls += 1
                payload = _payload(text)
                if name == "record_finding":
                    if payload.get("accepted"):
                        deps.stall = 0
                    elif any(v.get("rule") == "unknown_hit" for v in payload.get("violations", [])):
                        deps.stall += 1  # citing a hit that does not exist
                else:
                    ran.add(name)
                    deps.stall = 0
                    hits_seen = hits_seen or bool(payload.get("hits"))
                deps.say(progress_line(deps, name, payload, time.monotonic() - started))
                return text
            return ""  # unreachable

        loop_started = time.monotonic()
        used = 0
        while True:
            if used >= MAX_TOOL_CALLS:
                deps.gaps.append("tool-call limit reached")
                break
            if deps.stall >= MAX_STALL:
                deps.gaps.append("stopped: two unusable tool calls in a row")
                break
            if time.monotonic() - loop_started > INVESTIGATE_SECONDS:
                deps.gaps.append("investigation time limit reached")
                break
            reply = await invoke_with_retry(deps.llm.bind_tools(list(menu().values())), messages)
            deps.count_llm(reply)
            messages.append(reply)
            if not reply.tool_calls:
                break
            for call in reply.tool_calls:
                if used >= MAX_TOOL_CALLS:
                    text = json.dumps({"error": "limit", "message": "Tool-call limit reached."})
                else:
                    used += 1
                    text = await run_tool(call["name"], dict(call["args"] or {}))
                messages.append(
                    ToolMessage(content=text, tool_call_id=call["id"], name=call["name"])
                )
        return {"outcome": "investigated"}

    async def verify(state: AgentState) -> dict[str, Any]:
        path = deps.log.path
        chain = verify_log(path)
        records = list(iter_records(path)) if chain.ok else []
        seen: dict[str, str] = {}
        checks: list[dict[str, Any]] = []
        for record in records:
            result = record.get("result") if record.get("event") == "tool_call" else None
            if isinstance(result, dict) and "hits_total" in result:
                checks.append(
                    {
                        "indicator": record["tool"],
                        "hits_total": result["hits_total"],
                        "awards_checked": result.get("eligible_awards"),
                    }
                )
                for hit in result.get("hits", []):
                    seen[hit["hit_id"]] = record["tool"]

        reasons_for = reasons_lookup(discover())
        kept: list[str] = []
        demoted: list[dict[str, str]] = []
        with closing(runstate.connect(deps.data_dir)) as conn:
            for row in runstate.kept_findings(conn, state["run_id"]):
                outcome = verify_finding(
                    records, row["hit_id"], row["severity"], row["reason_code"] or "", reasons_for
                )
                if outcome.ok:
                    kept.append(row["hit_id"])
                else:
                    rules = ",".join(v.rule for v in outcome.violations)
                    runstate.reject_kept_finding(conn, row["id"], f"failed_reverification:{rules}")
                    demoted.append({"hit_id": row["hit_id"], "rules": rules})
        summary = {
            "chain_ok": chain.ok,
            "kept": len(kept),
            "demoted": demoted,
            "hits_seen": len(seen),
            "undecided": sorted(set(seen) - set(kept)),
            "checks": checks,
            "gaps": list(deps.gaps),
        }
        deps.log.append("verify", **summary)
        deps.say(
            f"  verify: {len(kept)} findings kept, {len(demoted)} demoted, "
            f"{len(summary['undecided'])} hits without a finding  (run {deps.clock()})"
        )
        return {"verification": summary}

    async def draft(state: AgentState) -> dict[str, Any]:
        """Ask the model for a summary, let draft_memo check it, and fall back to fixed text."""
        run_id = state["run_id"]
        with closing(runstate.connect(deps.data_dir)) as conn:
            findings = runstate.kept_findings(conn, run_id)

        async def ask_server(arguments: dict[str, Any]) -> dict[str, Any]:
            started = time.monotonic()
            result = await deps.session.call_tool("draft_memo", arguments)
            deps.tool_calls += 1
            payload: dict[str, Any] = result.structuredContent or {}
            deps.say(progress_line(deps, "draft_memo", payload, time.monotonic() - started))
            return payload

        memo: dict[str, Any] | None = None
        if findings:
            listing = "\n".join(
                f"- {row['claim_text']} Severity: {row['severity']}. {row['rationale']}"
                for row in findings
            )
            feedback = ""
            for _ in range(2):  # SPEC: if the summary fails twice, a fixed summary is used
                prompt = (
                    prompts.SUMMARY.format(
                        buyer_name=state["buyer_name"],
                        date_from=state["date_from"],
                        date_to=state["date_to"],
                        findings=listing,
                    )
                    + feedback
                )
                reply = await invoke_with_retry(
                    deps.llm, [SystemMessage(prompts.SYSTEM), HumanMessage(prompt)]
                )
                deps.count_llm(reply)
                payload = await ask_server(
                    {"run_id": run_id, "summary": str(reply.content).strip()}
                )
                if payload.get("drafted"):
                    memo = payload
                    break
                reasons = " ".join(v["message"] for v in payload.get("violations", []))
                feedback = f"\n\nYour previous summary was refused. {reasons} Write it again."
            if memo is None:
                deps.gaps.append("the model's summary failed the rules twice; fixed summary used")
        if memo is None:
            payload = await ask_server({"run_id": run_id})
            memo = payload if payload.get("drafted") else None
        return {"memo": memo} if memo else {"outcome": "memo_failed"}

    graph = StateGraph(AgentState)
    graph.add_node("check_coverage", check_coverage)
    graph.add_node("plan", plan)
    graph.add_node("investigate", investigate)
    graph.add_node("verify", verify)
    graph.add_node("draft", draft)
    graph.add_edge(START, "check_coverage")
    # Every route ends in a memo, even "no awards" or "nothing runnable" (SPEC section 2).
    graph.add_conditional_edges(
        "check_coverage", lambda s: "draft" if s.get("outcome") else "plan", ["plan", "draft"]
    )
    graph.add_conditional_edges(
        "plan", lambda s: "investigate" if s.get("plan") else "draft", ["investigate", "draft"]
    )
    graph.add_edge("investigate", "verify")
    graph.add_edge("verify", "draft")
    graph.add_edge("draft", END)
    return graph.compile()
