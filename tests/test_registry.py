import asyncio
import json
from pathlib import Path

import pytest
from helpers import CONFIG_PATH
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent

from flagbox.server.config import IndicatorParams, load_config
from flagbox.server.indicators import discover
from flagbox.server.server import build_server

THRESHOLD_NAMES = {"min_wins", "min_value_share", "max_hits", "max_ocids_per_hit"}


def test_discover_finds_repeat_winner():
    found = discover()
    assert "repeat_winner" in found
    assert issubclass(found["repeat_winner"].params_model, IndicatorParams)


def test_every_indicator_has_a_valid_config_block():
    cfg = load_config(CONFIG_PATH)
    for name, indicator in discover().items():
        assert name in cfg.indicators, f"no block for {name} in config/indicators.yaml"
        params = indicator.params_model.model_validate(cfg.indicators[name])
        assert (params.rationale or params.source or "").strip()


def test_every_indicator_need_is_a_known_field():
    from flagbox.server.db import FLAG_SQL

    for indicator in discover().values():
        assert set(indicator.needs) <= set(FLAG_SQL)


def test_a_config_block_without_a_reason_is_refused(tmp_path: Path):
    text = CONFIG_PATH.read_text(encoding="utf-8")
    broken = tmp_path / "indicators.yaml"
    # Remove the repeat_winner rationale block (everything from its key to the end of the file).
    start = text.index("    rationale: >\n      Decided 11 Oct")
    broken.write_text(text[:start], encoding="utf-8")
    cfg = load_config(broken)
    with pytest.raises(ValueError, match="source.*rationale"):
        discover()["repeat_winner"].params_model.model_validate(cfg.indicators["repeat_winner"])


def test_unknown_config_keys_are_refused(tmp_path: Path):
    path = tmp_path / "indicators.yaml"
    path.write_text(
        CONFIG_PATH.read_text(encoding="utf-8").replace(
            "min_wins: 3", "min_wins: 3\n    sneaky: 1"
        ),
        encoding="utf-8",
    )
    cfg = load_config(path)
    with pytest.raises(ValueError):
        discover()["repeat_winner"].params_model.model_validate(cfg.indicators["repeat_winner"])


def test_server_and_config_must_agree(data_dir: Path, tmp_path: Path):
    path = tmp_path / "indicators.yaml"
    path.write_text(
        CONFIG_PATH.read_text(encoding="utf-8") + "  mystery_indicator:\n    rationale: x\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="disagree"):
        build_server(data_dir, path)


async def _list_and_call(data_dir: Path):
    server = build_server(data_dir, CONFIG_PATH)
    async with create_connected_server_and_client_session(server) as client:
        listed = (await client.list_tools()).tools
        plain = await client.call_tool(
            "repeat_winner",
            {"buyer_key": "b1", "date_from": "2024-01-01", "date_to": "2024-12-31"},
        )
        pushy = await client.call_tool(
            "repeat_winner",
            {
                "buyer_key": "b1",
                "date_from": "2024-01-01",
                "date_to": "2024-12-31",
                "min_wins": 1,
                "min_value_share": 0.001,
                "max_hits": 99,
            },
        )
        return listed, plain, pushy


def test_server_registers_one_tool_per_indicator_with_no_threshold_arguments(data_dir: Path):
    listed, plain, pushy = asyncio.run(_list_and_call(data_dir))
    names = {t.name for t in listed}
    assert {"find_buyer", "data_coverage", "fetch_buyer_awards", "repeat_winner"} <= names
    # No write tools exist yet, and the indicator takes only buyer and range.
    by_name = {t.name: t for t in listed}
    assert set(by_name["repeat_winner"].inputSchema["properties"]) == {
        "buyer_key",
        "date_from",
        "date_to",
    }
    assert not any(THRESHOLD_NAMES & set(t.inputSchema.get("properties", {})) for t in listed)
    # Passing thresholds anyway changes nothing: they are ignored, not obeyed.
    assert plain.isError is False and pushy.isError is False
    assert plain.structuredContent == pushy.structuredContent
    assert plain.structuredContent is not None
    assert plain.structuredContent["params_used"]["min_wins"] == 3
    block = plain.content[0]
    assert isinstance(block, TextContent)
    assert json.loads(block.text)["hits_total"] == 2
