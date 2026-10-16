"""The agent graph, driven by a scripted fake model (no Ollama needed)."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from helpers import CONFIG_PATH
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import InMemorySaver
from mcp.shared.memory import create_connected_server_and_client_session

from flagbox.agent.graph import Deps, build_graph
from flagbox.agent.logged_tools import LoggedSession
from flagbox.agent.runlog import RunLog, iter_records, verify_log
from flagbox.server import runstate, tools
from flagbox.server.server import build_server

GOOD = "needs_context"  # a reason_code that is true for every repeat_winner hit
Step = Callable[[list[BaseMessage]], AIMessage]


def call(name: str, args: dict[str, Any], n: int = 0) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": f"call-{name}-{n}"}]
    )


def last_result(messages: list[BaseMessage]) -> dict[str, Any]:
    tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
    return json.loads(str(tool_messages[-1].content))


def hit_id(messages: list[BaseMessage], index: int = 0) -> str:
    for message in messages:
        if isinstance(message, ToolMessage):
            payload = json.loads(str(message.content))
            if payload.get("hits"):
                return payload["hits"][index]["hit_id"]
    raise AssertionError("no hits seen yet")


def usage() -> dict[str, int]:
    return {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}


class ScriptedLLM:
    """Plays a fixed list of model turns. After the script ends it says DONE."""

    def __init__(
        self, plan: list[str], steps: list[Step], skipped: list[dict[str, str]] | None = None
    ):
        self.plan, self.steps, self.skipped = plan, list(steps), skipped or []
        self.plan_calls = self.turns = 0
        self.bound: list[str] = []
        self.menus: list[list[str]] = []
        self.break_plan = False

    def with_structured_output(
        self, model: Any, method: str | None = None, include_raw: bool = False
    ):
        outer = self

        class Structured:
            async def ainvoke(self, messages: list[BaseMessage]) -> dict[str, Any]:
                outer.plan_calls += 1
                raw = AIMessage(content="", usage_metadata=usage())  # type: ignore[arg-type]
                if outer.break_plan:
                    return {"raw": raw, "parsed": None, "parsing_error": ValueError("bad")}
                parsed = model(indicators=outer.plan, skipped=outer.skipped)
                return {"raw": raw, "parsed": parsed, "parsing_error": None}

        return Structured()

    def bind_tools(self, tool_list: list[Any]) -> "ScriptedLLM":
        self.bound = [t.name for t in tool_list]
        self.menus.append(self.bound)
        return self

    async def ainvoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.turns += 1
        message = self.steps.pop(0)(messages) if self.steps else AIMessage(content="DONE")
        message.usage_metadata = usage()  # type: ignore[assignment]
        return message


async def _run(
    data_dir: Path,
    tmp_path: Path,
    llm: ScriptedLLM,
    buyer: str = "b1",
    dates=("2024-01-01", "2024-12-31"),
):
    logs_dir = tmp_path / "logs"
    log = RunLog.create(logs_dir)
    ctx = tools.RunContext(log.run_id, logs_dir, data_dir, drafts_dir=tmp_path / "drafts")
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
        )
        state = await build_graph(deps, InMemorySaver()).ainvoke(
            {
                "run_id": log.run_id,
                "buyer_key": buyer,
                "buyer_name": "Buyer One",
                "date_from": dates[0],
                "date_to": dates[1],
            },
            {"configurable": {"thread_id": log.run_id}},
        )
    log.finish("completed")
    return state, deps, log, lines


def run(data_dir: Path, tmp_path: Path, llm: ScriptedLLM, **kwargs: Any):
    return asyncio.run(_run(data_dir, tmp_path, llm, **kwargs))


def tool_names(log: RunLog, include_draft: bool = False) -> list[str]:
    names = [r["tool"] for r in iter_records(log.path) if r["event"] == "tool_call"]
    return names if include_draft else [n for n in names if n != "draft_memo"]


def draft_calls(log: RunLog) -> list[dict[str, Any]]:
    return [r for r in iter_records(log.path) if r.get("tool") == "draft_memo"]


def finding_rows(data_dir: Path, status: str) -> list[Any]:
    from contextlib import closing

    with closing(runstate.connect(data_dir)) as conn:
        return conn.execute(
            "SELECT * FROM findings WHERE status = ? ORDER BY id", (status,)
        ).fetchall()


# ---- the happy path ----------


def happy_steps() -> list[Step]:
    return [
        lambda m: call("repeat_winner", {}),
        lambda m: call(
            "record_finding", {"hit_id": hit_id(m, 0), "severity": "high", "reason_code": GOOD}, 1
        ),
        lambda m: call(
            "record_finding", {"hit_id": hit_id(m, 1), "severity": "low", "reason_code": GOOD}, 2
        ),
    ]


def test_a_full_run_plans_investigates_records_and_verifies(data_dir, tmp_path):
    llm = ScriptedLLM(["repeat_winner"], happy_steps())
    state, deps, log, lines = run(data_dir, tmp_path, llm)

    assert state["outcome"] == "investigated" and state["plan"] == ["repeat_winner"]
    assert state["verification"]["kept"] == 2 and state["verification"]["undecided"] == []
    assert state["verification"]["chain_ok"] is True
    assert tool_names(log) == ["data_coverage", "repeat_winner", "record_finding", "record_finding"]
    assert [r["event"] for r in iter_records(log.path)][:2] == ["run_start", "tool_call"]
    assert {"plan", "verify"} <= {r["event"] for r in iter_records(log.path)}
    assert len(finding_rows(data_dir, "kept")) == 2
    assert verify_log(log.path).ok

    # one progress line per tool call, each with its call id
    assert any("[tc-0001] data_coverage" in line for line in lines)
    assert any(
        "[tc-0002] repeat_winner" in line and "2 hits among 10 awards" in line for line in lines
    )
    assert sum("recorded (" in line for line in lines) == 2
    # the model was only offered the planned indicator and record_finding
    assert sorted(llm.bound) == ["record_finding", "repeat_winner"]
    assert (
        deps.llm_calls == 1 + 4 + 2
    )  # plan, investigation turns, two summary attempts and deps.tokens_in > 0


def test_the_model_cannot_choose_the_buyer_or_dates(data_dir, tmp_path):
    steps = [lambda m: call("repeat_winner", {"buyer_key": "b2", "date_from": "2019-01-01"})]
    llm = ScriptedLLM(["repeat_winner"], steps)
    _, _, log, _ = run(data_dir, tmp_path, llm)
    logged = next(r for r in iter_records(log.path) if r.get("tool") == "repeat_winner")
    assert logged["arguments"] == {
        "buyer_key": "b1",
        "date_from": "2024-01-01",
        "date_to": "2024-12-31",
    }


# ---- refusals and repairs ----------


def test_a_refused_finding_can_be_repaired(data_dir, tmp_path):
    steps = [
        lambda m: call("repeat_winner", {}),
        lambda m: call(
            "record_finding",
            {"hit_id": hit_id(m), "severity": "high", "reason_code": "Won 4 awards"},
            1,
        ),
        lambda m: call(
            "record_finding", {"hit_id": hit_id(m), "severity": "high", "reason_code": GOOD}, 2
        ),
    ]
    state, _, _, lines = run(data_dir, tmp_path, ScriptedLLM(["repeat_winner"], steps))
    assert state["verification"]["kept"] == 1
    assert any("refused: reason_code" in line for line in lines)
    assert [r["status"] for r in finding_rows(data_dir, "rejected")] == ["rejected"]


def test_after_two_repairs_the_hit_is_left_alone_and_the_refusal_is_logged(data_dir, tmp_path):
    bad = lambda n: (  # noqa: E731
        lambda m: call(
            "record_finding",
            {"hit_id": hit_id(m), "severity": "high", "reason_code": "made_up_reason"},
            n,
        )
    )
    steps = [lambda m: call("repeat_winner", {}), bad(1), bad(2), bad(3), bad(4)]
    state, deps, log, _ = run(data_dir, tmp_path, ScriptedLLM(["repeat_winner"], steps))

    assert state["verification"]["kept"] == 0
    assert len(finding_rows(data_dir, "rejected")) == 3  # the 4th attempt never reached the server
    refused = [r for r in iter_records(log.path) if r["event"] == "refused_call"]
    assert len(refused) == 1 and "Repair limit" in refused[0]["reason"]
    assert any("repair limit" in gap for gap in deps.gaps)
    assert len(state["verification"]["undecided"]) == 2  # both hits ended without a finding


def test_a_model_that_stops_early_leaves_undecided_hits_that_are_reported(data_dir, tmp_path):
    steps = [lambda m: call("repeat_winner", {}), lambda m: AIMessage(content="DONE")]
    state, _, _, _ = run(data_dir, tmp_path, ScriptedLLM(["repeat_winner"], steps))
    assert state["verification"]["kept"] == 0 and len(state["verification"]["undecided"]) == 2


def test_an_unknown_tool_is_refused_without_reaching_the_server(data_dir, tmp_path):
    steps = [lambda m: call("delete_everything", {})]
    _, _, log, _ = run(data_dir, tmp_path, ScriptedLLM(["repeat_winner"], steps))
    assert "delete_everything" not in tool_names(log)
    assert any(r["event"] == "refused_call" for r in iter_records(log.path))


def test_a_hit_id_from_the_model_must_be_real(data_dir, tmp_path):
    steps = [
        lambda m: call("repeat_winner", {}),
        lambda m: call(
            "record_finding", {"hit_id": "RW-invented", "severity": "high", "reason_code": GOOD}
        ),
    ]
    state, _, _, lines = run(data_dir, tmp_path, ScriptedLLM(["repeat_winner"], steps))
    assert state["verification"]["kept"] == 0
    assert any("refused: unknown_hit" in line for line in lines)


# ---- limits and early exits ----------


def test_the_tool_call_limit_stops_a_model_that_keeps_going(data_dir, tmp_path, monkeypatch):
    monkeypatch.setattr("flagbox.agent.graph.MAX_TOOL_CALLS", 2)
    _, deps, log, _ = run(data_dir, tmp_path, ScriptedLLM(["repeat_winner"], happy_steps()))
    assert tool_names(log) == ["data_coverage", "repeat_winner", "record_finding"]
    assert "tool-call limit reached" in deps.gaps


def test_no_awards_in_range_ends_early_and_never_calls_the_model(data_dir, tmp_path):
    llm = ScriptedLLM(["repeat_winner"], happy_steps())
    state, _, log, _ = run(data_dir, tmp_path, llm, dates=("2019-01-01", "2019-12-31"))
    assert state["outcome"] == "no_awards" and llm.plan_calls == 0 and llm.turns == 0
    assert tool_names(log) == ["data_coverage"]


def test_an_unusable_plan_falls_back_to_all_runnable_indicators(data_dir, tmp_path):
    llm = ScriptedLLM(["repeat_winner"], happy_steps())
    llm.break_plan = True
    state, _, log, lines = run(data_dir, tmp_path, llm)
    assert set(state["plan"]) == {"repeat_winner", "non_competitive_share"}
    assert state["verification"]["kept"] == 2
    plan = next(r for r in iter_records(log.path) if r["event"] == "plan")
    assert plan["fallback"] is True and any("plan unusable" in line for line in lines)


def test_an_empty_plan_stops_and_wording_rules_apply_to_skip_reasons(data_dir, tmp_path):
    skipped = [{"indicator": "repeat_winner", "reason": "Skipped because of 4 reasons."}]
    llm = ScriptedLLM([], [], skipped=skipped)
    state, _, log, _ = run(data_dir, tmp_path, llm)
    assert state["outcome"] == "plan_empty" and llm.turns == 0
    plan = next(r for r in iter_records(log.path) if r["event"] == "plan")
    assert "withheld" in plan["skipped"][0]["reason"]  # it contained a digit


def test_the_log_of_a_finished_run_verifies_and_breaks_when_edited(data_dir, tmp_path):
    state, _, log, _ = run(data_dir, tmp_path, ScriptedLLM(["repeat_winner"], happy_steps()))
    assert state["verification"]["chain_ok"] is True and verify_log(log.path).ok
    edited = log.path.read_text(encoding="utf-8").replace("repeat_winner", "something_else", 1)
    log.path.write_text(edited, encoding="utf-8")
    assert not verify_log(log.path).ok


def test_a_model_that_keeps_inventing_hits_is_stopped_early(data_dir, tmp_path):
    def invent(n: int) -> Step:
        return lambda m: call(
            "record_finding", {"hit_id": f"RW-fake{n}", "severity": "high", "reason_code": GOOD}, n
        )

    steps = [lambda m: call("repeat_winner", {})] + [invent(n) for n in range(10)]
    _, deps, log, _ = run(data_dir, tmp_path, ScriptedLLM(["repeat_winner"], steps))
    assert tool_names(log).count("record_finding") == 2  # stopped after two invented ids
    assert "stopped: two unusable tool calls in a row" in deps.gaps


# ---- the tool menu ----------


def test_record_finding_is_not_offered_until_a_tool_has_returned_hits(data_dir, tmp_path):
    llm = ScriptedLLM(["repeat_winner"], happy_steps())
    run(data_dir, tmp_path, llm)
    assert llm.menus[0] == ["repeat_winner"]  # nothing to record yet
    assert "record_finding" in llm.menus[1]  # offered once the hits are in


def test_with_no_hits_record_finding_is_never_offered_and_is_refused_if_called(data_dir, tmp_path):
    steps = [
        lambda m: call("repeat_winner", {}),
        lambda m: call(
            "record_finding", {"hit_id": "RW-fake", "severity": "low", "reason_code": GOOD}
        ),
    ]
    llm = ScriptedLLM(["repeat_winner"], steps)
    # b2 has a single award, so repeat_winner finds nothing.
    state, _, log, _ = run(data_dir, tmp_path, llm, buyer="b2")
    assert all("record_finding" not in menu for menu in llm.menus)
    assert "record_finding" not in tool_names(log)  # the server was never asked
    refused = [r for r in iter_records(log.path) if r["event"] == "refused_call"]
    assert len(refused) == 1 and "only after a tool has returned hits" in refused[0]["reason"]
    assert state["verification"]["kept"] == 0 and state["verification"]["hits_seen"] == 0


def test_running_the_same_indicator_twice_is_refused(data_dir, tmp_path):
    steps = [lambda m: call("repeat_winner", {}), lambda m: call("repeat_winner", {}, 1)]
    _, deps, log, _ = run(data_dir, tmp_path, ScriptedLLM(["repeat_winner"], steps))
    assert tool_names(log).count("repeat_winner") == 1
    refused = [r for r in iter_records(log.path) if r["event"] == "refused_call"]
    assert len(refused) == 1 and "already been run" in refused[0]["reason"]
    assert deps.stall == 1


# ---- the draft step -----------------------------------------------------------

GOOD_SUMMARY = (
    "Some suppliers received a share of this buyer's award value at or above the review "
    "threshold. A human reviewer should examine these patterns against the source records."
)


def with_summaries(*summaries: str) -> list[Step]:
    """Happy-path investigation, then the model's summary attempts."""
    done: Step = lambda m: AIMessage(content="DONE")  # noqa: E731
    attempts: list[Step] = [lambda m, t=t: AIMessage(content=t) for t in summaries]
    return [*happy_steps(), done, *attempts]


def memo_text(state: dict[str, Any]) -> str:
    return Path(state["memo"]["path"]).read_text(encoding="utf-8")


def test_a_model_summary_that_passes_the_rules_is_used_and_labelled(data_dir, tmp_path):
    llm = ScriptedLLM(["repeat_winner"], with_summaries(GOOD_SUMMARY))
    state, deps, log, lines = run(data_dir, tmp_path, llm)

    assert state["memo"]["summary_source"] == "model" and len(state["memo"]["sha256"]) == 64
    text = memo_text(state)
    assert (
        GOOD_SUMMARY in text and "written by the language model and checked by fixed rules" in text
    )
    assert len(draft_calls(log)) == 1 and deps.llm_calls == 1 + 4 + 1
    assert any("draft_memo" in line for line in lines)
    assert verify_log(log.path).ok


def test_a_refused_summary_is_retried_once_with_the_reason(data_dir, tmp_path):
    bad = "Two suppliers received a share of this buyer's award value, so a human should look."
    llm = ScriptedLLM(["repeat_winner"], with_summaries(bad, GOOD_SUMMARY))
    state, _, log, _ = run(data_dir, tmp_path, llm)

    first, second = draft_calls(log)
    assert first["result"]["drafted"] is False
    assert [v["rule"] for v in first["result"]["violations"]] == ["numbers"]
    assert second["result"]["drafted"] is True and state["memo"]["summary_source"] == "model"


def test_after_two_refusals_the_fixed_summary_is_used(data_dir, tmp_path):
    bad = "These suppliers look fraudulent to us, so someone should check this buyer."
    llm = ScriptedLLM(["repeat_winner"], with_summaries("Two suppliers.", bad))
    state, deps, log, _ = run(data_dir, tmp_path, llm)

    calls = draft_calls(log)
    assert [c["result"]["drafted"] for c in calls] == [False, False, True]
    assert "summary" not in calls[2]["arguments"]  # the third call sends no summary at all
    assert state["memo"]["summary_source"] == "fixed"
    assert "found 2 patterns that warrant review by a human reviewer" in memo_text(state)
    assert "the model's summary failed the rules twice; fixed summary used" in deps.gaps


def test_the_model_is_never_offered_draft_memo(data_dir, tmp_path):
    llm = ScriptedLLM(["repeat_winner"], with_summaries(GOOD_SUMMARY))
    run(data_dir, tmp_path, llm)
    assert all("draft_memo" not in menu for menu in llm.menus)


def test_with_no_findings_the_model_is_not_asked_and_the_memo_says_why(data_dir, tmp_path):
    steps = [lambda m: call("repeat_winner", {})]
    llm = ScriptedLLM(["repeat_winner"], steps)
    state, _, log, _ = run(data_dir, tmp_path, llm, buyer="b2")  # one award: no hit
    text = memo_text(state)

    assert len(draft_calls(log)) == 1 and state["memo"]["summary_source"] == "fixed"
    assert llm.turns == 2  # the investigation only; no summary request
    assert "No finding was recorded." in text and "valid result, not a failure" in text


def test_every_early_exit_still_produces_a_memo(data_dir, tmp_path):
    llm = ScriptedLLM(["repeat_winner"], happy_steps())
    state, _, log, _ = run(data_dir, tmp_path, llm, dates=("2019-01-01", "2019-12-31"))
    assert state["outcome"] == "no_awards" and llm.turns == 0
    assert "No awards were found for this buyer in this range." in memo_text(state)
    assert tool_names(log, include_draft=True) == ["data_coverage", "draft_memo"]

    empty = ScriptedLLM([], [], skipped=[{"indicator": "repeat_winner", "reason": "Not needed."}])
    state, _, _, _ = run(data_dir, tmp_path / "second", empty)
    text = memo_text(state)
    assert "The plan ran no indicators." in text and "Skipped repeat_winner: Not needed." in text
