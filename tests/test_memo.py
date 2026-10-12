import hashlib
import json
import re
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from flagbox.agent.runlog import RunLog
from flagbox.agent.verify import _BANNED, check_summary, known_supplier_terms
from flagbox.memo.builder import CAVEATS, _cell
from flagbox.server import db, runstate, tools
from flagbox.server.indicators.repeat_winner import INDICATOR, Params

RANGE = ("2024-01-01", "2024-12-31")
ACROSS = "repeated_across_tenders"
LOTS = "lots_of_one_tender"
GOOD_SUMMARY = (
    "Some suppliers received a share of this buyer's award value at or above the review "
    "threshold. A human reviewer should examine these patterns against the source records."
)
ALPHA_CLAIM = (
    "Alpha Ltd (ID S1) received 4 of 10 awards, in 3 tender processes, "
    "and 60% of award value from this buyer, 2024-01-01 to 2024-12-31."
)
BETA_CLAIM = (
    "Supplier S2 (name withheld) received 3 of 10 awards, in 1 tender process, "
    "and 15% of award value from this buyer, 2024-01-01 to 2024-12-31."
)


# ---- a run with a realistic log: coverage, an indicator, a plan, findings, a v ----------


@pytest.fixture
def make_run(data_dir: Path, conn, cfg, indicators, tmp_path: Path):
    def build(record_findings: bool = True, gaps=(), skipped=(), undecided=0):
        logs_dir, drafts_dir = tmp_path / "logs", tmp_path / "drafts"
        log = RunLog.create(
            logs_dir,
            buyer_query="Buyer One",
            date_from=RANGE[0],
            date_to=RANGE[1],
            model="test-model",
            num_ctx=16384,
            prompt_version="test-prompts",
        )
        ctx = tools.RunContext(log.run_id, logs_dir, data_dir, drafts_dir)
        log.append("buyer_selected", buyer_key="b1", name="Buyer One", how="exact_name")

        def logged(tool: str, result: dict[str, Any]) -> None:
            log.append(
                "tool_call",
                tool_call_id=log.next_tool_call_id(),
                server="flagbox",
                tool=tool,
                arguments={},
                is_error=False,
                result=result,
            )

        logged("data_coverage", tools.data_coverage(conn, cfg, indicators, "b1", *RANGE))
        params = Params.model_validate(cfg.indicators["repeat_winner"])
        result = tools.run_indicator(conn, cfg, INDICATOR, params, "b1", *RANGE)
        logged("repeat_winner", result)
        logged(
            "fetch_buyer_awards", tools.fetch_buyer_awards(conn, cfg, "b1", *RANGE, page_size=50)
        )
        log.append("plan", indicators=["repeat_winner"], skipped=list(skipped), fallback=False)
        if record_findings:
            alpha, beta = result["hits"]
            assert tools.record_finding(ctx, indicators, alpha["hit_id"], "high", ACROSS)[
                "accepted"
            ]
            assert tools.record_finding(ctx, indicators, beta["hit_id"], "low", LOTS)["accepted"]
        log.append(
            "verify",
            chain_ok=True,
            kept=2 if record_findings else 0,
            demoted=[],
            hits_seen=2,
            undecided=["RW-x"] * undecided,
            checks=[],
            gaps=list(gaps),
        )
        return ctx, log

    return build


def draft(ctx: tools.RunContext, summary: str | None = None) -> dict[str, Any]:
    return tools.draft_memo(ctx, ctx.run_id, summary)


def read(reply: dict[str, Any]) -> str:
    return Path(reply["path"]).read_text(encoding="utf-8")


# ---- the file, its hash, and determinism ----------


def test_the_draft_is_written_and_its_sha256_is_returned(make_run, data_dir):
    ctx, log = make_run()
    reply = draft(ctx)
    path = Path(reply["path"])

    assert reply["drafted"] is True and path == ctx.drafts_dir / f"{ctx.run_id}.md"
    data = path.read_bytes()
    assert reply["sha256"] == hashlib.sha256(data).hexdigest() and reply["bytes"] == len(data)
    assert b"\r" not in data  # the same bytes on Windows and Linux
    with closing(runstate.connect(data_dir)) as state:
        row = state.execute("SELECT * FROM memos WHERE run_id = ?", (ctx.run_id,)).fetchone()
    assert row["sha256"] == reply["sha256"] and row["summary_source"] == "fixed"


def test_drafting_twice_gives_byte_identical_memos(make_run):
    ctx, _ = make_run()
    first, second = draft(ctx, GOOD_SUMMARY), draft(ctx, GOOD_SUMMARY)
    assert first["sha256"] == second["sha256"]


def test_a_different_summary_changes_the_hash(make_run):
    ctx, _ = make_run()
    assert draft(ctx)["sha256"] != draft(ctx, GOOD_SUMMARY)["sha256"]


def test_the_log_head_hash_at_drafting_is_in_the_memo(make_run):
    ctx, log = make_run()
    reply = draft(ctx)
    assert reply["log_head_hash"] == log.head_hash and f"`{log.head_hash}`" in read(reply)


# ---- what is in the memo ----------


def test_the_memo_has_every_required_section(make_run):
    ctx, _ = make_run()
    text = read(draft(ctx))
    for heading in (
        "# Screening memo (DRAFT): Buyer One",
        "## Summary",
        "## Findings",
        "## What the data covered",
        "## Checks run",
        "## Checks skipped or not completed",
        "## Appendix A: hits the model dismissed",
        "## Appendix B: data-quality notes",
        "## Caveats",
        "## Sources",
    ):
        assert heading in text, heading
    assert "**Not approved. Not filed.**" in text
    for caveat in CAVEATS:
        assert caveat in text


def test_findings_carry_the_verbatim_claim_fixed_reason_and_sources(make_run):
    ctx, _ = make_run()
    text = read(draft(ctx))
    assert f"### 1. {ALPHA_CLAIM}" in text and f"### 2. {BETA_CLAIM}" in text
    assert "- Severity: high" in text and "- Severity: low" in text
    assert INDICATOR.reasons[1].text in text and INDICATOR.reasons[0].text in text
    # Alpha's three tender processes, each with its source file, line and portal page
    for line_no, number, ocid in ((1, 1001, "p1"), (2, 1002, "p2"), (3, 1003, "p3")):
        page = f"[tender page](https://tenders.go.ke/tenders/{number})"
        row = f"| {ocid} | 2024.jsonl.gz | {line_no} | {page} |"
        assert row in text
    assert "| p4 | 2024.jsonl.gz | 4 | [tender page](https://tenders.go.ke/tenders/1004) |" in text


def test_a_withheld_supplier_name_never_reaches_the_memo(make_run):
    ctx, _ = make_run()
    text = read(draft(ctx, GOOD_SUMMARY))
    assert "JOHN DOE" not in text and "Supplier S2 (name withheld)" in text


def test_coverage_checks_and_data_quality_come_from_the_log(make_run):
    ctx, _ = make_run()
    text = read(draft(ctx))
    assert "| Tender processes with a closing date in range | 5 |" in text
    assert "| Awards in range | 14 |" in text and "| Awards with a supplier ID | 93% |" in text
    assert "| repeat_winner | 71% | yes |" in text and "at least 60% of the awards" in text
    assert (
        "| repeat_winner | 2 | 10 | min_value_share 0.1, min_wins 3, "
        "value_sanity_cap_kes 10000000000.0 |" in text
    )
    assert (
        "awards left out of its statistics: 1 zero or missing value, 1 not in Kenya shillings, "
        "1 above the value cap, 1 no supplier recorded." in text
    )


def test_sources_list_each_file_with_its_url_and_hash(make_run):
    ctx, _ = make_run()
    text = read(draft(ctx))
    match = re.search(
        r"\| 2024\.jsonl\.gz \| https://example\.test/2024\.jsonl\.gz \| `([0-9a-f]{64})` \|", text
    )
    assert match is not None


def test_skipped_checks_and_gaps_are_reported(make_run):
    ctx, _ = make_run(
        gaps=["tool-call limit reached"],
        skipped=[{"indicator": "value_outlier", "reason": "Not enough usable data."}],
        undecided=2,
    )
    text = read(draft(ctx))
    assert "- Skipped value_outlier: Not enough usable data." in text
    assert "- Not completed: tool-call limit reached" in text
    assert "- 2 hit(s) received no decision" in text
    assert "Nothing was skipped." not in text


def test_a_clean_run_says_nothing_was_skipped(make_run):
    ctx, _ = make_run()
    assert "Nothing was skipped. Every planned check ran" in read(draft(ctx))


def test_dismissed_hits_appear_in_the_appendix(make_run, data_dir):
    ctx, _ = make_run(record_findings=False)
    with closing(runstate.connect(data_dir)) as state:
        state.execute(
            "INSERT INTO hits VALUES "
            "(?, 'RW-d', 'tc-0002', 'repeat_winner', ?, '[]', '[]', '[]', '{}')",
            (ctx.run_id, "Dismissed claim text."),
        )
        state.execute(
            "INSERT INTO findings (run_id, hit_id, rationale, reason_code, status, created_at) "
            "VALUES (?, 'RW-d', 'Looks like lots of one tender.', "
            "'lots_of_one_tender', 'dismissed', 'now')",
            (ctx.run_id,),
        )
        state.commit()
    assert "- Dismissed claim text. Reason: Looks like lots of one tender." in read(draft(ctx))


def test_no_findings_gives_a_memo_that_says_why(make_run):
    ctx, _ = make_run(record_findings=False)
    reply = draft(ctx)
    text = read(reply)
    assert reply["findings"] == 0
    assert "No finding was recorded." in text and "produced no finding" in text


def test_table_cells_cannot_break_out_of_the_table():
    assert _cell("a | b\nc") == "a \\| b c"


def test_the_whole_memo_is_free_of_accusing_words_and_personal_data(make_run):
    ctx, _ = make_run()
    for summary in (None, GOOD_SUMMARY):
        text = read(draft(ctx, summary))
        assert _BANNED.search(text) is None
        assert "@" not in text and "JOHN" not in text


# ---- rule 3: the summary paragraph ----------


def test_a_summary_naming_a_supplier_not_in_any_finding_is_rejected(make_run, data_dir):
    ctx, _ = make_run()  # Gamma appears in the run's tool output but has no finding
    reply = draft(ctx, "Gamma Ltd (ID S3) also appears often. A human reviewer should look at it.")
    assert reply["drafted"] is False
    assert "uncited_supplier" in {v["rule"] for v in reply["violations"]}
    assert not (ctx.drafts_dir / f"{ctx.run_id}.md").exists()  # nothing was written


def test_a_cited_supplier_label_is_allowed_exactly_as_the_claim_prints_it(make_run):
    ctx, _ = make_run()
    text = (
        "Alpha Ltd (ID S1) is the largest. A human reviewer should examine the records "
        "for this buyer."
    )
    assert draft(ctx, text)["drafted"] is True
    partial = "Alpha is the largest. A human reviewer should examine the records for this buyer."
    reply = draft(ctx, partial)
    assert reply["drafted"] is False  # the bare name is not the printed label
    assert "uncited_supplier" in {v["rule"] for v in reply["violations"]}


@pytest.mark.parametrize(
    ("summary", "rule"),
    [
        (
            "Seven suppliers hold much of the value. A human reviewer should examine them.",
            "numbers",
        ),
        (
            "Suppliers hold 77% of the value. A human reviewer should examine them closely.",
            "number",
        ),
        (
            "These suppliers look fraudulent. A human reviewer should examine them closely.",
            "banned_word",
        ),
        (
            "The records mention ocds-5whusi-999-X/1. A human reviewer should examine them.",
            "uncited_ocid",
        ),
        (
            "These patterns involve Mary Nobody. A human reviewer should examine them closely.",
            "proper_noun",
        ),
        ("Too short.", "format"),
        (
            "First line of the summary goes here.\nSecond line goes here as well, not allowed.",
            "format",
        ),
        ("x " * 400, "format"),
    ],
)
def test_summaries_that_break_a_rule_are_rejected(make_run, summary, rule):
    ctx, _ = make_run()
    reply = draft(ctx, summary)
    assert reply["drafted"] is False and rule in {v["rule"] for v in reply["violations"]}
    assert not (ctx.drafts_dir / f"{ctx.run_id}.md").exists()


def test_numbers_that_appear_in_a_claim_are_allowed(make_run):
    ctx, _ = make_run()
    text = "One supplier holds 60% of the value in this range. A human reviewer should examine it."
    assert draft(ctx, text)["drafted"] is True


def test_a_cited_ocid_and_the_buyer_name_are_allowed(make_run):
    ctx, _ = make_run()
    text = "Awards by Buyer One include p1 and p2. A human reviewer should examine those records."
    assert draft(ctx, text)["drafted"] is True  # "p1" is an OCID that belongs to a finding


def test_check_summary_directly_reports_all_problems_at_once():
    problems = check_summary(
        "Fraud by Bob involving 9 suppliers, as the second one admitted, near ocds-5whusi-1.",
        ["Alpha Ltd (ID S1) received 4 of 10 awards."],
        {"p1"},
        {"Alpha Ltd (ID S1)"},
        {"gamma"},
        {"S3"},
        "Buyer One",
    )
    assert {v.rule for v in problems} >= {"banned_word", "number", "uncited_ocid", "proper_noun"}


# ---- guards on the tool itself ----------


def test_run_id_must_be_a_uuid_and_the_runs_own(make_run):
    ctx, _ = make_run()
    for bad in ("../../x", "not-a-uuid", ctx.run_id.upper()):
        with pytest.raises(db.ToolInputError) as caught:
            tools.draft_memo(ctx, bad)
        assert caught.value.code == "bad_run_id"
    with pytest.raises(db.ToolInputError) as caught:
        tools.draft_memo(ctx, str(uuid.uuid4()))
    assert caught.value.code == "wrong_run"


def test_no_active_run_means_no_memo():
    with pytest.raises(db.ToolInputError) as caught:
        tools.draft_memo(None, str(uuid.uuid4()))
    assert caught.value.code == "no_active_run"


def test_a_tampered_log_blocks_drafting(make_run):
    ctx, log = make_run()
    log.path.write_text(
        log.path.read_text(encoding="utf-8").replace("repeat_winner", "something_else", 1),
        encoding="utf-8",
    )
    with pytest.raises(db.ToolInputError) as caught:
        draft(ctx)
    assert caught.value.code == "log_unverifiable"
    assert not (ctx.drafts_dir / f"{ctx.run_id}.md").exists()


def test_supplier_terms_for_the_check_include_suppliers_without_findings(make_run):
    ctx, log = make_run()
    from flagbox.agent.runlog import iter_records

    phrases, _ = known_supplier_terms(list(iter_records(log.path)))
    assert "gamma" in phrases and json.dumps(
        sorted(phrases)
    )  # Gamma is known, so it can be refused
