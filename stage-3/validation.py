"""Input validation helpers shared by the HTTP handlers.

Each helper raises a small typed exception (``MalformedRequest``,
``ValidationFailed``) that the handler converts into the spec's error
envelope. We keep the helpers tiny on purpose: every branch is a place a
regression could hide, and the rules are easier to read when each one is a
direct check.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Optional

from time_utils import LocalTimeError, parse_local, parse_iso_date


class HttpError(Exception):
    """Base class for any HTTP-visible error from validation."""

    def __init__(self, status: int, code: str, message: Optional[str] = None):
        self.status = status
        self.code = code
        self.message = message or code
        super().__init__(message or code)


class MalformedRequest(HttpError):
    """400 ``malformed_request``. JSON that cannot be parsed, or a wrong type."""

    def __init__(self, message: Optional[str] = None):
        super().__init__(400, "malformed_request", message or "body could not be parsed")


class ValidationFailed(HttpError):
    """422 ``validation_failed``. Right type, invalid value."""

    def __init__(self, message: Optional[str] = None, *, code: Optional[str] = None):
        super().__init__(422, code or "validation_failed", message or code or "validation failed")


_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_REFERENCE_PATTERN = re.compile(r"^[A-Z0-9]{6,12}$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+$")


def is_valid_id(value: Any) -> bool:
    """ID per §3.4: opaque string of at most 64 characters."""
    return isinstance(value, str) and bool(_ID_PATTERN.match(value))


def is_valid_reference(value: Any) -> bool:
    """``reference`` per §8: 6..12 characters of A-Z0-9."""
    return isinstance(value, str) and bool(_REFERENCE_PATTERN.match(value))


def check_id(field: str, value: Any) -> str:
    """An opaque 1..64-character identifier.

    Per §5, a value of the wrong JSON type (int, bool, list, …) is 400
    ``malformed_request``. A missing field (``None``) and a string of
    the wrong shape are 422 ``validation_failed`` — the field is
    expected but not present or not valid.
    """
    if value is None:
        raise ValidationFailed(f"{field!r} is required")
    if not isinstance(value, str):
        raise MalformedRequest(f"{field!r} must be a string")
    if not is_valid_id(value):
        raise ValidationFailed(f"{field!r} must be 1..64 opaque characters")
    return value


def check_reference(field: str, value: Any) -> str:
    if value is None:
        raise ValidationFailed(f"{field!r} is required")
    if not isinstance(value, str):
        raise MalformedRequest(f"{field!r} must be a string")
    if not is_valid_reference(value):
        raise ValidationFailed(f"{field!r} must match ^[A-Z0-9]{{6,12}}$")
    return value


def check_email(value: Any) -> str:
    if not isinstance(value, str):
        raise MalformedRequest("email must be a string")
    if not _EMAIL_PATTERN.match(value):
        raise ValidationFailed("email must look like local@domain")
    return value


def check_password(value: Any) -> str:
    if not isinstance(value, str):
        # Wrong type is reserved for 400 by §5, but signup's password rule
        # is a content rule and the carve-out for party_size doesn't apply,
        # so we keep this as a malformed body for non-strings.
        raise MalformedRequest("password must be a string")
    if len(value) < 8:
        raise ValidationFailed("password must be at least 8 characters")
    return value


def check_display_name(value: Any) -> str:
    if not isinstance(value, str):
        raise MalformedRequest("display_name must be a string")
    return value


def check_string_field(name: str, value: Any, *, allow_none: bool = False) -> Optional[str]:
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise MalformedRequest(f"{name!r} must be a string")
    return value


def parse_json_body(raw: bytes) -> dict:
    """Parse an HTTP body into a dict. Raises MalformedRequest on failure."""
    if not raw:
        return {}
    try:
        parsed = __import__("json").loads(raw)
    except ValueError as exc:
        raise MalformedRequest(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise MalformedRequest("body must be a JSON object")
    return parsed


def check_idempotency_key(value: Any) -> str:
    """Per §7: 1..255 characters, otherwise 422 ``validation_failed``."""
    if not isinstance(value, str):
        # The header is supposed to be a string; missing/empty is 400.
        raise HttpError(400, "missing_idempotency_key", "Idempotency-Key header is required")
    if not value:
        raise HttpError(400, "missing_idempotency_key", "Idempotency-Key must not be empty")
    if len(value) > 255:
        raise ValidationFailed("Idempotency-Key must be at most 255 characters")
    return value


def check_local_datetime(value: Any, field: str = "starts_at_local") -> str:
    """``starts_at_local`` is a bare ``YYYY-MM-DDTHH:MM`` per §8."""
    if not isinstance(value, str):
        raise MalformedRequest(f"{field!r} must be a string")
    try:
        parse_local(value)
    except LocalTimeError:
        raise ValidationFailed(f"{field!r} must look like YYYY-MM-DDTHH:MM")
    return value


def check_date_param(value: Any, field: str = "date") -> str:
    """``date`` is a calendar date (``YYYY-MM-DD``)."""
    if not isinstance(value, str):
        raise ValidationFailed(f"{field!r} must be a date string")
    if parse_iso_date(value) is None:
        raise ValidationFailed(f"{field!r} must look like YYYY-MM-DD")
    return value


def check_party_size(value: Any) -> int:
    """``party_size`` is an integer >= 1.

    Per D3 the wrong-type cases (string, bool, float) are 422, not 400.
    Per §5 the wrong-type-with-correct-shape (``"4"`` in JSON becomes the
    integer 4; only an actual string stays a string) ends up here as a
    string and gets 422.
    """
    # Python bool is a subclass of int; treat it as invalid.
    if isinstance(value, bool):
        raise ValidationFailed("party_size must be an integer")
    if isinstance(value, int):
        if value < 1:
            raise ValidationFailed("party_size must be >= 1")
        return value
    # Any other type (str, float, list, None) is wrong: 422 per §5 carve-out.
    raise ValidationFailed("party_size must be an integer")


def check_query_integer(value: Any, field: str) -> int:
    """Query integer per D4: plain decimal digits only (``1e9`` and ``4.0`` fail)."""
    if not isinstance(value, str):
        raise ValidationFailed(f"{field!r} must be decimal digits")
    if not value or not value.isascii() or not value.isdigit():
        raise ValidationFailed(f"{field!r} must be decimal digits")
    out = int(value)
    if out < 1:
        raise ValidationFailed(f"{field!r} must be >= 1")
    return out


def check_required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationFailed(f"{field!r} is required and must be a non-empty string")
    return value


def check_non_negative_int_field(value: Any, field: str) -> int:
    """Field that must be a non-negative integer (e.g. capacity, slot_minutes)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationFailed(f"{field!r} must be a non-negative integer")
    if value < 0:
        raise ValidationFailed(f"{field!r} must be >= 0")
    return value


def check_weekday(value: Any, field: str = "weekday") -> str:
    valid = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    if not isinstance(value, str) or value not in valid:
        raise ValidationFailed(f"{field!r} must be one of {sorted(valid)}")
    return value


def check_hhmm(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationFailed(f"{field!r} must look like HH:MM")
    try:
        hh, mm = value.split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        raise ValidationFailed(f"{field!r} must look like HH:MM")
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValidationFailed(f"{field!r} must look like HH:MM")
    return f"{h:02d}:{m:02d}"


def check_explain(value: Any) -> bool:
    """The ``explain`` query parameter (stage 3, ledger 1).

    Only ``"true"`` is accepted; any other value (``"false"``, ``"1"``,
    ``""`` …) raises 422 ``validation_failed``. Missing returns ``False``
    so the caller can keep stage-1's response shape.
    """
    if value is None:
        return False
    if not isinstance(value, str):
        raise ValidationFailed("'explain' must be a string")
    if value == "true":
        return True
    raise ValidationFailed("'explain' must be 'true' when present")


def check_count(value: Any) -> int:
    """Series ``count``: integer 2..12 inclusive (stage 3, ledger 30).

    Booleans are not integers (ledger 30 last clause).
    """
    if isinstance(value, bool):
        raise ValidationFailed("count must be an integer")
    if isinstance(value, int):
        if 2 <= value <= 12:
            return value
        raise ValidationFailed("count must be between 2 and 12")
    raise ValidationFailed("count must be an integer")


def check_interval_weeks(value: Any) -> int:
    """Series ``interval_weeks``: integer 1..4 (stage 3, ledger 30)."""
    if isinstance(value, bool):
        raise ValidationFailed("interval_weeks must be an integer")
    if isinstance(value, int):
        if 1 <= value <= 4:
            return value
        raise ValidationFailed("interval_weeks must be between 1 and 4")
    raise ValidationFailed("interval_weeks must be an integer")


def check_table_id_or_ids(value: Any) -> list[str]:
    """Resolve ``table_id``/``table_ids`` per E1/E2.

    Returns a list of table ids. Stage-1 callers that send ``table_id``
    get a singleton list. Stage-2 callers that send ``table_ids`` get the
    array. Sending both in the same request is 422 ``validation_failed``.
    Missing both is 422. Wrong JSON type is 400.
    """
    if not isinstance(value, dict):
        raise MalformedRequest("body must be a JSON object")
    has_id = "table_id" in value
    has_ids = "table_ids" in value
    if has_id and has_ids:
        raise ValidationFailed(
            "send either 'table_id' or 'table_ids', not both")
    if not has_id and not has_ids:
        raise ValidationFailed(
            "either 'table_id' or 'table_ids' is required")
    if has_id:
        tid = check_id("table_id", value.get("table_id"))
        return [tid]
    raw = value.get("table_ids")
    if not isinstance(raw, list):
        raise MalformedRequest("'table_ids' must be an array")
    if len(raw) == 0:
        raise ValidationFailed("'table_ids' must not be empty")
    out: list[str] = []
    for entry in raw:
        tid = check_id("table_ids[]", entry)
        out.append(tid)
    if len(set(out)) != len(out):
        raise ValidationFailed("'table_ids' must contain distinct ids")
    if len(out) > 2:
        raise ValidationFailed(
            "'table_ids' accepts at most two tables in this stage",
            code="combination_not_allowed")
    return out
