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

Stage 3 adds:

* a ``policies`` list, ``manager_user_ids`` and a per-restaurant
  ``revision`` counter (F8, ledger 14-20),
* a per-reservation ``revision`` (starts at 1), ``accepted_terms`` snapshot
  and ``history`` list (ledger 21-29),
* a ``_series`` registry mapping ``series_id`` to an immutable agreement
  record (ledger 30-38).
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
from validation import ValidationFailed


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
        self._idempotency: dict[str, dict[str, dict[str, dict]]] = {}   # user_id -> key -> path -> receipt
        self._series: dict[str, dict] = {}               # series_id -> series record
        self._series_by_reference: dict[str, str] = {}   # reference -> series_id
        self._series_by_user: dict[str, set[str]] = {}   # user_id -> set of series_ids

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
                "series": {sid: _clean_series(s) for sid, s in self._series.items()},
                "series_by_reference": dict(self._series_by_reference),
                "series_by_user": {uid: sorted(sids)
                                   for uid, sids in self._series_by_user.items()},
            }

    def replace(self, snap: dict) -> None:
        """Atomic replacement from a snapshot. Per §10 this is opaque to the caller.

        Stage-1 snapshots have ``table_id`` (singular) on each reservation
        and no ``combinable`` field on restaurants; we forward both to the
        stage-2 shape on import (E8).

        Stage-1/2 snapshots may also lack stage-3 fields (``policies``,
        ``manager_user_ids``, ``revision``, ``accepted_terms``, ``history``);
        F12 requires us to default those so stage-1/2 exports continue to
        import cleanly.
        """
        with self._lock:
            self._users = {uid: dict(rec) for uid, rec in snap["users"].items()}
            self._users_by_email = dict(snap["users_by_email"])
            self._tokens = dict(snap["tokens"])
            self._restaurants = {}
            for rid, rec in snap["restaurants"].items():
                clean = dict(rec)
                if "combinable" not in clean:
                    clean["combinable"] = []
                if "policies" not in clean:
                    clean["policies"] = []
                if "manager_user_ids" not in clean:
                    clean["manager_user_ids"] = []
                if "revision" not in clean:
                    clean["revision"] = 0
                if "next_policy_version" not in clean:
                    clean["next_policy_version"] = len(clean["policies"]) + 1
                self._restaurants[rid] = clean
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
                # Stage-1 had ``table_id`` only; stage-2 needs ``table_ids``.
                if "table_ids" not in clean:
                    tid = clean.get("table_id")
                    if not isinstance(tid, str):
                        raise ValidationFailed(
                            f"reservation {ref!r} has no table_id")
                    clean["table_ids"] = [tid]
                # Stage-3 fields: default revision=1, accepted_terms=policy0,
                # history=[] so a stage-1/2 export imports cleanly.
                restaurant = self._restaurants[clean["restaurant_id"]]
                if "revision" not in clean:
                    clean["revision"] = 1
                if "accepted_terms" not in clean:
                    clean["accepted_terms"] = _policy0_terms(restaurant)
                if "history" not in clean:
                    clean["history"] = []
                self._reservations[ref] = clean
            self._reservations_by_user = {uid: set(refs)
                                          for uid, refs in snap["reservations_by_user"].items()}
            self._reservations_by_restaurant = {rid: set(refs)
                                                for rid, refs in snap["reservations_by_restaurant"].items()}
            self._idempotency = {uid: {k: dict(v) for k, v in d.items()}
                                 for uid, d in snap["idempotency"].items()}
            self._series = {sid: _hydrate_series(s) for sid, s in snap.get("series", {}).items()}
            self._series_by_reference = dict(snap.get("series_by_reference", {}))
            self._series_by_user = {uid: set(sids)
                                    for uid, sids in snap.get("series_by_user", {}).items()}


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
        "combinable": list(rest.get("combinable", ())),
        "policies": [dict(p) for p in rest.get("policies", ())],
        "manager_user_ids": list(rest.get("manager_user_ids", ())),
        "revision": rest.get("revision", 0),
        "next_policy_version": rest.get("next_policy_version",
                                          len(rest.get("policies", ())) + 1),
    }


def _clean_reservation(res: dict) -> dict:
    out = dict(res)
    if isinstance(out.get("starts_at"), dt.datetime):
        out["starts_at"] = rfc3339(out["starts_at"])
    if isinstance(out.get("ends_at"), dt.datetime):
        out["ends_at"] = rfc3339(out["ends_at"])
    if isinstance(out.get("created_at"), dt.datetime):
        out["created_at"] = rfc3339(out["created_at"])
    # E1: stage-2 records always carry table_ids; keep the singular only
    # for singletons so the wire format matches the spec.
    tids = list(out.get("table_ids") or ())
    if len(tids) == 1:
        out["table_id"] = tids[0]
    return out


def _clean_series(s: dict) -> dict:
    return {
        "series_id": s["series_id"],
        "user_id": s["user_id"],
        "restaurant_id": s["restaurant_id"],
        "anchor_reference": s["anchor_reference"],
        "interval_weeks": s["interval_weeks"],
        "revision": s.get("revision", 1),
        "occurrences": [dict(o) for o in s["occurrences"]],
    }


def _hydrate_series(snap: dict) -> dict:
    return {
        "series_id": snap["series_id"],
        "user_id": snap["user_id"],
        "restaurant_id": snap["restaurant_id"],
        "anchor_reference": snap["anchor_reference"],
        "interval_weeks": snap["interval_weeks"],
        "revision": snap.get("revision", 1),
        "occurrences": [dict(o) for o in snap["occurrences"]],
    }


def _policy0_terms(restaurant: dict) -> dict:
    """Build the policy-0 ``accepted_terms`` snapshot from a restaurant record."""
    return {
        "policy_version": 0,
        "slot_minutes": restaurant["slot_minutes"],
        "reservation_duration_minutes": restaurant["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": restaurant["cancellation_cutoff_minutes"],
        "opening_hours": list(restaurant["opening_hours"]),
        "capacities": {t["id"]: t["capacity"] for t in restaurant["tables"]},
    }
