"""The no-live-tenders filter (SPEC section 4, decision D20; CLAUDE.md hard rule 6).

The source files never fill in tender.status or award.status, so we cannot read a process's
stage from them. Instead a process is kept only when it has at least one award AND its tender
period ended before today (or has no end date, which an award already makes safe).
"""

from datetime import date
from typing import Any

NO_AWARD = "no_award"
END_DATE_NOT_PAST = "end_date_not_past"
END_DATE_UNREADABLE = "end_date_unreadable"

REASONS = (NO_AWARD, END_DATE_NOT_PAST, END_DATE_UNREADABLE)


def _end_date(release: dict[str, Any]) -> object:
    tender = release.get("tender")
    period = tender.get("tenderPeriod") if isinstance(tender, dict) else None
    return period.get("endDate") if isinstance(period, dict) else None


def exclusion_reason(release: dict[str, Any], today: date) -> str | None:
    """Return why this process must be excluded, or None if it may be ingested."""
    awards = release.get("awards")
    if not isinstance(awards, list) or not awards:
        return NO_AWARD

    end = _end_date(release)
    if end is None or end == "":
        return None
    try:
        # Dates look like "2024-04-15T00:00:00+03:00"; the calendar date is the first 10 chars.
        ended_on = date.fromisoformat(str(end)[:10])
    except ValueError:
        return END_DATE_UNREADABLE
    return END_DATE_NOT_PAST if ended_on >= today else None
