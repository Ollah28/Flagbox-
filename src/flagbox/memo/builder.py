"""Gathers the memo's facts and renders them (SPEC section 3.1, `draft` node).

Everything on the page comes from three places, none of them the model:
  - the run's hash-verified log (coverage, checks, the plan, the verify step);
  - the findings stored by `record_finding` (claim text and evidence copied from the log, and
    fixed reason wording);
  - the snapshot's own source-file table.
The one exception is the summary paragraph, written by the model, checked by
`verify.check_summary`, and replaced by a fixed sentence if it fails twice.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from flagbox.agent.verify import find_hit

CAVEATS = (
    "A pattern is not a conclusion. A supplier that wins often can have ordinary explanations, "
    "such as a framework agreement, specialist goods, or several lots of one tender. Only a "
    "human review of the underlying records can say.",
    "Awards are counted, not tenders. Several awards can be lots of one tender, which is why "
    "each finding states how many tender processes its awards fall in.",
    "The source has no register of suppliers. One company can appear under more than one "
    "supplier ID, so a share may be understated.",
    "Supplier names appear only when they carry an incorporation marker such as Ltd or "
    "Limited. Other names are withheld by design and shown as an ID.",
    "The records are published by the procuring bodies and were not independently checked. "
    "Fields can be missing or wrong.",
    "Only the checks listed above were run. The absence of a finding is not assurance.",
)

_EXCLUDED_LABELS = {
    "no_value_or_zero": "zero or missing value",
    "other_currency": "not in Kenya shillings",
    "above_value_cap": "above the value cap",
    "no_supplier": "no supplier recorded",
}
_USABLE_LABELS = {
    "award_value": "a usable value",
    "supplier_id": "a supplier ID",
    "procurement_category": "a procurement category",
    "procurement_method": "a procurement method",
}


def _cell(value: object) -> str:
    """Make text safe inside a markdown table cell."""
    return " ".join(str(value).split()).replace("|", "\\|")


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    env.filters["cell"] = _cell
    return env


def _events(records: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [r for r in records if r.get("event") == name]


def _results(
    records: list[dict[str, Any]], key: str
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(record, result) for every successful tool call whose result has `key`."""
    out = []
    for record in _events(records, "tool_call"):
        result = record.get("result")
        if not record.get("is_error") and isinstance(result, dict) and key in result:
            out.append((record, result))
    return out


def gather(
    records: list[dict[str, Any]],
    log_records: int,
    log_head: str,
    findings: list[sqlite3.Row],
    dismissed: list[sqlite3.Row],
    source_files: list[sqlite3.Row],
) -> dict[str, Any]:
    """Collect every fact the template needs, except the summary text."""
    start = (_events(records, "run_start") or [{}])[0]
    chosen = (_events(records, "buyer_selected") or [{}])[-1]
    plan = (_events(records, "plan") or [{}])[-1]
    verification = (_events(records, "verify") or [{}])[-1]

    coverage_results = _results(records, "runnable_indicators")
    coverage: dict[str, Any] | None = None
    if coverage_results:
        raw = coverage_results[-1][1]
        coverage = {
            "processes": raw["processes"],
            "awards": raw["awards"],
            "usable": [
                (_USABLE_LABELS.get(key, key), value if value is not None else "n/a")
                for key, value in raw["usable_pct"].items()
            ],
            "runnable": [
                {
                    "indicator": item["indicator"],
                    "share": "n/a"
                    if item["joint_coverage_pct"] is None
                    else f"{item['joint_coverage_pct']}%",
                    "runnable": item["runnable"],
                }
                for item in raw["runnable_indicators"]
            ],
            "min_share": raw["min_joint_coverage_pct"],
        }

    checks: list[dict[str, Any]] = []
    dq_lines: list[str] = []
    for _, result in _results(records, "hits_total"):
        settings = ", ".join(f"{k} {v}" for k, v in result.get("params_used", {}).items())
        checks.append(
            {
                "indicator": result["indicator"],
                "hits_total": result["hits_total"],
                "awards_checked": result.get("eligible_awards", "n/a"),
                "settings": settings,
            }
        )
        excluded = result.get("excluded_count", {})
        ordered = [k for k in _EXCLUDED_LABELS if k in excluded]
        ordered += [k for k in excluded if k not in _EXCLUDED_LABELS]  # keep any new reason
        parts = [f"{excluded[k]} {_EXCLUDED_LABELS.get(k, k)}" for k in ordered if excluded[k]]
        dq_lines.append(
            f"{result['indicator']}: awards left out of its statistics: "
            + (", ".join(parts) if parts else "none")
            + ". These are data-quality notes, never findings."
        )
    if not dq_lines:
        dq_lines.append("No indicator ran, so no awards were set aside for data quality.")

    finding_items: list[dict[str, Any]] = []
    for number, row in enumerate(findings, start=1):
        refs = [
            {
                "ocid": ref["ocid"],
                "file": ref["file"],
                "line_no": ref["line_no"],
                "portal_url": ref.get("portal_url"),
            }
            for ref in json.loads(row["source_refs"])
        ]
        logged = find_hit(records, row["hit_id"])
        total = logged.hit.get("evidence_processes_total", len(refs)) if logged else len(refs)
        finding_items.append(
            {
                "number": number,
                "finding_id": row["finding_id"],
                "hit_id": row["hit_id"],
                "tool_call_id": row["tool_call_id"],
                "severity": row["severity"],
                "claim_text": row["claim_text"],
                "rationale": row["rationale"],
                "refs": refs,
                "more_processes": max(0, int(total) - len(refs)),
                "supplier_label": (logged.hit.get("supplier") if logged else None) or "",
            }
        )

    return {
        "buyer_name": chosen.get("name") or start.get("buyer_query") or "unknown buyer",
        "buyer_key": chosen.get("buyer_key", "unknown"),
        "date_from": start.get("date_from", "unknown"),
        "date_to": start.get("date_to", "unknown"),
        "run_id": start.get("run_id", ""),
        "started_at": start.get("ts", "unknown"),
        "model": f"{start.get('model', 'unknown')} (context {start.get('num_ctx', 'unknown')})",
        "prompt_version": start.get("prompt_version", "unknown"),
        "log_records": log_records,
        "log_head": log_head,
        "coverage": coverage,
        "findings": finding_items,
        "checks": checks,
        "skipped": [
            {"indicator": s.get("indicator", "?"), "reason": s.get("reason", "")}
            for s in plan.get("skipped", [])
        ],
        "gaps": list(verification.get("gaps", [])),
        "undecided": len(verification.get("undecided", [])),
        "demoted": len(verification.get("demoted", [])),
        "dismissed": [
            {"claim_text": d["claim_text"], "rationale": d["rationale"] or d["reason_code"]}
            for d in dismissed
        ],
        "dq_lines": dq_lines,
        "caveats": CAVEATS,
        "sources": [
            {
                "file": s["name"],
                "url": s["url"],
                "sha256": s["sha256"],
                "downloaded_at": s["downloaded_at"],
                "lines": s["line_count"],
            }
            for s in source_files
        ],
        "_plan_empty": plan.get("indicators") == [] if plan else False,
    }


def no_findings_reason(context: dict[str, Any]) -> str:
    coverage = context["coverage"]
    if coverage is not None and coverage["awards"] == 0:
        return "No awards were found for this buyer in this range."
    if coverage is not None and not any(item["runnable"] for item in coverage["runnable"]):
        return "None of the indicators had enough usable data for this buyer and range."
    if context["_plan_empty"]:
        return "The plan ran no indicators."
    if context["undecided"]:
        return "Some hits were found but none received a decision; see the checks not completed."
    if context["checks"] and all(c["hits_total"] == 0 for c in context["checks"]):
        return "No indicator crossed its threshold. That is a valid result, not a failure."
    return "The run ended before any finding was recorded."


def fixed_summary(context: dict[str, Any]) -> str:
    """The wording used when the model's summary is missing or fails the rules twice."""
    head = (
        f"Screening of {context['buyer_name']} for tenders closing {context['date_from']} "
        f"to {context['date_to']}"
    )
    count = len(context["findings"])
    if count:
        noun = "pattern" if count == 1 else "patterns"
        return (
            f"{head} found {count} {noun} that warrant review by a human reviewer. Each is listed "
            "below with its source records. Nothing here establishes wrongdoing."
        )
    return (
        f"{head} produced no finding. {no_findings_reason(context)} "
        "This is a screening result only, not assurance."
    )


def render(context: dict[str, Any], summary: str, summary_source: str) -> str:
    """Render the memo. The same inputs always give the same text (and so the same SHA-256)."""
    labels = {
        "model": (
            "Summary written by the language model and checked by fixed rules.",
            "written by the language model and checked by fixed rules",
        ),
        "fixed": (
            "Summary generated from a fixed template.",
            "generated from a fixed template",
        ),
    }
    label, note = labels[summary_source]
    text = (
        _environment()
        .get_template("template.md.j2")
        .render(
            **{k: v for k, v in context.items() if not k.startswith("_")},
            summary=summary.strip(),
            summary_label=label,
            summary_source_note=note,
            no_findings_reason=no_findings_reason(context),
        )
    )
    return text.replace("\r\n", "\n")
