"""non_competitive_share: a large share of a buyer's award value went through non-open methods.

Within the buyer and date range, awards made by one of the configured methods (for example
"direct" or "selective") are counted. The buyer is listed when at least `min_awards` awards used
those methods AND they carry at least `min_value_share` of the buyer's total award value. Every
threshold and the method list come from config/indicators.yaml; the tool cannot be given others.

This says nothing about whether a direct or restricted award was justified: the law allows them in
set circumstances. It only says the share is high enough that a reviewer may want to check.

The claim text is built here from a fixed template, so the model never writes a number.
"""

import hashlib
from collections import defaultdict
from typing import Any

from pydantic import Field

from flagbox.server import db
from flagbox.server.config import IndicatorParams
from flagbox.server.indicators import Context, Indicator, Reason

CLAIM_TEMPLATE = (
    "{count} of {total_awards} awards, carrying {value_pct}% of award value, were made by "
    "non-open methods ({methods}), {date_from} to {date_to}."
)

# Fixed wording a finding can carry. Each text must be true whenever `applies` is true, and must
# pass the wording rules in flagbox.agent.verify.check_rationale (a test checks every phrase).
# The most specific reason comes first; the verifier stores the first one that applies (SPEC D34).
REASONS = (
    Reason(
        "direct_awards_present",
        "Some of these awards used direct procurement, which the law allows only in set "
        "circumstances; whether those circumstances were met warrants review.",
        lambda hit: hit["metrics"]["by_method"].get("direct", 0) > 0,
    ),
    Reason(
        "restricted_bidding_only",
        "These awards used restricted bidding rather than open tendering, so whether open "
        "tendering was possible warrants review.",
        lambda hit: hit["metrics"]["by_method"].get("direct", 0) == 0,
    ),
    Reason(
        "needs_context",
        "More context is needed to judge this pattern, so it warrants review.",
        lambda hit: True,
    ),
)


class Params(IndicatorParams):
    methods: list[str] = Field(min_length=1)
    min_awards: int = Field(ge=1)
    min_value_share: float = Field(gt=0, le=1)
    max_ocids_per_hit: int = Field(ge=1)
    severity_medium_value_share: float = Field(gt=0, le=1)
    severity_high_value_share: float = Field(gt=0, le=1)


def _hit_id(ctx: Context, params: Params) -> str:
    """Deterministic: the same buyer, range and thresholds always give the same ID."""
    basis = "|".join(
        [
            "non_competitive_share",
            ctx.scope.buyer_key,
            ctx.scope.date_from,
            ctx.scope.date_to,
            ",".join(sorted(params.methods)),
            str(params.min_awards),
            str(params.min_value_share),
        ]
    )
    return "NC-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:10]


def run(ctx: Context, params: Params) -> dict[str, Any]:
    scope, quality = ctx.scope, ctx.quality
    awards = db.awards_in_range(ctx.conn, scope.buyer_key, scope.date_from, scope.date_to)
    files = db.source_files(ctx.conn)

    excluded = {"no_value_or_zero": 0, "other_currency": 0, "above_value_cap": 0, "no_method": 0}
    eligible: list[Any] = []
    for award in awards:
        amount, currency = award["amount"], award["currency"]
        if amount is None or amount <= 0:
            excluded["no_value_or_zero"] += 1
        elif currency != quality.currency:
            excluded["other_currency"] += 1
        elif amount > quality.value_sanity_cap_kes:
            excluded["above_value_cap"] += 1
        elif not award["method"]:
            excluded["no_method"] += 1
        else:
            eligible.append(award)

    total_awards = len(eligible)
    total_value = sum(a["amount"] for a in eligible)
    wanted = {m.casefold() for m in params.methods}
    flagged = [a for a in eligible if str(a["method"]).casefold() in wanted]
    flagged_value = sum(a["amount"] for a in flagged)

    hits: list[dict[str, Any]] = []
    if (
        len(flagged) >= params.min_awards
        and total_value > 0
        and flagged_value / total_value >= params.min_value_share
    ):
        by_method: dict[str, int] = defaultdict(int)
        for award in flagged:
            by_method[str(award["method"]).casefold()] += 1
        # Evidence lists each tender process once (its largest flagged award).
        by_process: dict[str, Any] = {}
        for award in sorted(flagged, key=lambda a: (-a["amount"], a["ocid"], a["award_id"])):
            by_process.setdefault(award["ocid"], award)
        evidence = list(by_process.values())
        shown = evidence[: params.max_ocids_per_hit]
        share = flagged_value / total_value
        value_pct = round(100 * share)
        hit: dict[str, Any] = {
            "hit_id": _hit_id(ctx, params),
            "indicator": "non_competitive_share",
            "claim_text": CLAIM_TEMPLATE.format(
                count=len(flagged),
                total_awards=total_awards,
                value_pct=value_pct,
                methods=", ".join(sorted(wanted)),
                date_from=scope.date_from,
                date_to=scope.date_to,
            ),
            "metrics": {
                "non_open_awards": len(flagged),
                "total_awards": total_awards,
                "processes": len(evidence),
                "count_share_pct": round(100 * len(flagged) / total_awards),
                "value_share_pct": value_pct,
                "by_method": dict(sorted(by_method.items())),
            },
            "evidence_processes_total": len(evidence),
            "source_refs": [db.source_ref(a, files) for a in shown],
            "source_fields": [
                "tender.procurementMethod",
                "awards.value.amount",
                "tender.tenderPeriod.endDate",
            ],
            "severity": (
                "high"
                if share >= params.severity_high_value_share
                else "medium"
                if share >= params.severity_medium_value_share
                else "low"
            ),
        }
        hit["reason_codes"] = [r.code for r in REASONS if r.applies(hit)]
        hits.append(hit)

    return {
        "hits": hits,
        "hits_total": len(hits),
        "eligible_awards": total_awards,
        "eligible_value_kes": round(total_value),
        "sources": db.sources_block(files),
        "params_used": {
            "methods": "/".join(sorted(wanted)),
            "min_awards": params.min_awards,
            "min_value_share": params.min_value_share,
            "value_sanity_cap_kes": quality.value_sanity_cap_kes,
        },
        "excluded_count": excluded,
    }


INDICATOR = Indicator(
    name="non_competitive_share",
    description=(
        "Check whether a large share of one buyer's award value in a date range went through "
        "non-open procurement methods such as direct or selective. Returns at most one hit, with "
        "a hit_id, a ready-made claim_text and source references. Thresholds and the method list "
        "are fixed by configuration and cannot be changed by arguments."
    ),
    needs=("value", "method"),
    params_model=Params,
    run=run,
    reasons=REASONS,
)
