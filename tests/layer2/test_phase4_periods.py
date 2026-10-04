"""Phase 4: period phrases are resolved in code, anchored at the snapshot date."""
import datetime as dt

import pytest

from c360.layer2 import periods

SNAP = dt.date(2026, 9, 28)
D = dt.date

CASES = [
    # last N days | weeks | months (the N days ending on the snapshot date, inclusive)
    ("last 90 days", D(2026, 7, 1), SNAP),
    ("last 30 days", D(2026, 8, 30), SNAP),
    ("last 7 days", D(2026, 9, 22), SNAP),
    ("last 1 day", SNAP, SNAP),
    ("Last 90 Days", D(2026, 7, 1), SNAP),
    ("past 90 days", D(2026, 7, 1), SNAP),
    ("the last 90 days", D(2026, 7, 1), SNAP),
    ("last_90_days", D(2026, 7, 1), SNAP),
    ("last 2 weeks", D(2026, 9, 15), SNAP),
    ("last 4 weeks", D(2026, 9, 1), SNAP),
    ("last 3 months", D(2026, 6, 29), SNAP),
    ("last 6 months", D(2026, 3, 29), SNAP),
    ("last six months", D(2026, 3, 29), SNAP),
    ("last 12 months", D(2025, 9, 29), SNAP),
    ("last 2 years", D(2024, 9, 29), SNAP),
    ("last week", D(2026, 9, 22), SNAP),
    # calendar periods
    ("this month", D(2026, 9, 1), SNAP),
    ("month to date", D(2026, 9, 1), SNAP),
    ("last month", D(2026, 8, 1), D(2026, 8, 31)),
    ("previous month", D(2026, 8, 1), D(2026, 8, 31)),
    ("this quarter", D(2026, 7, 1), SNAP),
    ("last quarter", D(2026, 4, 1), D(2026, 6, 30)),
    ("year to date", D(2026, 1, 1), SNAP),
    ("YTD", D(2026, 1, 1), SNAP),
    ("this year", D(2026, 1, 1), SNAP),
    ("last year", D(2025, 1, 1), D(2025, 12, 31)),
    ("this week", D(2026, 9, 28), SNAP),          # the snapshot date is a Monday
    ("today", SNAP, SNAP),
    ("yesterday", D(2026, 9, 27), D(2026, 9, 27)),
    # explicit dates
    ("since 2026-03-15", D(2026, 3, 15), SNAP),
    ("2026-01-01 to 2026-06-30", D(2026, 1, 1), D(2026, 6, 30)),
    ("from 2026-01-01 to 2026-06-30", D(2026, 1, 1), D(2026, 6, 30)),
    ("between 2026-01-01 and 2026-06-30", D(2026, 1, 1), D(2026, 6, 30)),
    ("2026-08-15", D(2026, 8, 15), D(2026, 8, 15)),
    # month, quarter, year
    ("August 2026", D(2026, 8, 1), D(2026, 8, 31)),
    ("aug 2026", D(2026, 8, 1), D(2026, 8, 31)),
    ("in August 2026", D(2026, 8, 1), D(2026, 8, 31)),
    ("February 2024", D(2024, 2, 1), D(2024, 2, 29)),
    ("2026-08", D(2026, 8, 1), D(2026, 8, 31)),
    ("Q3 2026", D(2026, 7, 1), D(2026, 9, 30)),
    ("2026 Q1", D(2026, 1, 1), D(2026, 3, 31)),
    ("2025", D(2025, 1, 1), D(2025, 12, 31)),
    ("August", D(2026, 8, 1), D(2026, 8, 31)),
    ("November", D(2025, 11, 1), D(2025, 11, 30)),   # the most recent November on or before the snapshot
    ("all time", periods.MIN_DATE, periods.MAX_DATE),
]


@pytest.mark.parametrize("text,start,end", CASES)
def test_resolve(text, start, end):
    got_start, got_end, label = periods.resolve(text, SNAP)
    assert (got_start, got_end) == (start, end), text
    assert label


def test_exclusive_mode_counts_back_from_the_snapshot_date():
    assert periods.resolve("last 90 days", SNAP, "exclusive")[:2] == (D(2026, 6, 30), SNAP)     # as Layer 1's golden.py
    assert periods.resolve("last 30 days", SNAP, "exclusive")[:2] == (D(2026, 8, 29), SNAP)
    assert periods.resolve("last 7 days", SNAP, "exclusive")[:2] == (D(2026, 9, 21), SNAP)
    assert periods.resolve("last 3 months", SNAP, "exclusive")[:2] == (D(2026, 6, 28), SNAP)
    with pytest.raises(ValueError):
        periods.resolve("last 90 days", SNAP, "sideways")


def test_anchor_is_the_snapshot_not_today():
    assert periods.resolve("last 30 days", "2024-03-31")[:2] == (D(2024, 3, 2), D(2024, 3, 31))
    assert periods.resolve("last month", dt.datetime(2024, 3, 31, 5, 0))[:2] == (D(2024, 2, 1), D(2024, 2, 29))
    assert periods.resolve("last 1 months", "2024-03-31")[:2] == (D(2024, 3, 1), D(2024, 3, 31))
    assert periods.resolve("last quarter", "2026-02-10")[:2] == (D(2025, 10, 1), D(2025, 12, 31))


def test_labels():
    assert periods.resolve("last 90 days", SNAP)[2] == "1 Jul–28 Sep 2026"
    assert periods.resolve("last 12 months", SNAP)[2] == "29 Sep 2025–28 Sep 2026"
    assert periods.resolve("today", SNAP)[2] == "28 Sep 2026"
    assert periods.resolve("all time", SNAP)[2] == "all time"


@pytest.mark.parametrize("text", ["", "   ", "soon", "last few days", "next month", "last 0 days", "2026-13-01", "Smarch 2026",
                                  "since yesterday", "2026-02-30 to 2026-03-01", "last -3 days", "the before times"])
def test_unknown_period_lists_accepted_forms(text):
    with pytest.raises(periods.PeriodError) as e:
        periods.resolve(text, SNAP)
    assert "YYYY-MM-DD" in str(e.value) or "Accepted forms" in str(e.value)


def test_reversed_range_is_an_error():
    with pytest.raises(periods.PeriodError, match="starts after it ends"):
        periods.resolve("2026-06-30 to 2026-01-01", SNAP)
