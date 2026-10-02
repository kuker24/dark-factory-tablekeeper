"""Business operations: reset, signup/login, reservations, cancel, patch, moves,
export/import. Each function takes the locked ``State`` and returns a
``Result`` describing the HTTP response (or an ``OperationError`` for
business-rule violations, which the handler maps to a status+code pair).

The functions are split out from the HTTP plumbing so the same logic can be
called from a test without spinning up a request, and so the rules live in
one file rather than scattered through handler methods.
"""
from __future__ import annotations

import datetime as dt
import secrets
import string
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from passwords import hash_password, verify_password
from time_utils import (
    InvalidLocalTimeError,
    WEEKDAYS,
    hhmm_to_minutes,
    is_gap,
    overlaps,
    parse_iso_date,
    parse_local,
    rfc3339,
    reservation_end,
    resolve_local,
    slot_starts_for_weekday,
    weekday_name,
)
from validation import (
    HttpError,
    MalformedRequest,
    ValidationFailed,
    check_date_param,
    check_display_name,
    check_email,
    check_hhmm,
    check_id,
    check_idempotency_key,
    check_local_datetime,
    check_non_negative_int_field,
    check_party_size,
    check_password,
    check_query_integer,
    check_reference,
    check_required_string,
    check_string_field,
    check_table_id_or_ids,
    check_weekday,
    is_valid_id,
    is_valid_reference,
    parse_json_body,
)


REFERENCE_ALPHABET = string.ascii_uppercase + string.digits


@dataclass
class OperationError(Exception):
    """A business-rule failure the handler turns into a specific HTTP code."""

    status: int
    code: str
    message: str = ""

    def __str__(self) -> str:
        return f"{self.status} {self.code}: {self.message}"


@dataclass
class OperationResult:
    """A successful operation's HTTP response."""

    status: int
    body: Any = None


def _reference() -> str:
    """Generate an 8-char A-Z0-9 reference; collisions are vanishingly rare."""
    return "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(8))


def _new_user_id() -> str:
    return f"u_{uuid.uuid4().hex[:12]}"


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---- reset ---------------------------------------------------------------


def load_fixture(state, fixture: dict) -> None:
    """Replace all state with the supplied fixture (§4).

    The lock is acquired here so the resulting transition is atomic: when
    reset returns 204, the very next request sees only this fixture.
    """
    from state_core import State
    if not isinstance(fixture, dict):
        raise MalformedRequest("fixture must be a JSON object")

    users = fixture.get("users") or []
    restaurants = fixture.get("restaurants") or []
    reservations = fixture.get("reservations") or []

    cleaned_users: dict[str, dict] = {}
    cleaned_users_by_email: dict[str, str] = {}
    cleaned_restaurants: dict[str, dict] = {}
    cleaned_reservations: dict[str, dict] = {}
    cleaned_res_by_user: dict[str, set[str]] = {}
    cleaned_res_by_restaurant: dict[str, set[str]] = {}

    for user in users:
        if not isinstance(user, dict):
            raise ValidationFailed("every fixture user must be an object")
        uid = check_id("user.id", user.get("id"))
        email = check_email(user.get("email"))
        password = user.get("password")
        if not isinstance(password, str):
            raise MalformedRequest("user.password must be a string")
        display_name = check_display_name(user.get("display_name"))
        if uid in cleaned_users:
            raise ValidationFailed("duplicate user id in fixture")
        if email.lower() in cleaned_users_by_email:
            raise ValidationFailed("duplicate user email in fixture")
        cleaned_users[uid] = {
            "id": uid,
            "email": email,
            "display_name": display_name,
            "password": hash_password(password),
        }
        cleaned_users_by_email[email.lower()] = uid

    for rest in restaurants:
        if not isinstance(rest, dict):
            raise ValidationFailed("every fixture restaurant must be an object")
        rid = check_id("restaurant.id", rest.get("id"))
        if rid in cleaned_restaurants:
            raise ValidationFailed("duplicate restaurant id in fixture")
        cleaned_restaurants[rid] = _validate_fixture_restaurant(rest, rid)

    for res in reservations:
        if not isinstance(res, dict):
            raise ValidationFailed("every fixture reservation must be an object")
        ref = check_reference("reservation.reference", res.get("reference"))
        rid = check_id("reservation.restaurant_id", res.get("restaurant_id"))
        if rid not in cleaned_restaurants:
            raise ValidationFailed("reservation references unknown restaurant")
        user_id = check_id("reservation.user_id", res.get("user_id"))
        if user_id not in cleaned_users:
            raise ValidationFailed("reservation references unknown user")
        starts_at_local = check_local_datetime(res.get("starts_at_local"))
        party_size = check_party_size(res.get("party_size"))
        # E1: seeded reservations accept either table_id or table_ids.
        if "table_id" in res and "table_ids" in res:
            raise ValidationFailed(
                "seeded reservation: send either 'table_id' or 'table_ids'")
        if "table_ids" in res:
            raw_ids = res.get("table_ids")
            if not isinstance(raw_ids, list):
                raise MalformedRequest(
                    "seeded reservation 'table_ids' must be an array")
            if len(raw_ids) == 0 or len(raw_ids) > 2:
                raise ValidationFailed(
                    "seeded reservation 'table_ids' length must be 1 or 2")
            tids: list[str] = [check_id("reservation.table_ids[]", t) for t in raw_ids]
            if len(set(tids)) != len(tids):
                raise ValidationFailed(
                    "seeded reservation 'table_ids' must contain distinct ids")
        else:
            tids = [check_id("reservation.table_id", res.get("table_id"))]
        reservation_id = res.get("id") or f"res_{uuid.uuid4().hex[:12]}"
        if not is_valid_id(reservation_id):
            raise ValidationFailed("reservation.id must be 1..64 opaque characters")
        if ref in cleaned_reservations:
            raise ValidationFailed("duplicate reservation reference in fixture")
        restaurant = cleaned_restaurants[rid]
        if len(tids) == 2:
            allowed = {frozenset(pair) for pair in restaurant["combinable"]}
            if frozenset(tids) not in allowed:
                raise ValidationFailed(
                    "seeded reservation references non-combinable pair")
        capacity = sum(_table_capacity(restaurant, t) for t in tids)
        if party_size > capacity:
            raise ValidationFailed(
                "seeded reservation party_size exceeds table capacity")
        status = res.get("status") or "confirmed"
        if status not in ("confirmed", "cancelled"):
            raise ValidationFailed("seeded reservation status must be "
                                   "'confirmed' or 'cancelled'")
        start = resolve_local(starts_at_local, restaurant["timezone"])
        end = reservation_end(start, restaurant["reservation_duration_minutes"])
        cleaned_reservations[ref] = {
            "id": reservation_id,
            "reference": ref,
            "user_id": user_id,
            "restaurant_id": rid,
            "table_ids": list(tids),
            "starts_at_local": starts_at_local,
            "starts_at": start,
            "ends_at": end,
            "party_size": party_size,
            "status": status,
            "created_at": _now_utc(),
        }
        cleaned_res_by_user.setdefault(user_id, set()).add(ref)
        cleaned_res_by_restaurant.setdefault(rid, set()).add(ref)

    with state.lock():
        state._users = cleaned_users
        state._users_by_email = cleaned_users_by_email
        state._tokens = {}
        state._restaurants = cleaned_restaurants
        state._restaurants_order = list(cleaned_restaurants.keys())
        state._reservations = cleaned_reservations
        state._reservations_by_user = {uid: set(refs) for uid, refs in cleaned_res_by_user.items()}
        state._reservations_by_restaurant = {rid: set(refs)
                                              for rid, refs in cleaned_res_by_restaurant.items()}
        state._idempotency = {}


def _validate_fixture_restaurant(rest: dict, rid: str) -> dict:
    name = check_string_field("restaurant.name", rest.get("name"))
    timezone = check_string_field("restaurant.timezone", rest.get("timezone"))
    if timezone is None:
        raise ValidationFailed("restaurant.timezone is required")
    slot_minutes = check_non_negative_int_field(rest.get("slot_minutes"), "slot_minutes")
    if slot_minutes <= 0:
        raise ValidationFailed("slot_minutes must be > 0")
    duration = check_non_negative_int_field(
        rest.get("reservation_duration_minutes"), "reservation_duration_minutes")
    if duration <= 0:
        raise ValidationFailed("reservation_duration_minutes must be > 0")
    cutoff = check_non_negative_int_field(
        rest.get("cancellation_cutoff_minutes"), "cancellation_cutoff_minutes")
    opening_hours = rest.get("opening_hours") or []
    if not isinstance(opening_hours, list):
        raise ValidationFailed("opening_hours must be a list")
    seen_weekdays = set()
    cleaned_hours = []
    for entry in opening_hours:
        if not isinstance(entry, dict):
            raise ValidationFailed("opening_hours entries must be objects")
        wd = check_weekday(entry.get("weekday"))
        if wd in seen_weekdays:
            raise ValidationFailed(f"opening_hours repeated weekday {wd}")
        seen_weekdays.add(wd)
        opens = check_hhmm(entry.get("opens"), "opens")
        closes = check_hhmm(entry.get("closes"), "closes")
        if hhmm_to_minutes(opens) >= hhmm_to_minutes(closes):
            raise ValidationFailed("closes must be later than opens on the same local day")
        cleaned_hours.append({"weekday": wd, "opens": opens, "closes": closes})
    tables = rest.get("tables") or []
    if not isinstance(tables, list):
        raise ValidationFailed("tables must be a list")
    seen_tables = set()
    cleaned_tables = []
    for entry in tables:
        if not isinstance(entry, dict):
            raise ValidationFailed("tables entries must be objects")
        tid = check_id("table.id", entry.get("id"))
        if tid in seen_tables:
            raise ValidationFailed("duplicate table id")
        seen_tables.add(tid)
        label = check_string_field("table.label", entry.get("label")) or ""
        capacity = check_non_negative_int_field(entry.get("capacity"), "capacity")
        if capacity < 1:
            raise ValidationFailed("table capacity must be >= 1")
        cleaned_tables.append({"id": tid, "label": label, "capacity": capacity})
    table_ids = {t["id"] for t in cleaned_tables}
    combinable = rest.get("combinable") or []
    if not isinstance(combinable, list):
        raise ValidationFailed("combinable must be a list")
    cleaned_combinable: list[list[str]] = []
    seen_pairs: set[frozenset] = set()
    for entry in combinable:
        if not isinstance(entry, list) or len(entry) != 2:
            raise ValidationFailed(
                "every combinable entry must be a list of two table ids")
        a, b = entry
        tid_a = check_id("combinable[].id", a)
        tid_b = check_id("combinable[].id", b)
        if tid_a == tid_b:
            raise ValidationFailed(
                "combinable pairs must contain two distinct table ids")
        if tid_a not in table_ids:
            raise ValidationFailed(
                f"combinable references unknown table {tid_a!r}")
        if tid_b not in table_ids:
            raise ValidationFailed(
                f"combinable references unknown table {tid_b!r}")
        key = frozenset({tid_a, tid_b})
        if key in seen_pairs:
            raise ValidationFailed("combinable contains a repeated pair")
        seen_pairs.add(key)
        cleaned_combinable.append([tid_a, tid_b])
    return {
        "id": rid,
        "name": name,
        "timezone": timezone,
        "slot_minutes": slot_minutes,
        "reservation_duration_minutes": duration,
        "cancellation_cutoff_minutes": cutoff,
        "opening_hours": cleaned_hours,
        "tables": cleaned_tables,
        "combinable": cleaned_combinable,
    }


# ---- auth ----------------------------------------------------------------


def signup(state, body: dict) -> OperationResult:
    email = check_email(body.get("email"))
    password = check_password(body.get("password"))
    display_name = check_display_name(body.get("display_name"))
    with state.lock():
        if email.lower() in state._users_by_email:
            raise OperationError(409, "email_taken", "email is already registered")
        uid = _new_user_id()
        token = _new_token()
        state._users[uid] = {
            "id": uid,
            "email": email,
            "display_name": display_name,
            "password": hash_password(password),
        }
        state._users_by_email[email.lower()] = uid
        state._tokens[token] = uid
        return OperationResult(201, {
            "user_id": uid,
            "display_name": display_name,
            "token": token,
        })


def login(state, body: dict) -> OperationResult:
    email_obj = body.get("email")
    password_obj = body.get("password")
    if not isinstance(email_obj, str) or not isinstance(password_obj, str):
        raise MalformedRequest("email and password must be strings")
    with state.lock():
        user_id = state._users_by_email.get(email_obj.lower())
        if not user_id:
            raise OperationError(401, "unauthenticated", "unknown email")
        user = state._users[user_id]
        if not verify_password(password_obj, user["password"]):
            raise OperationError(401, "unauthenticated", "wrong password")
        token = _new_token()
        state._tokens[token] = user_id
        return OperationResult(200, {
            "user_id": user_id,
            "display_name": user["display_name"],
            "token": token,
        })


def authenticate(state, token: Optional[str]) -> str:
    """Resolve an Authorization: Bearer token to a user_id, or raise 401."""
    if not isinstance(token, str) or not token:
        raise OperationError(401, "unauthenticated", "missing bearer token")
    with state.lock():
        user_id = state._tokens.get(token)
        if not user_id:
            raise OperationError(401, "unauthenticated", "unknown bearer token")
        return user_id


# ---- restaurants --------------------------------------------------------


def list_restaurants(state) -> OperationResult:
    with state.lock():
        items = [
            {"id": rid, "name": r["name"], "timezone": r["timezone"]}
            for rid, r in state._restaurants.items()
        ]
    return OperationResult(200, {"restaurants": items})


def get_restaurant(state, rid: str) -> OperationResult:
    with state.lock():
        r = state._restaurants.get(rid)
        if r is None:
            raise OperationError(404, "not_found", "unknown restaurant")
        return OperationResult(200, _restaurant_payload(r))


def _restaurant_payload(r: dict) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "timezone": r["timezone"],
        "slot_minutes": r["slot_minutes"],
        "reservation_duration_minutes": r["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": r["cancellation_cutoff_minutes"],
        "opening_hours": list(r["opening_hours"]),
        "tables": list(r["tables"]),
    }


# ---- availability --------------------------------------------------------


def availability(state, *, restaurant_id: Optional[str], date: Optional[str],
                 party_size: Optional[int]) -> OperationResult:
    rid = check_id("restaurant_id", restaurant_id)
    date_str = check_date_param(date)
    psize = check_query_integer(party_size, "party_size")
    with state.lock():
        r = state._restaurants.get(rid)
        if r is None:
            raise OperationError(404, "not_found", "unknown restaurant")
        d = parse_iso_date(date_str)
        weekday = weekday_name(d)
        hours = next((h for h in r["opening_hours"] if h["weekday"] == weekday), None)
        if hours is None:
            slots = []
        else:
            slot_minutes = slot_starts_for_weekday(
                hours["opens"], hours["closes"], r["slot_minutes"],
                r["reservation_duration_minutes"], d, r["timezone"],
            )
            slot_payload = []
            for hhmm in slot_minutes:
                start = resolve_local(f"{date_str}T{hhmm}", r["timezone"])
                end = reservation_end(start, r["reservation_duration_minutes"])
                # Singles first (fixture order), then pairs in combinable order.
                available: list[str] = []
                options: list[dict] = []
                for table in r["tables"]:
                    if table["capacity"] < psize:
                        continue
                    if _has_overlap(state, r["id"], [table["id"]], start, end):
                        continue
                    available.append(table["id"])
                    options.append({
                        "table_ids": [table["id"]],
                        "capacity": table["capacity"],
                    })
                for pair in r["combinable"]:
                    a, b = pair
                    cap = _table_capacity(r, a) + _table_capacity(r, b)
                    if cap < psize:
                        continue
                    if _has_overlap(state, r["id"], [a, b], start, end):
                        continue
                    options.append({"table_ids": [a, b], "capacity": cap})
                slot_payload.append({
                    "starts_at_local": f"{date_str}T{hhmm}",
                    "starts_at": rfc3339(start),
                    "available_table_ids": available,
                    "available_options": options,
                })
            slots = slot_payload
        return OperationResult(200, {
            "restaurant_id": rid,
            "date": date_str,
            "timezone": r["timezone"],
            "slots": slots,
        })


def _has_overlap(state, rid: str, tids: list[str], start: dt.datetime,
                 end: dt.datetime, *, exclude_ref: Optional[str] = None) -> bool:
    """True iff any confirmed reservation in ``rid`` overlaps ``[start, end)``
    AND shares at least one table with ``tids``. E1: reservations hold
    ``table_ids`` (a list); ``exclude_ref`` is used by PATCH/moves to skip
    the reservation being amended."""
    target = set(tids)
    for ref in state._reservations_by_restaurant.get(rid, ()):
        if ref == exclude_ref:
            continue
        res = state._reservations[ref]
        if res["status"] != "confirmed":
            continue
        if not (target & set(res.get("table_ids") or ())):
            continue
        if overlaps(start, end, res["starts_at"], res["ends_at"]):
            return True
    return False


# ---- reservation writes --------------------------------------------------


def _table_for(restaurant: dict, table_id: str) -> Optional[dict]:
    for t in restaurant["tables"]:
        if t["id"] == table_id:
            return t
    return None


def _table_capacity(restaurant: dict, table_id: str) -> int:
    t = _table_for(restaurant, table_id)
    return t["capacity"] if t is not None else 0


def _pair_in_combinable(restaurant: dict, tids: list[str]) -> bool:
    if len(tids) != 2:
        return False
    return frozenset(tids) in {frozenset(p) for p in restaurant["combinable"]}


def _reservation_payload(res: dict) -> dict:
    """Serialise a reservation record (E1: ``table_id`` only for singletons)."""
    tids = list(res.get("table_ids") or [])
    out = {
        "reservation_id": res["id"],
        "reference": res["reference"],
        "restaurant_id": res["restaurant_id"],
        "table_ids": tids,
        "party_size": res["party_size"],
        "status": res["status"],
        "starts_at_local": res["starts_at_local"],
        "starts_at": rfc3339(res["starts_at"]),
        "ends_at": rfc3339(res["ends_at"]),
        "created_at": rfc3339(res["created_at"]),
    }
    if len(tids) == 1:
        out["table_id"] = tids[0]
    return out


def _idempotency_lookup(state, user_id: str, key: str, path: str, body: dict):
    """Return ``(receipt, replay_status)`` for an idempotent replay, or
    ``(None, None)``.

    Per ledger §7, replay = same user, same key, same method+path+body.
    A key reused on a different path is treated as a fresh request (no
    receipt match), so a ``POST /reservation-moves`` after a
    ``POST /reservations`` with the same key is not a replay. A key
    reused on the same path with a different body is 409.

    ``replay_status`` is 200 even when the original request was 201, per
    the spec's "replay returns same body, status 200 vs first 201"
    contract. Must be called under ``state.lock()`` because it reads
    mutable state.
    """
    bucket = state._idempotency.get(user_id, {})
    path_bucket = bucket.get(key, {})
    rec = path_bucket.get(path)
    if rec is None:
        return None, None
    if rec["body"] != body:
        raise OperationError(409, "idempotency_key_reuse",
                             "Idempotency-Key was already used with a different body")
    return rec, 200


def _idempotency_store(state, user_id: str, key: str, path: str, body: dict,
                       status_code: int, response_body: dict) -> None:
    bucket = state._idempotency.setdefault(user_id, {})
    path_bucket = bucket.setdefault(key, {})
    path_bucket[path] = {
        "body": body,
        "status_code": status_code,
        "response": response_body,
    }


def _check_cutoff(state, res: dict) -> None:
    """A reservation cannot be cancelled or amended within/after its cutoff."""
    restaurant = state._restaurants[res["restaurant_id"]]
    now = _now_utc()
    delta = res["starts_at"] - now
    minutes = delta.total_seconds() / 60.0
    if minutes <= restaurant["cancellation_cutoff_minutes"]:
        raise OperationError(409, "cutoff_passed",
                             "now is within the cancellation cutoff")


def create_reservation(state, *, user_id: str, body: dict,
                       idempotency_key: str) -> OperationResult:
    # D5: auth → idempotency resolution → field validation → 404 resource
    # checks → rule checks (grid, opening hours, capacity, local time)
    # → availability/conflict.

    with state.lock():
        # Idempotency lookup happens under the lock so two concurrent
        # requests with the same unused key serialise: exactly one wins
        # the race and stores the receipt, the other sees it on lookup.
        receipt, replay_status = _idempotency_lookup(state, user_id, idempotency_key, "POST /reservations", body)
        if receipt is not None:
            return OperationResult(replay_status, receipt["response"])

        # Field validation happens after the lock is acquired so a bad
        # field on a brand-new key still rejects before any state change.
        rid = check_id("restaurant_id", body.get("restaurant_id"))
        tids = check_table_id_or_ids(body)
        starts_at_local = check_local_datetime(body.get("starts_at_local"))
        party_size = check_party_size(body.get("party_size"))

        restaurant = state._restaurants.get(rid)
        if restaurant is None:
            raise OperationError(404, "not_found", "unknown restaurant")
        if len(tids) == 1:
            table = _table_for(restaurant, tids[0])
            if table is None:
                raise OperationError(404, "not_found", "table not in restaurant")
        else:
            if not _pair_in_combinable(restaurant, tids):
                raise OperationError(422, "combination_not_allowed",
                                     "pair is not declared combinable")
        capacity = sum(_table_capacity(restaurant, t) for t in tids)
        if party_size > capacity:
            raise OperationError(422, "party_exceeds_capacity",
                                 "party_size exceeds combined table capacity")

        try:
            start = resolve_local(starts_at_local, restaurant["timezone"])
        except InvalidLocalTimeError:
            raise OperationError(422, "invalid_local_time",
                                 "local time does not exist in this timezone")

        if not _within_opening_hours(start, restaurant):
            raise OperationError(422, "outside_opening_hours",
                                 "slot is outside opening hours or extends past closing")
        if not _is_on_slot_grid(starts_at_local, restaurant):
            raise OperationError(422, "not_on_slot_grid",
                                 "starts_at_local is not on the slot grid")

        end = reservation_end(start, restaurant["reservation_duration_minutes"])

        if _has_overlap(state, rid, tids, start, end):
            raise OperationError(409, "table_unavailable",
                                 "table is taken for an overlapping interval")

        reference = _allocate_reference(state)
        reservation = {
            "id": f"res_{uuid.uuid4().hex[:12]}",
            "reference": reference,
            "user_id": user_id,
            "restaurant_id": rid,
            "table_ids": list(tids),
            "starts_at_local": starts_at_local,
            "starts_at": start,
            "ends_at": end,
            "party_size": party_size,
            "status": "confirmed",
            "created_at": _now_utc(),
        }
        state._reservations[reference] = reservation
        state._reservations_by_user.setdefault(user_id, set()).add(reference)
        state._reservations_by_restaurant.setdefault(rid, set()).add(reference)
        payload = _reservation_payload(reservation)
        _idempotency_store(state, user_id, idempotency_key, "POST /reservations", body, 201, payload)
        return OperationResult(201, payload)


def _allocate_reference(state) -> str:
    """An 8-char A-Z0-9 string, unique across all reservations."""
    for _ in range(64):
        ref = _reference()
        if ref not in state._reservations:
            return ref
    raise OperationError(500, "internal", "could not allocate unique reference")


def _is_on_slot_grid(starts_at_local: str, restaurant: dict) -> bool:
    """True iff the wall-clock minute is on the slot grid from the day's opens."""
    parsed = parse_local(starts_at_local)
    weekday = weekday_name(parsed.date())
    hours = next((h for h in restaurant["opening_hours"]
                  if h["weekday"] == weekday), None)
    if hours is None:
        return False
    opens = hhmm_to_minutes(hours["opens"])
    slot = parsed.hour * 60 + parsed.minute
    if slot < opens:
        return False
    if (slot - opens) % restaurant["slot_minutes"] != 0:
        return False
    return slot + restaurant["reservation_duration_minutes"] <= hhmm_to_minutes(hours["closes"])


def _within_opening_hours(start: dt.datetime, restaurant: dict) -> bool:
    """True if the reservation starts and ends within the same opening-hours entry."""
    weekday = weekday_name(start.date())
    hours = next((h for h in restaurant["opening_hours"]
                  if h["weekday"] == weekday), None)
    if hours is None:
        return False
    opens = hhmm_to_minutes(hours["opens"])
    closes = hhmm_to_minutes(hours["closes"])
    slot = start.hour * 60 + start.minute
    if slot < opens:
        return False
    if slot + restaurant["reservation_duration_minutes"] > closes:
        return False
    return True


# ---- reads ---------------------------------------------------------------


def list_reservations(state, *, user_id: str) -> OperationResult:
    with state.lock():
        refs = sorted(
            state._reservations_by_user.get(user_id, set()),
            key=lambda ref: state._reservations[ref]["starts_at"],
            reverse=True,
        )
        return OperationResult(200, {
            "reservations": [_reservation_payload(state._reservations[ref]) for ref in refs],
        })


def get_reservation(state, *, user_id: str, reference: str) -> OperationResult:
    check_reference("reference", reference)
    with state.lock():
        res = state._reservations.get(reference)
        if res is None or res["user_id"] != user_id:
            raise OperationError(404, "not_found", "reservation not found")
        return OperationResult(200, _reservation_payload(res))


def cancel_reservation(state, *, user_id: str, reference: str) -> OperationResult:
    check_reference("reference", reference)
    with state.lock():
        res = state._reservations.get(reference)
        if res is None or res["user_id"] != user_id:
            raise OperationError(404, "not_found", "reservation not found")
        if res["status"] == "cancelled":
            return OperationResult(200, _reservation_payload(res))
        _check_cutoff(state, res)
        res["status"] = "cancelled"
        return OperationResult(200, _reservation_payload(res))


def patch_reservation(state, *, user_id: str, reference: str,
                      body: dict) -> OperationResult:
    check_reference("reference", reference)
    with state.lock():
        res = state._reservations.get(reference)
        if res is None or res["user_id"] != user_id:
            raise OperationError(404, "not_found", "reservation not found")
        if res["status"] == "cancelled":
            raise OperationError(409, "reservation_cancelled",
                                 "reservation is cancelled")
        _check_cutoff(state, res)

        restaurant = state._restaurants[res["restaurant_id"]]

        # Resolve desired table set; omitted fields keep current (E1/E2).
        has_id = "table_id" in body
        has_ids = "table_ids" in body
        if has_id and has_ids:
            raise ValidationFailed(
                "send either 'table_id' or 'table_ids', not both")
        if has_ids:
            new_tids = check_table_id_or_ids(body)
        elif has_id:
            new_tids = [check_id("table_id", body.get("table_id"))]
        else:
            new_tids = list(res["table_ids"])
        new_starts_at_local = body.get("starts_at_local", res["starts_at_local"])
        new_party_size = body.get("party_size", res["party_size"])

        # Field-level validation.
        check_local_datetime(new_starts_at_local)
        check_party_size(new_party_size)

        # Resource checks.
        if len(new_tids) == 1:
            table = _table_for(restaurant, new_tids[0])
            if table is None:
                raise OperationError(404, "not_found", "table not in restaurant")
        else:
            if not _pair_in_combinable(restaurant, new_tids):
                raise OperationError(422, "combination_not_allowed",
                                     "pair is not declared combinable")
        capacity = sum(_table_capacity(restaurant, t) for t in new_tids)
        if new_party_size > capacity:
            raise OperationError(422, "party_exceeds_capacity",
                                 "party_size exceeds combined table capacity")
        try:
            new_start = resolve_local(new_starts_at_local, restaurant["timezone"])
        except InvalidLocalTimeError:
            raise OperationError(422, "invalid_local_time",
                                 "local time does not exist in this timezone")
        if not _within_opening_hours(new_start, restaurant):
            raise OperationError(422, "outside_opening_hours",
                                 "slot is outside opening hours or extends past closing")
        if not _is_on_slot_grid(new_starts_at_local, restaurant):
            raise OperationError(422, "not_on_slot_grid",
                                 "starts_at_local is not on the slot grid")
        new_end = reservation_end(new_start, restaurant["reservation_duration_minutes"])

        # Conflict check (ignore self).
        if _has_overlap(state, res["restaurant_id"], new_tids,
                        new_start, new_end, exclude_ref=reference):
            raise OperationError(409, "table_unavailable",
                                 "table is taken for an overlapping interval")

        # Atomically apply.
        res["table_ids"] = list(new_tids)
        res["starts_at_local"] = new_starts_at_local
        res["starts_at"] = new_start
        res["ends_at"] = new_end
        res["party_size"] = new_party_size
        return OperationResult(200, _reservation_payload(res))


def _has_overlap_excluding(state, rid: str, table_id: str, start: dt.datetime,
                           end: dt.datetime, exclude_ref: str) -> bool:
    """Kept for any remaining stage-1 callers; the new patch path uses
    ``_has_overlap(..., exclude_ref=...)`` directly."""
    for ref in state._reservations_by_restaurant.get(rid, ()):
        if ref == exclude_ref:
            continue
        res = state._reservations[ref]
        if res["status"] != "confirmed":
            continue
        if res.get("table_id") != table_id:
            continue
        if overlaps(start, end, res["starts_at"], res["ends_at"]):
            return True
    return False


# ---- batch moves ---------------------------------------------------------


def moves(state, *, user_id: str, body: dict,
          idempotency_key: str) -> OperationResult:
    with state.lock():
        receipt, replay_status = _idempotency_lookup(state, user_id, idempotency_key, "POST /reservation-moves", body)
        if receipt is not None:
            return OperationResult(replay_status, receipt["response"])

        moves_in = body.get("moves")
        if not isinstance(moves_in, list) or not (1 <= len(moves_in) <= 8):
            raise ValidationFailed("moves must contain 1..8 objects")
        refs = []
        for entry in moves_in:
            if not isinstance(entry, dict):
                raise ValidationFailed("every move must be an object")
            refs.append(check_reference("move.reference", entry.get("reference")))
        if len(set(refs)) != len(refs):
            raise ValidationFailed("moves must contain distinct references")

        # Ownership + same-restaurant check first.
        loaded = []
        seen_restaurants = set()
        for entry, ref in zip(moves_in, refs):
            res = state._reservations.get(ref)
            if res is None or res["user_id"] != user_id:
                raise OperationError(404, "not_found", "reservation not found")
            seen_restaurants.add(res["restaurant_id"])
            loaded.append((entry, res))
        if len(seen_restaurants) != 1:
            raise ValidationFailed("all moves must reference the same restaurant")
        restaurant = state._restaurants[next(iter(seen_restaurants))]

        # Per-item validation, cutoff first, then other rules.
        plans = []
        for entry, res in loaded:
            if res["status"] == "cancelled":
                raise OperationError(409, "reservation_cancelled",
                                     "reservation is cancelled")
            _check_cutoff(state, res)
            # E1/E2: each move item may carry table_id or table_ids.
            has_id = "table_id" in entry
            has_ids = "table_ids" in entry
            if has_id and has_ids:
                raise ValidationFailed(
                    "send either 'table_id' or 'table_ids', not both")
            if has_ids:
                new_tids = check_table_id_or_ids(entry)
            elif has_id:
                new_tids = [check_id("table_id", entry.get("table_id"))]
            else:
                new_tids = list(res["table_ids"])
            new_starts_at_local = entry.get("starts_at_local", res["starts_at_local"])
            new_party_size = entry.get("party_size", res["party_size"])
            check_local_datetime(new_starts_at_local)
            check_party_size(new_party_size)
            if len(new_tids) == 1:
                table = _table_for(restaurant, new_tids[0])
                if table is None:
                    raise OperationError(404, "not_found", "table not in restaurant")
            else:
                if not _pair_in_combinable(restaurant, new_tids):
                    raise OperationError(422, "combination_not_allowed",
                                         "pair is not declared combinable")
            capacity = sum(_table_capacity(restaurant, t) for t in new_tids)
            if new_party_size > capacity:
                raise OperationError(422, "party_exceeds_capacity",
                                     "party_size exceeds combined table capacity")
            try:
                new_start = resolve_local(new_starts_at_local, restaurant["timezone"])
            except InvalidLocalTimeError:
                raise OperationError(422, "invalid_local_time",
                                     "local time does not exist in this timezone")
            if not _within_opening_hours(new_start, restaurant):
                raise OperationError(422, "outside_opening_hours",
                                     "slot is outside opening hours or extends past closing")
            if not _is_on_slot_grid(new_starts_at_local, restaurant):
                raise OperationError(422, "not_on_slot_grid",
                                     "starts_at_local is not on the slot grid")
            new_end = reservation_end(new_start, restaurant["reservation_duration_minutes"])
            plans.append((entry, res, new_tids, new_starts_at_local,
                          new_start, new_end, new_party_size))

        # Conflict check: no two listed bookings may overlap with each other,
        # and no listed booking may overlap with an unlisted one. E1:
        # bookings occupy whole table sets, so sharing any member with an
        # overlapping confirmed booking is a conflict.
        listed_refs = {res["reference"] for _, res, *_ in plans}
        for i in range(len(plans)):
            for j in range(i + 1, len(plans)):
                _, _, tids_i, _, start_i, end_i, _ = plans[i]
                _, _, tids_j, _, start_j, end_j, _ = plans[j]
                if set(tids_i) & set(tids_j) and overlaps(start_i, end_i, start_j, end_j):
                    raise OperationError(409, "table_unavailable",
                                         "table is taken for an overlapping interval")
        for (_, _, tids, _, start, end, _) in plans:
            target = set(tids)
            for ref in state._reservations_by_restaurant.get(restaurant["id"], ()):
                if ref in listed_refs:
                    continue
                other = state._reservations[ref]
                if other["status"] != "confirmed":
                    continue
                if not (target & set(other.get("table_ids") or ())):
                    continue
                if overlaps(start, end, other["starts_at"], other["ends_at"]):
                    raise OperationError(409, "table_unavailable",
                                         "table is taken for an overlapping interval")

        # Apply atomically.
        for (entry, res, new_tids, new_starts_at_local, new_start,
             new_end, new_party_size) in plans:
            res["table_ids"] = list(new_tids)
            res["starts_at_local"] = new_starts_at_local
            res["starts_at"] = new_start
            res["ends_at"] = new_end
            res["party_size"] = new_party_size

        # Build response in input order, including unchanged items.
        output = []
        for ref in refs:
            output.append(_reservation_payload(state._reservations[ref]))
        payload = {"reservations": output}
        _idempotency_store(state, user_id, idempotency_key, "POST /reservation-moves", body, 201, payload)
        return OperationResult(201, payload)


# ---- export / import -----------------------------------------------------


def export_state(state) -> OperationResult:
    snap = state.snapshot()
    return OperationResult(200, {
        "track": "tablekeeper",
        "format_version": 1,
        "state": snap,
    })


def import_state(state, body: dict) -> OperationResult:
    if not isinstance(body, dict):
        raise MalformedRequest("body must be a JSON object")
    track = body.get("track")
    version = body.get("format_version")
    snap = body.get("state")
    if track != "tablekeeper":
        raise ValidationFailed("track must be 'tablekeeper'")
    if version != 1:
        raise ValidationFailed("format_version must be 1")
    if not isinstance(snap, dict):
        raise ValidationFailed("state must be an object")
    _validate_snapshot(snap)
    state.replace(snap)
    return OperationResult(204, None)


def _validate_snapshot(snap: dict) -> None:
    required = {
        "users", "users_by_email", "tokens", "restaurants",
        "restaurants_order", "reservations", "reservations_by_user",
        "reservations_by_restaurant", "idempotency",
    }
    missing = required - snap.keys()
    if missing:
        raise ValidationFailed(f"state is missing fields: {sorted(missing)}")
    if not isinstance(snap["users"], dict):
        raise ValidationFailed("state.users must be an object")
    if not isinstance(snap["users_by_email"], dict):
        raise ValidationFailed("state.users_by_email must be an object")
    if not isinstance(snap["tokens"], dict):
        raise ValidationFailed("state.tokens must be an object")
    if not isinstance(snap["restaurants"], dict):
        raise ValidationFailed("state.restaurants must be an object")
    if not isinstance(snap["restaurants_order"], list):
        raise ValidationFailed("state.restaurants_order must be a list")
    if not isinstance(snap["reservations"], dict):
        raise ValidationFailed("state.reservations must be an object")
    if not isinstance(snap["reservations_by_user"], dict):
        raise ValidationFailed("state.reservations_by_user must be an object")
    if not isinstance(snap["reservations_by_restaurant"], dict):
        raise ValidationFailed("state.reservations_by_restaurant must be an object")
    if not isinstance(snap["idempotency"], dict):
        raise ValidationFailed("state.idempotency must be an object")
