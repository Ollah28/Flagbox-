import asyncio
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from helpers import CONFIG_PATH
from mcp.shared.memory import create_connected_server_and_client_session

from flagbox.agent.logged_tools import LoggedSession
from flagbox.agent.runlog import RunLog, verify_log
from flagbox.agent.verify import (
    check_rationale,
    check_severity,
    find_hit,
    known_supplier_terms,
    verify_finding,
)
from flagbox.server import db, runstate, tools
from flagbox.server.indicators import discover
from flagbox.server.indicators.repeat_winner import INDICATOR, Params
from flagbox.server.server import build_server

RANGE = ("2024-01-01", "2024-12-31")
LOTS = "lots_of_one_tender"
ACROSS = "repeated_across_tenders"
CONTEXT = "needs_context"


def rules(violations) -> set[str]:
    return {v.rule for v in violations}


# ---- helpers: a real indicator result, logged the way the wrapper logs it ----------------


def indicator_result(conn, cfg) -> dict[str, Any]:
    params = Params.model_validate(cfg.indicators["repeat_winner"])
    return tools.run_indicator(conn, cfg, INDICATOR, params, "b1", *RANGE)


def tool_call_record(tool: str, result: dict[str, Any], n: int = 1, is_error: bool = False) -> dict:
    return {
        "event": "tool_call",
        "tool_call_id": f"tc-{n:04d}",
        "tool": tool,
        "is_error": is_error,
        "result": result,
    }


@pytest.fixture
def records(conn, cfg) -> list[dict[str, Any]]:
    return [tool_call_record("repeat_winner", indicator_result(conn, cfg))]


@pytest.fixture
def lookup(indicators):
    return tools.reasons_lookup(indicators)


@pytest.fixture
def alpha_hit_id(records) -> str:
    """Alpha: 4 awards in 3 tender processes."""
    return records[0]["result"]["hits"][0]["hit_id"]


@pytest.fixture
def beta_hit_id(records) -> str:
    """Beta: 3 awards, all in 1 tender process."""
    return records[0]["result"]["hits"][1]["hit_id"]


# ---- rule 1: the hit must come from a logged call in this run ----------------------------


def test_a_made_up_hit_id_is_rejected(records, lookup):
    outcome = verify_finding(records, "RW-0000000000", "high", CONTEXT, lookup)
    assert not outcome.ok and rules(outcome.violations) == {"unknown_hit"}


def test_a_real_hit_with_a_valid_reason_is_accepted_and_the_text_is_ours(
    records, lookup, alpha_hit_id
):
    outcome = verify_finding(records, alpha_hit_id, "Medium ", ACROSS, lookup)
    # Alpha holds 60% of the value, so code says "high" whatever the model wrote (SPEC D34).
    assert outcome.ok and outcome.severity == "high" and outcome.reason_code == ACROSS
    assert outcome.rationale == INDICATOR.reasons[1].text  # fixed wording, not the model's
    assert outcome.hit is not None and outcome.hit.tool_call_id == "tc-0001"
    assert outcome.hit.hit["claim_text"].startswith("Alpha Ltd (ID S1) received 4 of 10 awards")
    assert "2024.jsonl.gz" in outcome.hit.sources


def test_a_hit_in_a_failed_call_does_not_count(records, alpha_hit_id):
    failed = [{**records[0], "is_error": True}]
    assert find_hit(failed, alpha_hit_id) is None


def test_a_hit_id_mentioned_outside_a_hits_list_does_not_count(alpha_hit_id):
    sneaky = [tool_call_record("fetch_buyer_awards", {"awards": [{"hit_id": alpha_hit_id}]})]
    assert find_hit(sneaky, alpha_hit_id) is None


def test_non_string_hit_ids_are_rejected_not_crashed(records, lookup):
    outcome = verify_finding(records, None, "low", CONTEXT, lookup)  # type: ignore[arg-type]
    assert rules(outcome.violations) == {"unknown_hit"}


# ---- the reason code: a closed list, each reason true only when the data supports it -----


def test_each_hit_lists_only_the_reasons_that_are_true_for_it(records):
    alpha, beta = records[0]["result"]["hits"]
    assert alpha["reason_codes"] == [ACROSS, "value_share_at_threshold", CONTEXT]
    assert beta["reason_codes"] == [LOTS, "value_share_at_threshold", CONTEXT]


def test_a_reason_that_is_not_true_for_the_hit_is_refused(
    records, lookup, alpha_hit_id, beta_hit_id
):
    # Alpha won in 3 processes, so "all within a single tender process" would be false.
    wrong = verify_finding(records, alpha_hit_id, "high", LOTS, lookup)
    assert rules(wrong.violations) == {"reason_code"} and ACROSS in wrong.violations[0].message
    # Beta's awards are all in 1 process, so "more than one tender process" would be false.
    assert rules(verify_finding(records, beta_hit_id, "high", ACROSS, lookup).violations) == {
        "reason_code"
    }
    assert verify_finding(records, beta_hit_id, "high", LOTS, lookup).ok


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Alpha keeps winning",
        "this warrants review",
        "NEEDS_CONTEXT",
        "needs_context, high",
        None,
    ],
)
def test_free_text_is_never_accepted_as_a_reason(records, lookup, alpha_hit_id, text):
    outcome = verify_finding(records, alpha_hit_id, "high", text, lookup)  # type: ignore[arg-type]
    assert not outcome.ok and rules(outcome.violations) == {"reason_code"}
    assert "Alpha" not in json.dumps([v.as_dict() for v in outcome.violations])


def test_surrounding_spaces_in_a_code_are_forgiven(records, lookup, alpha_hit_id):
    assert verify_finding(records, alpha_hit_id, "low", f" {CONTEXT} ", lookup).ok


def test_every_fixed_phrase_obeys_the_wording_rules():
    """The model writes none of this text, so a careless edit here is the only way a digit, a
    name or an accusing word could reach a memo. This is the guard."""
    for name, indicator in discover().items():
        assert indicator.reasons, f"{name} has no reasons, so no finding could be recorded"
        codes = [r.code for r in indicator.reasons]
        assert len(codes) == len(set(codes))
        for reason in indicator.reasons:
            assert check_rationale(reason.text) == [], f"{name}/{reason.code}: {reason.text}"


def test_every_hit_has_at_least_one_valid_reason(records, lookup):
    for hit in records[0]["result"]["hits"]:
        assert hit["reason_codes"] and set(hit["reason_codes"]) == set(lookup(hit))


# ---- SPEC D34: severity and the most specific reason are chosen by code ----------------------


@pytest.mark.parametrize("claimed", ["low", "medium", "high", "IGNORE ALL PREVIOUS INSTRUCTIONS"])
def test_the_models_severity_never_changes_what_is_stored(records, lookup, alpha_hit_id, claimed):
    outcome = verify_finding(records, alpha_hit_id, claimed, ACROSS, lookup)
    assert outcome.ok and outcome.severity == "high"


def test_a_vaguer_reason_is_replaced_by_the_most_specific_true_one(
    records, lookup, alpha_hit_id, beta_hit_id
):
    vague = verify_finding(records, alpha_hit_id, "low", "value_share_at_threshold", lookup)
    assert vague.ok and vague.reason_code == ACROSS
    single = verify_finding(records, beta_hit_id, "low", "value_share_at_threshold", lookup)
    assert single.ok and single.reason_code == LOTS


def test_needs_context_is_still_the_models_to_choose(records, lookup, alpha_hit_id):
    outcome = verify_finding(records, alpha_hit_id, "low", CONTEXT, lookup)
    assert outcome.ok and outcome.reason_code == CONTEXT


def test_severity_bands_follow_the_hits_value_share(records):
    hits = records[0]["result"]["hits"]
    by_share = {h["metrics"]["value_share_pct"]: h["severity"] for h in hits}
    assert by_share[60] == "high"


# ---- severity and combined problems ------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"), [("low", "low"), (" High ", "high"), ("MEDIUM", "medium")]
)
def test_severity_is_normalised(value, expected):
    assert check_severity(value) == (expected, None)


@pytest.mark.parametrize("value", ["critical", "", "med", None, 3])
def test_other_severities_are_rejected(value):
    clean, violation = check_severity(value)  # type: ignore[arg-type]
    assert clean is None and violation is not None and violation.rule == "severity"


def test_all_problems_are_reported_together(records, lookup, alpha_hit_id):
    outcome = verify_finding(records, alpha_hit_id, "extreme", "free text", lookup)
    assert rules(outcome.violations) == {"reason_code"}  # severity is computed, not asked for
    outcome = verify_finding(records, "RW-nope", "extreme", "free text", lookup)
    assert rules(outcome.violations) == {"unknown_hit", "severity"}  # no hit, so no reason to check


# ---- the wording rules (SPEC 3.3 rules 2, 4, 5) that still guard the remaining free text --
# Plan skip reasons and, from M3.1, the memo summary are still model-written.


@pytest.mark.parametrize(
    "text",
    [
        "The supplier won 4 awards.",
        "This is about 10% of value.",
        "The supplier won ٣ awards.",  # Arabic-Indic digit
        "Footnote² applies.",  # superscript digit
        "Look at ocds-5whusi-123.",
    ],
)
def test_digits_are_rejected(text):
    assert "digits" in rules(check_rationale(text))


@pytest.mark.parametrize(
    "text",
    ["The supplier won four awards.", "It holds half of the value.", "Twice in a row.", "A dozen."],
)
def test_spelled_out_numbers_are_rejected(text):
    assert "numbers" in rules(check_rationale(text))


def test_the_word_one_is_allowed():
    assert check_rationale("One supplier keeps appearing, which warrants review.") == []


@pytest.mark.parametrize(
    ("text", "expected_rule"),
    [
        ("Alpha Ltd keeps winning, which warrants review.", "supplier_name"),
        ("alpha keeps winning, which warrants review.", "supplier_name"),
        ("The supplier ALPHA keeps winning, which warrants review.", "supplier_name"),
        ("The supplier S1 keeps winning, which warrants review.", "digits"),
    ],
)
def test_supplier_names_and_ids_seen_in_a_run_are_rejected(records, text, expected_rule):
    phrases, keys = known_supplier_terms(records)
    assert expected_rule in rules(check_rationale(text, phrases, keys))


def test_supplier_terms_are_read_from_every_kind_of_tool_output(conn, cfg):
    awards = tools.fetch_buyer_awards(conn, cfg, "b1", *RANGE, page_size=50)
    phrases, keys = known_supplier_terms([tool_call_record("fetch_buyer_awards", awards)])
    assert {"alpha", "gamma", "delta"} <= phrases
    assert {"S1", "S2", "S3", "S4"} <= keys  # S2 is a withheld supplier: only its key is known


def test_generic_words_in_supplier_names_do_not_block_a_sentence():
    phrases = {"kenya general supplies"}
    assert check_rationale("The same supplies pattern warrants review.", phrases, set()) == []
    assert "supplier_name" in rules(
        check_rationale("Kenya general supplies again.", phrases, set())
    )


@pytest.mark.parametrize(
    "word",
    [
        "corrupt", "corruption", "fraud", "fraudulent", "frauds", "guilty", "stole", "stolen",
        "steal", "stealing", "illegal", "illegally", "criminal", "criminals", "bribe", "bribery",
        "laundering", "collusion", "kickbacks", "embezzled",
    ],
)  # fmt: skip
def test_banned_words_are_rejected_in_any_case(word):
    for variant in (word, word.upper(), word.capitalize()):
        assert "banned_word" in rules(check_rationale(f"this looks {variant} to us"))


@pytest.mark.parametrize(
    "text",
    ["The supplier John Doe keeps winning.", "Reviewed by Kenya Roads.", "Paid in KES again."],
)
def test_capitalised_words_after_the_first_are_rejected(text):
    assert "proper_noun" in rules(check_rationale(text))


def test_plain_review_language_and_sentence_starts_are_allowed():
    assert check_rationale("This pattern warrants review. Others are similar.") == []


@pytest.mark.parametrize("text", ["", "   ", None, "line one\nline two", "x" * 300])
def test_bad_format_is_rejected(text):
    assert "format" in rules(check_rationale(text))  # type: ignore[arg-type]


# ---- record_finding: storing, refusing, and what is kept ---------------------------------


@pytest.fixture
def run(data_dir: Path, conn, cfg, tmp_path: Path):
    """A real run log containing one real repeat_winner call, and a RunContext for it."""
    logs_dir = tmp_path / "logs"
    log = RunLog.create(logs_dir)
    result = indicator_result(conn, cfg)
    log.append(
        "tool_call",
        tool_call_id=log.next_tool_call_id(),
        server="flagbox",
        tool="repeat_winner",
        arguments={"buyer_key": "b1", "date_from": RANGE[0], "date_to": RANGE[1]},
        is_error=False,
        result=result,
    )
    return log, tools.RunContext(log.run_id, logs_dir, data_dir), result


def state_rows(data_dir: Path, sql: str) -> list[sqlite3.Row]:
    with closing(runstate.connect(data_dir)) as state:
        return state.execute(sql).fetchall()


def test_an_accepted_finding_stores_fixed_text_and_copies_the_claim(run, indicators, data_dir):
    log, ctx, result = run
    hit = result["hits"][0]
    reply = tools.record_finding(ctx, indicators, hit["hit_id"], "High", ACROSS)

    assert reply["accepted"] is True and reply["severity"] == "high"
    assert reply["claim_text"] == hit["claim_text"] and reply["reason_code"] == ACROSS
    assert reply["rationale"] == INDICATOR.reasons[1].text
    assert reply["finding_id"] == runstate.finding_id_for(log.run_id, hit["hit_id"])

    (finding,) = state_rows(data_dir, "SELECT * FROM findings")
    assert (finding["status"], finding["severity"], finding["reason_code"]) == (
        "kept",
        "high",
        ACROSS,
    )
    assert finding["rationale"] == INDICATOR.reasons[1].text
    (stored,) = state_rows(data_dir, "SELECT * FROM hits")
    assert stored["claim_text"] == hit["claim_text"] and stored["tool_call_id"] == "tc-0001"
    assert json.loads(stored["source_refs"]) == hit["source_refs"]
    assert json.loads(stored["ocids"]) == [r["ocid"] for r in hit["source_refs"]]


def test_a_refused_finding_is_stored_as_rejected_with_no_text(run, indicators, data_dir):
    _, ctx, result = run
    hit_id = result["hits"][0]["hit_id"]
    reply = tools.record_finding(ctx, indicators, hit_id, "high", "Alpha stole four awards")

    assert reply["accepted"] is False and "hint" in reply
    assert [v["rule"] for v in reply["violations"]] == ["reason_code"]
    (row,) = state_rows(data_dir, "SELECT * FROM findings")
    assert row["status"] == "rejected" and row["rationale"] is None and row["finding_id"] is None
    assert row["reason_code"] is None and row["reason"] == "reason_code"
    assert state_rows(data_dir, "SELECT * FROM hits") == []


def test_a_fake_hit_id_is_refused_and_nothing_is_kept(run, indicators, data_dir):
    _, ctx, _ = run
    reply = tools.record_finding(ctx, indicators, "RW-fake", "low", CONTEXT)
    assert reply["accepted"] is False and reply["violations"][0]["rule"] == "unknown_hit"
    assert state_rows(data_dir, "SELECT * FROM findings WHERE status = 'kept'") == []


def test_a_model_can_retry_after_a_refusal(run, indicators, data_dir):
    _, ctx, result = run
    hit_id = result["hits"][0]["hit_id"]
    assert tools.record_finding(ctx, indicators, hit_id, "low", LOTS)["accepted"] is False
    assert tools.record_finding(ctx, indicators, hit_id, "low", ACROSS)["accepted"] is True
    statuses = [
        r["status"] for r in state_rows(data_dir, "SELECT status FROM findings ORDER BY id")
    ]
    assert statuses == ["rejected", "kept"]


def test_a_hit_cannot_be_decided_twice(run, indicators):
    _, ctx, result = run
    hit_id = result["hits"][0]["hit_id"]
    assert tools.record_finding(ctx, indicators, hit_id, "low", CONTEXT)["accepted"] is True
    again = tools.record_finding(ctx, indicators, hit_id, "high", CONTEXT)
    assert again["accepted"] is False and again["violations"][0]["rule"] == "already_decided"


def test_a_hit_from_another_run_is_unknown(run, indicators, data_dir, tmp_path):
    _, _, result = run
    other = RunLog.create(tmp_path / "logs")  # a different run that never called the indicator
    ctx = tools.RunContext(other.run_id, tmp_path / "logs", data_dir)
    reply = tools.record_finding(ctx, indicators, result["hits"][0]["hit_id"], "low", CONTEXT)
    assert reply["accepted"] is False and reply["violations"][0]["rule"] == "unknown_hit"


def test_no_active_run_means_no_findings(indicators):
    with pytest.raises(db.ToolInputError) as caught:
        tools.record_finding(None, indicators, "RW-x", "low", CONTEXT)
    assert caught.value.code == "no_active_run"


def test_a_tampered_log_blocks_recording(run, indicators, data_dir):
    log, ctx, result = run
    text = log.path.read_text(encoding="utf-8").replace("repeat_winner", "something_else", 1)
    log.path.write_text(text, encoding="utf-8")
    with pytest.raises(db.ToolInputError) as caught:
        tools.record_finding(ctx, indicators, result["hits"][0]["hit_id"], "low", CONTEXT)
    assert caught.value.code == "log_unverifiable"
    assert state_rows(data_dir, "SELECT * FROM findings") == []


def test_a_missing_log_blocks_recording(indicators, data_dir, tmp_path):
    ctx = tools.RunContext(str(uuid.uuid4()), tmp_path / "logs", data_dir)
    with pytest.raises(db.ToolInputError) as caught:
        tools.record_finding(ctx, indicators, "RW-x", "low", CONTEXT)
    assert caught.value.code == "log_unverifiable"


def test_a_database_made_before_reason_codes_is_upgraded(tmp_path: Path):
    old = sqlite3.connect(tmp_path / runstate.RUN_STATE_NAME)
    old.executescript(
        "CREATE TABLE findings (id INTEGER PRIMARY KEY AUTOINCREMENT, finding_id TEXT UNIQUE, "
        "run_id TEXT NOT NULL, hit_id TEXT NOT NULL, severity TEXT, rationale TEXT, "
        "status TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL);"
    )
    old.close()
    with closing(runstate.connect(tmp_path)) as upgraded:
        columns = {r["name"] for r in upgraded.execute("PRAGMA table_info(findings)")}
    assert "reason_code" in columns


# ---- the whole path: the model-facing tool, over the real protocol, through the wrapper ----


async def _end_to_end(data_dir: Path, logs_dir: Path):
    log = RunLog.create(logs_dir)
    ctx = tools.RunContext(log.run_id, logs_dir, data_dir)
    server = build_server(data_dir, CONFIG_PATH, ctx)
    async with create_connected_server_and_client_session(server) as client:
        session = LoggedSession(client, log, "flagbox")
        listed = (await client.list_tools()).tools
        args = {"buyer_key": "b1", "date_from": RANGE[0], "date_to": RANGE[1]}
        found = await session.call_tool("repeat_winner", args)
        assert found.structuredContent is not None
        hit_id = found.structuredContent["hits"][0]["hit_id"]
        fake = await session.call_tool(
            "record_finding", {"hit_id": "RW-invented", "severity": "high", "reason_code": CONTEXT}
        )
        prose = await session.call_tool(
            "record_finding",
            {"hit_id": hit_id, "severity": "high", "reason_code": "Alpha won four awards."},
        )
        good = await session.call_tool(
            "record_finding", {"hit_id": hit_id, "severity": "high", "reason_code": ACROSS}
        )
    log.finish("completed")
    return log, listed, found, fake, prose, good


def test_record_finding_end_to_end(data_dir: Path, tmp_path: Path):
    log, listed, found, fake, prose, good = asyncio.run(_end_to_end(data_dir, tmp_path / "logs"))

    tool = next(t for t in listed if t.name == "record_finding")
    assert set(tool.inputSchema["properties"]) == {"hit_id", "severity", "reason_code"}
    assert "rationale" not in tool.inputSchema["properties"]  # the model writes no text
    assert tool.description is not None and f"- {ACROSS}:" in tool.description
    assert found.structuredContent is not None
    assert found.structuredContent["hits"][0]["reason_codes"]

    assert fake.structuredContent is not None and fake.structuredContent["accepted"] is False
    assert prose.structuredContent is not None
    assert [v["rule"] for v in prose.structuredContent["violations"]] == ["reason_code"]
    assert good.structuredContent is not None and good.structuredContent["accepted"] is True
    assert good.structuredContent["rationale"] == INDICATOR.reasons[1].text
    assert good.structuredContent["tool_call_id"] == "tc-0004"

    assert verify_log(log.path).ok  # every attempt, accepted or refused, is in the chain
    statuses = [
        r["status"] for r in state_rows(data_dir, "SELECT status FROM findings ORDER BY id")
    ]
    assert statuses == ["rejected", "rejected", "kept"]


async def _without_run(data_dir: Path):
    server = build_server(data_dir, CONFIG_PATH)
    async with create_connected_server_and_client_session(server) as client:
        return await client.call_tool(
            "record_finding", {"hit_id": "RW-x", "severity": "low", "reason_code": CONTEXT}
        )


def test_the_server_refuses_findings_when_started_without_a_run(data_dir: Path):
    reply = asyncio.run(_without_run(data_dir))
    assert reply.structuredContent is not None
    assert reply.structuredContent["error"] == "no_active_run"
