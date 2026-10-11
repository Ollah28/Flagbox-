"""Indicator registry.

Adding an indicator = one new module in this folder that defines `INDICATOR`, one block in
config/indicators.yaml, one claim template and one fixture test. Nothing else changes: the
server registers a tool for every indicator it finds here, and `data_coverage` lists it.
"""

import importlib
import pkgutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from flagbox.server.config import DataQuality, IndicatorParams


@dataclass(frozen=True)
class Scope:
    """What an indicator is asked about. These three values are ALL a tool call can set."""

    buyer_key: str
    date_from: str
    date_to: str


@dataclass(frozen=True)
class Context:
    conn: sqlite3.Connection
    scope: Scope
    quality: DataQuality
    max_title_chars: int


@dataclass(frozen=True)
class Reason:
    """One fixed explanation a finding may carry (SPEC D31).

    The model never writes an explanation. It picks a `code`; the memo and the stored finding use
    `text`, which is written here, in code review, once. `applies` decides from the hit itself
    whether the text is true for that hit, so a model cannot attach a reason the data does not
    support.
    """

    code: str
    text: str
    applies: Callable[[dict[str, Any]], bool]


@dataclass(frozen=True)
class Indicator:
    name: str
    description: str  # shown to the model as the tool description
    needs: tuple[str, ...]  # usable-field names (db.FLAG_SQL) the indicator relies on
    params_model: type[IndicatorParams]
    run: Callable[[Context, Any], dict[str, Any]]
    reasons: tuple[Reason, ...] = ()


def discover() -> dict[str, Indicator]:
    """Import every module in this package that defines INDICATOR."""
    found: dict[str, Indicator] = {}
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        indicator = getattr(module, "INDICATOR", None)
        if isinstance(indicator, Indicator):
            found[indicator.name] = indicator
    return dict(sorted(found.items()))
