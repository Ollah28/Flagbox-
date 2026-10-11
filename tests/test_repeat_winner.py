import json

import pytest

from flagbox.server import tools
from flagbox.server.indicators.repeat_winner import INDICATOR, Params

RANGE = ("2024-01-01", "2024-12-31")

ALPHA_CLAIM = (
    "Alpha Ltd (ID S1) received 4 of 10 awards, in 3 tender processes, "
    "and 60% of award value from this buyer, 2024-01-01 to 2024-12-31."
)
BETA_CLAIM = (
    "Supplier S2 (name withheld) received 3 of 10 awards, in 1 tender process, "
    "and 15% of award value from this buyer, 2024-01-01 to 2024-12-31."
)


@pytest.fixture
def params(cfg):
    return Params.model_validate(cfg.indicators["repeat_winner"])


def run(conn, cfg, params, buyer="b1", date_range=RANGE):
    return tools.run_indicator(conn, cfg, INDICATOR, params, buyer, *date_range)


def test_hits_use_the_fixed_claim_template(conn, cfg, params):
    result = run(conn, cfg, params)
    assert [h["claim_text"] for h in result["hits"]] == [ALPHA_CLAIM, BETA_CLAIM]
    assert result["hits_total"] == 2
    assert result["truncated"] is False


def test_a_supplier_below_the_win_minimum_is_not_a_hit(conn, cfg, params):
    # Gamma has 20% of the value but only 2 awards (minimum is 3).
    result = run(conn, cfg, params)
    assert "S3" not in {h["supplier_key"] for h in result["hits"]}
    assert "Gamma" not in json.dumps(result["hits"])


def test_a_withheld_name_never_appears_in_the_output(conn, cfg, params):
    text = json.dumps(run(conn, cfg, params))
    assert "JOHN" not in text and "DOE" not in text
    assert "Supplier S2 (name withheld)" in text


def test_metrics_and_params_used(conn, cfg, params):
    result = run(conn, cfg, params)
    assert result["hits"][0]["metrics"] == {
        "wins": 4,
        "total_awards": 10,
        "processes": 3,
        "count_share_pct": 40,
        "value_share_pct": 60,
    }
    assert result["params_used"] == {
        "min_wins": 3,
        "min_value_share": 0.1,
        "value_sanity_cap_kes": 10_000_000_000.0,
    }
    assert result["eligible_awards"] == 10
    assert result["eligible_value_kes"] == 1000


def test_data_quality_exclusions_are_counted_not_flagged(conn, cfg, params):
    assert run(conn, cfg, params)["excluded_count"] == {
        "no_value_or_zero": 1,
        "other_currency": 1,
        "above_value_cap": 1,
        "no_supplier": 1,
    }


def test_range_and_buyer_scoping(conn, cfg, params):
    # p6 (closed 2023) is outside the range; b2's award belongs to another buyer.
    result = run(conn, cfg, params)
    assert result["hits"][0]["metrics"]["wins"] == 4
    other = run(conn, cfg, params, buyer="b2")
    assert other["hits"] == [] and other["eligible_awards"] == 1


def test_evidence_lists_each_process_once_with_a_source_reference(conn, cfg, params):
    beta = run(conn, cfg, params)["hits"][1]
    assert beta["metrics"]["wins"] == 3 and beta["evidence_processes_total"] == 1
    assert len(beta["source_refs"]) == 1
    ref = beta["source_refs"][0]
    assert ref["ocid"] == "p4" and ref["file"] == "2024.jsonl.gz" and ref["line_no"] == 4
    assert ref["portal_url"] == "https://tenders.go.ke/tenders/1004"


def test_sources_block_gives_dataset_url_and_hash_once(conn, cfg, params):
    sources = run(conn, cfg, params)["sources"]
    assert list(sources) == ["2024.jsonl.gz"]
    assert sources["2024.jsonl.gz"]["dataset_url"].startswith("https://")
    assert len(sources["2024.jsonl.gz"]["sha256"]) == 64


def test_hit_ids_are_stable_and_depend_on_the_range(conn, cfg, params):
    first = [h["hit_id"] for h in run(conn, cfg, params)["hits"]]
    again = [h["hit_id"] for h in run(conn, cfg, params)["hits"]]
    assert first == again and len(set(first)) == 2
    assert all(i.startswith("RW-") for i in first)
    narrower = run(conn, cfg, params, date_range=("2024-01-01", "2024-05-31"))
    assert not set(first) & {h["hit_id"] for h in narrower["hits"]}


def test_no_hit_is_a_valid_result_with_a_message(conn, cfg, params):
    result = run(conn, cfg, params, date_range=("2024-05-01", "2024-05-31"))  # only Beta's process
    assert [h["supplier_key"] for h in result["hits"]] == ["S2"]
    empty = run(conn, cfg, params, date_range=("2019-01-01", "2019-12-31"))
    assert empty["hits"] == [] and "not an error" in empty["message"]


def test_max_hits_truncates_and_says_so(conn, cfg, params):
    limited = params.model_copy(update={"max_hits": 1})
    result = run(conn, cfg, limited)
    assert len(result["hits"]) == 1 and result["hits_total"] == 2 and result["truncated"] is True
