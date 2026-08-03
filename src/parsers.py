"""ISO8601 duration parsing and UTC <-> KST conversion.

KST has no DST, so a fixed +9h offset is used instead of a tzdata-backed
zoneinfo lookup (avoids the optional `tzdata` package on Windows).
"""
import re
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

_DURATION_RE = re.compile(
    r"^P"
    r"(?:(?P<years>\d+)Y)?"
    r"(?:(?P<months>\d+)M)?"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T"
    r"(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+)S)?"
    r")?$"
)


def parse_iso8601_duration(duration: str) -> int:
    """Parse an ISO8601 duration (e.g. 'PT1H2M3S') into total seconds.

    Returns 0 for falsy input. Raises ValueError for malformed strings.
    """
    if not duration:
        return 0
    match = _DURATION_RE.match(duration)
    if not match:
        raise ValueError(f"invalid ISO8601 duration: {duration!r}")
    parts = match.groupdict()
    if not any(parts.values()):
        raise ValueError(f"invalid ISO8601 duration: {duration!r}")
    days = int(parts["years"] or 0) * 365 + int(parts["months"] or 0) * 30 + int(parts["days"] or 0)
    hours = int(parts["hours"] or 0)
    minutes = int(parts["minutes"] or 0)
    seconds = int(parts["seconds"] or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def is_short_candidate(duration_sec: int, max_duration_sec: int = 180) -> bool:
    return duration_sec <= max_duration_sec


def parse_utc(iso_str: str) -> datetime:
    """Parse an ISO8601 UTC timestamp (accepts trailing 'Z') into an aware datetime."""
    s = iso_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_to_kst(value) -> datetime:
    """Convert a UTC ISO8601 string or aware/naive datetime to KST."""
    if isinstance(value, str):
        dt = parse_utc(value)
    elif isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        raise TypeError(f"unsupported type for utc_to_kst: {type(value)!r}")
    return dt.astimezone(KST)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_utc_iso() -> str:
    return now_utc().isoformat()
