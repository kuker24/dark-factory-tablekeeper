"""Business operations: reset, signup/login, reservations, cancel, patch, moves,
export/import, policies, history, recurring series.

Each function takes the locked ``State`` and returns a ``Result``
describing the HTTP response (or an ``OperationError`` for business-rule
violations, which the handler maps to a status+code pair).

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
    check_count,
    check_date_param,
    check_display_name,
    check_email,
    check_explain,
    check_hhmm,
    check_id,
    check_idempotency_key,
    check_interval_weeks,
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


def _new_series_id() -> str:
    return f"s_{uuid.uuid4().hex[:12]}"


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
        # Seeded reservations sit under policy 0 (F12). They start at revision
        # 1 with an empty history.
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
            "revision": 1,
            "accepted_terms": _policy0_terms(restaurant),
            "history": [],
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
        state._series = {}
        state._series_by_reference = {}
        state._series_by_user = {}


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
    managers = rest.get("manager_user_ids") or []
    if not isinstance(managers, list):
        raise ValidationFailed("manager_user_ids must be a list")
    cleaned_managers = [check_id("manager_user_ids[]", m) for m in managers]
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
        "policies": [],
        "manager_user_ids": cleaned_managers,
        "revision": 0,
        "next_policy_version": 1,
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
            {
                "id": rid,
                "name": r["name"],
                "timezone": r["timezone"],
                "tables": list(r["tables"]),
                "combinable": list(r.get("combinable") or []),
            }
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


# ---- policies -----------------------------------------------------------


def _policy0_terms(restaurant: dict) -> dict:
    """Build the policy-0 ``accepted_terms`` snapshot from a restaurant record.

    Per ledger 21 and F2: the accepted_terms is a snapshot of the entire
    selected policy excluding ``effective_from``. Policy 0 has no
    ``effective_from``; we still copy the rest verbatim.
    """
    return {
        "policy_version": 0,
        "slot_minutes": restaurant["slot_minutes"],
        "reservation_duration_minutes": restaurant["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": restaurant["cancellation_cutoff_minutes"],
        "opening_hours": list(restaurant["opening_hours"]),
        "capacities": {t["id"]: t["capacity"] for t in restaurant["tables"]},
    }


def _policy_terms(policy: dict) -> dict:
    """Project a published policy into the accepted_terms shape (no effective_from)."""
    return {
        "policy_version": policy["policy_version"],
        "slot_minutes": policy["slot_minutes"],
        "reservation_duration_minutes": policy["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": policy["cancellation_cutoff_minutes"],
        "opening_hours": list(policy["opening_hours"]),
        "capacities": dict(policy["capacities"]),
    }


def _select_policy_for_date(restaurant: dict, local_date: dt.date) -> dict:
    """Pick the policy whose effective_from is the greatest not later than
    ``local_date``; on ties, the greatest ``policy_version`` (ledger 18).

    Policy 0 (the fixture) is the implicit fallback when no published
    policy applies.
    """
    best = None
    best_eff = None
    best_ver = -1
    for pol in restaurant.get("policies", ()):
        eff = parse_iso_date(pol.get("effective_from"))
        if eff is None or eff > local_date:
            continue
        ver = pol.get("policy_version", -1)
        if best is None:
            best, best_eff, best_ver = pol, eff, ver
            continue
        if (eff, ver) > (best_eff, best_ver):
            best, best_eff, best_ver = pol, eff, ver
    if best is None:
        return _policy0_terms(restaurant)
    return _policy_terms(best)


def publish_policy(state, *, user_id: str, rid: str, body: dict,
                   idempotency_key: str) -> OperationResult:
    """POST /restaurants/{id}/policies (ledger 14-18, F14)."""
    with state.lock():
        # Per ledger 15: idempotency replay rules mirror stage 1. Same
        # user + same key + same body returns 200 with the original
        # response. Same key with a different body returns 409.
        receipt, replay_status = _idempotency_lookup(
            state, user_id, idempotency_key,
            f"POST /restaurants/{rid}/policies", body)
        if receipt is not None:
            return OperationResult(replay_status, receipt["response"])

        restaurant = state._restaurants.get(rid)
        if restaurant is None:
            raise OperationError(404, "not_found", "unknown restaurant")

        # Per ledger 14: 401 with no token (handler enforces). Authenticated
        # non-manager → 403; manager → ok.
        if user_id not in restaurant.get("manager_user_ids", ()):
            raise OperationError(403, "forbidden",
                                 "user is not a manager of this restaurant")

        policy = _validate_policy_body(body, restaurant)
        next_version = restaurant.get("next_policy_version",
                                       len(restaurant.get("policies", ())) + 1)
        policy["policy_version"] = next_version
        restaurant.setdefault("policies", []).append(policy)
        restaurant["next_policy_version"] = next_version + 1
        # Restaurant revision +1 once per policy publication (F8 / ledger 38).
        restaurant["revision"] = restaurant.get("revision", 0) + 1

        _idempotency_store(state, user_id, idempotency_key,
                           f"POST /restaurants/{rid}/policies", body,
                           201, policy)
        return OperationResult(201, policy)


def _validate_policy_body(body: dict, restaurant: dict) -> dict:
    """Validate a complete policy body per ledger 16. Returns the canonicalised
    policy (with ``effective_from`` preserved). 422 ``validation_failed`` on
    any rule violation. Unknown fields ignored.
    """
    if not isinstance(body, dict):
        raise MalformedRequest("policy body must be a JSON object")
    required = ("effective_from", "slot_minutes", "reservation_duration_minutes",
                "cancellation_cutoff_minutes", "opening_hours", "capacities")
    for field in required:
        if field not in body:
            raise ValidationFailed(f"{field!r} is required")

    eff = body.get("effective_from")
    if not isinstance(eff, str) or parse_iso_date(eff) is None:
        raise ValidationFailed("effective_from must be a YYYY-MM-DD date")

    slot_minutes = body.get("slot_minutes")
    if isinstance(slot_minutes, bool) or not isinstance(slot_minutes, int):
        raise ValidationFailed("slot_minutes must be an integer")
    if not (1 <= slot_minutes <= 1440):
        raise ValidationFailed("slot_minutes must be between 1 and 1440")

    duration = body.get("reservation_duration_minutes")
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise ValidationFailed("reservation_duration_minutes must be an integer")
    if not (1 <= duration <= 1440):
        raise ValidationFailed("reservation_duration_minutes must be between 1 and 1440")

    cutoff = body.get("cancellation_cutoff_minutes")
    if isinstance(cutoff, bool) or not isinstance(cutoff, int):
        raise ValidationFailed("cancellation_cutoff_minutes must be an integer")
    if not (0 <= cutoff <= 10080):
        raise ValidationFailed("cancellation_cutoff_minutes must be between 0 and 10080")

    opening_hours = body.get("opening_hours")
    if not isinstance(opening_hours, list):
        raise ValidationFailed("opening_hours must be a list")
    seen_wd = set()
    cleaned_hours = []
    for entry in opening_hours:
        if not isinstance(entry, dict):
            raise ValidationFailed("opening_hours entries must be objects")
        wd = check_weekday(entry.get("weekday"))
        if wd in seen_wd:
            raise ValidationFailed(f"opening_hours repeated weekday {wd}")
        seen_wd.add(wd)
        opens = check_hhmm(entry.get("opens"), "opens")
        closes = check_hhmm(entry.get("closes"), "closes")
        if hhmm_to_minutes(opens) >= hhmm_to_minutes(closes):
            raise ValidationFailed(
                "closes must be later than opens on the same local day")
        cleaned_hours.append({"weekday": wd, "opens": opens, "closes": closes})

    capacities = body.get("capacities")
    if not isinstance(capacities, dict):
        raise ValidationFailed("capacities must be an object")
    expected_table_ids = {t["id"] for t in restaurant["tables"]}
    given_table_ids = set(capacities.keys())
    if given_table_ids != expected_table_ids:
        raise ValidationFailed(
            "capacities must name exactly the restaurant's table ids")
    cleaned_capacities: dict[str, int] = {}
    for tid, cap in capacities.items():
        if isinstance(cap, bool) or not isinstance(cap, int):
            raise ValidationFailed("capacities values must be integers")
        if not (1 <= cap <= 100):
            raise ValidationFailed("capacities values must be between 1 and 100")
        cleaned_capacities[tid] = cap

    return {
        "effective_from": eff,
        "slot_minutes": slot_minutes,
        "reservation_duration_minutes": duration,
        "cancellation_cutoff_minutes": cutoff,
        "opening_hours": cleaned_hours,
        "capacities": cleaned_capacities,
    }


def list_policies(state, rid: str) -> OperationResult:
    """GET /restaurants/{id}/policies (ledger 20). Public. Omits policy 0."""
    with state.lock():
        restaurant = state._restaurants.get(rid)
        if restaurant is None:
            raise OperationError(404, "not_found", "unknown restaurant")
        policies = [dict(p) for p in restaurant.get("policies", ())]
        return OperationResult(200, {"policies": policies})


# ---- availability --------------------------------------------------------


def availability(state, *, restaurant_id: Optional[str], date: Optional[str],
                 party_size: Optional[int], explain: bool = False) -> OperationResult:
    rid = check_id("restaurant_id", restaurant_id)
    date_str = check_date_param(date)
    psize = check_query_integer(party_size, "party_size")
    with state.lock():
        r = state._restaurants.get(rid)
        if r is None:
            raise OperationError(404, "not_found", "unknown restaurant")
        d = parse_iso_date(date_str)
        weekday = weekday_name(d)
        # Per ledger 6 / F3: slot grid, duration and capacities come from
        # the selected policy for the requested date.
        terms = _select_policy_for_date(r, d)
        hours = next((h for h in terms["opening_hours"]
                      if h["weekday"] == weekday), None)
        slots_out: list[dict] = []
        if hours is not None:
            slot_minutes_list = slot_starts_for_weekday(
                hours["opens"], hours["closes"], terms["slot_minutes"],
                terms["reservation_duration_minutes"], d, r["timezone"],
            )
            for hhmm in slot_minutes_list:
                start = resolve_local(f"{date_str}T{hhmm}", r["timezone"])
                end = reservation_end(start, terms["reservation_duration_minutes"])
                available: list[str] = []
                options: list[dict] = []
                explain_entries: list[dict] = []
                for table in r["tables"]:
                    cap = terms["capacities"].get(table["id"], table["capacity"])
                    holds_capacity = cap >= psize
                    overlap = _has_overlap(state, r["id"], [table["id"]], start, end)
                    holds_no_overlap = not overlap
                    is_available = holds_capacity and holds_no_overlap
                    if is_available:
                        available.append(table["id"])
                        options.append({
                            "table_ids": [table["id"]],
                            "capacity": cap,
                        })
                    if explain:
                        explain_entries.append({
                            "table_id": table["id"],
                            "policy_version": terms["policy_version"],
                            "available": is_available,
                            "rules": [
                                {"rule": "capacity", "holds": holds_capacity},
                                {"rule": "no_overlap", "holds": holds_no_overlap},
                            ],
                        })
                for pair in r["combinable"]:
                    a, b = pair
                    cap = (terms["capacities"].get(a, 0)
                           + terms["capacities"].get(b, 0))
                    holds_capacity = cap >= psize
                    overlap = _has_overlap(state, r["id"], [a, b], start, end)
                    if holds_capacity and not overlap:
                        options.append({"table_ids": [a, b], "capacity": cap})
                slot = {
                    "starts_at_local": f"{date_str}T{hhmm}",
                    "starts_at": rfc3339(start),
                    "available_table_ids": available,
                    "available_options": options,
                }
                if explain:
                    slot["explain"] = explain_entries
                slots_out.append(slot)
        return OperationResult(200, {
            "restaurant_id": rid,
            "date": date_str,
            "timezone": r["timezone"],
            "slots": slots_out,
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


def _reservation_payload(state, res: dict) -> dict:
    """Serialise a reservation record (E1: ``table_id`` only for singletons).

    Also includes ``table_labels`` in ``table_ids`` order so the UI can render
    human-readable table lists (F6: ``confirmation-tables`` /
    ``reservation-tables``).
    """
    tids = list(res.get("table_ids") or [])
    labels: list[str] = []
    rest = state._restaurants.get(res.get("restaurant_id") or "")
    if rest is not None:
        by_id = {t["id"]: t.get("label") or t["id"] for t in rest.get("tables") or []}
        labels = [by_id.get(tid, tid) for tid in tids]
    out = {
        "reservation_id": res["id"],
        "reference": res["reference"],
        "restaurant_id": res["restaurant_id"],
        "table_ids": tids,
        "table_labels": labels,
        "party_size": res["party_size"],
        "status": res["status"],
        "starts_at_local": res["starts_at_local"],
        "starts_at": rfc3339(res["starts_at"]),
        "ends_at": rfc3339(res["ends_at"]),
        "created_at": rfc3339(res["created_at"]),
        "revision": res.get("revision", 1),
        "accepted_terms": dict(res.get("accepted_terms") or {}),
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


def _check_cutoff_with_terms(res: dict, terms: dict) -> None:
    """The cancellation cutoff uses the *accepted* terms (ledger 24, F4)."""
    now = _now_utc()
    delta = res["starts_at"] - now
    minutes = delta.total_seconds() / 60.0
    if minutes <= terms["cancellation_cutoff_minutes"]:
        raise OperationError(409, "cutoff_passed",
                             "now is within the cancellation cutoff")


# ---- history helpers -----------------------------------------------------


def _next_seq(res: dict) -> int:
    return len(res.get("history", [])) + 1


def _append_history(res: dict, event: str, changes: list[dict]) -> None:
    """Append a history entry to ``res`` with the next monotonic seq number.

    Per F5 timestamps are UTC; per ledger 8 seq starts at 1 and is +1.
    """
    entries = res.setdefault("history", [])
    seq = len(entries) + 1
    entry = {
        "seq": seq,
        "at": rfc3339(_now_utc()),
        "event": event,
        "changes": list(changes),
        "revision": res.get("revision", 1),
        "accepted_terms": dict(res.get("accepted_terms") or {}),
    }
    entries.append(entry)


def _history_changes_for(res: dict, new_tids: list[str],
                         new_starts_at_local: str, new_party_size: int,
                         new_table_order: Optional[list[str]] = None) -> list[dict]:
    """Build the ``changes`` array per F6 (single vs pair) and ledger 9-11.

    The table-set order is the declared combination order (F6); we pass it
    in explicitly when a pair is involved so ``table_ids`` lists reflect
    that order even when the input was reversed.
    """
    out: list[dict] = []
    old_tids = list(res["table_ids"])
    is_pair_before = len(old_tids) == 2
    is_pair_after = len(new_tids) == 2

    # table / tables field: present if the table-set really changed.
    table_set_changed = old_tids != new_tids
    if table_set_changed:
        if is_pair_before or is_pair_after:
            before = old_tids if is_pair_before else None
            after = new_tids
            out.append({"field": "table_ids", "from": before, "to": after})
        else:
            out.append({"field": "table_id", "from": old_tids[0],
                        "to": new_tids[0]})

    if res["starts_at_local"] != new_starts_at_local:
        out.append({"field": "starts_at_local",
                    "from": res["starts_at_local"],
                    "to": new_starts_at_local})
    if res["party_size"] != new_party_size:
        out.append({"field": "party_size",
                    "from": res["party_size"],
                    "to": new_party_size})
    return out


# ---- create --------------------------------------------------------------


def create_reservation(state, *, user_id: str, body: dict,
                       idempotency_key: str) -> OperationResult:
    with state.lock():
        receipt, replay_status = _idempotency_lookup(state, user_id, idempotency_key, "POST /reservations", body)
        if receipt is not None:
            return OperationResult(replay_status, receipt["response"])

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
            for tid in tids:
                if _table_for(restaurant, tid) is None:
                    raise OperationError(404, "not_found", "table not in restaurant")
            if not _pair_in_combinable(restaurant, tids):
                raise OperationError(422, "combination_not_allowed",
                                     "pair is not declared combinable")

        try:
            start = resolve_local(starts_at_local, restaurant["timezone"])
        except InvalidLocalTimeError:
            raise OperationError(422, "invalid_local_time",
                                 "local time does not exist in this timezone")
        # F1 / ledger 6 / ledger 21: policy is selected by the booking's
        # local start date; grid, duration, cutoff, capacity all follow it.
        booking_date = start.date()
        terms = _select_policy_for_date(restaurant, booking_date)
        cap_total = sum(terms["capacities"].get(t, 0) for t in tids)
        if party_size > cap_total:
            raise OperationError(422, "party_exceeds_capacity",
                                 "party_size exceeds combined table capacity")

        if not _within_opening_hours_with_terms(
                start, terms["opening_hours"], restaurant["timezone"],
                terms["reservation_duration_minutes"]):
            raise OperationError(422, "outside_opening_hours",
                                 "slot is outside opening hours or extends past closing")
        if not _is_on_slot_grid_with_terms(
                starts_at_local, terms["opening_hours"], terms["slot_minutes"],
                terms["reservation_duration_minutes"]):
            raise OperationError(422, "not_on_slot_grid",
                                 "starts_at_local is not on the slot grid")

        end = reservation_end(start, terms["reservation_duration_minutes"])

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
            "revision": 1,
            "accepted_terms": terms,
            "history": [],
        }
        state._reservations[reference] = reservation
        state._reservations_by_user.setdefault(user_id, set()).add(reference)
        state._reservations_by_restaurant.setdefault(rid, set()).add(reference)

        # Write the "created" history entry. F6: pair creation uses
        # table_ids, single-to-single uses table_id. From values are null.
        created_changes = _history_changes_for_create(
            tids, starts_at_local, party_size,
            pair_uses_ids=len(tids) == 2)
        _append_history(reservation, "created", created_changes)

        payload = _reservation_payload(state, reservation)
        _idempotency_store(state, user_id, idempotency_key, "POST /reservations", body, 201, payload)
        return OperationResult(201, payload)


def _history_changes_for_create(tids: list[str], starts_at_local: str,
                                party_size: int, *, pair_uses_ids: bool) -> list[dict]:
    """Ledger 9 / F6: creation lists all three fields with ``from: null``.
    Pairs use ``table_ids``; singles use ``table_id``.
    """
    if pair_uses_ids:
        table_field = {"field": "table_ids", "from": None, "to": list(tids)}
    else:
        table_field = {"field": "table_id", "from": None, "to": tids[0]}
    return [
        table_field,
        {"field": "starts_at_local", "from": None, "to": starts_at_local},
        {"field": "party_size", "from": None, "to": party_size},
    ]


def _allocate_reference(state) -> str:
    """An 8-char A-Z0-9 string, unique across all reservations."""
    for _ in range(64):
        ref = _reference()
        if ref not in state._reservations:
            return ref
    raise OperationError(500, "internal", "could not allocate unique reference")


def _is_on_slot_grid_with_terms(starts_at_local: str, opening_hours: list,
                                slot_minutes: int, duration: int) -> bool:
    """True iff the wall-clock minute is on the slot grid from the day's opens."""
    parsed = parse_local(starts_at_local)
    weekday = weekday_name(parsed.date())
    hours = next((h for h in opening_hours if h["weekday"] == weekday), None)
    if hours is None:
        return False
    opens = hhmm_to_minutes(hours["opens"])
    slot = parsed.hour * 60 + parsed.minute
    if slot < opens:
        return False
    if (slot - opens) % slot_minutes != 0:
        return False
    return slot + duration <= hhmm_to_minutes(hours["closes"])


def _within_opening_hours_with_terms(start: dt.datetime, opening_hours: list,
                                     tz_name: str, duration: int) -> bool:
    """True if the reservation starts and ends within the same opening-hours entry."""
    weekday = weekday_name(start.date())
    hours = next((h for h in opening_hours if h["weekday"] == weekday), None)
    if hours is None:
        return False
    opens = hhmm_to_minutes(hours["opens"])
    closes = hhmm_to_minutes(hours["closes"])
    slot = start.hour * 60 + start.minute
    if slot < opens:
        return False
    if slot + duration > closes:
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
            "reservations": [_reservation_payload(state, state._reservations[ref]) for ref in refs],
        })


def get_reservation(state, *, user_id: str, reference: str) -> OperationResult:
    check_reference("reference", reference)
    with state.lock():
        res = state._reservations.get(reference)
        if res is None or res["user_id"] != user_id:
            raise OperationError(404, "not_found", "reservation not found")
        return OperationResult(200, _reservation_payload(state, res))


# ---- history / decision --------------------------------------------------


def get_history(state, *, user_id: Optional[str], reference: str) -> OperationResult:
    """Ledger 7: history is owner-only 404 even without auth."""
    check_reference("reference", reference)
    with state.lock():
        res = state._reservations.get(reference)
        if res is None or res["user_id"] != user_id:
            raise OperationError(404, "not_found", "reservation not found")
        return OperationResult(200, {
            "reference": res["reference"],
            "entries": [dict(e) for e in res.get("history", [])],
        })


def get_decision(state, *, user_id: Optional[str], reference: str) -> OperationResult:
    """Ledger 29: same owner-only 404 rule as history."""
    check_reference("reference", reference)
    with state.lock():
        res = state._reservations.get(reference)
        if res is None or res["user_id"] != user_id:
            raise OperationError(404, "not_found", "reservation not found")
        return OperationResult(200, {
            "reference": res["reference"],
            "revision": res.get("revision", 1),
            "accepted_terms": dict(res.get("accepted_terms") or {}),
        })


# ---- cancel --------------------------------------------------------------


def cancel_reservation(state, *, user_id: str, reference: str) -> OperationResult:
    check_reference("reference", reference)
    with state.lock():
        res = state._reservations.get(reference)
        if res is None or res["user_id"] != user_id:
            raise OperationError(404, "not_found", "reservation not found")
        if res["status"] == "cancelled":
            # Repeated cancel does nothing — no history, no revision bump.
            return OperationResult(200, _reservation_payload(state, res))
        terms = dict(res.get("accepted_terms") or _policy0_terms(
            state._restaurants[res["restaurant_id"]]))
        _check_cutoff_with_terms(res, terms)
        res["status"] = "cancelled"
        # Cancel increments revision once (ledger 27).
        res["revision"] = res.get("revision", 1) + 1
        _append_history(res, "cancelled", [])
        return OperationResult(200, _reservation_payload(state, res))


# ---- patch ---------------------------------------------------------------


def _check_expected_revision(body: dict, current_revision: int) -> None:
    """Ledger 28: optional ``expected_revision`` on PATCH.

    A positive integer differing from the current revision → 409
    ``stale_revision`` *before* cutoff/validation. Invalid type/range → 422.
    Omission retains stage-1 semantics.
    """
    if "expected_revision" not in body:
        return
    val = body["expected_revision"]
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValidationFailed("expected_revision must be a positive integer")
    if val < 1:
        raise ValidationFailed("expected_revision must be a positive integer")
    if val != current_revision:
        raise OperationError(409, "stale_revision",
                             "expected_revision does not match current revision")


def _validate_amendment_target(state, restaurant: dict, res: dict,
                               new_tids: list[str], new_starts_at_local: str,
                               new_party_size: int) -> tuple[dt.datetime, dt.datetime, dict]:
    """Run every rule against the *resulting* date's policy (ledger 25)."""
    if len(new_tids) == 1:
        table = _table_for(restaurant, new_tids[0])
        if table is None:
            raise OperationError(404, "not_found", "table not in restaurant")
    else:
        for tid in new_tids:
            if _table_for(restaurant, tid) is None:
                raise OperationError(404, "not_found", "table not in restaurant")
        if not _pair_in_combinable(restaurant, new_tids):
            raise OperationError(422, "combination_not_allowed",
                                 "pair is not declared combinable")
    try:
        new_start = resolve_local(new_starts_at_local, restaurant["timezone"])
    except InvalidLocalTimeError:
        raise OperationError(422, "invalid_local_time",
                             "local time does not exist in this timezone")

    terms = _select_policy_for_date(restaurant, new_start.date())
    cap_total = sum(terms["capacities"].get(t, 0) for t in new_tids)
    if new_party_size > cap_total:
        raise OperationError(422, "party_exceeds_capacity",
                             "party_size exceeds combined table capacity")
    if not _within_opening_hours_with_terms(
            new_start, terms["opening_hours"], restaurant["timezone"],
            terms["reservation_duration_minutes"]):
        raise OperationError(422, "outside_opening_hours",
                             "slot is outside opening hours or extends past closing")
    if not _is_on_slot_grid_with_terms(
            new_starts_at_local, terms["opening_hours"], terms["slot_minutes"],
            terms["reservation_duration_minutes"]):
        raise OperationError(422, "not_on_slot_grid",
                             "starts_at_local is not on the slot grid")

    new_end = reservation_end(new_start, terms["reservation_duration_minutes"])
    return new_start, new_end, terms


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

        # expected_revision check happens before cutoff per ledger 28.
        _check_expected_revision(body, res.get("revision", 1))

        # F4 / ledger 24/25: cutoff uses *accepted* terms, against current start.
        terms = dict(res.get("accepted_terms") or _policy0_terms(
            state._restaurants[res["restaurant_id"]]))
        _check_cutoff_with_terms(res, terms)

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

        check_local_datetime(new_starts_at_local)
        check_party_size(new_party_size)

        new_start, new_end, new_terms = _validate_amendment_target(
            state, restaurant, res, new_tids, new_starts_at_local, new_party_size)

        # Conflict check (ignore self).
        if _has_overlap(state, res["restaurant_id"], new_tids,
                        new_start, new_end, exclude_ref=reference):
            raise OperationError(409, "table_unavailable",
                                 "table is taken for an overlapping interval")

        # Detect no-op (ledger 26): no real change → leave terms/revision,
        # no history entry. The fields are unchanged ⇒ changes list empty.
        same_table = list(new_tids) == list(res["table_ids"])
        same_time = new_starts_at_local == res["starts_at_local"]
        same_party = new_party_size == res["party_size"]
        if same_table and same_time and same_party:
            return OperationResult(200, _reservation_payload(state, res))

        changes = _history_changes_for(res, new_tids, new_starts_at_local, new_party_size)
        if not changes:
            return OperationResult(200, _reservation_payload(state, res))

        res["table_ids"] = list(new_tids)
        res["starts_at_local"] = new_starts_at_local
        res["starts_at"] = new_start
        res["ends_at"] = new_end
        res["party_size"] = new_party_size
        res["accepted_terms"] = new_terms
        res["revision"] = res.get("revision", 1) + 1
        _append_history(res, "changed", changes)

        return OperationResult(200, _reservation_payload(state, res))


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

        # Pre-compute each per-move plan (cutoff check against accepted terms,
        # expected_revision check, then validation against the resulting
        # date's policy).
        plans = []
        for entry, res in loaded:
            if res["status"] == "cancelled":
                raise OperationError(409, "reservation_cancelled",
                                     "reservation is cancelled")
            _check_expected_revision(entry, res.get("revision", 1))
            terms = dict(res.get("accepted_terms") or _policy0_terms(restaurant))
            _check_cutoff_with_terms(res, terms)
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

            same_table = list(new_tids) == list(res["table_ids"])
            same_time = new_starts_at_local == res["starts_at_local"]
            same_party = new_party_size == res["party_size"]
            noop = same_table and same_time and same_party

            if not noop:
                new_start, new_end, new_terms = _validate_amendment_target(
                    state, restaurant, res, new_tids, new_starts_at_local, new_party_size)
            else:
                new_start = res["starts_at"]
                new_end = res["ends_at"]
                new_terms = dict(res.get("accepted_terms") or {})

            plans.append((entry, res, new_tids, new_starts_at_local,
                          new_start, new_end, new_party_size, new_terms, noop))

        # Conflict check: no two listed bookings may overlap with each other,
        # and no listed booking may overlap with an unlisted one.
        listed_refs = {res["reference"] for _, res, *_ in plans}
        for i in range(len(plans)):
            for j in range(i + 1, len(plans)):
                _, _, tids_i, _, start_i, end_i, *_ = plans[i]
                _, _, tids_j, _, start_j, end_j, *_ = plans[j]
                if set(tids_i) & set(tids_j) and overlaps(start_i, end_i, start_j, end_j):
                    raise OperationError(409, "table_unavailable",
                                         "table is taken for an overlapping interval")
        for (_, _, tids, _, start, end, _, _, _) in plans:
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

        # Apply atomically. Each real change increments revision once;
        # restaurant revision +1 once for the batch (F8 / ledger 43).
        restaurant_revision_bumped = False
        affected_series: dict[str, dict] = {}
        for (entry, res, new_tids, new_starts_at_local,
             new_start, new_end, new_party_size, new_terms, noop) in plans:
            if noop:
                continue
            res["table_ids"] = list(new_tids)
            res["starts_at_local"] = new_starts_at_local
            res["starts_at"] = new_start
            res["ends_at"] = new_end
            res["party_size"] = new_party_size
            res["accepted_terms"] = new_terms
            res["revision"] = res.get("revision", 1) + 1
            _append_history(res, "changed",
                            _history_changes_for(res, new_tids, new_starts_at_local,
                                                  new_party_size))
            restaurant_revision_bumped = True
            # Mark the affected series and occurrence exception.
            series_id = state._series_by_reference.get(res["reference"])
            if series_id is not None:
                series = state._series.get(series_id)
                if series is not None and series_id not in affected_series:
                    affected_series[series_id] = series
                for occ in (series["occurrences"] if series else []):
                    if occ["reference"] == res["reference"]:
                        occ["exception"] = True
                        break
        if restaurant_revision_bumped:
            restaurant["revision"] = restaurant.get("revision", 0) + 1
            for sid, series in affected_series.items():
                series["revision"] = series.get("revision", 1) + 1

        output = []
        for ref in refs:
            output.append(_reservation_payload(state, state._reservations[ref]))
        payload = {"reservations": output}
        _idempotency_store(state, user_id, idempotency_key, "POST /reservation-moves", body, 201, payload)
        return OperationResult(201, payload)


# ---- series --------------------------------------------------------------


def create_series(state, *, user_id: str, body: dict,
                  idempotency_key: str) -> OperationResult:
    """Ledger 30-38: adopt a reservation as occurrence 0 of a recurring series."""
    with state.lock():
        receipt, replay_status = _idempotency_lookup(
            state, user_id, idempotency_key, "POST /series", body)
        if receipt is not None:
            return OperationResult(replay_status, receipt["response"])

        anchor_reference = check_reference("anchor_reference",
                                           body.get("anchor_reference"))
        count = check_count(body.get("count"))
        interval_weeks = check_interval_weeks(body.get("interval_weeks"))

        anchor = state._reservations.get(anchor_reference)
        if anchor is None or anchor["user_id"] != user_id:
            raise OperationError(404, "not_found", "reservation not found")
        if anchor["status"] == "cancelled":
            raise OperationError(409, "reservation_cancelled",
                                 "anchor reservation is cancelled")
        # Anchor cutoff check (F9): the anchor must satisfy its accepted
        # cutoff at adoption time.
        anchor_terms = dict(anchor.get("accepted_terms") or _policy0_terms(
            state._restaurants[anchor["restaurant_id"]]))
        _check_cutoff_with_terms(anchor, anchor_terms)
        # Already-in-series guard (ledger 31).
        if anchor_reference in state._series_by_reference:
            raise OperationError(409, "already_in_series",
                                 "reservation is already adopted into a series")

        restaurant = state._restaurants[anchor["restaurant_id"]]
        # Compute local-clock shift for the anchor.
        anchor_dt = anchor["starts_at"]
        anchor_local_date = anchor_dt.date()
        anchor_time = anchor_dt.replace(year=anchor_local_date.year,
                                        month=anchor_local_date.month,
                                        day=anchor_local_date.day,
                                        tzinfo=anchor_dt.tzinfo)
        anchor_local_str = anchor["starts_at_local"]
        # Split "YYYY-MM-DDTHH:MM" to compute later occurrences' local dates.
        hhmm = anchor_local_str.split("T", 1)[1]

        # Pre-validate every occurrence up front; on failure, nothing
        # persists (ledger 34). First failing occurrence determines the
        # error (ledger 34).
        occurrence_records: list[tuple[int, str, dict, dt.datetime,
                                       dt.datetime, str, dict]] = []
        for i in range(1, count):
            target_date = anchor_local_date + dt.timedelta(weeks=interval_weeks * i)
            local_str = f"{target_date.isoformat()}T{hhmm}"
            try:
                new_start = resolve_local(local_str, restaurant["timezone"])
            except InvalidLocalTimeError:
                raise OperationError(422, "invalid_local_time",
                                     "occurrence local time does not exist")
            terms = _select_policy_for_date(restaurant, new_start.date())
            cap_total = sum(terms["capacities"].get(t, 0)
                            for t in anchor["table_ids"])
            if anchor["party_size"] > cap_total:
                raise OperationError(422, "party_exceeds_capacity",
                                     "party exceeds selected policy capacity")
            if not _within_opening_hours_with_terms(
                    new_start, terms["opening_hours"], restaurant["timezone"],
                    terms["reservation_duration_minutes"]):
                raise OperationError(422, "outside_opening_hours",
                                     "occurrence outside opening hours")
            if not _is_on_slot_grid_with_terms(
                    local_str, terms["opening_hours"], terms["slot_minutes"],
                    terms["reservation_duration_minutes"]):
                raise OperationError(422, "not_on_slot_grid",
                                     "occurrence not on slot grid")
            new_end = reservation_end(new_start,
                                      terms["reservation_duration_minutes"])
            if _has_overlap(state, restaurant["id"], anchor["table_ids"],
                            new_start, new_end):
                raise OperationError(409, "table_unavailable",
                                     "table is taken for an overlapping interval")
            # Pre-generate the reference but do not insert until all are OK.
            ref = _allocate_reference(state)
            occurrence_records.append(
                (i, ref, list(anchor["table_ids"]), new_start, new_end,
                 local_str, terms))

        # All-or-nothing insert (ledger 34).
        new_reservations: list[dict] = []
        try:
            for (i, ref, tids, start, end, local_str, terms) in occurrence_records:
                reservation = {
                    "id": f"res_{uuid.uuid4().hex[:12]}",
                    "reference": ref,
                    "user_id": user_id,
                    "restaurant_id": restaurant["id"],
                    "table_ids": list(tids),
                    "starts_at_local": local_str,
                    "starts_at": start,
                    "ends_at": end,
                    "party_size": anchor["party_size"],
                    "status": "confirmed",
                    "created_at": _now_utc(),
                    "revision": 1,
                    "accepted_terms": terms,
                    "history": [],
                }
                state._reservations[ref] = reservation
                state._reservations_by_user.setdefault(user_id, set()).add(ref)
                state._reservations_by_restaurant.setdefault(
                    restaurant["id"], set()).add(ref)
                new_reservations.append((i, reservation))
        except Exception:
            # Rollback everything we inserted.
            for _, r in new_reservations:
                ref = r["reference"]
                state._reservations_by_user.get(user_id, set()).discard(ref)
                state._reservations_by_restaurant.get(
                    restaurant["id"], set()).discard(ref)
                state._reservations.pop(ref, None)
            raise

        # Build the series record.
        series_id = _new_series_id()
        occurrences = [
            {"index": 0, "reference": anchor_reference, "exception": False}
        ]
        for (i, reservation) in new_reservations:
            occurrences.append({
                "index": i, "reference": reservation["reference"],
                "exception": False})
        series = {
            "series_id": series_id,
            "user_id": user_id,
            "restaurant_id": restaurant["id"],
            "anchor_reference": anchor_reference,
            "interval_weeks": interval_weeks,
            "revision": 1,
            "occurrences": occurrences,
        }
        state._series[series_id] = series
        for occ in occurrences:
            state._series_by_reference[occ["reference"]] = series_id
        state._series_by_user.setdefault(user_id, set()).add(series_id)
        # Restaurant revision +1 once for the adoption (ledger 38 / F8).
        restaurant["revision"] = restaurant.get("revision", 0) + 1

        response = _series_payload(state, series)
        _idempotency_store(state, user_id, idempotency_key,
                           "POST /series", body, 201, response)
        return OperationResult(201, response)


def get_series(state, *, user_id: Optional[str], series_id: str) -> OperationResult:
    check_id("series_id", series_id)
    with state.lock():
        series = state._series.get(series_id)
        # Owner-only 404 (also without auth) per ledger 36.
        if series is None or series["user_id"] != user_id:
            raise OperationError(404, "not_found", "series not found")
        return OperationResult(200, _series_payload(state, series))


def _series_payload(state, series: dict) -> dict:
    out = {
        "series_id": series["series_id"],
        "revision": series.get("revision", 1),
        "interval_weeks": series["interval_weeks"],
        "occurrences": [],
    }
    for occ in series["occurrences"]:
        ref = occ["reference"]
        res = state._reservations.get(ref)
        reservation_payload = (
            _reservation_payload(state, res) if res is not None else None)
        out["occurrences"].append({
            "index": occ["index"],
            "reference": ref,
            "exception": bool(occ.get("exception", False)),
            "reservation": reservation_payload,
        })
    return out


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
