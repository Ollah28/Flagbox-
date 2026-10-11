"""Gives the model tools whose buyer and date arguments are already filled in.

The officer picked the buyer and the range before the agent started (SPEC 3.1). Hiding those
arguments from the model means it cannot drift to another buyer, mistype a date, or ask a
question that was never approved. The values are re-applied on every call, so even if a model
passes its own, ours win.
"""

from typing import Any

from langchain_core.tools import BaseTool, StructuredTool


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") if isinstance(block, dict) else str(block) for block in content
        )
    return str(content)


def bind(base: BaseTool, fixed: dict[str, Any]) -> StructuredTool:
    """Wrap one adapter tool, hiding (and always supplying) the arguments named in `fixed`."""
    schema: dict[str, Any] = base.args_schema if isinstance(base.args_schema, dict) else {}
    properties = {k: v for k, v in schema.get("properties", {}).items() if k not in fixed}
    required = [r for r in schema.get("required", []) if r not in fixed]
    visible = {
        "type": "object",
        "title": base.name,
        "properties": properties,
        "required": required,
    }
    applicable = {k: v for k, v in fixed.items() if k in schema.get("properties", {})}

    async def call(**arguments: Any) -> str:
        merged = {**{k: v for k, v in arguments.items() if k in properties}, **applicable}
        return _text(await base.ainvoke(merged))

    return StructuredTool(
        name=base.name,
        description=base.description,
        args_schema=visible,
        coroutine=call,
    )
