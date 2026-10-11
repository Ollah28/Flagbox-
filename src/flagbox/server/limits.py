"""Keeps tool output small and safe to show a model with a 16k context window."""

import json
import re
from typing import Any

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
CHARS_PER_TOKEN = 3.5  # rough, deliberately pessimistic for JSON with many short tokens


def estimate_tokens(payload: Any) -> int:
    return int(len(json.dumps(payload, ensure_ascii=False)) / CHARS_PER_TOKEN) + 1


def sanitize_text(text: str | None, max_chars: int) -> str | None:
    """Record text is untrusted data. Flatten whitespace and control characters, then truncate."""
    if text is None:
        return None
    flat = _CONTROL_CHARS.sub(" ", text)
    flat = " ".join(flat.split())
    return flat if len(flat) <= max_chars else flat[: max_chars - 3] + "..."


def fit(result: dict[str, Any], list_key: str, max_tokens: int) -> dict[str, Any]:
    """Drop items from the end of result[list_key] until the whole result fits max_tokens.

    Adds `returned`, `truncated` and (if the caller set it) leaves `total` alone, so the model
    always knows when it is looking at part of the data and never gets a silently cut result.
    """
    items: list[Any] = list(result[list_key])
    result[list_key] = items
    result["truncated"] = bool(result.get("truncated", False))
    result["returned"] = len(items)  # set first so the size check below counts it
    while len(items) > 1 and estimate_tokens(result) > max_tokens:
        items.pop()
        result["truncated"] = True
        result["returned"] = len(items)
    return result
