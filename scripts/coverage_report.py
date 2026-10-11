"""M1.3 / spike S1: how complete is the data, which indicators can run, which buyers to evaluate.

Streams the raw PPRA files (not the SQLite snapshot, because some fields we need to judge, such as
numberOfTenderers and contractPeriod, are deliberately not stored), applies the same D20
live-tender filter as `flagbox ingest`, and writes docs/data_coverage.md.

Run from the repo root:  uv run python scripts/coverage_report.py

The "indicative preview" numbers use the SPEC A2 placeholder parameters. They exist only to pick
eval buyers that have something to find; they are NOT the indicators (those arrive in M2/M4 with
parameters in config/indicators.yaml).
"""

import gzip
import json
import statistics
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from flagbox.data.download import read_manifest
from flagbox.data.live_filter import exclusion_reason
from flagbox.data.redact import normalise_name

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT = ROOT / "docs" / "data_coverage.md"
TODAY = date.today()

MIN_COVERAGE = 0.60  # SPEC 3.2 rule: a field must be at least 60% populated to be relied on
PLAUSIBLE_YEARS = range(2015, 2027)

# SPEC A2 placeholders, used only for the indicative preview.
PREVIEW_MIN_WINS = 3
RW_SHARES = (0.30, 0.20, 0.10)  # sensitivity: value share needed for a repeat-winner hit
SURVEY_MIN_AWARDS = 50  # smaller buyers are left out of the cross-buyer survey
PREVIEW_NON_COMPETITIVE_SHARE = 0.40
PREVIEW_IQR_K = 3
PREVIEW_MIN_CATEGORY_AWARDS = 8
NON_COMPETITIVE_METHODS = {"direct", "selective", "AlternativeSelection", "SpeciallyPermitted"}

# Candidate eval buyers: (buyer id, expected name). Chosen from the buyer list for a mix of
# national bodies and counties, and of sizes (6 of 11 have 300 processes or fewer).
CANDIDATES: list[tuple[str, str]] = [
    ("5301", "Makueni County Government"),
    ("5019", "Kenya Rural Roads Authority"),
    ("5367", "Nakuru County Government"),
    ("5144", "National Transport and Safety Authority"),
    ("5225", "Uasin Gishu County Government"),
    ("5126", "Geothermal Development Company"),
    ("5132", "Energy Regulatory Commission"),
    ("5174", "Kenya Maritime Authority"),
    ("5090", "Narok County Government"),
    ("5221", "Turkana County Government"),
    ("5241", "Nairobi County Government"),
]

# Fields each candidate indicator needs, as award-level "usable" flags (see award_flags).
INDICATORS: dict[str, tuple[str, list[str]]] = {
    "repeat_winner": ("1", ["value", "supplier_id"]),
    "value_outlier": ("2", ["value", "category"]),
    "single_bidder": ("3", ["tenderers", "method"]),
    "non_competitive_share": ("3", ["method", "value"]),
    "short_window": ("3", ["window"]),
    "date_sequence": ("3", ["sequence"]),
}

PROCESS_FIELDS = [
    "buyer.id",
    "tender.title",
    "tender.procurementMethod",
    "tender.procurementMethodDetails",
    "tender.mainProcurementCategory",
    "tender.numberOfTenderers",
    "tender.value.amount",
    "tender.status",
    "tenderPeriod.startDate (plausible year)",
    "tenderPeriod.endDate (plausible year)",
    "tenderPeriod start <= end",
]
AWARD_FIELDS = [
    "award.value.amount > 0",
    "award.value.currency = KES",
    "award.suppliers (at least one)",
    "award.suppliers[].id",
    "award.suppliers[].name (award or party)",
    "award.date",
    "award.status",
    "award.contractPeriod.startDate (plausible year)",
]
CONTRACT_FIELDS = [
    "contract.dateSigned (plausible year)",
    "contract.value.amount > 0",
    "contract.period.startDate (plausible year)",
]


# ---- small helpers -----------------------------------------------------------------------------


def as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def present(value: object) -> bool:
    return value is not None and str(value).strip() != ""


def positive(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


def parse_date(value: object) -> date | None:
    """The calendar date of an ISO string, or None if unreadable or outside 2015-2026."""
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value[:10])
    except ValueError:
        return None
    return parsed if parsed.year in PLAUSIBLE_YEARS else None


def pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "n/a"


def quartiles(values: list[float]) -> tuple[float, float]:
    q1, _, q3 = statistics.quantiles(values, n=4)
    return q1, q3


# ---- one pass over the data --------------------------------------------------------------------


class Tally:
    """Counts how often each named flag is true, overall and per buyer, per level."""

    def __init__(self) -> None:
        self.true: Counter[tuple[str, str, str]] = Counter()  # (scope, level, flag)
        self.total: Counter[tuple[str, str]] = Counter()  # (scope, level)

    def add(self, buyer: str, level: str, flags: dict[str, bool]) -> None:
        for scope in ("ALL", buyer):
            self.total[(scope, level)] += 1
            for name, value in flags.items():
                if value:
                    self.true[(scope, level, name)] += 1

    def count(self, scope: str, level: str, flag: str) -> int:
        return self.true[(scope, level, flag)]

    def n(self, scope: str, level: str) -> int:
        return self.total[(scope, level)]


def award_flags(
    award: dict[str, Any],
    release: dict[str, Any],
    party_names: dict[str, str],
    contracts_by_award: dict[str, list[dict[str, Any]]],
    process_ok: dict[str, bool],
) -> dict[str, bool]:
    value = as_dict(award.get("value"))
    suppliers = [as_dict(s) for s in as_list(award.get("suppliers"))]
    contract_dates = [
        parse_date(c.get("dateSigned")) for c in contracts_by_award.get(str(award.get("id")), [])
    ]
    flags = {
        "award.value.amount > 0": positive(value.get("amount")),
        "award.value.currency = KES": value.get("currency") == "KES",
        "award.suppliers (at least one)": bool(suppliers),
        "award.suppliers[].id": any(present(s.get("id")) for s in suppliers),
        "award.suppliers[].name (award or party)": any(
            present(s.get("name")) or str(s.get("id")) in party_names for s in suppliers
        ),
        "award.date": present(award.get("date")),
        "award.status": present(award.get("status")),
        "award.contractPeriod.startDate (plausible year)": parse_date(
            as_dict(award.get("contractPeriod")).get("startDate")
        )
        is not None,
    }
    usable = {
        "value": flags["award.value.amount > 0"] and flags["award.value.currency = KES"],
        "supplier_id": flags["award.suppliers[].id"],
        "category": process_ok["category"],
        "method": process_ok["method"],
        "tenderers": process_ok["tenderers"],
        "window": process_ok["window"],
        "sequence": process_ok["end"] and any(d is not None for d in contract_dates),
    }
    for name, (_, needs) in INDICATORS.items():
        flags[f"indicator:{name}"] = all(usable[need] for need in needs)
    return flags


class Preview:
    """Per buyer: just enough award detail to run the indicative previews."""

    def __init__(self) -> None:
        self.awards: list[dict[str, Any]] = []
        self.sequence_checked = 0
        self.sequence_fires = 0


def scan() -> dict[str, Any]:
    tally = Tally()
    previews: dict[str, Preview] = defaultdict(Preview)
    names: dict[str, str] = {}
    date_hist: dict[str, Counter[str]] = defaultdict(Counter)
    date_present: Counter[str] = Counter()
    counts = Counter()  # excluded by reason, plus lines read

    manifest = read_manifest(DATA_DIR / "MANIFEST.json")
    for entry in sorted(manifest.values(), key=lambda e: str(e["name"])):
        with gzip.open(DATA_DIR / "raw" / str(entry["name"]), "rt", encoding="utf-8") as lines:
            for line in lines:
                counts["read"] += 1
                release = json.loads(line)
                reason = exclusion_reason(release, TODAY)
                if reason is not None:
                    counts[f"excluded:{reason}"] += 1
                    continue
                counts["kept"] += 1
                _scan_release(release, tally, previews, names, date_hist, date_present)
    return {
        "tally": tally,
        "previews": previews,
        "names": names,
        "date_hist": date_hist,
        "date_present": date_present,
        "counts": counts,
        "manifest": manifest,
    }


def _scan_release(
    release: dict[str, Any],
    tally: Tally,
    previews: dict[str, Preview],
    names: dict[str, str],
    date_hist: dict[str, Counter[str]],
    date_present: Counter[str],
) -> None:
    buyer = as_dict(release.get("buyer"))
    buyer_id = str(buyer.get("id")) if present(buyer.get("id")) else "name:" + normalise_name(
        str(buyer.get("name") or "unknown")
    )
    names.setdefault(buyer_id, str(buyer.get("name") or "(unknown)"))
    tender = as_dict(release.get("tender"))
    period = as_dict(tender.get("tenderPeriod"))
    start, end = parse_date(period.get("startDate")), parse_date(period.get("endDate"))

    process_flags = {
        "buyer.id": present(buyer.get("id")),
        "tender.title": present(tender.get("title")),
        "tender.procurementMethod": present(tender.get("procurementMethod")),
        "tender.procurementMethodDetails": present(tender.get("procurementMethodDetails")),
        "tender.mainProcurementCategory": present(tender.get("mainProcurementCategory")),
        "tender.numberOfTenderers": isinstance(tender.get("numberOfTenderers"), int),
        "tender.value.amount": positive(as_dict(tender.get("value")).get("amount")),
        "tender.status": present(tender.get("status")),
        "tenderPeriod.startDate (plausible year)": start is not None,
        "tenderPeriod.endDate (plausible year)": end is not None,
        "tenderPeriod start <= end": start is not None and end is not None and start <= end,
    }
    tally.add(buyer_id, "process", process_flags)
    process_ok = {
        "category": process_flags["tender.mainProcurementCategory"],
        "method": process_flags["tender.procurementMethod"],
        "tenderers": process_flags["tender.numberOfTenderers"],
        "window": process_flags["tenderPeriod start <= end"],
        "end": end is not None,
    }

    party_names = {
        str(p.get("id")): str(p.get("name"))
        for p in map(as_dict, as_list(release.get("parties")))
        if present(p.get("id")) and present(p.get("name"))
    }
    contracts = [as_dict(c) for c in as_list(release.get("contracts"))]
    contracts_by_award: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for contract in contracts:
        contracts_by_award[str(contract.get("awardID"))].append(contract)

    if end is not None:
        date_present["tender.endDate"] += 1
        date_hist["tender.endDate"][str(end.year)] += 1
    for contract in contracts:
        tally.add(
            buyer_id,
            "contract",
            {
                "contract.dateSigned (plausible year)": parse_date(contract.get("dateSigned"))
                is not None,
                "contract.value.amount > 0": positive(as_dict(contract.get("value")).get("amount")),
                "contract.period.startDate (plausible year)": parse_date(
                    as_dict(contract.get("period")).get("startDate")
                )
                is not None,
            },
        )
        signed = parse_date(contract.get("dateSigned"))
        if signed is not None:
            date_present["contract.dateSigned"] += 1
            date_hist["contract.dateSigned"][str(signed.year)] += 1

    preview = previews[buyer_id]
    signed_dates = [d for d in (parse_date(c.get("dateSigned")) for c in contracts) if d]
    if end is not None and signed_dates:
        preview.sequence_checked += 1
        preview.sequence_fires += min(signed_dates) < end
    if start is not None and end is not None and start <= end:
        days = (end - start).days
        date_present["window:known"] += 1
        date_present["window:0-6"] += days < 7
        date_present["window:7-13"] += 7 <= days < 14
        date_present["window:14-27"] += 14 <= days < 28
        date_present["window:28+"] += days >= 28

    for award in map(as_dict, as_list(release.get("awards"))):
        flags = award_flags(award, release, party_names, contracts_by_award, process_ok)
        tally.add(buyer_id, "award", flags)
        started = parse_date(as_dict(award.get("contractPeriod")).get("startDate"))
        if started is not None:
            date_present["award.contractPeriod.startDate"] += 1
            date_hist["award.contractPeriod.startDate"][str(started.year)] += 1
        value = as_dict(award.get("value"))
        preview.awards.append(
            {
                "suppliers": [
                    str(as_dict(s).get("id"))
                    for s in as_list(award.get("suppliers"))
                    if present(as_dict(s).get("id"))
                ],
                "amount": float(value["amount"])
                if positive(value.get("amount")) and value.get("currency") == "KES"
                else None,
                "category": tender.get("mainProcurementCategory"),
                "method": tender.get("procurementMethod"),
            }
        )


# ---- indicative previews -----------------------------------------------------------------------


def preview_repeat_winner(awards: list[dict[str, Any]], min_share: float) -> int:
    valued = [a for a in awards if a["amount"] is not None]
    total = sum(a["amount"] for a in valued)
    wins: Counter[str] = Counter()
    value: Counter[str] = Counter()
    for award in valued:
        for supplier in set(award["suppliers"]):
            wins[supplier] += 1
            value[supplier] += award["amount"]
    return sum(
        1
        for s in wins
        if wins[s] >= PREVIEW_MIN_WINS and total and value[s] / total >= min_share
    )


def preview_value_outlier(awards: list[dict[str, Any]]) -> int:
    by_category: dict[str, list[float]] = defaultdict(list)
    for award in awards:
        if award["amount"] is not None and award["category"]:
            by_category[award["category"]].append(award["amount"])
    fires = 0
    for values in by_category.values():
        if len(values) >= PREVIEW_MIN_CATEGORY_AWARDS:
            q1, q3 = quartiles(values)
            fires += sum(1 for v in values if v > q3 + PREVIEW_IQR_K * (q3 - q1))
    return fires


def preview_non_competitive_share(awards: list[dict[str, Any]]) -> float | None:
    valued = [a for a in awards if a["amount"] is not None and a["method"]]
    total = sum(a["amount"] for a in valued)
    if not total:
        return None
    return sum(a["amount"] for a in valued if a["method"] in NON_COMPETITIVE_METHODS) / total


# ---- markdown ----------------------------------------------------------------------------------


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(row) + " |" for row in rows]
    return out + [""]


def field_rows(tally: Tally, scope: str, level: str, fields: list[str]) -> list[list[str]]:
    n = tally.n(scope, level)
    return [[f"`{f}`", f"{tally.count(scope, level, f):,}", pct(tally.count(scope, level, f), n)] for f in fields]


def build_report(result: dict[str, Any]) -> str:
    tally: Tally = result["tally"]
    previews: dict[str, Preview] = result["previews"]
    names: dict[str, str] = result["names"]
    counts: Counter[str] = result["counts"]
    n_proc, n_award, n_contract = (tally.n("ALL", lvl) for lvl in ("process", "award", "contract"))

    lines = [
        "# Data coverage report",
        "",
        "*Generated by `scripts/coverage_report.py`. Do not edit by hand; re-run the script.*",
        "*Decisions taken from these numbers are logged in SPEC.md section 13.*",
        "",
        f"- Live-tender filter date: **{TODAY}** (decision D20).",
        f"- Lines read: {counts['read']:,}. Kept: **{counts['kept']:,}** processes, "
        f"{n_award:,} awards, {n_contract:,} contracts.",
        "- Excluded: "
        + ", ".join(f"{k.split(':')[1]} {v:,}" for k, v in sorted(counts.items()) if k.startswith("excluded")),
        "- Source files (SHA-256 prefix): "
        + ", ".join(f"{e['name']} `{str(e['sha256'])[:12]}`" for e in result["manifest"].values()),
        "- A date counts as *plausible* when it parses and falls in 2015-2026.",
        "",
        "## 1. Field population, all kept data",
        "",
        f"### Processes (n = {n_proc:,})",
        "",
        *table(["Field", "Populated", "%"], field_rows(tally, "ALL", "process", PROCESS_FIELDS)),
        f"### Awards (n = {n_award:,})",
        "",
        *table(["Field", "Populated", "%"], field_rows(tally, "ALL", "award", AWARD_FIELDS)),
        f"### Contracts (n = {n_contract:,})",
        "",
        *table(["Field", "Populated", "%"], field_rows(tally, "ALL", "contract", CONTRACT_FIELDS)),
        "## 2. Which indicators can run",
        "",
        "*Joint coverage* = share of awards where **every** field the indicator needs is usable "
        f"(value > 0 and KES, supplier ID present, and so on). Rule from SPEC 3.2: at least {MIN_COVERAGE:.0%}.",
        "",
    ]
    rows = []
    for name, (pref, needs) in INDICATORS.items():
        joint = tally.count("ALL", "award", f"indicator:{name}")
        rows.append(
            [
                f"`{name}`",
                pref,
                ", ".join(needs),
                pct(joint, n_award),
                "**runnable**" if n_award and joint / n_award >= MIN_COVERAGE else "not runnable",
            ]
        )
    lines += table(["Indicator", "Pref", "Needs (usable)", "Joint coverage", "Verdict"], rows)

    lines += [
        "## 3. Which date should `--from/--to` use?",
        "",
        "Awards carry no date of their own. Candidates, with how often each is present and plausible:",
        "",
    ]
    date_rows = []
    years = [str(y) for y in range(2019, 2027)]
    for label, base in (
        ("`tenderPeriod.endDate`", n_proc),
        ("`contract.dateSigned`", n_contract),
        ("`award.contractPeriod.startDate`", n_award),
    ):
        key = label.strip("`")
        key = {"tenderPeriod.endDate": "tender.endDate", "contract.dateSigned": "contract.dateSigned"}.get(
            key, key
        )
        hist = result["date_hist"][key]
        date_rows.append(
            [label, pct(result["date_present"][key], base)] + [f"{hist[y]:,}" for y in years]
        )
    lines += table(["Field", "Plausible"] + years, date_rows)
    lines += [
        f"`tenderPeriod.startDate` is not a candidate: it is after `endDate` in "
        f"{100 - 100 * tally.count('ALL', 'process', 'tenderPeriod start <= end') / n_proc:.0f}% of processes "
        f"(start <= end holds for {pct(tally.count('ALL', 'process', 'tenderPeriod start <= end'), n_proc)}).",
        "",
        "### Tender window length (end minus start, where start <= end)",
        "",
        *table(
            ["Days", "Processes", "%"],
            [
                [
                    label,
                    f"{result['date_present']['window:' + label]:,}",
                    pct(result["date_present"]["window:" + label], result["date_present"]["window:known"]),
                ]
                for label in ("0-6", "7-13", "14-27", "28+")
            ],
        ),
        "Most windows are under a week, so `startDate` does not look like a real opening date and "
        "`short_window` would flag almost every process.",
        "",
        "## 4. Candidate eval buyers",
        "",
        "Joint coverage per indicator (share of that buyer's awards where the indicator's fields are usable):",
        "",
    ]
    cand_rows = []
    for buyer_id, expected in CANDIDATES:
        if buyer_id not in names:
            cand_rows.append([buyer_id, f"{expected} (**NOT FOUND**)"] + ["-"] * (3 + len(INDICATORS)))
            continue
        np_, na = tally.n(buyer_id, "process"), tally.n(buyer_id, "award")
        kind = "county" if "County" in names[buyer_id] else "national/other"
        cand_rows.append(
            [buyer_id, names[buyer_id], kind, f"{np_:,}", f"{na:,}"]
            + [pct(tally.count(buyer_id, "award", f"indicator:{n}"), na) for n in INDICATORS]
        )
    lines += table(["ID", "Buyer", "Type", "Processes", "Awards"] + list(INDICATORS), cand_rows)

    lines += [
        "### Indicative preview (placeholder parameters from SPEC A2; not the real indicators)",
        "",
        f"Repeat winner: suppliers with >= {PREVIEW_MIN_WINS} awards and >= 30% / 20% / 10% of award value. "
        f"Value outlier: awards above Q3 + {PREVIEW_IQR_K} x IQR within a category of >= {PREVIEW_MIN_CATEGORY_AWARDS} awards. "
        f"Non-competitive share: value via direct/selective methods (flag above {PREVIEW_NON_COMPETITIVE_SHARE:.0%}). "
        "Date sequence: a contract signed before the tender's end date. Counts are of items that would be "
        "reviewed, not findings.",
        "",
    ]
    prev_rows = []
    for buyer_id, _ in CANDIDATES:
        if buyer_id not in names:
            continue
        p = previews[buyer_id]
        share = preview_non_competitive_share(p.awards)
        prev_rows.append(
            [
                names[buyer_id],
                "/".join(str(preview_repeat_winner(p.awards, x)) for x in RW_SHARES),
                str(preview_value_outlier(p.awards)),
                "n/a" if share is None else f"{share:.0%}" + (" (flag)" if share > PREVIEW_NON_COMPETITIVE_SHARE else ""),
                f"{p.sequence_fires} of {p.sequence_checked}",
            ]
        )
    lines += table(
        ["Buyer", "repeat_winner suppliers at 30%/20%/10%", "value_outlier awards", "non-competitive value share", "date_sequence processes"],
        prev_rows,
    )
    lines += survey_section(previews, names)
    return "\n".join(lines)


def survey_section(previews: dict[str, Preview], names: dict[str, str]) -> list[str]:
    big = {b: p for b, p in previews.items() if len(p.awards) >= SURVEY_MIN_AWARDS}
    lines = [
        f"## 5. Signal survey, buyers with at least {SURVEY_MIN_AWARDS} awards ({len(big)} buyers)",
        "",
        "How many buyers would have at least one item under each indicative rule. This decides "
        "whether an indicator can demonstrate anything on real data.",
        "",
    ]
    vo = {b: preview_value_outlier(p.awards) for b, p in big.items()}
    nc = {b: preview_non_competitive_share(p.awards) for b, p in big.items()}
    ds = {b: p.sequence_fires for b, p in big.items()}
    rows = [
        [
            f"repeat_winner (>= {PREVIEW_MIN_WINS} wins, >= {x:.0%} of value)",
            str(sum(1 for p in big.values() if preview_repeat_winner(p.awards, x))),
        ]
        for x in RW_SHARES
    ]
    rows.append(["value_outlier (>= 1 award above Q3 + 3 x IQR)", str(sum(1 for v in vo.values() if v))])
    for threshold in (0.10, 0.20, PREVIEW_NON_COMPETITIVE_SHARE):
        rows.append(
            [
                f"non_competitive_share (>= {threshold:.0%} of value)",
                str(sum(1 for v in nc.values() if v is not None and v >= threshold)),
            ]
        )
    rows.append(["date_sequence (>= 1 contract signed before tender end)", str(sum(1 for v in ds.values() if v))])
    lines += table(["Rule", "Buyers with a signal"], rows)

    def top(metric: dict[str, Any], label: str) -> list[str]:
        ranked = sorted(((b, v) for b, v in metric.items() if v), key=lambda item: item[1], reverse=True)[:5]
        body = [
            [names[b], f"{len(big[b].awards):,}", f"{v:.0%}" if isinstance(v, float) else str(v)]
            for b, v in ranked
        ]
        return [f"**Top 5 by {label}**", ""] + table(["Buyer", "Awards", label], body)

    rw_rows = [
        [names[b], f"{len(p.awards):,}"] + [str(preview_repeat_winner(p.awards, x)) for x in RW_SHARES]
        for b, p in big.items()
        if preview_repeat_winner(p.awards, min(RW_SHARES))
    ]
    rw_rows.sort(key=lambda row: (int(row[2]), int(row[3]), int(row[4])), reverse=True)
    lines += ["**Buyers where repeat_winner fires at the loosest setting**", ""]
    lines += table(["Buyer", "Awards", "suppliers at 30%", "at 20%", "at 10%"], rw_rows)
    lines += top(nc, "non-competitive value share")
    lines += top(vo, "value_outlier awards")
    lines += top(ds, "date_sequence processes")
    return lines


def main() -> int:
    result = scan()
    OUTPUT.write_text(build_report(result), encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
