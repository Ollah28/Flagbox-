import json
from pathlib import Path

import pytest
from helpers import CONFIG_PATH

from flagbox.baseline import BaselineError, run_baseline
from flagbox.cli import main

RANGE = ("2024-01-01", "2024-12-31")


def test_the_baseline_keeps_every_hit_with_no_model_and_no_judgement(data_dir: Path):
    result = run_baseline(data_dir, CONFIG_PATH, "Buyer One", *RANGE)
    assert result["model_calls"] == 0 and result["indicators_run"] == ["repeat_winner"]
    assert [f["decision"] for f in result["findings"]] == ["kept without triage"] * 2
    assert all(f["severity"] is None and f["reason_code"] is None for f in result["findings"])
    assert {o for f in result["findings"] for o in f["ocids"]} == {"p1", "p2", "p3", "p4"}
    assert result["checks"] == [
        {"indicator": "repeat_winner", "hits_total": 2, "awards_checked": 10}
    ]


def test_the_baseline_uses_the_same_hits_as_the_agent_tools(data_dir: Path):
    from contextlib import closing

    from flagbox.server import db, tools
    from flagbox.server.config import load_config
    from flagbox.server.indicators import discover

    cfg, found = load_config(CONFIG_PATH), discover()
    params = found["repeat_winner"].params_model.model_validate(cfg.indicators["repeat_winner"])
    with closing(db.connect_readonly(data_dir / "flagbox.sqlite")) as conn:
        agent = tools.run_indicator(conn, cfg, found["repeat_winner"], params, "b1", *RANGE)
    baseline = run_baseline(data_dir, CONFIG_PATH, "Buyer One", *RANGE)
    assert [f["hit_id"] for f in baseline["findings"]] == [h["hit_id"] for h in agent["hits"]]


def test_an_unknown_or_ambiguous_buyer_is_an_error_not_a_guess(data_dir: Path):
    with pytest.raises(BaselineError, match="No single buyer"):
        run_baseline(data_dir, CONFIG_PATH, "Buyer", *RANGE)  # matches both Buyer One and Two
    with pytest.raises(BaselineError):
        run_baseline(data_dir, CONFIG_PATH, "zzzz", *RANGE)
    with pytest.raises(BaselineError, match="No snapshot"):
        run_baseline(data_dir / "nowhere", CONFIG_PATH, "Buyer One", *RANGE)


def test_bad_dates_are_an_error(data_dir: Path):
    with pytest.raises(BaselineError):
        run_baseline(data_dir, CONFIG_PATH, "Buyer One", "2024-12-31", "2024-01-01")


def test_a_range_with_no_awards_gives_no_findings(data_dir: Path):
    result = run_baseline(data_dir, CONFIG_PATH, "Buyer One", "2019-01-01", "2019-12-31")
    assert result["findings"] == [] and result["indicators_run"] == []


def test_the_command_prints_text_and_json(data_dir: Path, capsys):
    args = ["baseline", "--buyer", "Buyer One", "--from", RANGE[0], "--to", RANGE[1]]
    base = ["--data-dir", str(data_dir), "--config", str(CONFIG_PATH)]
    assert main([*args, *base]) == 0
    text = capsys.readouterr().out
    assert (
        "BASELINE (no model)" in text and "kept without triage" in text and "0 model calls" in text
    )
    assert main([*args, *base, "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["findings"]) == 2
    assert main(["baseline", "--buyer", "zzzz", "--from", RANGE[0], "--to", RANGE[1], *base]) == 1
