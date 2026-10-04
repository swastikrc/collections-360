"""Resolve a period phrase such as "last 90 days" into explicit dates, in code.

The agent passes the phrase; it never does date arithmetic itself. Everything is anchored at the
snapshot date of the serving database, never at today's date.

Accepted forms (case-insensitive):
  last N days | last N weeks | last N months | last N years      (also: past / previous / trailing)
  today | yesterday | last week | this week
  this month | month to date | last month
  this quarter | quarter to date | last quarter
  this year | year to date | last year | last 12 months
  since YYYY-MM-DD
  YYYY-MM-DD to YYYY-MM-DD          (also: from A to B, between A and B)
  YYYY-MM-DD                        (a single day)
  August 2026 | Aug 2026 | 2026-08  (a calendar month)
  Q3 2026 | 2026 Q3                 (a calendar quarter)
  2026                              (a calendar year)
  all time
"""
import calendar
import datetime as dt
import re

ACCEPTED = ("last N days|weeks|months|years, today, yesterday, this week, last week, this month, last month, "
            "this quarter, last quarter, this year, last year, year to date, last 12 months, since YYYY-MM-DD, "
            "YYYY-MM-DD to YYYY-MM-DD, a single date YYYY-MM-DD, a month such as 'August 2026' or '2026-08', "
            "a quarter such as 'Q3 2026', a year such as '2026', all time")

MIN_DATE = dt.date(1900, 1, 1)
MAX_DATE = dt.date(2999, 12, 31)

_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
_MONTHS["sept"] = 9
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10, "eleven": 11, "twelve": 12}
_ISO = r"(\d{4}-\d{2}-\d{2})"


class PeriodError(ValueError):
    pass


def _to_date(value) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def _iso(text) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError as e:
        raise PeriodError(f"'{text}' is not a valid date (use YYYY-MM-DD)") from e


def _month_end(year, month) -> dt.date:
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def _shift_months(d: dt.date, months: int) -> dt.date:
    y, m = divmod(d.year * 12 + d.month - 1 + months, 12)
    return dt.date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def _quarter_start(d: dt.date) -> dt.date:
    return dt.date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def label(start: dt.date, end: dt.date) -> str:
    """Human label for a window, e.g. '1 Jul-28 Sep 2026'."""
    if start <= MIN_DATE and end >= MAX_DATE:
        return "all time"
    fmt = lambda d, year: f"{d.day} {calendar.month_abbr[d.month]}" + (f" {d.year}" if year else "")  # noqa: E731
    if start == end:
        return fmt(start, True)
    if start <= MIN_DATE:
        return f"up to {fmt(end, True)}"
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day}\u2013{fmt(end, True)}"
    return f"{fmt(start, start.year != end.year)}–{fmt(end, True)}"


def resolve(text, snapshot, last_n_days="inclusive"):
    """(start_date, end_date, label) for a period phrase. Both dates are inclusive.

    last_n_days: "inclusive" -> "last 90 days" is the 90 days ending on the snapshot date;
                 "exclusive" -> it starts 90 days before the snapshot date (91 calendar days).
    """
    snap = _to_date(snapshot)
    if last_n_days not in ("inclusive", "exclusive"):
        raise ValueError("last_n_days must be 'inclusive' or 'exclusive'")
    back = 1 if last_n_days == "inclusive" else 0
    raw = str(text or "").strip()
    t = re.sub(r"\s+", " ", raw.lower().replace("_", " ")).strip(" .")
    t = re.sub(r"^(in|for|during|over|within) ", "", t)
    t = re.sub(r"^the ", "", t)
    if not t:
        raise PeriodError(f"empty period. Accepted forms: {ACCEPTED}")

    def out(start, end):
        if start > end:
            raise PeriodError(f"period '{raw}' starts after it ends ({start} > {end})")
        return start, end, label(start, end)

    if t in ("all time", "all", "alltime", "ever", "any", "to date", "overall", "entire history", "lifetime"):
        return out(MIN_DATE, MAX_DATE)
    if t in ("today", "now", "current", "snapshot", "as of today", "snapshot date"):
        return out(snap, snap)
    if t == "yesterday":
        return out(snap - dt.timedelta(days=1), snap - dt.timedelta(days=1))

    m = re.fullmatch(r"(?:last|past|previous|prior|trailing|recent) (\d+|[a-z]+) (day|week|month|year)s?", t)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _WORDS.get(m.group(1))
        if not n or n <= 0:
            raise PeriodError(f"cannot read the number in '{raw}'. Accepted forms: {ACCEPTED}")
        unit = m.group(2)
        if unit == "day":
            return out(snap - dt.timedelta(days=n - back), snap)
        if unit == "week":
            return out(snap - dt.timedelta(days=7 * n - back), snap)
        months = n if unit == "month" else 12 * n
        return out(_shift_months(snap, -months) + dt.timedelta(days=back), snap)

    if t in ("last week", "past week", "previous week"):
        return out(snap - dt.timedelta(days=7 - back), snap)
    if t == "this week":
        return out(snap - dt.timedelta(days=snap.weekday()), snap)
    if t in ("this month", "month to date", "mtd", "current month"):
        return out(snap.replace(day=1), snap)
    if t in ("last month", "previous month", "prior month", "past month"):
        first = _shift_months(snap.replace(day=1), -1)
        return out(first, _month_end(first.year, first.month))
    if t in ("this quarter", "quarter to date", "qtd", "current quarter"):
        return out(_quarter_start(snap), snap)
    if t in ("last quarter", "previous quarter", "prior quarter", "past quarter"):
        start = _shift_months(_quarter_start(snap), -3)
        last = _shift_months(start, 2)
        return out(start, _month_end(last.year, last.month))
    if t in ("this year", "year to date", "ytd", "current year"):
        return out(dt.date(snap.year, 1, 1), snap)
    if t in ("last year", "previous year", "prior year"):
        return out(dt.date(snap.year - 1, 1, 1), dt.date(snap.year - 1, 12, 31))

    m = re.fullmatch(rf"(?:since|from|after|starting) {_ISO}(?: (?:onwards|on))?", t)
    if m:
        return out(_iso(m.group(1)), snap)
    m = re.fullmatch(rf"(?:from |between )?{_ISO} ?(?:to|and|until|through|thru|-|–|\.\.) ?{_ISO}", t)
    if m:
        return out(_iso(m.group(1)), _iso(m.group(2)))
    m = re.fullmatch(_ISO, t)
    if m:
        return out(_iso(m.group(1)), _iso(m.group(1)))

    m = re.fullmatch(r"([a-z]+)\.?,? (\d{4})", t)
    if m and m.group(1) in _MONTHS:
        year, month = int(m.group(2)), _MONTHS[m.group(1)]
        return out(dt.date(year, month, 1), _month_end(year, month))
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", t)
    if m and 1 <= int(m.group(2)) <= 12:
        year, month = int(m.group(1)), int(m.group(2))
        return out(dt.date(year, month, 1), _month_end(year, month))
    m = re.fullmatch(r"q([1-4]) (\d{4})", t) or re.fullmatch(r"(\d{4}) ?-?q([1-4])", t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        quarter, year = (a, b) if a <= 4 else (b, a)
        start = dt.date(year, 3 * quarter - 2, 1)
        return out(start, _month_end(year, 3 * quarter))
    m = re.fullmatch(r"(?:year |calendar year |fy ?)?(\d{4})", t)
    if m and 1990 <= int(m.group(1)) <= 2100:
        return out(dt.date(int(m.group(1)), 1, 1), dt.date(int(m.group(1)), 12, 31))
    if t in _MONTHS:        # a bare month name: the most recent such month on or before the snapshot
        month = _MONTHS[t]
        year = snap.year if month <= snap.month else snap.year - 1
        return out(dt.date(year, month, 1), _month_end(year, month))

    raise PeriodError(f"cannot understand the period '{raw}'. Accepted forms: {ACCEPTED}")
