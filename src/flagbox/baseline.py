"""The no-model baseline (SPEC section 9, eval "B"; PLAN M4.5).

Runs every indicator that has enough data for the buyer and range, and keeps EVERY hit. There is
no planning, no triage, no severity and no memo wording from a model. It uses the same tools and
the same thresholds as the agent, so a difference between the two is what the model adds (or
costs). Output has the same shape as the agent's findings so the two can be compared.
"""

import time
from contextlib import closing
from pathlib import Path
from typing import Any

from flagbox.data.ingest import DB_NAME
from flagbox.server import db, tools
from flagbox.server.config import load_config
from flagbox.server.indicators import discover


class BaselineError(Exception):
    """The baseline could not run (unknown or ambiguous buyer, bad dates, no snapshot)."""


def run_baseline(
    data_dir: Path, config_path: Path, buyer: str, date_from: str, date_to: str
) -> dict[str, Any]:
    started = time.perf_counter()
    cfg = load_config(config_path)
    indicators = discover()
    params = {n: i.params_model.model_validate(cfg.indicators[n]) for n, i in indicators.items()}
    snapshot = data_dir / DB_NAME
    if not snapshot.exists():
        raise BaselineError(f"No snapshot at {snapshot}. Run `flagbox ingest` first.")

    with closing(db.connect_readonly(snapshot)) as conn:
        try:
            found = tools.find_buyer(conn, cfg, buyer, limit=5)
        except db.ToolInputError as error:
            raise BaselineError(str(error)) from None
        exact = [c for c in found["candidates"] if c["name"].casefold() == buyer.casefold()]
        if len(exact) == 1:
            chosen = exact[0]
        elif found["match"] == "all_words" and len(found["candidates"]) == 1:
            chosen = found["candidates"][0]
        else:
            names = ", ".join(c["name"] for c in found["candidates"]) or "none"
            raise BaselineError(f"No single buyer matches '{buyer}'. Closest: {names}")

        try:
            coverage = tools.data_coverage(
                conn, cfg, indicators, chosen["buyer_key"], date_from, date_to
            )
        except db.ToolInputError as error:
            raise BaselineError(str(error)) from None
        runnable = [e["indicator"] for e in coverage["runnable_indicators"] if e["runnable"]]
        findings: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []
        for name in runnable:
            result = tools.run_indicator(
                conn, cfg, indicators[name], params[name], chosen["buyer_key"], date_from, date_to
            )
            checks.append(
                {
                    "indicator": name,
                    "hits_total": result["hits_total"],
                    "awards_checked": result.get("eligible_awards"),
                }
            )
            for hit in result["hits"]:
                findings.append(
                    {
                        "indicator": name,
                        "hit_id": hit["hit_id"],
                        "claim_text": hit["claim_text"],
                        "ocids": [ref["ocid"] for ref in hit["source_refs"]],
                        "severity": None,  # a baseline does not judge
                        "reason_code": None,
                        "decision": "kept without triage",
                    }
                )
    return {
        "buyer_key": chosen["buyer_key"],
        "buyer_name": chosen["name"],
        "date_from": date_from,
        "date_to": date_to,
        "indicators_run": runnable,
        "checks": checks,
        "findings": findings,
        "seconds": round(time.perf_counter() - started, 2),
        "model_calls": 0,
    }
