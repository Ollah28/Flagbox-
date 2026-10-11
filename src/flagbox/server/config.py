"""Loads and validates config/indicators.yaml (the only home of indicator thresholds)."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Block(BaseModel):
    """Every config block must say why its numbers are what they are."""

    model_config = ConfigDict(extra="forbid")

    source: str | None = None
    rationale: str | None = None

    @model_validator(mode="after")
    def _needs_a_reason(self) -> "_Block":
        if not ((self.source or "").strip() or (self.rationale or "").strip()):
            raise ValueError("every config block needs a `source:` or `rationale:` line")
        return self


class IndicatorParams(_Block):
    """Base class for an indicator's parameter block; indicators add their own fields."""


class Limits(_Block):
    max_output_tokens: int = Field(gt=0)
    max_title_chars: int = Field(gt=0)


class DataQuality(_Block):
    currency: str = Field(min_length=3, max_length=3)
    value_sanity_cap_kes: float = Field(gt=0)
    min_joint_coverage: float = Field(gt=0, le=1)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limits: Limits
    data_quality: DataQuality
    indicators: dict[str, dict[str, Any]]


def load_config(path: Path) -> Config:
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return Config.model_validate(raw)
