#!/usr/bin/env python3
"""Independent stage-1 probes for tablekeeper (Reviewer).

Runs against a live service base URL. Written from plans/stage-1/ledger.md and the
specification, not from the supplied checks.

    probe_stage1.py --base-url http://127.0.0.1:PORT

Prints one line per probe: PASS/FAIL name (detail). Exit 1 if any probe fails.
Uses httpx (present in the kickoff venv); every request carries a 5s timeout so the
whole run cannot hang.
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

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, fn):
    try:
        detail = fn()
        RESULTS.append((True, name, detail or ""))
    except AssertionError as exc:
        RESULTS.append((False, name, str(exc)))
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((False, name, f"{type(exc).__name__}: {exc}"))


# ---------------------------------------------------------------- fixtures
def all_week(opens="18:00", closes="23:00"):
    return [{"weekday": d, "opens": opens, "closes": closes} for d in WEEKDAYS]


def restaurant(rid="r_anker", *, name="Zum Anker", timezone="Europe/Berlin",
               slot_minutes=30, reservation_duration_minutes=90,
               cancellation_cutoff_minutes=120, opening_hours=None, tables=None):
    return {
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


def fixture(*, users=None, restaurants=None, reservations=None):
    return {"users": [ADA, BOB] if users is None else users,
            "restaurants": [restaurant()] if restaurants is None else restaurants,
            "reservations": reservations or []}


def booking_date(timezone="Europe/Berlin", lead=7):
    return (dt.datetime.now(ZoneInfo(timezone)).date() + dt.timedelta(days=lead)).isoformat()


def local(date, hhmm="19:00"):
    return f"{date}T{hhmm}"


def seeded(date, *, reference="SEED01", table_id="t_2", at="19:00",
           user_id="u_ada", party_size=4, rest="r_anker"):
    return {"id": "res_seed", "reference": reference, "user_id": user_id,
            "restaurant_id": rest, "table_id": table_id,
            "starts_at_local": local(date, at), "party_size": party_size}


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
        body = resp.json()
        return body["error"]["code"]
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


def book(c, base, *, table_id="t_2", date=None, at="19:00", party_size=4,
         rest="r_anker", key=None, starts_at_local=None, **extra):
    date = date or booking_date()
    body = {"restaurant_id": rest, "table_id": table_id,
            "starts_at_local": starts_at_local or local(date, at), "party_size": party_size}
    body.update(extra)
    return c.post("/reservations", json=body, key=key or new_key())


# ---------------------------------------------------------------- probes
def p_delivery_health(base):
    reset(base)
    r = httpx.get(f"{base}/health", timeout=TIMEOUT)
    assert r.status_code == 200 and r.json() == {"status": "ok"}, r.text[:200]
    ra = httpx.post(f"{base}/_test/reset", json=fixture(), timeout=RESET_TIMEOUT)
    assert ra.status_code == 204
    return "health ok; reset 204 unauthenticated"


def p_concurrent_availability(base):
    reset(base)
    date = booking_date()
    def one(i):
        with httpx.Client(timeout=TIMEOUT) as hc:
            return hc.get(f"{base}/availability", params={
                "restaurant_id": "r_anker", "date": date, "party_size": 2}).status_code
    with cf.ThreadPoolExecutor(max_workers=50) as ex:
        codes = list(ex.map(one, range(50)))
    assert all(c == 200 for c in codes), f"codes={set(codes)}"
    return "50 concurrent availability -> all 200"


def p_reset_replaces_state(base):
    reset(base, fixture(restaurants=[restaurant("r_one", name="One")]))
    ids = httpx.get(f"{base}/restaurants").json()["restaurants"]
    assert [r["id"] for r in ids] == ["r_one"], ids
    reset(base, fixture(restaurants=[restaurant("r_two", name="Two")]))
    ids = httpx.get(f"{base}/restaurants").json()["restaurants"]
    assert [r["id"] for r in ids] == ["r_two"], ids
    for _ in range(3):
        reset(base)
    return "reset replaces all state, repeatable"


def p_error_envelope_and_json(base):
    reset(base)
    r = httpx.get(f"{base}/availability", params={"restaurant_id": "r_nope",
                  "date": booking_date(), "party_size": 2}, timeout=TIMEOUT)
    assert r.status_code == 404
    b = r.json()
    assert set(b.keys()) == {"error"} and isinstance(b["error"].get("code"), str) \
        and isinstance(b["error"].get("message"), str), b
    ct = r.headers.get("content-type", "")
    assert "application/json" in ct and "utf-8" in ct.lower(), ct
    return "error envelope + content-type"


def p_rfc3339_offsets(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    b = book(c, base).json()
    for f in ("starts_at", "ends_at", "created_at"):
        assert re.search(r"([+-]\d{2}:\d{2}|Z)$", b[f]), f"{f}={b[f]!r}"
    return "timestamps carry offsets"


def p_unknown_fields_ignored(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    r = book(c, base, souvenir="yes", nested={"x": 1})
    expect(r, 201)
    date = booking_date()
    r2 = c.get("/availability", params={"restaurant_id": "r_anker", "date": date,
             "party_size": 2, "sort": "nope", "page": "3"})
    expect(r2, 200)
    return "unknown body/query fields ignored"


def p_id_length_limits(base):
    reset(base)
    r = reset(base, {"users": [{**ADA, "id": "u" * 65}],
                     "restaurants": [restaurant()], "reservations": []}, raw=True)
    expect(r, 422, "validation_failed")
    ok = "u" * 64
    reset(base, {"users": [{**ADA, "id": ok}], "restaurants": [restaurant()], "reservations": []})
    reset(base)
    for bad in ("x", "lower01", "TOO-LONG-WITH-DASH", "ABCDEFGHIJKLM"):
        r = reset(base, fixture(reservations=[seeded(booking_date(), reference=bad)]), raw=True)
        expect(r, 422, "validation_failed")
    return "64-char id ok; 65 rejected; bad refs rejected"


def p_malformed_vs_validation(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    r = c.post("/reservations", json={
        "restaurant_id": 17, "table_id": "t_2",
        "starts_at_local": local(booking_date()), "party_size": 4}, key=new_key())
    expect(r, 400, "malformed_request")
    r = c.post("/reservations", content="{not json", key=new_key())
    expect(r, 400, "malformed_request")
    r = c.post("/reservations", json={
        "restaurant_id": "r_anker", "table_id": "t_2",
        "starts_at_local": 17, "party_size": 4}, key=new_key())
    expect(r, 400, "malformed_request")
    # correct type, bad value -> 422
    r = book(c, base, party_size=-3)
    expect(r, 422, "validation_failed")
    # idempotency key length
    r = book(c, base, key="")
    expect(r, 400, "missing_idempotency_key")
    r = book(c, base, key="k" * 256)
    expect(r, 422, "validation_failed")
    return "malformed vs validation distinguished; key length"


def p_query_integer_rule(base):
    reset(base)
    date = booking_date()
    for bad in ("1e9", "4.0", "+4", " 4", "abc"):
        r = httpx.get(f"{base}/availability", params={
            "restaurant_id": "r_anker", "date": date, "party_size": bad}, timeout=TIMEOUT)
        assert r.status_code == 422, f"{bad!r} -> {r.status_code}"
        assert code_of(r) == "validation_failed", f"{bad!r} -> {code_of(r)}"
    return "non-decimal query integers -> 422"


def p_no_5xx_bad_inputs(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    for body in ({"users": None}, {"restaurants": "x"}, {}, {"a": 1},
                 {"moves": "no"}, [], "string"):
        try:
            httpx.post(f"{base}/_test/reset", json=body, timeout=RESET_TIMEOUT)
        except Exception:
            pass
        final = httpx.get(f"{base}/health", timeout=TIMEOUT)
        assert final.status_code < 500, f"health 5xx after reset {body!r}: {final.status_code}"
    c2 = auth(base, ADA["email"], ADA["password"])
    r = book(c2, base, party_size={"x": 1})
    assert r.status_code < 500, r.status_code
    reset(base)
    return "no 5xx on hostile inputs"


def p_auth_signup_login(base):
    reset(base)
    r = httpx.post(f"{base}/auth/signup", json={
        "email": "new@example.com", "password": "correct horse", "display_name": "New"},
        timeout=TIMEOUT)
    expect(r, 201)
    b = r.json()
    assert set(("user_id", "display_name", "token")) <= set(b), b
    assert b["display_name"] == "New" and b["token"]
    c = C(base, b["token"])
    expect(c.get("/reservations"), 200)
    r = httpx.post(f"{base}/auth/signup", json={
        "email": "new@example.com", "password": "correct horse", "display_name": "X"},
        timeout=TIMEOUT)
    expect(r, 409, "email_taken")
    r = httpx.post(f"{base}/auth/signup", json={
        "email": "s@example.com", "password": "1234567", "display_name": "X"}, timeout=TIMEOUT)
    expect(r, 422, "validation_failed")
    r = httpx.post(f"{base}/auth/signup", json={
        "email": "not-an-email", "password": "correct horse", "display_name": "X"},
        timeout=TIMEOUT)
    expect(r, 422, "validation_failed")
    r = httpx.post(f"{base}/auth/login", json={"email": ADA["email"], "password": "wrong"},
                   timeout=TIMEOUT)
    expect(r, 401, "unauthenticated")
    r = httpx.post(f"{base}/auth/login", json={"email": "nobody@example.com",
                   "password": "correct horse"}, timeout=TIMEOUT)
    expect(r, 401, "unauthenticated")
    return "signup/login matrix"


def p_auth_protected_public(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    anon = C(base)
    for path in ("/reservations", "/reservations/ABC123"):
        expect(anon.get(path), 401, "unauthenticated")
        expect(C(base, "not-a-token").get(path), 401, "unauthenticated")
    for path in ("/restaurants", "/availability"):
        params = {"restaurant_id": "r_anker", "date": booking_date(), "party_size": 2}
        r = anon.get(path, params=params if path == "/availability" else None)
        expect(r, 200)
    expect(anon.get("/restaurants/r_anker"), 200)
    expect(C(base, "").get("/reservations"), 401, "unauthenticated")
    return "auth gating"


def p_other_user_reservation_404(base):
    reset(base)
    ada = auth(base, ADA["email"], ADA["password"])
    bob = auth(base, BOB["email"], BOB["password"])
    ref = book(ada, base).json()["reference"]
    expect(bob.get(f"/reservations/{ref}"), 404, "not_found")
    expect(bob.post(f"/reservations/{ref}/cancel"), 404, "not_found")
    expect(bob.patch(f"/reservations/{ref}", json={"party_size": 2}), 404, "not_found")
    return "foreign reservation -> 404 not 403"


def p_multiple_tokens(base):
    reset(base)
    t1 = auth(base, ADA["email"], ADA["password"]).token
    t2 = auth(base, ADA["email"], ADA["password"]).token
    expect(C(base, t1).get("/reservations"), 200)
    expect(C(base, t2).get("/reservations"), 200)
    assert t1 and t2
    return f"two tokens usable (distinct={t1 != t2})"


def p_passwords_hashed(base):
    reset(base)
    exp = httpx.get(f"{base}/_test/export", timeout=TIMEOUT)
    expect(exp, 200)
    blob = json.dumps(exp.json())
    assert "correct horse" not in blob, "plaintext password present in export"
    return "no plaintext password in export"


def p_fixture_seeded_and_closed(base):
    date = booking_date()
    reset(base, fixture(reservations=[seeded(date)]))
    slots = httpx.get(f"{base}/availability", params={
        "restaurant_id": "r_anker", "date": date, "party_size": 4}).json()["slots"]
    at19 = next(s for s in slots if s["starts_at_local"].endswith("19:00"))
    assert "t_2" not in at19["available_table_ids"], "seeded booking did not hold table"
    past = (dt.date.fromisoformat(date) - dt.timedelta(days=30)).isoformat()
    reset(base, fixture(reservations=[seeded(past)]))
    ada = auth(base, ADA["email"], ADA["password"])
    expect(ada.get("/reservations/SEED01"), 200)
    closed = [h for h in all_week() if h["weekday"] != WEEKDAYS[dt.date.fromisoformat(date).weekday()]]
    reset(base, fixture(restaurants=[restaurant(opening_hours=closed)]))
    slots = httpx.get(f"{base}/availability", params={
        "restaurant_id": "r_anker", "date": date, "party_size": 2}).json()["slots"]
    assert slots == [], slots
    return "seeded booking occupies; past accepted; closed day empty"


def p_idem_missing_empty(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    body = {"restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": local(booking_date()), "party_size": 4}
    expect(c.post("/reservations", json=body), 400, "missing_idempotency_key")
    expect(c.post("/reservations", json=body, key=""), 400, "missing_idempotency_key")
    return "missing/empty key -> 400"


def p_idem_scoped_per_user(base):
    reset(base)
    ada = auth(base, ADA["email"], ADA["password"])
    bob = auth(base, BOB["email"], BOB["password"])
    date = booking_date()
    k = "shared-key"
    expect(book(ada, base, table_id="t_2", key=k), 201)
    expect(book(bob, base, table_id="t_3", key=k), 201)
    return "same key independent per user"


def p_idem_body_then_path(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    date = booking_date()
    k = new_key()
    b1 = {"restaurant_id": "r_anker", "table_id": "t_2",
          "starts_at_local": local(date, "19:00"), "party_size": 4}
    first = expect(c.post("/reservations", json=b1, key=k), 201).json()
    # same JSON value, different key order -> replay
    b2 = {"party_size": 4, "starts_at_local": local(date, "19:00"),
          "table_id": "t_2", "restaurant_id": "r_anker"}
    rep = expect(c.post("/reservations", json=b2, key=k), 200).json()
    assert rep == first, "key order changed the replay"
    # different body
    expect(c.post("/reservations", json={**b1, "party_size": 3}, key=k),
           409, "idempotency_key_reuse")
    # same key different path is a different request
    mv = {"moves": [{"reference": first["reference"], "table_id": "t_3"}]}
    r = c.post("/reservation-moves", json=mv, key=k)
    assert r.status_code in (200, 201), f"same key different path -> {r.status_code} {r.text[:200]}"
    return "replay by JSON value; reuse 409; other path independent"


def p_idem_replay_after_change(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    date = booking_date()
    k = new_key()
    body = {"restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": local(date, "19:00"), "party_size": 4}
    first = expect(c.post("/reservations", json=body, key=k), 201).json()
    expect(c.post(f"/reservations/{first['reference']}/cancel"), 200)
    rep = expect(c.post("/reservations", json=body, key=k), 200).json()
    assert rep == first and rep["status"] == "confirmed", rep
    return "replay after cancel returns original, no state change"


def p_idem_failed_key_reusable(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    k = new_key()
    r = book(c, base, table_id="t_nope", key=k)
    expect(r, 404, "not_found")
    r = book(c, base, table_id="t_2", key=k)
    expect(r, 201)
    return "key after 4xx failure reusable"


def p_idem_reuse_before_validation(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    date = booking_date()
    k = new_key()
    good = {"restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": local(date, "19:00"), "party_size": 4}
    expect(c.post("/reservations", json=good, key=k), 201)
    bad = {"restaurant_id": "r_anker", "table_id": "t_2",
           "starts_at_local": local(date, "19:00"), "party_size": 999999}
    expect(c.post("/reservations", json=bad, key=k), 409, "idempotency_key_reuse")
    return "used key with different invalid body -> 409 reuse"


def p_idem_concurrent(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    date = booking_date()
    k = new_key()
    body = {"restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": local(date, "19:00"), "party_size": 4}
    def one(i):
        with httpx.Client(timeout=TIMEOUT) as hc:
            r = hc.post(f"{base}/reservations", json=body, headers={
                "Authorization": f"Bearer {c.token}", "Idempotency-Key": k})
            return r.status_code, r.text
    with cf.ThreadPoolExecutor(max_workers=20) as ex:
        out = list(ex.map(one, range(20)))
    codes = [s for s, _ in out]
    assert codes.count(201) == 1, f"201s={codes.count(201)} codes={set(codes)}"
    assert codes.count(200) == 19, f"200s={codes.count(200)} codes={set(codes)}"
    bodies = {t for s, t in out if s == 200}
    assert len(bodies) == 1, "replay bodies differ"
    lst = c.get("/reservations").json()["reservations"]
    assert len(lst) == 1, f"effect happened {len(lst)} times"
    return "20 concurrent same key -> one 201/one effect"


def p_restaurants_public(base):
    reset(base)
    anon = C(base)
    r = expect(anon.get("/restaurants"), 200).json()
    assert r["restaurants"][0]["id"] == "r_anker"
    assert set(("id", "name", "timezone")) <= set(r["restaurants"][0])
    d = expect(anon.get("/restaurants/r_anker"), 200).json()
    for f in ("slot_minutes", "reservation_duration_minutes",
              "cancellation_cutoff_minutes", "opening_hours", "tables"):
        assert f in d, f
    expect(anon.get("/restaurants/r_nope"), 404, "not_found")
    return "restaurant list/detail public + 404"


def p_availability_shape_grid(base):
    reset(base)
    anon = C(base)
    date = booking_date()
    r = expect(anon.get("/availability", params={"restaurant_id": "r_anker",
                  "date": date, "party_size": 2}), 200).json()
    assert r["restaurant_id"] == "r_anker" and r["date"] == date
    assert r["timezone"] == "Europe/Berlin"
    times = [s["starts_at_local"].split("T")[1] for s in r["slots"]]
    expected = []
    t = 18 * 60
    while t + 90 <= 23 * 60:
        expected.append(f"{t // 60:02d}:{t % 60:02d}")
        t += 30
    assert times == expected, f"{times} != {expected}"
    for s in r["slots"]:
        assert s["starts_at_local"] == f"{date}T{s['starts_at_local'].split('T')[1]}"
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$", s["starts_at_local"]), s
    slots4 = expect(anon.get("/availability", params={"restaurant_id": "r_anker",
                     "date": date, "party_size": 4}), 200).json()["slots"]
    assert slots4[0]["available_table_ids"] == ["t_2", "t_3"], slots4[0]
    for drop in ("restaurant_id", "date", "party_size"):
        p = {"restaurant_id": "r_anker", "date": date, "party_size": 2}
        p.pop(drop)
        expect(anon.get("/availability", params=p), 422, "validation_failed")
    for bad in ("2026-02-30", "not-a-date", "24-09-2026"):
        expect(anon.get("/availability", params={"restaurant_id": "r_anker",
               "date": bad, "party_size": 2}), 422, "validation_failed")
    expect(anon.get("/availability", params={"restaurant_id": "r_nope",
           "date": date, "party_size": 2}), 404, "not_found")
    return "availability grid/capacity/params"


def p_create_shape_and_ref(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    b = expect(book(c, base, table_id="t_2", at="19:00", party_size=4), 201).json()
    assert b["status"] == "confirmed" and b["party_size"] == 4
    assert b["starts_at_local"] == local(booking_date(), "19:00")
    assert REF.match(b["reference"]), b["reference"]
    assert b["starts_at"].startswith(f"{booking_date()}T19:00:00")
    assert b["ends_at"].startswith(f"{booking_date()}T20:30:00")
    refs = {b["reference"]}
    for t in ("t_1", "t_3"):
        refs.add(expect(book(c, base, table_id=t, party_size=2), 201).json()["reference"])
    assert len(refs) == 3
    return "create shape, reference format/uniqueness"


def p_overlap_and_concurrency(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    expect(book(c, base, table_id="t_2", at="19:00"), 201)
    expect(book(c, base, table_id="t_2", at="20:00"), 409, "table_unavailable")
    expect(book(c, base, table_id="t_2", at="19:45"), 409, "table_unavailable")
    expect(book(c, base, table_id="t_2", at="20:30"), 201)
    expect(book(c, base, table_id="t_3", at="19:00"), 201)
    reset(base)
    users = [dict(ADA, id=f"u_{i}", email=f"r{i}@example.com") for i in range(10)]
    reset(base, fixture(users=users))
    date = booking_date()
    tokens = [auth(base, u["email"], u["password"]).token for u in users]
    body = {"restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": local(date, "19:00"), "party_size": 4}
    def one(i):
        with httpx.Client(timeout=TIMEOUT) as hc:
            r = hc.post(f"{base}/reservations", json=body, headers={
                "Authorization": f"Bearer {tokens[i]}", "Idempotency-Key": new_key()})
            return r.status_code
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        codes = list(ex.map(one, range(10)))
    assert codes.count(201) == 1 and codes.count(409) == 9, f"codes={sorted(codes)}"
    return "half-open overlap + concurrency exactly one winner"


def p_create_rule_codes(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    expect(book(c, base, at="19:15"), 422, "not_on_slot_grid")
    expect(book(c, base, at="18:01"), 422, "not_on_slot_grid")
    expect(book(c, base, at="17:00"), 422, "outside_opening_hours")
    expect(book(c, base, at="23:30"), 422, "outside_opening_hours")
    expect(book(c, base, at="22:00"), 422, "outside_opening_hours")
    expect(book(c, base, table_id="t_1", party_size=4), 422, "party_exceeds_capacity")
    for p in (0, -1, "4", 1.5, True):
        r = book(c, base, party_size=p)
        expect(r, 422, "validation_failed")
    expect(book(c, base, rest="r_nope"), 404, "not_found")
    expect(book(c, base, table_id="t_nope"), 404, "not_found")
    other = restaurant("r_other", tables=[{"id": "t_x", "label": "X", "capacity": 4}])
    reset(base, fixture(restaurants=[restaurant(), other]))
    c = auth(base, ADA["email"], ADA["password"])
    expect(book(c, base, table_id="t_x"), 404, "not_found")
    for v in ("2026-09-24T19:00:00+02:00", "2026-09-24T19:00Z",
              "2026-09-24T19:00:00", "not-a-time"):
        expect(book(c, base, starts_at_local=v), 422, "validation_failed")
    return "create rule error matrix"


def p_reservation_reads_and_cancel(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    for at in ("18:00", "21:00", "19:30"):
        expect(book(c, base, table_id="t_2", at=at), 201)
    starts = [r["starts_at"] for r in c.get("/reservations").json()["reservations"]]
    assert starts == sorted(starts, reverse=True), starts
    ref = book(c, base, table_id="t_3", at="18:00").json()["reference"]
    expect(c.post(f"/reservations/{ref}/cancel"), 200).json()
    statuses = {r["status"] for r in c.get("/reservations").json()["reservations"]}
    assert "cancelled" in statuses
    expect(c.post(f"/reservations/{ref}/cancel"), 200)
    expect(c.get("/reservations/ZZZZZZ"), 404, "not_found")
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    assert c.get("/reservations").json() == {"reservations": []}
    return "list desc + cancelled; double cancel; empty list"


def p_cancel_cutoff_and_frees(base):
    reset(base)
    cutoff = restaurant(cancellation_cutoff_minutes=60 * 24 * 3650)
    reset(base, fixture(restaurants=[cutoff]))
    c = auth(base, ADA["email"], ADA["password"])
    date = booking_date()
    ref = expect(book(c, base, table_id="t_2", date=date, at="19:00"), 201).json()["reference"]
    expect(c.post(f"/reservations/{ref}/cancel"), 409, "cutoff_passed")
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    ref = expect(book(c, base, table_id="t_2", date=date, at="19:00"), 201).json()["reference"]
    expect(c.post(f"/reservations/{ref}/cancel"), 200)
    slots = httpx.get(f"{base}/availability", params={
        "restaurant_id": "r_anker", "date": date, "party_size": 4}).json()["slots"]
    at19 = next(s for s in slots if s["starts_at_local"].endswith("19:00"))
    assert "t_2" in at19["available_table_ids"]
    return "cancel frees; cutoff 409"


def p_patch_semantics(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    created = book(c, base, table_id="t_2", at="19:00", party_size=4).json()
    ref = created["reference"]
    b = expect(c.patch(f"/reservations/{ref}", json={
        "starts_at_local": local(booking_date(), "20:30")}), 200).json()
    assert b["reference"] == ref and b["reservation_id"] == created["reservation_id"]
    expect(c.patch(f"/reservations/{ref}", json={"party_size": 3}), 200)
    ref2 = book(c, base, table_id="t_3", at="18:00").json()["reference"]
    expect(c.patch(f"/reservations/{ref2}", json={"table_id": "t_2"}), 409, "table_unavailable")
    expect(c.patch(f"/reservations/{ref}", json={"starts_at_local": local(booking_date(), "19:15")}),
           422, "not_on_slot_grid")
    expect(c.patch(f"/reservations/{ref}", json={"table_id": "t_1"}), 422, "party_exceeds_capacity")
    expect(c.patch(f"/reservations/{ref}", json={"table_id": "t_nope"}), 404, "not_found")
    # failed patch leaves original unchanged
    before = c.get(f"/reservations/{ref}").json()
    expect(c.patch(f"/reservations/{ref}", json={"starts_at_local": local(booking_date(), "17:00")}),
           422, "outside_opening_hours")
    after = c.get(f"/reservations/{ref}").json()
    assert before == after, "failed patch mutated the booking"
    # cancelled -> 409
    expect(c.post(f"/reservations/{ref}/cancel"), 200)
    expect(c.patch(f"/reservations/{ref}", json={"party_size": 2}), 409, "reservation_cancelled")
    # patch cutoff
    reset(base, fixture(restaurants=[restaurant(cancellation_cutoff_minutes=60 * 24 * 3650)]))
    c = auth(base, ADA["email"], ADA["password"])
    r = book(c, base, table_id="t_2").json()["reference"]
    expect(c.patch(f"/reservations/{r}", json={"table_id": "t_3"}), 409, "cutoff_passed")
    return "patch subset/atomic/failure/cancelled/cutoff"


def p_rejected_creates_nothing(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    n0 = len(c.get("/reservations").json()["reservations"])
    expect(book(c, base, at="19:15"), 422, "not_on_slot_grid")
    expect(book(c, base, table_id="t_nope"), 404, "not_found")
    expect(book(c, base, party_size=0), 422, "validation_failed")
    n1 = len(c.get("/reservations").json()["reservations"])
    assert n0 == n1 == 0, (n0, n1)
    return "rejected requests create no booking"


# ---------------------------------------------------------------- DST
def _zoned(base):
    berlin = restaurant("r_berlin", timezone="Europe/Berlin",
                        opening_hours=all_week("00:00", "23:30"))
    ny = restaurant("r_ny", timezone="America/New_York",
                    opening_hours=all_week("00:00", "23:30"))
    reset(base, fixture(restaurants=[berlin, ny]))
    return auth(base, ADA["email"], ADA["password"])


def _slots(base, rid, date):
    r = expect(httpx.get(f"{base}/availability", params={
        "restaurant_id": rid, "date": date, "party_size": 4}, timeout=TIMEOUT), 200)
    return [(s["starts_at_local"].split("T")[1], s["starts_at"]) for s in r.json()["slots"]]


def p_dst_berlin_spring(base):
    c = _zoned(base)
    times = [t for t, _ in _slots(base, "r_berlin", "2026-03-29")]
    for t in ("02:00", "02:30"):
        assert t not in times, f"skipped local time {t} offered"
    r = c.post("/reservations", json={"restaurant_id": "r_berlin", "table_id": "t_2",
              "starts_at_local": "2026-03-29T02:30", "party_size": 4}, key=new_key())
    expect(r, 422, "invalid_local_time")
    return "Berlin spring gap absent + invalid_local_time"


def p_dst_berlin_fall(base):
    c = _zoned(base)
    times = [t for t, _ in _slots(base, "r_berlin", "2026-10-25")]
    assert times.count("02:00") == 1, f"02:00 count={times.count('02:00')}"
    assert times.count("02:30") == 1
    b = expect(c.post("/reservations", json={"restaurant_id": "r_berlin", "table_id": "t_2",
             "starts_at_local": "2026-10-25T02:00", "party_size": 4}, key=new_key()), 201).json()
    assert b["starts_at"].endswith("+02:00"), b["starts_at"]
    return "Berlin fall repeated hour once; first occurrence +02:00"


def p_dst_berlin_duration_absolute(base):
    c = _zoned(base)
    b = expect(c.post("/reservations", json={"restaurant_id": "r_berlin", "table_id": "t_2",
             "starts_at_local": "2026-10-25T01:30", "party_size": 4}, key=new_key()), 201).json()
    assert "T02:00" in b["ends_at"], b["ends_at"]
    start = dt.datetime.fromisoformat(b["starts_at"])
    end = dt.datetime.fromisoformat(b["ends_at"])
    assert (end - start) == dt.timedelta(minutes=90), (b["starts_at"], b["ends_at"])
    return "90-min booking 01:30 fall night ends local 02:00, 90 real min"


def p_dst_new_york(base):
    c = _zoned(base)
    spring = [t for t, _ in _slots(base, "r_ny", "2026-03-08")]
    for t in ("02:00", "02:30"):
        assert t not in spring, f"NY gap {t} offered"
    r = c.post("/reservations", json={"restaurant_id": "r_ny", "table_id": "t_2",
              "starts_at_local": "2026-03-08T02:30", "party_size": 4}, key=new_key())
    expect(r, 422, "invalid_local_time")
    fall = _slots(base, "r_ny", "2026-11-01")
    times = [t for t, _ in fall]
    assert times.count("01:00") == 1 and times.count("01:30") == 1, times
    b = expect(c.post("/reservations", json={"restaurant_id": "r_ny", "table_id": "t_2",
             "starts_at_local": "2026-11-01T01:00", "party_size": 4}, key=new_key()), 201).json()
    assert b["starts_at"].endswith("-04:00"), b["starts_at"]
    return "NY spring gap + fall first occurrence -04:00"


def p_dst_same_instant(base):
    c = _zoned(base)
    a = expect(c.post("/reservations", json={"restaurant_id": "r_berlin", "table_id": "t_2",
             "starts_at_local": "2026-12-01T18:00", "party_size": 4}, key=new_key()), 201).json()
    b = expect(c.post("/reservations", json={"restaurant_id": "r_ny", "table_id": "t_2",
             "starts_at_local": "2026-12-01T12:00", "party_size": 4}, key=new_key()), 201).json()
    assert dt.datetime.fromisoformat(a["starts_at"]) == dt.datetime.fromisoformat(b["starts_at"]), \
        (a["starts_at"], b["starts_at"])
    return "Berlin 18:00 CET == NY 12:00 EST"


def p_dst_offsets_both_sides(base):
    c = _zoned(base)
    before = expect(c.post("/reservations", json={"restaurant_id": "r_berlin", "table_id": "t_2",
             "starts_at_local": "2026-10-25T01:00", "party_size": 4}, key=new_key()), 201).json()
    after = expect(c.post("/reservations", json={"restaurant_id": "r_berlin", "table_id": "t_3",
             "starts_at_local": "2026-10-25T12:00", "party_size": 4}, key=new_key()), 201).json()
    assert before["starts_at"].endswith("+02:00"), before["starts_at"]
    assert after["starts_at"].endswith("+01:00"), after["starts_at"]
    return "offsets change across Berlin fall"


# ---------------------------------------------------------------- export/import
def p_export_shape_and_atomic(base):
    c = auth(base, ADA["email"], ADA["password"])
    body = {"restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": local(booking_date(), "19:00"), "party_size": 4}
    receipt = expect(c.post("/reservations", json=body, key=new_key()), 201).json()
    snap = expect(httpx.get(f"{base}/_test/export", timeout=TIMEOUT), 200).json()
    assert snap.get("track") == "tablekeeper" and snap.get("format_version") == 1, snap.keys()
    assert isinstance(snap.get("state"), (dict, list)), type(snap.get("state"))
    # a write after export does not change it
    expect(book(c, base, table_id="t_3", at="19:00"), 201)
    snap2 = httpx.get(f"{base}/_test/export", timeout=TIMEOUT).json()
    assert snap2 != snap, "export not a fresh snapshot"
    return "export shape; snapshot reflects state at call time"


def p_import_roundtrip_and_idempotent(base):
    c = auth(base, ADA["email"], ADA["password"])
    body = {"restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": local(booking_date(), "19:00"), "party_size": 4}
    receipt = expect(c.post("/reservations", json=body, key=new_key()), 201).json()
    snap = httpx.get(f"{base}/_test/export", timeout=TIMEOUT).json()
    reset(base)
    r = httpx.post(f"{base}/_test/import", json=snap, timeout=RESET_TIMEOUT)
    assert r.status_code == 204, f"import -> {r.status_code} {r.text[:200]}"
    assert c.get(f"/reservations/{receipt['reference']}").json() == receipt
    # repeating import does not duplicate
    expect(httpx.post(f"{base}/_test/import", json=snap, timeout=RESET_TIMEOUT), 204)
    lst = c.get("/reservations").json()["reservations"]
    refs = [x["reference"] for x in lst]
    assert refs.count(receipt["reference"]) == 1, refs
    return "import round-trip preserves receipt; repeatable no dupes"


def p_import_invalid(base):
    c = auth(base, ADA["email"], ADA["password"])
    body = {"restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": local(booking_date(), "19:00"), "party_size": 4}
    ref = expect(c.post("/reservations", json=body, key=new_key()), 201).json()["reference"]
    before = c.get(f"/reservations/{ref}").json()
    for bad in ({"track": "other", "format_version": 1, "state": {}},
                {"track": "tablekeeper", "format_version": 2, "state": {}},
                {"format_version": 1, "state": {}},
                {"track": "tablekeeper", "state": {}},
                {"track": "tablekeeper", "format_version": 1, "state": None},
                {"track": "tablekeeper", "format_version": 1}):
        r = httpx.post(f"{base}/_test/import", json=bad, timeout=RESET_TIMEOUT)
        expect(r, 422, "validation_failed")
    r = httpx.post(f"{base}/_test/import", content="{not json", timeout=RESET_TIMEOUT)
    assert r.status_code == 400 and code_of(r) == "malformed_request", (r.status_code, code_of(r))
    after = c.get(f"/reservations/{ref}").json()
    assert before == after, "invalid import changed state"
    return "invalid import 422/400 with no destination change"


def p_import_fidelity(base):
    # source: users+reservations+idempotent receipt, then export
    reset(base)
    src = auth(base, ADA["email"], ADA["password"])
    date = booking_date()
    key = new_key()
    body = {"restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": local(date, "19:00"), "party_size": 4}
    receipt = expect(src.post("/reservations", json=body, key=key), 201).json()
    # a failed key that must stay reusable
    failed_key = new_key()
    book(src, base, table_id="t_nope", key=failed_key)
    # a batch receipt
    b2 = expect(book(src, base, table_id="t_3", at="20:30", party_size=2), 201).json()
    mv_body = {"moves": [{"reference": receipt["reference"], "table_id": "t_3"}]}
    batch = expect(src.post("/reservation-moves", json=mv_body, key=new_key()), 201).json()
    snap = httpx.get(f"{base}/_test/export", timeout=TIMEOUT).json()
    # destination: a *different* state with different users
    other = {"id": "u_zed", "email": "zed@example.com", "password": "another pass", "display_name": "Zed"}
    reset(base, fixture(users=[other], restaurants=[restaurant("r_x", name="X")]))
    # old token must not work at destination before import (sanity, not a requirement)
    expect(httpx.post(f"{base}/_test/import", json=snap, timeout=RESET_TIMEOUT), 204)
    # token survives
    expect(src.get("/reservations"), 200)
    # hashed-password login survives
    fresh = auth(base, ADA["email"], ADA["password"])
    # receipt replay returns original body
    rep = expect(fresh.post("/reservations", json=body, key=key), 200).json()
    assert rep == receipt, "receipt replay after import differs"
    # reference survives exactly
    assert fresh.get(f"/reservations/{receipt['reference']}").json() == receipt
    # failed key still reusable
    expect(book(fresh, base, table_id="t_2", at="19:00", party_size=4, key=failed_key), 201)
    # destination's old user is gone
    r = httpx.post(f"{base}/auth/login", json={"email": "zed@example.com", "password": "another pass"},
                   timeout=TIMEOUT)
    assert r.status_code == 401, f"import left old destination credentials: {r.status_code}"
    # reset clears imported state
    reset(base)
    expect(fresh.get("/reservations"), 401, "unauthenticated")
    return "import fidelity: token/login/receipt/ref/failed-key; removes destination; reset clears"


def p_import_preserves_batch_receipt(base):
    reset(base)
    src = auth(base, ADA["email"], ADA["password"])
    date = booking_date()
    b1 = expect(book(src, base, table_id="t_1", at="19:00", party_size=2), 201).json()
    b2 = expect(book(src, base, table_id="t_2", at="19:00", party_size=2), 201).json()
    mv_body = {"moves": [{"reference": b1["reference"], "table_id": "t_2"},
                         {"reference": b2["reference"], "table_id": "t_1"}]}
    key = new_key()
    batch = expect(src.post("/reservation-moves", json=mv_body, key=key), 201).json()
    snap = httpx.get(f"{base}/_test/export", timeout=TIMEOUT).json()
    other = {"id": "u_zed", "email": "zed@example.com", "password": "another pass", "display_name": "Zed"}
    reset(base, fixture(users=[other], restaurants=[restaurant("r_x", name="X")]))
    expect(httpx.post(f"{base}/_test/import", json=snap, timeout=RESET_TIMEOUT), 204)
    fresh = auth(base, ADA["email"], ADA["password"])
    rep = expect(fresh.post("/reservation-moves", json=mv_body, key=key), 200).json()
    assert rep == batch, "batch replay after import differs from original"
    return "batch receipt survives export/import"


# ---------------------------------------------------------------- moves
def p_moves_shape_auth_key(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    anon = C(base)
    b = expect(book(c, base, table_id="t_1", party_size=2), 201).json()
    body = {"moves": [{"reference": b["reference"], "table_id": "t_2"}]}
    expect(anon.post("/reservation-moves", json=body, key=new_key()), 401, "unauthenticated")
    expect(c.post("/reservation-moves", json=body), 400, "missing_idempotency_key")
    too_many = {"moves": [{"reference": b["reference"], "table_id": "t_2"}] * 9}
    for bad in ({"moves": []}, {"moves": ""}, "string", {}, too_many,
                {"moves": [{"reference": b["reference"], "table_id": "t_2"}, "x"]}):
        r = c.post("/reservation-moves", json=bad, key=new_key())
        assert r.status_code == 422 and code_of(r) == "validation_failed", \
            f"{bad!r} -> {r.status_code} {code_of(r)}"
    dup = {"moves": [{"reference": b["reference"], "table_id": "t_2"},
                     {"reference": b["reference"], "table_id": "t_3"}]}
    expect(c.post("/reservation-moves", json=dup, key=new_key()), 422, "validation_failed")
    return "moves auth/key/shape/duplicates/limits"


def p_moves_cross_restaurant(base):
    reset(base)
    other = restaurant("r_other", name="Other",
                       tables=[{"id": "t_x", "label": "X", "capacity": 4}])
    reset(base, fixture(restaurants=[restaurant(), other]))
    ada = auth(base, ADA["email"], ADA["password"])
    a = expect(book(ada, base, rest="r_anker", table_id="t_1", party_size=2), 201).json()
    b = expect(book(ada, base, rest="r_other", table_id="t_x", party_size=2), 201).json()
    expect(ada.post("/reservation-moves", json={"moves": [
        {"reference": a["reference"], "table_id": "t_2"},
        {"reference": b["reference"], "table_id": "t_x"}]}, key=new_key()),
        422, "validation_failed")
    # foreign owner's reference -> 404
    bob = auth(base, BOB["email"], BOB["password"])
    bobs = expect(book(bob, base, rest="r_anker", table_id="t_3", party_size=2), 201).json()
    expect(ada.post("/reservation-moves", json={"moves": [
        {"reference": bobs["reference"], "table_id": "t_1"}]}, key=new_key()),
        404, "not_found")
    return "moves across restaurants -> 422; foreign ref -> 404"


def p_moves_success_and_replay(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    b1 = expect(book(c, base, table_id="t_1", party_size=2), 201).json()
    b2 = expect(book(c, base, table_id="t_2", at="20:30", party_size=4), 201).json()
    key = new_key()
    body = {"moves": [{"reference": b1["reference"], "table_id": "t_2", "starts_at_local": local(booking_date(), "20:30")},
                      {"reference": b2["reference"]}]}
    res = expect(c.post("/reservation-moves", json=body, key=key), 201).json()
    assert [r["reference"] for r in res["reservations"]] == [b1["reference"], b2["reference"]], res
    # unchanged item retained same identity
    assert res["reservations"][1]["reservation_id"] == b2["reservation_id"]
    assert res["reservations"][1]["created_at"] == b2["created_at"]
    # replay after amendment/cancel
    expect(c.post(f"/reservations/{b2['reference']}/cancel"), 200)
    rep = expect(c.post("/reservation-moves", json=body, key=key), 200).json()
    assert rep == res, "batch replay differs after cancel"
    return "moves success order/replay"


def p_moves_atomicity(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    b1 = expect(book(c, base, table_id="t_1", party_size=2), 201).json()
    b2 = expect(book(c, base, table_id="t_2", party_size=2), 201).json()
    # second move targets a table occupied by an unlisted booking -> whole batch fails
    b3 = expect(book(c, base, table_id="t_3", at="20:00", party_size=2), 201).json()
    key = new_key()
    body = {"moves": [{"reference": b1["reference"], "table_id": "t_2"},
                      {"reference": b2["reference"], "table_id": "t_3"}]}
    before1 = c.get(f"/reservations/{b1['reference']}").json()
    # t_3 is free at b2's time? b3 is at 20:00 for 90 min; b2 defaults 19:00-20:30 -> overlaps
    expect(c.post("/reservation-moves", json=body, key=key), 409, "table_unavailable")
    after1 = c.get(f"/reservations/{b1['reference']}").json()
    assert before1 == after1, "failing batch mutated first booking"
    # key remains reusable (first use never committed)
    body2 = {"moves": [{"reference": b1["reference"], "table_id": "t_2"},
                       {"reference": b2["reference"], "table_id": "t_1"}]}
    expect(c.post("/reservation-moves", json=body2, key=key), 201)
    return "batch all-or-nothing; failed key reusable"


def p_moves_cancelled_and_precedence(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    b1 = expect(book(c, base, table_id="t_1", party_size=2), 201).json()
    expect(c.post(f"/reservations/{b1['reference']}/cancel"), 200)
    r = c.post("/reservation-moves", json={"moves": [
        {"reference": b1["reference"], "table_id": "t_2"}]}, key=new_key())
    expect(r, 409, "reservation_cancelled")
    # cutoff precedes other changes for that booking
    reset(base, fixture(restaurants=[restaurant(cancellation_cutoff_minutes=60 * 24 * 3650)]))
    c = auth(base, ADA["email"], ADA["password"])
    b = expect(book(c, base, table_id="t_1", party_size=2), 201).json()
    r = c.post("/reservation-moves", json={"moves": [
        {"reference": b["reference"], "table_id": "t_1", "party_size": 0}]}, key=new_key())
    expect(r, 409, "cutoff_passed")
    return "batch cancelled -> 409; cutoff precedence"


def p_moves_duplicate_and_unknown_fields(base):
    reset(base)
    c = auth(base, ADA["email"], ADA["password"])
    b = expect(book(c, base, table_id="t_1", party_size=2), 201).json()
    body = {"moves": [{"reference": b["reference"], "table_id": "t_3", "souvenir": "x"}]}
    r = expect(c.post("/reservation-moves", json=body, key=new_key()), 201).json()
    assert r["reservations"][0]["table_id"] == "t_3"
    return "moves ignore unknown fields"


# ---------------------------------------------------------------- runner
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    args = ap.parse_args()
    base = args.base_url

    probes = [
        ("P1/P3 health+reset", lambda: p_delivery_health(base)),
        ("P1b reset replaces", lambda: p_reset_replaces_state(base)),
        ("P3 concurrent availability", lambda: p_concurrent_availability(base)),
        ("P5 error envelope", lambda: p_error_envelope_and_json(base)),
        ("P6 rfc3339 offsets", lambda: p_rfc3339_offsets(base)),
        ("P7 unknown fields", lambda: p_unknown_fields_ignored(base)),
        ("P8 id length/refs", lambda: p_id_length_limits(base)),
        ("P9 malformed vs validation", lambda: p_malformed_vs_validation(base)),
        ("P10 query integer", lambda: p_query_integer_rule(base)),
        ("P11 no 5xx", lambda: p_no_5xx_bad_inputs(base)),
        ("P12 signup/login", lambda: p_auth_signup_login(base)),
        ("P13 auth gating", lambda: p_auth_protected_public(base)),
        ("P13b other-user 404", lambda: p_other_user_reservation_404(base)),
        ("P14 multiple tokens", lambda: p_multiple_tokens(base)),
        ("P15 hashed passwords", lambda: p_passwords_hashed(base)),
        ("P16 seeded/closed", lambda: p_fixture_seeded_and_closed(base)),
        ("P17 missing key", lambda: p_idem_missing_empty(base)),
        ("P18 key per user", lambda: p_idem_scoped_per_user(base)),
        ("P19/P20 body/path", lambda: p_idem_body_then_path(base)),
        ("P21 replay after change", lambda: p_idem_replay_after_change(base)),
        ("P22 failed key reusable", lambda: p_idem_failed_key_reusable(base)),
        ("P23 reuse before validation", lambda: p_idem_reuse_before_validation(base)),
        ("P24 concurrent idempotency", lambda: p_idem_concurrent(base)),
        ("P25 restaurants", lambda: p_restaurants_public(base)),
        ("P26 availability shape", lambda: p_availability_shape_grid(base)),
        ("P27 create shape/ref", lambda: p_create_shape_and_ref(base)),
        ("P28 overlap/concurrency", lambda: p_overlap_and_concurrency(base)),
        ("P29 create rule matrix", lambda: p_create_rule_codes(base)),
        ("P31 list/cancel basics", lambda: p_reservation_reads_and_cancel(base)),
        ("P32 cutoff/free", lambda: p_cancel_cutoff_and_frees(base)),
        ("P33 patch semantics", lambda: p_patch_semantics(base)),
        ("P34 rejected create nothing", lambda: p_rejected_creates_nothing(base)),
        ("P35 Berlin spring", lambda: p_dst_berlin_spring(base)),
        ("P36 Berlin fall", lambda: p_dst_berlin_fall(base)),
        ("P37 NY transitions", lambda: p_dst_new_york(base)),
        ("P38 absolute duration", lambda: p_dst_berlin_duration_absolute(base)),
        ("P39 same instant", lambda: p_dst_same_instant(base)),
        ("P39b offsets both sides", lambda: p_dst_offsets_both_sides(base)),
        ("P40 export shape", lambda: p_export_shape_and_atomic(base)),
        ("P41 import round-trip", lambda: p_import_roundtrip_and_idempotent(base)),
        ("P42 import invalid", lambda: p_import_invalid(base)),
        ("P43 import fidelity", lambda: p_import_fidelity(base)),
        ("P43b batch receipt import", lambda: p_import_preserves_batch_receipt(base)),
        ("P45 moves shape/auth", lambda: p_moves_shape_auth_key(base)),
        ("P46 moves cross-restaurant", lambda: p_moves_cross_restaurant(base)),
        ("P47 moves success/replay", lambda: p_moves_success_and_replay(base)),
        ("P48 moves cancelled/precedence", lambda: p_moves_cancelled_and_precedence(base)),
        ("P49 moves atomicity", lambda: p_moves_atomicity(base)),
        ("P50 moves unknown fields", lambda: p_moves_duplicate_and_unknown_fields(base)),
    ]
    for name, fn in probes:
        check(name, fn)
    failed = [r for r in RESULTS if not r[0]]
    for ok, name, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'} {name} :: {detail}")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} probes passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
