"""repeat_winner: one supplier takes a large share of a buyer's awards.

A supplier is listed when, within the buyer and date range, it has at least `min_wins` awards AND
at least `min_value_share` of the buyer's total award value. Thresholds come from
config/indicators.yaml; the tool cannot be given different ones.

The claim text is built here from a fixed template, so the model never writes a number or a name.
"""

import hashlib
from collections import defaultdict
from typing import Any

from pydantic import Field

from flagbox.server import db
from flagbox.server.config import IndicatorParams
from flagbox.server.indicators import Context, Indicator, Reason

CLAIM_TEMPLATE = (
    "{supplier} received {wins} of {total_awards} awards, in {processes} tender "
    "{process_word}, and {value_pct}% of award value from this buyer, {date_from} to {date_to}."
)


# Fixed wording a finding can carry. Each text must be true whenever `applies` is true, and must
# pass the wording rules in flagbox.agent.verify.check_rationale (a test checks every phrase).
REASONS = (
    Reason(
        "lots_of_one_tender",
        "All of these awards sit within a single tender process, so they may be lots of one "
        "tender rather than repeated wins; that process warrants review.",
        lambda hit: hit["metrics"]["processes"] == 1,
    ),
    Reason(
        "repeated_across_tenders",
        "The supplier won awards in more than one tender process from this buyer, which "
        "warrants review.",
        lambda hit: hit["metrics"]["processes"] >= 2,
    ),
    Reason(
        "value_share_at_threshold",
        "The supplier's share of this buyer's award value is at or above the review "
        "threshold, which warrants review.",
        lambda hit: True,
    ),
    Reason(
        "needs_context",
        "More context is needed to judge this pattern, so it warrants review.",
        lambda hit: True,
    ),
)


class Params(IndicatorParams):
    min_wins: int = Field(ge=1)
    min_value_share: float = Field(gt=0, le=1)
    max_hits: int = Field(ge=1)
    max_ocids_per_hit: int = Field(ge=1)
    severity_medium_value_share: float = Field(gt=0, le=1)
    severity_high_value_share: float = Field(gt=0, le=1)


def _hit_id(ctx: Context, params: Params, supplier_key: str) -> str:
    """Deterministic: the same buyer, range, supplier and thresholds always give the same ID."""
    basis = "|".join(
        [
            "repeat_winner",
            ctx.scope.buyer_key,
            ctx.scope.date_from,
            ctx.scope.date_to,
            supplier_key,
            str(params.min_wins),
            str(params.min_value_share),
        ]
    )
    return "RW-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:10]


def run(ctx: Context, params: Params) -> dict[str, Any]:
    scope, quality = ctx.scope, ctx.quality
    awards = db.awards_in_range(ctx.conn, scope.buyer_key, scope.date_from, scope.date_to)
    suppliers = db.suppliers_in_range(ctx.conn, scope.buyer_key, scope.date_from, scope.date_to)
    files = db.source_files(ctx.conn)

    excluded = {"no_value_or_zero": 0, "other_currency": 0, "above_value_cap": 0, "no_supplier": 0}
    eligible: list[tuple[Any, list[Any]]] = []
    for award in awards:
        amount, currency = award["amount"], award["currency"]
        if amount is None or amount <= 0:
            excluded["no_value_or_zero"] += 1
        elif currency != quality.currency:
            excluded["other_currency"] += 1
        elif amount > quality.value_sanity_cap_kes:
            excluded["above_value_cap"] += 1
        elif not suppliers.get((award["ocid"], award["award_id"])):
            excluded["no_supplier"] += 1
        else:
            eligible.append((award, suppliers[(award["ocid"], award["award_id"])]))

    total_awards = len(eligible)
    total_value = sum(award["amount"] for award, _ in eligible)

    wins: dict[str, int] = defaultdict(int)
    value: dict[str, float] = defaultdict(float)
    won: dict[str, list[Any]] = defaultdict(list)
    label: dict[str, str] = {}
    for award, rows in eligible:
        for row in rows:
            key = row["supplier_key"]
            wins[key] += 1
            value[key] += award["amount"] / len(
                rows
            )  # an award with several suppliers is split evenly
            won[key].append(award)
            label[key] = db.supplier_label(row["display_name"], key, bool(row["name_withheld"]))

    qualifying = sorted(
        (
            key
            for key in wins
            if wins[key] >= params.min_wins
            and total_value > 0
            and value[key] / total_value >= params.min_value_share
        ),
        key=lambda key: (-value[key], key),
    )

    hits: list[dict[str, Any]] = []
    for key in qualifying[: params.max_hits]:
        # Several awards can be lots of one tender. Evidence lists each process once (its
        # largest award), and the claim says how many processes the awards fall in.
        by_process: dict[str, Any] = {}
        for award in sorted(won[key], key=lambda a: (-a["amount"], a["ocid"], a["award_id"])):
            by_process.setdefault(award["ocid"], award)
        evidence = list(by_process.values())
        shown = evidence[: params.max_ocids_per_hit]
        count_pct = round(100 * wins[key] / total_awards)
        value_pct = round(100 * value[key] / total_value)
        hit: dict[str, Any] = {
            "hit_id": _hit_id(ctx, params, key),
            "indicator": "repeat_winner",
            "supplier_key": key,
            "supplier": label[key],
            "claim_text": CLAIM_TEMPLATE.format(
                supplier=label[key],
                wins=wins[key],
                total_awards=total_awards,
                processes=len(evidence),
                process_word="process" if len(evidence) == 1 else "processes",
                value_pct=value_pct,
                date_from=scope.date_from,
                date_to=scope.date_to,
            ),
            "metrics": {
                "wins": wins[key],
                "total_awards": total_awards,
                "processes": len(evidence),
                "count_share_pct": count_pct,
                "value_share_pct": value_pct,
            },
            "evidence_processes_total": len(evidence),
            "source_refs": [db.source_ref(a, files) for a in shown],
            "source_fields": [
                "awards.suppliers",
                "awards.value.amount",
                "tender.tenderPeriod.endDate",
            ],
        }
        share = value[key] / total_value
        hit["severity"] = (
            "high"
            if share >= params.severity_high_value_share
            else "medium"
            if share >= params.severity_medium_value_share
            else "low"
        )
        hit["reason_codes"] = [r.code for r in REASONS if r.applies(hit)]
        hits.append(hit)

    return {
        "hits": hits,
        "hits_total": len(qualifying),
        "eligible_awards": total_awards,
        "eligible_value_kes": round(total_value),
        "sources": db.sources_block(files),
        "params_used": {
            "min_wins": params.min_wins,
            "min_value_share": params.min_value_share,
            "value_sanity_cap_kes": quality.value_sanity_cap_kes,
        },
        "excluded_count": excluded,
    }


INDICATOR = Indicator(
    name="repeat_winner",
    description=(
        "Find suppliers that won a large share of one buyer's awards in a date range. "
        "Returns hits, each with a hit_id, a ready-made claim_text and source references. "
        "Thresholds are fixed by configuration and cannot be changed by arguments."
    ),
    needs=("value", "supplier_id"),
    params_model=Params,
    run=run,
    reasons=REASONS,
)
