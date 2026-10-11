"""The language model: settings, a check that Ollama is up, and one place that builds the client.

SPEC section 7: num_ctx 16384, Qwen3 thinking off, temperature 0, 180 s per Ollama call.
"""

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from langchain_ollama import ChatOllama

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3:8b"
DEFAULT_NUM_CTX = 16384
CALL_TIMEOUT_SECONDS = 180


class ModelUnavailable(Exception):
    """Ollama is not running, or the model is not installed."""


@dataclass(frozen=True)
class ModelSettings:
    model: str = DEFAULT_MODEL
    host: str = DEFAULT_HOST
    num_ctx: int = DEFAULT_NUM_CTX


def settings_from_env() -> ModelSettings:
    host = os.environ.get("OLLAMA_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    if "://" not in host:  # Ollama itself accepts "127.0.0.1:11434"
        host = "http://" + host
    return ModelSettings(
        model=os.environ.get("FLAGBOX_MODEL", DEFAULT_MODEL),
        host=host.rstrip("/"),
        num_ctx=int(os.environ.get("FLAGBOX_NUM_CTX", DEFAULT_NUM_CTX)),
    )


def check_ollama(settings: ModelSettings) -> None:
    """Raise ModelUnavailable with a message the officer can act on."""
    try:
        with urllib.request.urlopen(f"{settings.host}/api/tags", timeout=5) as response:
            installed = json.load(response).get("models", [])
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        raise ModelUnavailable(
            f"Ollama not running at {settings.host}. Start it (open the Ollama app, or run "
            "`ollama serve`) and try again."
        ) from None
    names = {m.get("name") or m.get("model") for m in installed}
    if settings.model not in names:
        raise ModelUnavailable(
            f"Model {settings.model} is not installed. Run `ollama pull {settings.model}`."
        )


def make_llm(settings: ModelSettings) -> ChatOllama:
    return ChatOllama(
        model=settings.model,
        base_url=settings.host,
        temperature=0,
        num_ctx=settings.num_ctx,
        reasoning=False,  # Qwen3 thinking off (SPEC section 7)
        client_kwargs={"timeout": CALL_TIMEOUT_SECONDS},
    )


async def invoke_with_retry(runnable: Any, messages: list[Any]) -> Any:
    """One Ollama call with one retry (SPEC section 7). Raises ModelUnavailable if both fail."""
    last: Exception | None = None
    for _ in range(2):
        try:
            return await runnable.ainvoke(messages)
        except Exception as error:  # connection reset, timeout, malformed tool JSON from Ollama...
            last = error
    raise ModelUnavailable(f"The model call failed twice: {type(last).__name__}: {last}")


def token_counts(message: Any) -> tuple[int, int]:
    usage = getattr(message, "usage_metadata", None) or {}
    return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
