#!/usr/bin/env python3
"""Independent stage-2 API probes for tablekeeper (Reviewer).

Runs against a live service base URL. Written from plans/stage-2/ledger.md (reqs 1-41,
E1-E12) and the stage-1+stage-2 specifications, not from the supplied checks.

    probe_stage2_api.py --base-url http://127.0.0.1:8080
    probe_stage2_api.py --base-url http://127.0.0.1:8081 --previous-base-url http://127.0.0.1:8082

`--previous-base-url` is a stage-1 service used to produce an authentic stage-1 export for
the upgrade probes (reqs 21-25). Without it those probes report SKIP.

Prints one line per probe: PASS/FAIL/SKIP name (detail). Exit 1 if any probe fails. Every
request carries a 5s timeout (10s for reset/import) so the run cannot hang.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import re
import sys
import uuid
from zoneinfo import ZoneInfo

import httpx

TIMEOUT = 5.0
RESET_TIMEOUT = 10.0
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
REF = re.compile(r"^[A-Z0-9]{6,12}$")

ADA = {"id": "u_ada", "email": "ada@example.com", "password": "correct horse", "display_name": "Ada"}
BOB = {"id": "u_bob", "email": "bob@example.com", "password": "correct horse", "display_name": "Bob"}

RESULTS: list[tuple[str, str, str]] = []  # (status, name, detail)


def check(name: str, fn):
    try:
        detail = fn()
        RESULTS.append(("PASS", name, detail or ""))
    except Skip as exc:
        RESULTS.append(("SKIP", name, str(exc)))
    except AssertionError as exc:
        RESULTS.append(("FAIL", name, str(exc)))
    except Exception as exc:  # noqa: BLE001
        RESULTS.append(("FAIL", name, f"{type(exc).__name__}: {exc}"))


class Skip(Exception):
    pass


# ---------------------------------------------------------------- fixtures
def all_week(opens="18:00", closes="23:00"):
    return [{"weekday": d, "opens": opens, "closes": closes} for d in WEEKDAYS]


def restaurant(rid="r_anker", *, name="Zum Anker", timezone="Europe/Berlin",
               slot_minutes=30, reservation_duration_minutes=90,
               cancellation_cutoff_minutes=120, opening_hours=None, tables=None,
               combinable=None):
    r = {
        "id": rid, "name": name, "timezone": timezone,
        "slot_minutes": slot_minutes,
        "reservation_duration_minutes": reservation_duration_minutes,
        "cancellation_cutoff_minutes": cancellation_cutoff_minutes,
        "opening_hours": all_week() if opening_hours is None else opening_hours,
        "tables": tables if tables is not None else [
            {"id": "t_1", "label": "1", "capacity": 2},
            {"id": "t_2", "label": "2", "capacity": 4},
            {"id": "t_3", "label": "3", "capacity": 6},
        ],
    }
    if combinable is not None:
        r["combinable"] = combinable
    return r


def fixture(*, users=None, restaurants=None, reservations=None):
    return {"users": [ADA, BOB] if users is None else users,
            "restaurants": [restaurant()] if restaurants is None else restaurants,
            "reservations": reservations or []}


def booking_date(timezone="Europe/Berlin", lead=7):
    return (dt.datetime.now(ZoneInfo(timezone)).date() + dt.timedelta(days=lead)).isoformat()


def local(date, hhmm="19:00"):
    return f"{date}T{hhmm}"


def seeded(date, *, reference="SEED01", table_id="t_2", table_ids=None,
           user_id="u_ada", party_size=4, rest="r_anker", status=None,
           at="19:00"):
    r = {"id": "res_seed", "reference": reference, "user_id": user_id,
         "restaurant_id": rest, "starts_at_local": local(date, at), "party_size": party_size}
    if table_ids is not None:
        r["table_ids"] = table_ids
    else:
        r["table_id"] = table_id
    if status is not None:
        r["status"] = status
    return r


# ---------------------------------------------------------------- client
class C:
    def __init__(self, base, token=None):
        self.base = base.rstrip("/")
        self.token = token
        self.h = httpx.Client(base_url=self.base, timeout=TIMEOUT)

    def close(self):
        self.h.close()

    def req(self, method, path, *, json=None, content=None, key=None,
            token=..., params=None, timeout=None):
        hdrs = {}
        tok = self.token if token is ... else token
        if tok is not None:
            hdrs["Authorization"] = f"Bearer {tok}"
        if key is not None:
            hdrs["Idempotency-Key"] = key
        if json is not None or content is not None:
            hdrs["Content-Type"] = "application/json"
        kw = {"headers": hdrs}
        if json is not None:
            kw["json"] = json
        if content is not None:
            kw["content"] = content
        if params is not None:
            kw["params"] = params
        if timeout is not None:
            kw["timeout"] = timeout
        return self.h.request(method, path, **kw)

    def get(self, p, **k):
        return self.req("GET", p, **k)

    def post(self, p, **k):
        return self.req("POST", p, **k)

    def patch(self, p, **k):
        return self.req("PATCH", p, **k)


def reset(base, body=None, raw=False):
    resp = httpx.post(f"{base}/_test/reset", json=body if body is not None else fixture(),
                      timeout=RESET_TIMEOUT)
    if not raw:
        assert resp.status_code == 204, f"reset -> {resp.status_code} {resp.text[:200]}"
    return resp


def auth(base, email, password):
    r = httpx.post(f"{base}/auth/login", json={"email": email, "password": password},
                   timeout=TIMEOUT)
    assert r.status_code == 200, f"login -> {r.status_code} {r.text[:200]}"
    c = C(base)
    c.token = r.json()["token"]
    return c


def code_of(resp):
    try:
        return resp.json()["error"]["code"]
    except Exception:
        return None


def expect(resp, status, code=None):
    assert resp.status_code == status, \
        f"expected {status}, got {resp.status_code}: {resp.text[:240]}"
    if code is not None:
        got = code_of(resp)
        assert got == code, f"expected code {code}, got {got}: {resp.text[:240]}"
    return resp


def new_key():
    return uuid.uuid4().hex


def book(c, base, *, table_id="t_2", table_ids=None, date=None, at="19:00",
         party_size=4, rest="r_anker", key=None, starts_at_local=None, both=False, **extra):
    date = date or booking_date()
    body = {"restaurant_id": rest,
            "starts_at_local": starts_at_local or local(date, at), "party_size": party_size}
    if both:
        body["table_id"] = table_id
        body["table_ids"] = table_ids if table_ids is not None else [table_id]
    elif table_ids is not None:
        body["table_ids"] = table_ids
    else:
        body["table_id"] = table_id
    body.update(extra)
    return c.post("/reservations", json=body, key=key or new_key())


# ---------------------------------------------------------------- probes
def p_health_and_stage1_regressions(base):
    """Stage-1 health + a couple of stage-1 fundamentals still hold."""
    reset(base)
    r = httpx.get(f"{base}/health", timeout=TIMEOUT)
    assert r.status_code == 200 and r.json() == {"status": "ok"}, r.text[:200]
    c = auth(base, ADA["email"], ADA["password"])
    expect(book(c, base), 201)
    # wrong JSON type is still 400 (stage-1 finding 1 fix)
    expect(book(c, base, rest=17), 400, "malformed_request")
    expect(book(c, base, table_id=17), 400, "malformed_request")
    # availability unchanged singles field present
    a = httpx.get(f"{base}/availability", params={
        "restaurant_id": "r_anker", "date": booking_date(), "party_size": 2}, timeout=TIMEOUT)
    expect(a, 200)
    assert "available_table_ids" in a.json()["slots"][0]
    return "health + stage-1 fundamentals"


def p_combinable_and_nontransitive(base):
    """Req 26-27: pairs only; unlisted pair rejected; non-transitive; capacity summed."""
    reset(base, fixture(restaurants=[restaurant(
        tables=[{"id": "t_1", "label": "1", "capacity": 2},
                {"id": "t_2", "label": "2", "capacity": 4},
                {"id": "t_3", "label": "3", "capacity": 6},
                {"id": "t_4", "label": "4", "capacity": 6}],
        combinable=[["t_1", "t_2"], ["t_2", "t_3"]])]))
    c = auth(base, ADA["email"], ADA["password"])
    # listed pair, summed capacity 6
    r = expect(book(c, base, table_ids=["t_1", "t_2"], party_size=6), 201)
    assert r.json()["table_ids"] == ["t_1", "t_2"], r.json()
    assert "table_id" not in r.json(), "combined set must omit table_id"
    # non-transitive: t_1+t_3 is not a declared pair
    expect(book(c, base, table_ids=["t_1", "t_3"], party_size=8), 422, "combination_not_allowed")
    # unlisted pair regardless of sizes
    expect(book(c, base, table_ids=["t_3", "t_4"], party_size=12), 422, "combination_not_allowed")
    # three tables -> 422 combination_not_allowed (req 33: pairs only)
    expect(book(c, base, table_ids=["t_1", "t_2", "t_3"], party_size=6), 422, "combination_not_allowed")
    # four tables likewise
    expect(book(c, base, table_ids=["t_1", "t_2", "t_3", "t_4"], party_size=6), 422, "combination_not_allowed")
    return "pairs only, non-transitive, capacity summed"


def p_available_options_order_and_shape(base):
    """Req 29-30: singles first in fixture order then pairs in combinable order; pair ids in
    combinable order; singles-only available_table_ids."""
    reset(base, fixture(restaurants=[restaurant(
        tables=[{"id": "t_1", "label": "1", "capacity": 2},
                {"id": "t_2", "label": "2", "capacity": 4},
                {"id": "t_3", "label": "3", "capacity": 6}],
        combinable=[["t_1", "t_2"], ["t_2", "t_3"]])]))
    date = booking_date()
    r = expect(httpx.get(f"{base}/availability", params={
        "restaurant_id": "r_anker", "date": date, "party_size": 2}, timeout=TIMEOUT), 200)
    slot = next(s for s in r.json()["slots"] if s["starts_at_local"].endswith("18:00"))
    assert slot["available_table_ids"] == ["t_1", "t_2", "t_3"], slot["available_table_ids"]
    opts = slot["available_options"]
    singles = [o for o in opts if len(o["table_ids"]) == 1]
    pairs = [o for o in opts if len(o["table_ids"]) == 2]
    assert [o["table_ids"][0] for o in singles] == ["t_1", "t_2", "t_3"], opts
    assert [o["table_ids"] for o in pairs] == [["t_1", "t_2"], ["t_2", "t_3"]], opts
    assert all(o["capacity"] == sum(
        t["capacity"] for t in restaurant()["tables"] if t["id"] in o["table_ids"]) for o in opts), opts
    # party_size filter: pair t_1+t_2 capacity 6 >= 5, fine; singles <5 dropped
    r4 = expect(httpx.get(f"{base}/availability", params={
        "restaurant_id": "r_anker", "date": date, "party_size": 5}, timeout=TIMEOUT), 200)
    s4 = next(s for s in r4.json()["slots"] if s["starts_at_local"].endswith("18:00"))
    assert all(o["capacity"] >= 5 for o in s4["available_options"]), s4["available_options"]
    assert ["t_1", "t_2"] in [o["table_ids"] for o in s4["available_options"]], s4["available_options"]
    # occupied member removes the pair from options
    c = auth(base, ADA["email"], ADA["password"])
    expect(book(c, base, table_id="t_1", party_size=2, at="18:00"), 201)
    r5 = expect(httpx.get(f"{base}/availability", params={
        "restaurant_id": "r_anker", "date": date, "party_size": 2}, timeout=TIMEOUT), 200)
    s5 = next(s for s in r5.json()["slots"] if s["starts_at_local"].endswith("18:00"))
    assert s5["available_table_ids"] == ["t_2", "t_3"], s5["available_table_ids"]
    assert all("t_1" not in o["table_ids"] for o in s5["available_options"]), s5["available_options"]
    return "ordering + capacity + occupancy filtering"


def p_table_ids_request_response_rules(base):
    """Req 31-33, E1-E2: table_ids, table_id=set of one, both=422, response shape, errors."""
    reset(base, fixture(restaurants=[restaurant(
        tables=[{"id": "t_1", "label": "1", "capacity": 2},
                {"id": "t_2", "label": "2", "capacity": 4},
                {"id": "t_3", "label": "3", "capacity": 6}],
        combinable=[["t_1", "t_2"]])]))
    c = auth(base, ADA["email"], ADA["password"])
    # single via table_ids: response carries table_id and table_ids
    s = expect(book(c, base, table_ids=["t_3"], party_size=4, at="18:00"), 201).json()
    assert s["table_ids"] == ["t_3"] and s["table_id"] == "t_3", s
    # single via table_id: still works, response carries both
    s2 = expect(book(c, base, table_id="t_3", party_size=4, at="20:00"), 201).json()
    assert s2["table_ids"] == ["t_3"] and s2["table_id"] == "t_3", s2
    # both fields -> 422
    expect(book(c, base, table_ids=["t_2"], table_id="t_2", both=True), 422, "validation_failed")
    # pair over summed capacity -> party_exceeds_capacity
    expect(book(c, base, table_ids=["t_1", "t_2"], party_size=7), 422, "party_exceeds_capacity")
    # duplicate id -> validation_failed
    expect(book(c, base, table_ids=["t_1", "t_1"], party_size=2), 422, "validation_failed")
    # occupy t_1, then a pair containing t_1 overlapping -> table_unavailable
    expect(book(c, base, table_id="t_1", party_size=2, at="18:00"), 201)
    expect(book(c, base, table_ids=["t_1", "t_2"], party_size=4, at="18:00"), 409, "table_unavailable")
    # E13(a): an unknown member must be 404 not_found, resolved before the
    # pair-combinable check.
    expect(book(c, base, table_ids=["t_1", "t_nope"], party_size=2), 404, "not_found")
    # E13(c) before (d): duplicate member is 422 validation_failed even when the
    # pair is also not combinable.
    expect(book(c, base, table_ids=["t_1", "t_1"], party_size=2), 422, "validation_failed")
    # E13(b): three ids -> combination_not_allowed, not validation_failed.
    expect(book(c, base, table_ids=["t_1", "t_2", "t_3"], party_size=2), 422, "combination_not_allowed")
    return "table_ids matrix incl. E13 check order"


def p_combination_patch_and_cancel(base):
    """Req 34: PATCH accepts table_ids; cancelling frees every member."""
    reset(base, fixture(restaurants=[restaurant(
        combinable=[["t_1", "t_2"]])]))
    c = auth(base, ADA["email"], ADA["password"])
    b = expect(book(c, base, table_id="t_3", party_size=4, at="18:00"), 201).json()
    ref = b["reference"]
    # patch to the pair
    p = expect(c.patch(f"/reservations/{ref}", json={"table_ids": ["t_1", "t_2"], "party_size": 6}), 200).json()
    assert p["table_ids"] == ["t_1", "t_2"] and "table_id" not in p, p
    # cancel frees both
    expect(c.post(f"/reservations/{ref}/cancel"), 200)
    a = expect(httpx.get(f"{base}/availability", params={
        "restaurant_id": "r_anker", "date": booking_date(), "party_size": 6}, timeout=TIMEOUT), 200).json()
    slot = next(s for s in a["slots"] if s["starts_at_local"].endswith("18:00"))
    assert ["t_1", "t_2"] in [o["table_ids"] for o in slot["available_options"]], slot["available_options"]
    return "patch to set; cancel frees all members"


def p_seeded_sets(base):
    """Req 28, E1: seeded reservations carry table_id or table_ids; default confirmed."""
    date = booking_date()
    reset(base, fixture(
        restaurants=[restaurant(combinable=[["t_1", "t_2"]])],
        reservations=[seeded(date, reference="SEED01", table_id="t_2"),
                      seeded(date, reference="SEED02", table_ids=["t_1", "t_2"],
                             party_size=6, at="20:00")]))
    c = auth(base, ADA["email"], ADA["password"])
    rows = expect(c.get("/reservations"), 200).json()["reservations"]
    by_ref = {r["reference"]: r for r in rows}
    assert by_ref["SEED01"]["status"] == "confirmed"
    assert by_ref["SEED01"]["table_ids"] == ["t_2"], by_ref["SEED01"]
    assert by_ref["SEED02"]["table_ids"] == ["t_1", "t_2"], by_ref["SEED02"]
    # seeded set blocks both members
    a = expect(httpx.get(f"{base}/availability", params={
        "restaurant_id": "r_anker", "date": date, "party_size": 6}, timeout=TIMEOUT), 200).json()
    slot = next(s for s in a["slots"] if s["starts_at_local"].endswith("20:00"))
    assert all(o["table_ids"] != ["t_1", "t_2"] for o in slot["available_options"]), slot["available_options"]
    return "seeded table_ids & default status"


def p_moves_with_table_ids(base):
    """Req 39: moves accept table_ids; atomic; no overlapping result."""
    reset(base, fixture(restaurants=[restaurant(combinable=[["t_1", "t_2"]])]))
    c = auth(base, ADA["email"], ADA["password"])
    b1 = expect(book(c, base, table_id="t_3", party_size=4, at="18:00"), 201).json()
    b2 = expect(book(c, base, table_id="t_3", party_size=4, at="20:00"), 201).json()
    # move b1 onto the pair
    body = {"moves": [{"reference": b1["reference"], "table_ids": ["t_1", "t_2"], "party_size": 6},
                      {"reference": b2["reference"]}]}
    k = new_key()
    r = expect(c.post("/reservation-moves", json=body, key=k), 201).json()
    moved = {x["reference"]: x for x in r["reservations"]}
    assert moved[b1["reference"]]["table_ids"] == ["t_1", "t_2"], moved[b1["reference"]]
    # replay returns original
    r2 = expect(c.post("/reservation-moves", json=body, key=k), 200)
    assert r2.json() == r, "moves replay must equal first response"
    # now a move that would overlap the pair must 409 and change nothing.
    # b3 sits on t_3 at 21:30 (b2 is 20:00-21:30, so no overlap with b3).
    b3 = expect(book(c, base, table_id="t_3", party_size=4, at="21:30"), 201).json()
    bad = {"moves": [{"reference": b3["reference"], "table_ids": ["t_1", "t_2"], "party_size": 6,
                      "starts_at_local": local(booking_date(), "18:00")}]}
    j = new_key()
    expect(c.post("/reservation-moves", json=bad, key=j), 409, "table_unavailable")
    rows = {x["reference"]: x for x in c.get("/reservations").json()["reservations"]}
    assert rows[b3["reference"]]["table_ids"] == ["t_3"], rows[b3["reference"]]
    assert rows[b3["reference"]]["starts_at_local"].endswith("21:30"), rows[b3["reference"]]
    return "moves with table_ids + replay + atomic conflict"


def p_concurrent_pair_bookings(base):
    """Req 41: concurrent bookings of the same pair -> exactly one 201."""
    reset(base, fixture(restaurants=[restaurant(combinable=[["t_1", "t_2"]])]))
    users = [dict(ADA, id=f"u_{i}", email=f"r{i}@example.com") for i in range(10)]
    reset(base, fixture(users=users, restaurants=[restaurant(combinable=[["t_1", "t_2"]])]))
    date = booking_date()
    tokens = [auth(base, u["email"], u["password"]).token for u in users]
    body = {"restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
            "starts_at_local": local(date, "19:00"), "party_size": 6}

    def one(i):
        with httpx.Client(timeout=TIMEOUT) as hc:
            return hc.post(f"{base}/reservations", json=body, headers={
                "Authorization": f"Bearer {tokens[i]}", "Idempotency-Key": new_key()}).status_code

    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        codes = list(ex.map(one, range(10)))
    assert codes.count(201) == 1 and codes.count(409) == 9, f"codes={sorted(codes)}"
    return "concurrent pair bookings: exactly one winner"


def p_stage1_error_matrix_still_holds(base):
    """Stage-1 reqs still binding: a representative slice."""
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    expect(book(c, base, at="19:15"), 422, "not_on_slot_grid")
    expect(book(c, base, at="17:00"), 422, "outside_opening_hours")
    expect(book(c, base, table_id="t_1", party_size=4), 422, "party_exceeds_capacity")
    expect(book(c, base, rest="r_nope"), 404, "not_found")
    expect(book(c, base, table_id="t_nope"), 404, "not_found")
    expect(book(c, base, starts_at_local="not-a-time"), 422, "validation_failed")
    expect(book(c, base, party_size="4"), 422, "validation_failed")
    return "stage-1 error matrix intact"


def p_upgrade_continuity(base, previous_base):
    """Req 21-25, E8: stage-1 export imports; token/reference/retry continuity."""
    if not previous_base:
        raise Skip("no --previous-base-url (build stage-1/ and pass its URL)")
    # 1. seed stage-1, sign in, lose a booking response, export, import into stage-2
    reset(previous_base, fixture())
    pc = auth(previous_base, ADA["email"], ADA["password"])
    key = new_key()
    body = {"restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": local(booking_date(), "19:00"), "party_size": 4}
    first = pc.post("/reservations", json=body, key=key)
    assert first.status_code == 201, f"stage-1 create -> {first.status_code} {first.text[:200]}"
    expected_ref = first.json()["reference"]
    snapshot = expect(pc.get("/_test/export"), 200).json()
    # 2. import into stage-2
    imp = httpx.post(f"{base}/_test/import", json=snapshot, timeout=RESET_TIMEOUT)
    assert imp.status_code == 204, f"stage-2 import -> {imp.status_code} {imp.text[:200]}"
    # 3. the stage-1 token still works
    c2 = C(base, token=pc.token)
    rows = expect(c2.get("/reservations"), 200).json()["reservations"]
    assert any(r["reference"] == expected_ref for r in rows), rows
    # 4. retained reference works through lookup
    one = expect(c2.get(f"/reservations/{expected_ref}"), 200).json()
    assert one["reference"] == expected_ref
    # 5. retry with same body+key recovers the original confirmation (replay 200)
    retry = c2.post("/reservations", json=body, key=key)
    assert retry.status_code in (200, 201), f"retry -> {retry.status_code} {retry.text[:200]}"
    assert retry.json()["reference"] == expected_ref, retry.json()
    # 6. stage-2 export re-imports unchanged
    snap2 = expect(c2.get("/_test/export"), 200).json()
    assert httpx.post(f"{base}/_test/import", json=snap2, timeout=RESET_TIMEOUT).status_code == 204
    rows2 = expect(C(base, token=pc.token).get("/reservations"), 200).json()["reservations"]
    assert any(r["reference"] == expected_ref for r in rows2)
    return "stage-1 export -> stage-2 import; token/ref/retry continuity"


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--previous-base-url", default=None)
    args = ap.parse_args(argv)
    base = args.base_url.rstrip("/")

    check("S1 health + stage-1 fundamentals", lambda: p_health_and_stage1_regressions(base))
    check("S2 combinable pairs only / non-transitive", lambda: p_combinable_and_nontransitive(base))
    check("S3 available_options order/shape", lambda: p_available_options_order_and_shape(base))
    check("S4 table_ids request/response/errors", lambda: p_table_ids_request_response_rules(base))
    check("S5 combination patch + cancel frees", lambda: p_combination_patch_and_cancel(base))
    check("S6 seeded table_ids & status", lambda: p_seeded_sets(base))
    check("S7 moves with table_ids + replay", lambda: p_moves_with_table_ids(base))
    check("S8 concurrent pair bookings", lambda: p_concurrent_pair_bookings(base))
    check("S9 stage-1 error matrix intact", lambda: p_stage1_error_matrix_still_holds(base))
    check("S10 upgrade continuity", lambda: p_upgrade_continuity(base, args.previous_base_url))

    width = max(len(n) for _, n, _ in RESULTS)
    failed = 0
    for status, name, detail in RESULTS:
        print(f"{status} {name.ljust(width)} :: {detail}")
        if status == "FAIL":
            failed += 1
    print(f"\n{sum(1 for s, _, _ in RESULTS if s == 'PASS')}/{len(RESULTS)} passed"
          f" ({failed} failed)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
