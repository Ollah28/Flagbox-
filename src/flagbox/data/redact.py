"""Privacy rules applied at ingest (SPEC section 6.2, CLAUDE.md hard rule 5).

- contactPoint blocks (names, emails, phone numbers) are dropped before anything is read.
- A supplier name is kept only if it carries an incorporation marker (Ltd, Limited, PLC, LLP,
  Inc, Corporation). Anything else could be a person, so only a placeholder is stored.
- Even with a marker, a name is withheld if it contains an '@' or a run of 7+ digits: the data
  has names like "... LIMITED TEL 0721..." where a phone number was typed into the name field.
"""

import hashlib
import re
from typing import Any

# "Company Limited" and "Co. Ltd" are covered by "Limited" and "Ltd".
_MARKER = re.compile(r"\b(?:ltd|limited|plc|llp|inc|corporation)\b", re.IGNORECASE)
_NON_WORD = re.compile(r"[^\w]+")
_CONTACT_DETAIL = re.compile(r"@|\d{7,}")


def has_incorporation_marker(name: str) -> bool:
    return _MARKER.search(name) is not None


def normalise_name(name: str) -> str:
    """Lower-case, punctuation to single spaces. 'Acme  Ltd.' and 'ACME LTD' match."""
    return _NON_WORD.sub(" ", name.casefold()).strip()


def supplier_key(raw_id: str | None, name: str | None) -> str | None:
    """Raw ID if present, else a hash of the normalised name (D21). None if neither exists.

    The hash means a withheld name can never be read back out of the key.
    """
    if raw_id:
        return raw_id
    normalised = normalise_name(name) if name else ""
    if not normalised:
        return None
    return "name:" + hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:12]


def display_name(name: str | None, key: str) -> tuple[str, bool]:
    """Return (display_name, name_withheld). The real name is returned only with a marker."""
    if name and has_incorporation_marker(name) and not _CONTACT_DETAIL.search(name):
        return " ".join(name.split()), False
    return f"Supplier {key} (name withheld)", True


def drop_contact_points(value: Any) -> Any:
    """Return a copy of a parsed JSON value with every 'contactPoint' key removed, at any depth."""
    if isinstance(value, dict):
        return {k: drop_contact_points(v) for k, v in value.items() if k != "contactPoint"}
    if isinstance(value, list):
        return [drop_contact_points(v) for v in value]
    return value
