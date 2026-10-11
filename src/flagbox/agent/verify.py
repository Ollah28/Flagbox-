"""The deterministic verifier (SPEC 3.3). No model is involved in any decision here.

A finding is accepted only if:
  1. its hit_id appears in the output of a logged tool call from THIS run (the claim text, OCIDs
     and source references are then copied from that logged output by code, never taken from
     the model);
  2. its severity is low, medium or high;
  3. its reason_code is one of the fixed reasons that is true for that hit (SPEC D31).
The model writes no explanation at all. The wording of a reason is fixed text in each indicator,
so rules 2, 4 and 5 (no digits or names, no accusing words) hold by construction. They are still
checked here on the fixed text, so a future edit to a phrase cannot break them unnoticed, and the
same checks (`check_rationale`) guard the free text that remains: skip reasons in the plan and,
from M3.1, the memo's summary paragraph.

Rule 5 is enforced by shape: withheld names are never stored anywhere, so they cannot be looked
up. Short plain text has no capitalised word after its first, so any that appears is refused.

Every function returns violations as data (rule id plus a message the model can act on).
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

SEVERITIES = ("low", "medium", "high")
MAX_RATIONALE_CHARS = 240

# SPEC 3.3 rule 4, written as prefixes so "frauds" and "fraudulent" are caught too.
BANNED_STEMS = (
    "corrupt",
    "fraud",
    "guilt",
    "stole",
    "steal",
    "illegal",
    "criminal",
    "bribe",
)
# Added on top of the SPEC list for the same reason (hard rule 1: the agent never accuses).
EXTRA_ACCUSATORY_STEMS = (
    "embezzl",
    "launder",
    "collu",
    "kickback",
    "theft",
    "thief",
    "thieves",
    "scam",
    "misappropriat",
    "rigged",
    "rigging",
)
_BANNED = re.compile(
    r"\b(?:" + "|".join(re.escape(s) for s in (*BANNED_STEMS, *EXTRA_ACCUSATORY_STEMS)) + r")\w*",
    re.IGNORECASE,
)

# Spelled-out numbers would get round the "no digits" rule ("won four of five awards").
# "one" and "first" are left out because they are ordinary words ("one supplier").
_NUMBER_WORDS = frozenset(
    "two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety "
    "hundred hundreds thousand thousands million millions billion billions dozen half double "
    "triple twice thrice percent".split()
)

# Words in supplier names that say nothing about who the supplier is, so they are not treated
# as identifying when checking a rationale.
_GENERIC_NAME_WORDS = frozenset(
    "company limited ltd plc llp inc corporation corp and the of for kenya kenyan general "
    "supplies supply services service traders trading enterprises enterprise contractors "
    "contractor construction suppliers ventures solutions holdings investments group "
    "international africa east national county printers printing building works".split()
)
_MARKERS = re.compile(r"\b(?:ltd|limited|plc|llp|inc|corporation)\b", re.IGNORECASE)
_LABEL = re.compile(r"^(?P<name>.*) \(ID (?P<key>[^)]+)\)$")
_WITHHELD_LABEL = re.compile(r"^Supplier (?P<key>\S+) \(name withheld\)$")
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


@dataclass(frozen=True)
class Violation:
    rule: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "message": self.message}


@dataclass(frozen=True)
class LoggedHit:
    """A hit exactly as an indicator tool returned it, plus where in the log it came from."""

    hit: dict[str, Any]
    tool_call_id: str
    tool: str
    sources: dict[str, Any]


@dataclass(frozen=True)
class Outcome:
    ok: bool
    violations: list[Violation] = field(default_factory=list)
    severity: str | None = None
    hit: LoggedHit | None = None
    reason_code: str | None = None
    rationale: str | None = None  # the fixed wording for the reason_code


def _normalise(text: str) -> str:
    return _NON_WORD.sub(" ", text.casefold()).strip()


# ---- reading the run's own tool outputs (rule 1, and the supplier list for rule 2) --------------


def _tool_results(
    records: Iterable[dict[str, Any]],
) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    for record in records:
        if record.get("event") == "tool_call" and not record.get("is_error"):
            result = record.get("result")
            if isinstance(result, dict):
                yield record, result


def find_hit(records: Iterable[dict[str, Any]], hit_id: str) -> LoggedHit | None:
    """The first logged tool result that contains this hit_id, or None (rule 1)."""
    for record, result in _tool_results(records):
        for hit in result.get("hits") or []:
            if isinstance(hit, dict) and hit.get("hit_id") == hit_id:
                return LoggedHit(
                    hit=hit,
                    tool_call_id=str(record["tool_call_id"]),
                    tool=str(record["tool"]),
                    sources=dict(result.get("sources") or {}),
                )
    return None


def _walk_supplier_labels(value: Any) -> Iterable[str]:
    """Every supplier label, supplier_key and supplier string anywhere in a tool result."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"supplier", "supplier_key"} and isinstance(item, str):
                yield item
            elif key == "suppliers" and isinstance(item, list):
                yield from (s for s in item if isinstance(s, str))
            else:
                yield from _walk_supplier_labels(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_supplier_labels(item)


def known_supplier_terms(records: Iterable[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """(identifying phrases, identifying keys) for every supplier this run has seen."""
    phrases: set[str] = set()
    keys: set[str] = set()
    for _, result in _tool_results(records):
        for label in _walk_supplier_labels(result):
            named = _LABEL.match(label)
            withheld = _WITHHELD_LABEL.match(label)
            if named:
                keys.add(named["key"])
                phrases.add(_normalise(_MARKERS.sub(" ", named["name"])))
            elif withheld:
                keys.add(withheld["key"])
            else:
                keys.add(label)  # a bare supplier_key
    return {p for p in phrases if p}, {k for k in keys if k}


# ---- the checks ----------------------------------------------------------------------


def check_severity(value: str) -> tuple[str | None, Violation | None]:
    cleaned = value.strip().lower() if isinstance(value, str) else ""
    if cleaned in SEVERITIES:
        return cleaned, None
    return None, Violation("severity", f"severity must be one of: {', '.join(SEVERITIES)}.")


def _capitalised_mid_sentence(text: str) -> list[str]:
    found: list[str] = []
    at_start = True
    for token in re.findall(r"[^\W\d_][\w'’-]*|[.!?]", text):
        if token in ".!?":
            at_start = True
            continue
        if not at_start and token[0].isupper():
            found.append(token)
        at_start = False
    return found


def check_rationale(
    text: str, supplier_phrases: set[str] | None = None, supplier_keys: set[str] | None = None
) -> list[Violation]:
    """Rules 2, 4 and 5 for a rationale (and the same shape rules for any short free text)."""
    if not isinstance(text, str) or not text.strip():
        return [Violation("format", "The rationale is empty. Write one short sentence.")]
    violations: list[Violation] = []

    if "\n" in text or "\r" in text or len(text) > MAX_RATIONALE_CHARS:
        violations.append(
            Violation(
                "format",
                f"Use one short line of at most {MAX_RATIONALE_CHARS} characters.",
            )
        )
    if any(ch.isdigit() for ch in text):
        violations.append(
            Violation("digits", "Remove every digit. Numbers come from the hit, not from you.")
        )

    words = set(_normalise(text).split())
    if words & _NUMBER_WORDS:
        violations.append(
            Violation(
                "numbers", "Remove spelled-out numbers and quantities such as 'four' or 'half'."
            )
        )

    padded = f" {_normalise(text)} "
    named = any(f" {phrase} " in padded for phrase in (supplier_phrases or set())) or any(
        token in words
        for phrase in (supplier_phrases or set())
        for token in phrase.split()
        if len(token) >= 4 and token not in _GENERIC_NAME_WORDS
    )
    keyed = any(
        len(key) >= 3 and f" {_normalise(key)} " in padded for key in (supplier_keys or set())
    )
    if named or keyed:
        violations.append(
            Violation(
                "supplier_name",
                "Do not name or identify any supplier. Say 'the supplier' or 'this pattern'.",
            )
        )

    banned = _BANNED.search(text)
    if banned:
        violations.append(
            Violation(
                "banned_word",
                f"Remove the word '{banned.group(0)}'. Describe a pattern that warrants review; "
                "never accuse.",
            )
        )

    if _capitalised_mid_sentence(text):
        violations.append(
            Violation(
                "proper_noun",
                "Remove capitalised words after the first word. No names of people, companies "
                "or places belong in a rationale.",
            )
        )
    return violations


# Given a hit, returns {reason_code: fixed text} for the reasons that are true for it.
ReasonLookup = Callable[[dict[str, Any]], dict[str, str]]


def verify_finding(
    records: list[dict[str, Any]],
    hit_id: str,
    severity: str,
    reason_code: str,
    reasons_for: ReasonLookup,
) -> Outcome:
    """Check one proposed finding against this run's log. Nothing here is written by the model
    except three short choices: which hit, how severe, and which fixed reason."""
    violations: list[Violation] = []

    logged = find_hit(records, hit_id) if isinstance(hit_id, str) else None
    if logged is None:
        violations.append(
            Violation(
                "unknown_hit",
                "That hit_id was not returned by any tool in this run. Use a hit_id exactly as a "
                "tool returned it.",
            )
        )

    clean_severity, bad_severity = check_severity(severity)
    if bad_severity:
        violations.append(bad_severity)

    code = reason_code.strip() if isinstance(reason_code, str) else ""
    allowed = reasons_for(logged.hit) if logged is not None else {}
    if logged is not None and code not in allowed:
        violations.append(
            Violation(
                "reason_code",
                "reason_code must be exactly one of the codes listed on the hit: "
                + ", ".join(allowed)
                + ".",
            )
        )

    if violations or logged is None:
        return Outcome(False, violations)

    rationale = allowed[code]
    for problem in check_rationale(rationale):  # the fixed text must itself obey the wording rules
        violations.append(Violation("fixed_text", f"{code}: {problem.rule}"))
    if violations:
        return Outcome(False, violations)
    return Outcome(True, [], clean_severity, logged, code, rationale)
