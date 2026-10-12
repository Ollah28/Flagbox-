"""The human gate: what a person must type, and how it is checked (SPEC 2, step 5; A8).

A named human approves or rejects. The decision carries the SHA-256 of the exact memo they
were shown, so an approval can never be applied to different bytes. Everything here is plain
code; the model has no part in it.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

APPROVE = "approve"
REJECT = "reject"
MIN_NAME = 2
MAX_NAME = 80
MAX_NOTE = 500
MAX_TRIES = 3

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Decision:
    approver_name: str
    decision: str
    note: str
    memo_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "approver_name": self.approver_name,
            "decision": self.decision,
            "note": self.note,
            "memo_sha256": self.memo_sha256,
        }


def check_name(value: object) -> str | None:
    """Return a problem with a typed name, or None if it is acceptable."""
    if not isinstance(value, str):
        return "The name must be text."
    name = value.strip()
    if _CONTROL.search(name):
        return "The name may not contain control characters."
    if not (MIN_NAME <= len(name) <= MAX_NAME):
        return f"The name must be {MIN_NAME} to {MAX_NAME} characters."
    if not any(ch.isalpha() for ch in name):
        return "The name must contain letters."
    return None


def parse_decision(raw: object) -> tuple[Decision | None, str | None]:
    """Validate a decision as it comes back from the gate. Returns (decision, problem)."""
    if not isinstance(raw, dict):
        return None, "The decision is not in the expected form."
    problem = check_name(raw.get("approver_name"))
    if problem:
        return None, problem
    verdict = str(raw.get("decision", "")).strip().lower()
    if verdict not in (APPROVE, REJECT):
        return None, "The decision must be approve or reject."
    sha = str(raw.get("memo_sha256", "")).strip().lower()
    if not _SHA256.match(sha):
        return None, "The decision does not carry a valid memo hash."
    note = " ".join(str(raw.get("note") or "").split())
    if len(note) > MAX_NOTE or _CONTROL.search(note):
        return None, f"The note must be at most {MAX_NOTE} characters."
    return Decision(str(raw["approver_name"]).strip(), verdict, note, sha), None


def collect_decision(
    ask: Callable[[str], str], say: Callable[[str], None], memo_sha256: str
) -> dict[str, str] | None:
    """Ask a person at the terminal. Returns None if they decline to decide (nothing is filed)."""
    say("Review the memo above before deciding.")
    say(f"Your decision is tied to this exact memo, SHA-256 {memo_sha256}.")
    say("Approve files exactly these bytes. Reject files nothing.")
    say("Leave the name empty to decide later (nothing is filed).")

    name = ""
    for _ in range(MAX_TRIES):
        name = ask("Your full name: ").strip()
        if not name:
            return None
        problem = check_name(name)
        if problem is None:
            break
        say(problem)
    else:
        return None

    verdict = ""
    for _ in range(MAX_TRIES):
        answer = ask("Decision, approve or reject [a/r]: ").strip().lower()
        if answer in ("a", APPROVE):
            verdict = APPROVE
        elif answer in ("r", REJECT):
            verdict = REJECT
        if verdict:
            break
        say("Type a for approve or r for reject.")
    if not verdict:
        return None

    note = ask("Note (optional, kept with the record): ").strip()
    return {"approver_name": name, "decision": verdict, "note": note, "memo_sha256": memo_sha256}


def summarise(decision: dict[str, Any]) -> str:
    return f"{decision['decision']} by {decision['approver_name']}"
