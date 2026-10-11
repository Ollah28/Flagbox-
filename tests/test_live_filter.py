from datetime import date

import pytest

from flagbox.data.live_filter import (
    END_DATE_NOT_PAST,
    END_DATE_UNREADABLE,
    NO_AWARD,
    exclusion_reason,
)

TODAY = date(2026, 10, 11)
AWARD = [{"id": "a1"}]


def release(awards=AWARD, end: str | None = "2024-04-15T00:00:00+03:00"):
    tender = {} if end is None else {"tenderPeriod": {"endDate": end}}
    return {"awards": awards, "tender": tender}


def test_process_with_award_and_past_end_date_is_kept():
    assert exclusion_reason(release(), TODAY) is None


@pytest.mark.parametrize("awards", [[], None, "not a list"])
def test_process_without_award_is_excluded(awards):
    assert exclusion_reason(release(awards=awards), TODAY) == NO_AWARD


def test_missing_awards_key_is_excluded():
    assert exclusion_reason({"tender": {}}, TODAY) == NO_AWARD


def test_future_end_date_is_excluded():
    assert exclusion_reason(release(end="2027-12-31T00:00:00+03:00"), TODAY) == END_DATE_NOT_PAST


def test_end_date_today_is_excluded_but_yesterday_is_kept():
    assert exclusion_reason(release(end="2026-10-11T00:00:00+03:00"), TODAY) == END_DATE_NOT_PAST
    assert exclusion_reason(release(end="2026-10-10T23:59:59+03:00"), TODAY) is None


def test_award_without_end_date_is_kept():
    assert exclusion_reason(release(end=None), TODAY) is None
    assert exclusion_reason(release(end=""), TODAY) is None


def test_unreadable_end_date_is_excluded():
    assert exclusion_reason(release(end="soon"), TODAY) == END_DATE_UNREADABLE


def test_no_award_is_reported_before_end_date_problems():
    assert exclusion_reason(release(awards=[], end="2099-01-01"), TODAY) == NO_AWARD


def test_odd_tender_shapes_do_not_crash():
    assert exclusion_reason({"awards": AWARD, "tender": "oops"}, TODAY) is None
    assert exclusion_reason({"awards": AWARD, "tender": {"tenderPeriod": 5}}, TODAY) is None
