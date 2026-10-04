"""Small helpers shared by the Layer 3 engines."""
import datetime as dt
import re


def as_date(v):
    """date from a date, datetime, ISO string or Mongo extended-JSON {"$date": ...}; else None."""
    if v is None or v == "":
        return None
    if isinstance(v, dict) and "$date" in v:
        v = v["$date"]
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return dt.date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    return None


def as_dt(v):
    """datetime from the same inputs as as_date (midnight when only a date is known)."""
    if v is None or v == "":
        return None
    if isinstance(v, dict) and "$date" in v:
        v = v["$date"]
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day)
    s = str(v).strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return dt.datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    d = as_date(s)
    return dt.datetime(d.year, d.month, d.day) if d else None


def money(v):
    if v is None:
        return None
    return f"${float(v):,.2f}"


def short_date(d):
    """28 Sep 2026."""
    d = as_date(d)
    return f"{d.day} {d.strftime('%b %Y')}" if d else None


def flag(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "t", "1", "yes", "y")
    return bool(v)


def jsonable(v):
    """Make DuckDB / Python values JSON-safe (dates to ISO strings, Decimals to float)."""
    import decimal
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, dt.datetime):
        return v.isoformat(sep=" ", timespec="seconds")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    return v
