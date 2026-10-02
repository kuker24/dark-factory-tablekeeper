"""In-memory state container.

A single ``threading.Lock`` guards every mutation and every consistency-
critical read. That is D1's "single-writer serialization": booking conflicts
are resolved by holding the lock across the availability check and the
insert, so two simultaneous ``POST /reservations`` for the same table and
overlapping slot are mutually exclusive and exactly one succeeds.

The lock is also held for read endpoints that need a stable view of
reservations (e.g. ``GET /availability``). Cheaper reads
(``GET /restaurants``) use the lock too: the critical section is short and
the harness only loads 50 concurrent requests, so the lock contention
budget is comfortable.
"""
from __future__ import annotations

import copy
import datetime as dt
import secrets
import string
import threading
import uuid
from typing import Any, Iterable, Optional

from time_utils import (
    WEEKDAYS,
    hhmm_to_minutes,
    overlaps,
    parse_iso_date,
    parse_rfc3339,
    rfc3339,
    reservation_end,
    resolve_local,
    slot_starts_for_weekday,
    weekday_name,
)


REFERENCE_ALPHABET = string.ascii_uppercase + string.digits


class State:
    """Thread-safe, in-memory container for the whole service."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._users: dict[str, dict] = {}                # user_id -> user record
        self._users_by_email: dict[str, str] = {}        # lower(email) -> user_id
        self._tokens: dict[str, str] = {}                # token -> user_id
        self._restaurants: dict[str, dict] = {}          # rid -> restaurant record
        self._restaurants_order: list[str] = []          # insertion order
        self._reservations: dict[str, dict] = {}         # reference -> reservation record
        self._reservations_by_user: dict[str, set[str]] = {}
        self._reservations_by_restaurant: dict[str, set[str]] = {}
        self._idempotency: dict[str, dict[str, dict]] = {}   # user_id -> key -> receipt

    # ---- lock helpers --------------------------------------------------

    def lock(self) -> threading.RLock:
        return self._lock

    def with_lock(self, fn):
        with self._lock:
            return fn()

    # ---- snapshot ------------------------------------------------------

    def snapshot(self) -> dict:
        """An opaque dict used for /_test/export. Identity-preserving."""
        with self._lock:
            return {
                "users": {uid: _clean_user(u) for uid, u in self._users.items()},
                "users_by_email": dict(self._users_by_email),
                "tokens": dict(self._tokens),
                "restaurants": {rid: _clean_restaurant(r)
                                for rid, r in self._restaurants.items()},
                "restaurants_order": list(self._restaurants_order),
                "reservations": {ref: _clean_reservation(r)
                                 for ref, r in self._reservations.items()},
                "reservations_by_user": {uid: sorted(refs)
                                          for uid, refs in self._reservations_by_user.items()},
                "reservations_by_restaurant": {rid: sorted(refs)
                                                for rid, refs in self._reservations_by_restaurant.items()},
                "idempotency": {uid: {k: dict(v) for k, v in d.items()}
                                for uid, d in self._idempotency.items()},
            }

    def replace(self, snap: dict) -> None:
        """Atomic replacement from a snapshot. Per §10 this is opaque to the caller."""
        with self._lock:
            self._users = {uid: dict(rec) for uid, rec in snap["users"].items()}
            self._users_by_email = dict(snap["users_by_email"])
            self._tokens = dict(snap["tokens"])
            self._restaurants = {rid: dict(rec) for rid, rec in snap["restaurants"].items()}
            self._restaurants_order = list(snap["restaurants_order"])
            self._reservations = {}
            for ref, rec in snap["reservations"].items():
                clean = dict(rec)
                # snapshot() stringified datetimes; rebuild them so
                # operations can keep using ``overlaps`` etc.
                if isinstance(clean.get("starts_at"), str):
                    clean["starts_at"] = parse_rfc3339(clean["starts_at"])
                if isinstance(clean.get("ends_at"), str):
                    clean["ends_at"] = parse_rfc3339(clean["ends_at"])
                if isinstance(clean.get("created_at"), str):
                    clean["created_at"] = parse_rfc3339(clean["created_at"])
                self._reservations[ref] = clean
            self._reservations_by_user = {uid: set(refs)
                                          for uid, refs in snap["reservations_by_user"].items()}
            self._reservations_by_restaurant = {rid: set(refs)
                                                for rid, refs in snap["reservations_by_restaurant"].items()}
            self._idempotency = {uid: {k: dict(v) for k, v in d.items()}
                                 for uid, d in snap["idempotency"].items()}


# ---- cleaning helpers (turn live objects into JSON-safe snapshots) ----


def _clean_user(user: dict) -> dict:
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "password": user["password"],
    }


def _clean_restaurant(rest: dict) -> dict:
    return {
        "id": rest["id"],
        "name": rest["name"],
        "timezone": rest["timezone"],
        "slot_minutes": rest["slot_minutes"],
        "reservation_duration_minutes": rest["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": rest["cancellation_cutoff_minutes"],
        "opening_hours": list(rest["opening_hours"]),
        "tables": list(rest["tables"]),
    }


def _clean_reservation(res: dict) -> dict:
    out = dict(res)
    if isinstance(out.get("starts_at"), dt.datetime):
        out["starts_at"] = rfc3339(out["starts_at"])
    if isinstance(out.get("ends_at"), dt.datetime):
        out["ends_at"] = rfc3339(out["ends_at"])
    if isinstance(out.get("created_at"), dt.datetime):
        out["created_at"] = rfc3339(out["created_at"])
    return out
