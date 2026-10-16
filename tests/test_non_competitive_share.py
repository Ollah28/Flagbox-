from contextlib import closing
from pathlib import Path

import pytest
from helpers import CONFIG_PATH, build_snapshot, make_release

from flagbox.agent.verify import check_rationale
from flagbox.server import db, tools
from flagbox.server.config import load_config
from flagbox.server.indicators.non_competitive_share import INDICATOR, REASONS, Params

RANGE = ("2024-01-01", "2024-12-31")
KES = "KES"


def _releases(direct: int, selective: int, open_: int, direct_value: float = 100.0):
    """One award per process; direct awards carry `direct_value`, the others 100 each."""
    out = []
    n = 0
    for method, count, value in (
        ("direct", direct, direct_value),
        ("selective", selective, 100.0),
        ("open", open_, 100.0),
    ):
        for _ in range(count):
            n += 1
            out.append(
                make_release(
                    f"p{n}", [("a1", value, KES, [("S1", "Alpha Ltd")])], method=method, number=n
                )
            )
    return out


def _run(tmp_path: Path, releases, buyer: str = "b1"):
    data_dir = build_snapshot(tmp_path, releases)
    cfg = load_config(CONFIG_PATH)
    params = Params.model_validate(cfg.indicators["non_competitive_share"])
    with closing(db.connect_readonly(data_dir / "flagbox.sqlite")) as conn:
        return tools.run_indicator(conn, cfg, INDICATOR, params, buyer, *RANGE)


def test_a_buyer_over_the_threshold_gets_one_hit_with_a_fixed_claim(tmp_path):
    result = _run(tmp_path, _releases(direct=2, selective=2, open_=4))  # 4 of 8, 50% of value
    (hit,) = result["hits"]
    assert hit["claim_text"] == (
        "4 of 8 awards, carrying 50% of award value, were made by non-open methods "
        "(direct, selective), 2024-01-01 to 2024-12-31."
    )
    assert hit["hit_id"].startswith("NC-") and hit["metrics"]["by_method"] == {
        "direct": 2,
        "selective": 2,
    }
    assert hit["severity"] == "medium"  # 50% is at least 40% and under 60%
    assert hit["reason_codes"] == ["direct_awards_present", "needs_context"]
    assert len(hit["source_refs"]) == 4 and result["truncated"] is False


def test_open_buyers_and_small_counts_are_not_hits(tmp_path):
    assert _run(tmp_path, _releases(0, 0, 8))["hits"] == []  # nothing non-open
    only_two = tmp_path / "two"
    only_two.mkdir()
    # 2 direct awards carrying most of the value is still below the 3-award minimum
    assert _run(only_two, _releases(2, 0, 2, direct_value=1000.0))["hits"] == []


def test_a_small_value_share_is_not_a_hit(tmp_path):
    # 3 direct awards of 100 against 40 open awards of 100: 7% of value, under 25%
    assert _run(tmp_path, _releases(3, 0, 40))["hits"] == []


def test_severity_bands_and_the_restricted_only_reason(tmp_path):
    result = _run(tmp_path, _releases(0, 4, 1))  # 4 of 5 selective: 80% of value
    (hit,) = result["hits"]
    assert hit["severity"] == "high"
    assert hit["reason_codes"] == ["restricted_bidding_only", "needs_context"]


def test_data_quality_exclusions_are_counted_not_flagged(tmp_path):
    releases = _releases(3, 0, 3)
    releases.append(make_release("z1", [("a1", 0, KES, [])], method="direct", number=90))
    releases.append(make_release("z2", [("a1", 50, "USD", [])], method="direct", number=91))
    releases.append(make_release("z3", [("a1", 50, KES, [])], method=None, number=92))
    result = _run(tmp_path, releases)
    assert result["excluded_count"] == {
        "no_value_or_zero": 1,
        "other_currency": 1,
        "above_value_cap": 0,
        "no_method": 1,
    }
    assert result["eligible_awards"] == 6


def test_every_fixed_reason_obeys_the_wording_rules():
    for reason in REASONS:
        assert check_rationale(reason.text) == [], reason.code


def test_the_tool_takes_only_a_buyer_and_a_range():
    import asyncio

    from flagbox.server.server import build_server

    async def listed(data_dir: Path):
        from mcp.shared.memory import create_connected_server_and_client_session

        server = build_server(data_dir, CONFIG_PATH)
        async with create_connected_server_and_client_session(server) as client:
            return (await client.list_tools()).tools

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tools_listed = asyncio.run(listed(build_snapshot(Path(tmp), _releases(3, 0, 3))))
    by_name = {t.name: t for t in tools_listed}
    assert set(by_name["non_competitive_share"].inputSchema["properties"]) == {
        "buyer_key",
        "date_from",
        "date_to",
    }


@pytest.mark.parametrize("name", ["methods", "min_awards", "min_value_share"])
def test_thresholds_exist_in_config(name):
    assert name in load_config(CONFIG_PATH).indicators["non_competitive_share"]
