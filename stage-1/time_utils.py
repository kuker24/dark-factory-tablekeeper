"""Time, slot-grid, and DST helpers.

Reservations are described in wall-clock at the restaurant's IANA timezone:
`starts_at_local` is a bare `YYYY-MM-DDTHH:MM`, with no offset and no seconds.
We resolve it through `zoneinfo` so the spring-forward gap and the fall-back
overlap follow the zone's published rules.

The slot grid is wall-clock minutes from the restaurant's `opens` time, stepped
by `slot_minutes`. A slot exists only when `slot + reservation_duration_minutes`
fits inside that day's closing minutes, AND the local time actually exists in
the zone (so the spring-forward hour is skipped). A fall-back hour is not
duplicated: the slot appears once and resolves to the first occurrence
(pre-DST), so the second wall-clock copy is not bookable.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Optional
from zoneinfo import ZoneInfo


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class LocalTimeError(ValueError):
    """Raised when a starts_at_local cannot be resolved against the zone."""


class InvalidLocalTimeError(LocalTimeError):
    """The local time does not exist (spring-forward gap)."""


_LT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$")


def parse_local(raw: str) -> dt.date | dt.datetime:
    """Parse a bare `YYYY-MM-DDTHH:MM`. Returns a datetime (no tz)."""
    if not isinstance(raw, str):
        raise LocalTimeError("not a string")
    m = _LT_RE.match(raw)
    if not m:
        raise LocalTimeError("format")
    y, mo, d, hh, mm = (int(x) for x in m.groups())
    try:
        return dt.datetime(y, mo, d, hh, mm)
    except ValueError as exc:
        raise LocalTimeError("range") from exc


def is_gap(local_str: str, tz_name: str) -> bool:
    """True iff `local_str` is a non-existent wall-clock time in `tz_name`.

    Detection: take the requested wall-clock with `fold=0` and `fold=1`,
    convert both to UTC, and look at the sign of the UTC delta. For a real
    ambiguous time (fall-back overlap), `fold=1` picks a later UTC instant
    (positive delta). For a gap (spring-forward skip), `fold=1` picks an
    EARLIER UTC instant (negative delta), because Python's `zoneinfo` maps
    the gap to either of two offsets, both of which lie before the
    transition in absolute time.
    """
    try:
        parsed = parse_local(local_str)
    except LocalTimeError:
        return False
    tz = ZoneInfo(tz_name)
    f0 = parsed.replace(tzinfo=tz, fold=0).astimezone(dt.timezone.utc)
    f1 = parsed.replace(tzinfo=tz, fold=1).astimezone(dt.timezone.utc)
    return (f1 - f0).total_seconds() < 0


def resolve_local(local_str: str, tz_name: str) -> dt.datetime:
    """Resolve a bare local timestamp against a timezone.

    Returns a timezone-aware datetime in `tz_name` (the first occurrence
    after a fold). Raises `InvalidLocalTimeError` if the local time is in
    the spring-forward gap.
    """
    parsed = parse_local(local_str)
    if is_gap(local_str, tz_name):
        raise InvalidLocalTimeError("gap")
    tz = ZoneInfo(tz_name)
    # `fold=0` picks the first occurrence on a fall-back night, per spec D11.
    return parsed.replace(tzinfo=tz, fold=0)


def weekday_name(date: dt.date) -> str:
    return WEEKDAYS[date.weekday()]


def is_finite_local_time(local_str: str, tz_name: str) -> bool:
    """True iff `local_str` exists as a wall-clock moment in `tz_name`."""
    try:
        resolve_local(local_str, tz_name)
        return True
    except InvalidLocalTimeError:
        return False
    except LocalTimeError:
        return False


def hhmm_to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def minutes_to_hhmm(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


@dataclass(frozen=True)
class SlotGrid:
    """Pre-computed opening-hour grid for one weekday."""

    opens_minutes: int
    closes_minutes: int
    slot_minutes: int
    duration_minutes: int

    def slot_starts(self) -> list[int]:
        """All slot starts (in local minutes from midnight) that fit before closing."""
        out = []
        t = self.opens_minutes
        end = self.closes_minutes
        while t + self.duration_minutes <= end:
            out.append(t)
            t += self.slot_minutes
        return out


def build_grid(opens: str, closes: str, slot_minutes: int,
               duration_minutes: int) -> SlotGrid:
    return SlotGrid(
        opens_minutes=hhmm_to_minutes(opens),
        closes_minutes=hhmm_to_minutes(closes),
        slot_minutes=slot_minutes,
        duration_minutes=duration_minutes,
    )


def slot_starts_for_weekday(opens_hhmm: str, closes_hhmm: str,
                            slot_minutes: int, duration_minutes: int,
                            date: dt.date, tz_name: str) -> list[str]:
    """Returns the local slot-start HH:MM strings that exist for `date`.

    Filters out wall-clock minutes that do not exist in the zone
    (spring-forward gap) and collapses the fall-back hour to the first
    occurrence by simply emitting each grid slot once: the first occurrence
    is the one before the clocks change, since `slot_minutes` is the step and
    we iterate in order. When the grid enters the repeated hour we still emit
    one slot; when it enters the spring-forward gap we skip the absent minute
    (the grid step naturally lands past it).
    """
    opens = hhmm_to_minutes(opens_hhmm)
    closes = hhmm_to_minutes(closes_hhmm)
    out: list[str] = []
    t = opens
    while t + duration_minutes <= closes:
        hhmm = minutes_to_hhmm(t)
        local = f"{date.isoformat()}T{hhmm}"
        if is_finite_local_time(local, tz_name):
            out.append(hhmm)
        t += slot_minutes
    return out


def datetime_to_local_str(value: dt.datetime) -> str:
    """Format a tz-aware datetime as `YYYY-MM-DDTHH:MM` in its own zone."""
    local = value.astimezone(value.tzinfo)
    return local.strftime("%Y-%m-%dT%H:%M")


def rfc3339(value: dt.datetime) -> str:
    """RFC 3339 with explicit offset (e.g. `2026-09-24T19:00:00+02:00`)."""
    aware = value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    offset = aware.utcoffset()
    if offset is None:
        offset = dt.timedelta(0)
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hh = total // 3600
    mm = (total % 3600) // 60
    return aware.strftime("%Y-%m-%dT%H:%M:%S") + f"{sign}{hh:02d}:{mm:02d}"


def reservation_end(start: dt.datetime, duration_minutes: int) -> dt.datetime:
    """Absolute end time, ignoring wall-clock (D11).

    Python's ``datetime + timedelta`` does wall-clock arithmetic on a
    tz-aware datetime when the offset is fixed, which is wrong across a DST
    transition: 90 minutes after 01:30 CEST on Berlin's fall-back night
    would otherwise land at 03:00 wall-clock instead of 02:00. We work in
    UTC so the addition is purely absolute and convert back at the end.
    """
    if start.tzinfo is None:
        return start + dt.timedelta(minutes=duration_minutes)
    utc = start.astimezone(dt.timezone.utc)
    end_utc = utc + dt.timedelta(minutes=duration_minutes)
    return end_utc.astimezone(start.tzinfo)


def overlaps(a_start: dt.datetime, a_end: dt.datetime,
             b_start: dt.datetime, b_end: dt.datetime) -> bool:
    """Half-open `[start, end)` overlap test."""
    return a_start < b_end and b_start < a_end


def parse_iso_date(raw: str) -> Optional[dt.date]:
    if not isinstance(raw, str):
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return None


def format_iso_date(d: dt.date) -> str:
    return d.isoformat()


def parse_rfc3339(raw: str) -> dt.datetime:
    """Parse an offset-bearing rfc3339 timestamp from ``rfc3339()``."""
    if not isinstance(raw, str):
        raise ValueError("not a string")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("format") from exc
    if parsed.tzinfo is None:
        raise ValueError("missing offset")
    return parsed
