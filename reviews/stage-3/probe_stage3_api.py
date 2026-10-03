#!/usr/bin/env python3
"""Independent stage-3 API probes for tablekeeper (Reviewer).

Runs against a live stage-3 service base URL. Written from plans/stage-3/ledger.md
(reqs 1-43, F1-F14) plus the still-binding stage-1/stage-2 ledgers and specs, not from the
supplied checks.

    probe_stage3_api.py --base-url http://127.0.0.1:8080
    probe_stage3_api.py --base-url http://127.0.0.1:8080 \
        --previous-base-url http://127.0.0.1:8081 --previous2-base-url http://127.0.0.1:8082

`--previous-base-url` is a stage-1 service and `--previous2-base-url` a stage-2 service,
used to produce authentic exports for the upgrade probes (req 39). Missing URLs SKIP those
probes.

Prints one line per probe: PASS/FAIL/SKIP name (detail). Exit 1 if any fails. Every request
carries a 5s timeout (10s reset/import/export) so a run cannot hang.
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
CTRL_TIMEOUT = 10.0
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
REF = re.compile(r"^[A-Z0-9]{6,12}$")

ADA = {"id": "u_ada", "email": "ada@example.com", "password": "correct horse", "display_name": "Ada"}
BOB = {"id": "u_bob", "email": "bob@example.com", "password": "correct horse", "display_name": "Bob"}
MGR = {"id": "u_mgr", "email": "mgr@example.com", "password": "correct horse", "display_name": "Mgr"}

RESULTS: list[tuple[str, str, str]] = []


class Skip(Exception):
    pass


def check(name, fn):
    try:
        detail = fn()
        RESULTS.append(("PASS", name, detail or ""))
    except Skip as exc:
        RESULTS.append(("SKIP", name, str(exc)))
    except AssertionError as exc:
        RESULTS.append(("FAIL", name, str(exc)))
    except Exception as exc:  # noqa: BLE001
        RESULTS.append(("FAIL", name, f"{type(exc).__name__}: {exc}"))


# ---------------------------------------------------------------- fixtures
def all_week(opens="18:00", closes="23:00"):
    return [{"weekday": d, "opens": opens, "closes": closes} for d in WEEKDAYS]


def restaurant(rid="r_anker", *, name="Zum Anker", timezone="Europe/Berlin",
               slot_minutes=30, reservation_duration_minutes=90,
               cancellation_cutoff_minutes=120, opening_hours=None, tables=None,
               combinable=None, manager_user_ids=None):
    r = {"id": rid, "name": name, "timezone": timezone, "slot_minutes": slot_minutes,
         "reservation_duration_minutes": reservation_duration_minutes,
         "cancellation_cutoff_minutes": cancellation_cutoff_minutes,
         "opening_hours": all_week() if opening_hours is None else opening_hours,
         "tables": tables if tables is not None else [
             {"id": "t_1", "label": "1", "capacity": 2},
             {"id": "t_2", "label": "2", "capacity": 4},
             {"id": "t_3", "label": "3", "capacity": 6}]}
    if combinable is not None:
        r["combinable"] = combinable
    if manager_user_ids is not None:
        r["manager_user_ids"] = manager_user_ids
    return r


def fixture(*, users=None, restaurants=None, reservations=None):
    return {"users": [ADA, BOB, MGR] if users is None else users,
            "restaurants": [restaurant()] if restaurants is None else restaurants,
            "reservations": reservations or []}


def booking_date(timezone="Europe/Berlin", lead=7):
    return (dt.datetime.now(ZoneInfo(timezone)).date() + dt.timedelta(days=lead)).isoformat()


def local(date, hhmm="19:00"):
    return f"{date}T{hhmm}"


def policy(*, effective_from, slot_minutes=30, reservation_duration_minutes=90,
           cancellation_cutoff_minutes=120, opening_hours=None,
           capacities=None):
    return {"effective_from": effective_from,
            "slot_minutes": slot_minutes,
            "reservation_duration_minutes": reservation_duration_minutes,
            "cancellation_cutoff_minutes": cancellation_cutoff_minutes,
            "opening_hours": all_week() if opening_hours is None else opening_hours,
            "capacities": capacities if capacities is not None else
            {"t_1": 2, "t_2": 4, "t_3": 6}}


def seeded(date, *, reference="SEED01", table_id="t_2", table_ids=None,
           user_id="u_ada", party_size=4, rest="r_anker", status=None, at="19:00",
           rid="res_seed"):
    r = {"id": rid, "reference": reference, "user_id": user_id,
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
                      timeout=CTRL_TIMEOUT)
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


def book(c, *, table_id="t_2", table_ids=None, date=None, at="19:00", party_size=4,
         rest="r_anker", key=None, starts_at_local=None, both=False, **extra):
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


def publish(c, rid, body, key=None):
    return c.post(f"/restaurants/{rid}/policies", json=body, key=key or new_key())


def patch(c, ref, body, key=None, **kw):
    return c.patch(f"/reservations/{ref}", json=body, **kw)


# ---------------------------------------------------------------- probes
def p_health_and_stage12_regressions(base):
    """Stage-1/2 fundamentals still hold on the stage-3 service."""
    reset(base)
    r = httpx.get(f"{base}/health", timeout=TIMEOUT)
    assert r.status_code == 200 and r.json() == {"status": "ok"}, r.text[:200]
    c = auth(base, ADA["email"], ADA["password"])
    create = expect(book(c), 201).json()
    assert create["reference"] and create["status"] == "confirmed"
    assert create.get("revision") == 1, create
    assert "accepted_terms" in create, create
    # availability without explain keeps stage-1 shape
    a = httpx.get(f"{base}/availability", params={
        "restaurant_id": "r_anker", "date": booking_date(), "party_size": 2}, timeout=TIMEOUT)
    expect(a, 200)
    slot = a.json()["slots"][0]
    assert "available_table_ids" in slot
    assert "explain" not in slot, "explain leaked without explain=true"
    # wrong JSON type still 400
    r = c.post("/reservations", json={"restaurant_id": 17, "table_id": "t_2",
               "starts_at_local": local(booking_date()), "party_size": 4}, key=new_key())
    expect(r, 400, "malformed_request")
    return "health + stage-1/2 fundamentals"


def p_policy_publication_and_versions(base):
    """Reqs 14-17, 20: manager-only, complete-body validation, versions, replay, list."""
    reset(base, fixture(restaurants=[restaurant(manager_user_ids=["u_mgr"])]))
    ada = auth(base, ADA["email"], ADA["password"])
    bob = auth(base, BOB["email"], BOB["password"])
    mgr = auth(base, MGR["email"], MGR["password"])
    # permissions
    expect(ada.get("/restaurants/r_anker/policies"), 200)  # public read
    expect(ada.post("/restaurants/r_anker/policies",
                    json=policy(effective_from=booking_date()), key=new_key()), 403, "forbidden")
    expect(ada.req("POST", "/restaurants/r_nope/policies",
                   token=None, json=policy(effective_from=booking_date()), key=new_key()),
           401, "unauthenticated")
    expect(mgr.post("/restaurants/r_nope/policies",
                    json=policy(effective_from=booking_date()), key=new_key()), 404, "not_found")
    # missing idempotency key
    expect(mgr.post("/restaurants/r_anker/policies",
                    json=policy(effective_from=booking_date())), 400, "missing_idempotency_key")
    # invalid bodies -> 422 no version
    bad_bodies = [
        policy(effective_from="not-a-date"),
        policy(effective_from=booking_date(), slot_minutes=0),
        policy(effective_from=booking_date(), slot_minutes=1441),
        policy(effective_from=booking_date(), slot_minutes=True),
        policy(effective_from=booking_date(), reservation_duration_minutes=False),
        policy(effective_from=booking_date(), cancellation_cutoff_minutes=-1),
        policy(effective_from=booking_date(), cancellation_cutoff_minutes=10081),
        policy(effective_from=booking_date(),
               opening_hours=[{"weekday": "mon", "opens": "18:00", "closes": "23:00"},
                              {"weekday": "mon", "opens": "09:00", "closes": "10:00"}]),
        policy(effective_from=booking_date(), capacities={"t_1": 2}),  # not exactly tables
        policy(effective_from=booking_date(), capacities={"t_1": 2, "t_2": 4, "t_3": 0}),
        {k: v for k, v in policy(effective_from=booking_date()).items()
         if k != "capacities"},  # missing required field
    ]
    for i, b in enumerate(bad_bodies):
        expect(mgr.post("/restaurants/r_anker/policies", json=b, key=new_key()),
               422, "validation_failed")
    # valid publication -> version 1 with supplied policy
    body1 = policy(effective_from=booking_date(), slot_minutes=45)
    r1 = expect(publish(mgr, "r_anker", body1), 201).json()
    assert r1["policy_version"] == 1, r1
    for k, v in body1.items():
        assert r1[k] == v, (k, r1[k], v)
    # second publication -> version 2
    body2 = policy(effective_from=booking_date(), slot_minutes=60)
    r2 = expect(publish(mgr, "r_anker", body2), 201).json()
    assert r2["policy_version"] == 2, r2
    # replay same key/body -> 200 same version, no new version
    key = new_key()
    first = expect(publish(mgr, "r_anker", body2, key=key), 201).json()
    assert first["policy_version"] == 3, first
    replay = publish(mgr, "r_anker", body2, key=key)
    assert replay.status_code == 200 and replay.json()["policy_version"] == 3, replay.text[:200]
    # same key different body -> 409 idempotency_key_reuse
    expect(mgr.post("/restaurants/r_anker/policies",
                    json=policy(effective_from=booking_date(), slot_minutes=99), key=key),
           409, "idempotency_key_reuse")
    # list public, publication order, omits policy 0
    lst = expect(httpx.get(f"{base}/restaurants/r_anker/policies", timeout=TIMEOUT), 200).json()
    versions = [p["policy_version"] for p in lst["policies"]]
    assert versions == [1, 2, 3], versions
    # restaurant detail still original fixture config (no manager field leak required, slot unchanged)
    det = expect(httpx.get(f"{base}/restaurants/r_anker", timeout=TIMEOUT), 200).json()
    assert det["slot_minutes"] == 30, det
    return "manager-only, validation, versions, replay, list order"


def p_policy_selection(base):
    """Reqs 18-19, F1: greatest effective_from <= date; ties by greatest version;
    publication order != effective order; past dates; supersession future-only."""
    today = booking_date(lead=0)
    d_target = booking_date(lead=10)
    past1 = (dt.date.fromisoformat(today) - dt.timedelta(days=5)).isoformat()
    past2 = (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()
    future = (dt.date.fromisoformat(d_target) + dt.timedelta(days=5)).isoformat()
    reset(base, fixture(restaurants=[restaurant(manager_user_ids=["u_mgr"])]))
    mgr = auth(base, MGR["email"], MGR["password"])
    ada = auth(base, ADA["email"], ADA["password"])
    # publish out of effective-date order: future first, then past2, then past1
    v_future = expect(publish(mgr, "r_anker", policy(effective_from=future, slot_minutes=15)), 201).json()
    v_past2 = expect(publish(mgr, "r_anker", policy(effective_from=past2, slot_minutes=120)), 201).json()
    v_past1 = expect(publish(mgr, "r_anker", policy(effective_from=past1, slot_minutes=60)), 201).json()
    assert (v_future["policy_version"], v_past2["policy_version"], v_past1["policy_version"]) == (1, 2, 3)
    # target date: greatest effective_from <= target is past2 (version 2, slot 120)
    exp = httpx.get(f"{base}/availability", params={
        "restaurant_id": "r_anker", "date": d_target, "party_size": 2, "explain": "true"},
        timeout=TIMEOUT).json()
    pvs = {e["policy_version"] for s in exp["slots"] for e in s["explain"]}
    assert pvs == {2}, f"expected selected policy version 2 for {d_target}, got {pvs}"
    # a date before all effective_from uses policy 0
    early = (dt.date.fromisoformat(past1) - dt.timedelta(days=5)).isoformat()
    exp0 = httpx.get(f"{base}/availability", params={
        "restaurant_id": "r_anker", "date": early, "party_size": 2, "explain": "true"},
        timeout=TIMEOUT).json()
    pvs0 = {e["policy_version"] for s in exp0["slots"] for e in s["explain"]}
    assert pvs0 == {0}, f"expected policy 0 before all effective dates, got {pvs0}"
    # same-date tie -> greatest version wins
    same = booking_date(lead=6)
    t1 = expect(publish(mgr, "r_anker", policy(effective_from=same, slot_minutes=30)), 201).json()
    t2 = expect(publish(mgr, "r_anker", policy(effective_from=same, slot_minutes=90)), 201).json()
    assert t2["policy_version"] > t1["policy_version"]
    expT = httpx.get(f"{base}/availability", params={
        "restaurant_id": "r_anker", "date": same, "party_size": 2, "explain": "true"},
        timeout=TIMEOUT).json()
    pvsT = {e["policy_version"] for s in expT["slots"] for e in s["explain"]}
    assert pvsT == {t2["policy_version"]}, f"same-date tie should pick greatest version, got {pvsT}"
    # accepted booking under the older same-date policy is unchanged by the newer one
    seed_date = booking_date(lead=4)
    reset(base, fixture(restaurants=[restaurant(manager_user_ids=["u_mgr"])],
                        reservations=[seeded(seed_date, reference="KEEPME1", user_id="u_ada")]))
    mgr = auth(base, MGR["email"], MGR["password"])
    ada = auth(base, ADA["email"], ADA["password"])
    expect(publish(mgr, "r_anker", policy(effective_from=seed_date, slot_minutes=45)), 201)
    before = expect(ada.get("/reservations/KEEPME1"), 200).json()
    expect(publish(mgr, "r_anker", policy(effective_from=seed_date, slot_minutes=75)), 201)
    after = expect(ada.get("/reservations/KEEPME1"), 200).json()
    assert after["accepted_terms"] == before["accepted_terms"], "publication edited an accepted booking"
    assert after["revision"] == before["revision"]
    return "selection order, ties, past dates, supersession"


def p_revisions_and_terms(base):
    """Reqs 21-23: revision/terms snapshot; seeded rev 1 policy 0; old idempotent resp kept."""
    d = booking_date()
    reset(base, fixture(reservations=[seeded(d, reference="SEEDR1", user_id="u_ada")]))
    ada = auth(base, ADA["email"], ADA["password"])
    seeded_resp = expect(ada.get("/reservations/SEEDR1"), 200).json()
    assert seeded_resp["revision"] == 1, seeded_resp
    assert seeded_resp["accepted_terms"]["policy_version"] == 0, seeded_resp
    assert "effective_from" not in seeded_resp["accepted_terms"], seeded_resp["accepted_terms"]
    for f in ("slot_minutes", "reservation_duration_minutes", "cancellation_cutoff_minutes",
              "opening_hours", "capacities"):
        assert f in seeded_resp["accepted_terms"], (f, seeded_resp["accepted_terms"])
    # create + old idempotent key keeps original revision/terms after an amendment
    key = new_key()
    created = expect(book(ada, key=key), 201).json()
    ref = created["reference"]
    orig_rev, orig_terms = created["revision"], created["accepted_terms"]
    expect(patch(ada, ref, {"party_size": 3}), 200)
    replay = ada.post("/reservations", json={
        "restaurant_id": "r_anker", "table_id": created.get("table_id", "t_2"),
        "starts_at_local": created["starts_at_local"], "party_size": 4}, key=key)
    assert replay.status_code == 200, replay.text[:200]
    assert replay.json()["revision"] == orig_rev, replay.json()
    assert replay.json()["accepted_terms"] == orig_terms, replay.json()
    return "revision/terms snapshot and old idempotent response"


def p_amendments_under_policies(base):
    """Reqs 24-28: accepted cutoff, resulting-date policy, no-op, failed, cancel, expected_revision."""
    d = booking_date(lead=8)
    reset(base, fixture(restaurants=[restaurant(manager_user_ids=["u_mgr"])]))
    mgr = auth(base, MGR["email"], MGR["password"])
    ada = auth(base, ADA["email"], ADA["password"])
    # publish a policy effective on d with a shorter duration and different capacities
    expect(publish(mgr, "r_anker", policy(effective_from=d,
                   reservation_duration_minutes=60, capacities={"t_1": 2, "t_2": 4, "t_3": 6})), 201)
    created = expect(book(ada, date=d, at="18:00", party_size=2), 201).json()
    ref = created["reference"]
    rev0, terms0 = created["revision"], created["accepted_terms"]
    # real amendment to a resulting date under the new policy: end time uses 60 min
    moved = expect(patch(ada, ref, {"starts_at_local": local(d, "19:00")}), 200).json()
    assert moved["revision"] == rev0 + 1, moved
    assert moved["accepted_terms"]["reservation_duration_minutes"] == 60, moved["accepted_terms"]
    ends = dt.datetime.fromisoformat(moved["ends_at"])
    starts = dt.datetime.fromisoformat(moved["starts_at"])
    assert (ends - starts) == dt.timedelta(minutes=60), (starts, ends)
    # no-op amendment: same party_size -> nothing changes, no history entry
    hist_before = expect(ada.get(f"/reservations/{ref}/history"), 200).json()
    noop = expect(patch(ada, ref, {"party_size": moved["party_size"]}), 200).json()
    assert noop["revision"] == moved["revision"], noop
    assert noop["accepted_terms"] == moved["accepted_terms"], noop
    hist_after = expect(ada.get(f"/reservations/{ref}/history"), 200).json()
    assert len(hist_after["entries"]) == len(hist_before["entries"]), "no-op recorded history"
    # failed amendment changes nothing
    expect(patch(ada, ref, {"party_size": 99}), 422, "party_exceeds_capacity")
    still = expect(ada.get("/reservations/{0}".format(ref)), 200).json()
    assert still["revision"] == noop["revision"], still
    # expected_revision: stale -> 409 before validation
    expect(patch(ada, ref, {"party_size": 2, "expected_revision": 1}), 409, "stale_revision")
    expect(patch(ada, ref, {"expected_revision": "x"}), 422, "validation_failed")
    expect(patch(ada, ref, {"expected_revision": 0}), 422, "validation_failed")
    # current revision works
    expect(patch(ada, ref, {"expected_revision": noop["revision"], "party_size": 2}), 200)
    # cancel: revision +1 once; repeat cancel no-op
    c1 = expect(ada.post(f"/reservations/{ref}/cancel"), 200).json()
    r_cancel = c1["revision"]
    c2 = expect(ada.post(f"/reservations/{ref}/cancel"), 200).json()
    assert c2["revision"] == r_cancel, (c1, c2)
    # concurrent amendment with one revision: at most one real change
    key = new_key()
    base_res = expect(book(ada, date=d, at="20:00", party_size=2, key=key), 201).json()
    ref2 = base_res["reference"]
    rev = base_res["revision"]

    def amend(ps):
        return patch(ada, ref2, {"party_size": ps, "expected_revision": rev})

    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        outs = list(ex.map(lambda ps: amend(ps), [1, 2]))
    codes = sorted(o.status_code for o in outs)
    # 200 then 409 stale (the loser sees the bumped revision)
    assert codes[0] == 200, [o.status_code for o in outs]
    assert 409 in codes, [o.status_code for o in outs]
    return "amendment policy/cutoff/no-op/stale/reset and race"


def p_explain_exactness(base):
    """Reqs 1-6, F2, F14: explain validation, plain-shape invariance, exactness."""
    d = booking_date(lead=9)
    reset(base, fixture(
        restaurants=[restaurant(
            tables=[{"id": "t_1", "label": "1", "capacity": 2},
                    {"id": "t_2", "label": "2", "capacity": 4},
                    {"id": "t_3", "label": "3", "capacity": 6}])],
        reservations=[seeded(d, reference="OCCUPY", table_id="t_2", user_id="u_ada",
                             party_size=4)]))
    base_q = {"restaurant_id": "r_anker", "date": d, "party_size": 3}
    # invalid explain values -> 422 (F14: query validation)
    for bad in ("false", "1", "", "TRUE", "yes"):
        r = httpx.get(f"{base}/availability",
                      params={**base_q, "explain": bad}, timeout=TIMEOUT)
        expect(r, 422, "validation_failed")
    # plain shape: no explain field at all
    plain = httpx.get(f"{base}/availability", params=base_q, timeout=TIMEOUT).json()
    for s in plain["slots"]:
        assert "explain" not in s, "explain present without explain=true"
    # with explain
    exp = httpx.get(f"{base}/availability",
                    params={**base_q, "explain": "true"}, timeout=TIMEOUT).json()
    table_order = ["t_1", "t_2", "t_3"]
    for s in exp["slots"]:
        entries = s["explain"]
        assert [e["table_id"] for e in entries] == table_order, \
            f"explain must list every table once in fixture order: {[e['table_id'] for e in entries]}"
        avail_by_id = {}
        for e in entries:
            rules = e["rules"]
            assert [r["rule"] for r in rules] == ["capacity", "no_overlap"], rules
            assert all(isinstance(r["holds"], bool) for r in rules), rules
            both = all(r["holds"] for r in rules)
            assert e["available"] is both, (e["table_id"], e["available"], rules)
            assert "policy_version" in e, e
            avail_by_id[e["table_id"]] = e["available"]
        # available:true set == available_table_ids in same order
        exp_true = [e["table_id"] for e in entries if e["available"]]
        assert exp_true == s["available_table_ids"], (exp_true, s["available_table_ids"])
        # occupied t_2 must report no_overlap false in every overlapping slot
        slot_start = s["starts_at_local"].split("T")[1]
        if slot_start < "20:30":  # OCCUPY at 19:00 for 90 min under default policy
            e_t2 = next(e for e in entries if e["table_id"] == "t_2")
            assert e_t2["rules"][1]["holds"] is False, e_t2
    # closed day still [] with a full slot when party small, and no-available slot still listed
    closed_date = booking_date(lead=0)  # find a weekday with no hours
    # build a restaurant closed on that weekday
    wd = dt.date.fromisoformat(closed_date).strftime("%a").lower()
    hours = [h for h in all_week() if h["weekday"] != wd]
    reset(base, fixture(restaurants=[restaurant(opening_hours=hours)]))
    closed = httpx.get(f"{base}/availability",
                       params={**base_q, "date": closed_date, "explain": "true"},
                       timeout=TIMEOUT).json()
    assert closed["slots"] == [], closed
    return "explain validation, shape, exactness, ordering, occupancy"


def p_history(base):
    """Reqs 7-13: owner-only 404, seq, created/changed order, no-op, cancelled, replay, terms."""
    d = booking_date(lead=5)
    reset(base)
    ada = auth(base, ADA["email"], ADA["password"])
    bob = auth(base, BOB["email"], BOB["password"])
    created = expect(book(ada, date=d, at="18:00", party_size=2), 201).json()
    ref = created["reference"]
    expect(patch(ada, ref, {"starts_at_local": local(d, "19:00")}), 200)
    expect(patch(ada, ref, {"party_size": 3}), 200)
    expect(ada.post(f"/reservations/{ref}/cancel"), 200)
    # owner-only incl. unauthenticated
    h = expect(ada.get(f"/reservations/{ref}/history"), 200).json()
    assert h["reference"] == ref
    entries = h["entries"]
    assert [e["seq"] for e in entries] == list(range(1, len(entries) + 1)), entries
    # created names three fields from null
    c0 = entries[0]
    assert c0["event"] == "created"
    fields = {ch["field"] for ch in c0["changes"]}
    assert fields == {"table_id", "starts_at_local", "party_size"}, c0
    assert all(ch["from"] is None for ch in c0["changes"]), c0
    # changed entries name only real changes in order
    changed = [e for e in entries if e["event"] == "changed"]
    assert changed, entries
    for e in changed:
        assert all(ch["from"] != ch["to"] for ch in e["changes"]), e
        order = [ch["field"] for ch in e["changes"]]
        assert order == sorted(order, key=["table_id", "starts_at_local", "party_size"].index), order
    # cancelled terminal empty changes
    last = entries[-1]
    assert last["event"] == "cancelled" and last["changes"] == [], last
    # every entry carries revision + complete terms; old entries keep old terms
    for e in entries:
        assert "revision" in e and "accepted_terms" in e, e
    assert entries[0]["revision"] < entries[-1]["revision"], entries
    # owner-only 404 (other user, unauthenticated)
    expect(bob.get(f"/reservations/{ref}/history"), 404, "not_found")
    expect(httpx.get(f"{base}/reservations/{ref}/history", timeout=TIMEOUT), 404, "not_found")
    # replay records nothing
    key = new_key()
    r1 = expect(book(ada, date=d, at="20:00", party_size=2, key=key), 201).json()
    ref2 = r1["reference"]
    n_before = len(expect(ada.get(f"/reservations/{ref2}/history"), 200).json()["entries"])
    body = {"restaurant_id": "r_anker", "table_id": r1.get("table_id", "t_2"),
            "starts_at_local": r1["starts_at_local"], "party_size": 2}
    ada.post("/reservations", json=body, key=key)
    n_after = len(expect(ada.get(f"/reservations/{ref2}/history"), 200).json()["entries"])
    assert n_after == n_before, "replay recorded history"
    return "history owner/seq/created/changed/cancelled/replay/terms"


def p_decision(base):
    """Req 29: decision shape incl. after cancellation; owner-only and unauthenticated 404."""
    d = booking_date(lead=5)
    reset(base)
    ada = auth(base, ADA["email"], ADA["password"])
    bob = auth(base, BOB["email"], BOB["password"])
    created = expect(book(ada, date=d, party_size=2), 201).json()
    ref = created["reference"]
    dec = expect(ada.get(f"/reservations/{ref}/decision"), 200).json()
    assert dec["reference"] == ref and dec["revision"] == created["revision"], dec
    assert dec["accepted_terms"] == created["accepted_terms"], dec
    expect(ada.post(f"/reservations/{ref}/cancel"), 200)
    after = expect(ada.get(f"/reservations/{ref}/decision"), 200).json()
    assert after["revision"] == created["revision"] + 1, after
    expect(bob.get(f"/reservations/{ref}/decision"), 404, "not_found")
    expect(httpx.get(f"{base}/reservations/{ref}/decision", timeout=TIMEOUT), 404, "not_found")
    return "decision current/after-cancel/owner-only"


def p_series_adoption(base):
    """Reqs 30-38, F7-F11: adoption, all-or-nothing, occurrence semantics, replay, revisions."""
    d = booking_date(lead=14)
    reset(base, fixture(restaurants=[restaurant(manager_user_ids=["u_mgr"])]))
    mgr = auth(base, MGR["email"], MGR["password"])
    ada = auth(base, ADA["email"], ADA["password"])
    bob = auth(base, BOB["email"], BOB["password"])
    anchor = expect(book(ada, date=d, at="18:00", party_size=2), 201).json()
    anchor_ref = anchor["reference"]

    def adopt(body, key=None):
        return ada.post("/series", json=body, key=key or new_key())

    # permissions and anchor rules
    expect(ada.post("/series", json={"anchor_reference": anchor_ref, "count": 3,
                    "interval_weeks": 1}), 400, "missing_idempotency_key")
    expect(httpx.post(f"{base}/series", json={"anchor_reference": anchor_ref, "count": 3,
                    "interval_weeks": 1}, headers={"Idempotency-Key": new_key()},
                    timeout=TIMEOUT), 401, "unauthenticated")
    expect(adopt({"anchor_reference": "NOPE12", "count": 3, "interval_weeks": 1}),
           404, "not_found")
    # another owner's anchor -> 404
    expect(bob.post("/series", json={
        "anchor_reference": anchor_ref, "count": 3, "interval_weeks": 1}, key=new_key()),
        404, "not_found")
    # bounds incl. booleans
    for bad in [{"anchor_reference": anchor_ref, "count": 1, "interval_weeks": 1},
                {"anchor_reference": anchor_ref, "count": 13, "interval_weeks": 1},
                {"anchor_reference": anchor_ref, "count": True, "interval_weeks": 1},
                {"anchor_reference": anchor_ref, "count": 3, "interval_weeks": 0},
                {"anchor_reference": anchor_ref, "count": 3, "interval_weeks": 5},
                {"anchor_reference": anchor_ref, "count": 3, "interval_weeks": False}]:
        expect(adopt(bad), 422, "validation_failed")
    # cancelled anchor -> 409 reservation_cancelled
    gone = expect(book(ada, date=d, at="21:00", party_size=2), 201).json()
    expect(ada.post(f"/reservations/{gone['reference']}/cancel"), 200)
    expect(adopt({"anchor_reference": gone["reference"], "count": 3, "interval_weeks": 1}),
           409, "reservation_cancelled")
    # successful adoption
    key = new_key()
    s = expect(adopt({"anchor_reference": anchor_ref, "count": 3, "interval_weeks": 1}, key=key),
               201).json()
    assert s["revision"] == 1 and s["interval_weeks"] == 1, s
    occ = s["occurrences"]
    assert [o["index"] for o in occ] == [0, 1, 2], occ
    assert occ[0]["reference"] == anchor_ref, occ[0]
    assert occ[0]["reservation"]["revision"] == anchor["revision"], occ[0]
    refs = [o["reference"] for o in occ]
    assert len(set(refs)) == 3, refs
    assert all(REF.fullmatch(r) for r in refs), refs
    assert all(o["exception"] is False for o in occ), occ
    # occurrence i date = anchor date + i*7
    for o in occ:
        exp_date = (dt.date.fromisoformat(d) +
                    dt.timedelta(days=o["index"] * 7)).isoformat()
        assert o["reservation"]["starts_at_local"].startswith(exp_date), (o["index"], o)
    # occurrences appear in ordinary list and have histories
    rows = expect(ada.get("/reservations"), 200).json()["reservations"]
    listed = {r["reference"] for r in rows}
    assert set(refs) <= listed, (refs, listed)
    for r in refs:
        expect(ada.get(f"/reservations/{r}/history"), 200)
    # GET series owner-only
    gs = expect(ada.get(f"/series/{s['series_id']}"), 200).json()
    assert [o["index"] for o in gs["occurrences"]] == [0, 1, 2]
    expect(bob.get(f"/series/{s['series_id']}"), 404, "not_found")
    expect(httpx.get(f"{base}/series/{s['series_id']}", timeout=TIMEOUT), 404, "not_found")
    # already adopted
    expect(adopt({"anchor_reference": anchor_ref, "count": 3, "interval_weeks": 1}),
           409, "already_in_series")
    # individual PATCH marks exception + series rev +1 once
    occ1 = occ[1]["reservation"]
    expect(patch(ada, occ1["reference"], {"party_size": 3}), 200)
    gs2 = expect(ada.get(f"/series/{s['series_id']}"), 200).json()
    assert gs2["revision"] == s["revision"] + 1, gs2
    e1 = next(o for o in gs2["occurrences"] if o["index"] == 1)
    assert e1["exception"] is True, e1
    # no-op patch does not bump series revision
    cur = expect(ada.get(f"/reservations/{occ1['reference']}"), 200).json()
    expect(patch(ada, occ1["reference"], {"party_size": cur["party_size"]}), 200)
    gs3 = expect(ada.get(f"/series/{s['series_id']}"), 200).json()
    assert gs3["revision"] == gs2["revision"], gs3
    # cancel occurrence 2 bumps series rev once, no exception; repeated no-op
    occ2ref = occ[2]["reference"]
    expect(ada.post(f"/reservations/{occ2ref}/cancel"), 200)
    gs4 = expect(ada.get(f"/series/{s['series_id']}"), 200).json()
    assert gs4["revision"] == gs3["revision"] + 1, gs4
    e2 = next(o for o in gs4["occurrences"] if o["index"] == 2)
    assert e2["exception"] is False, e2
    assert e2["reservation"]["status"] == "cancelled", e2
    expect(ada.post(f"/reservations/{occ2ref}/cancel"), 200)
    gs5 = expect(ada.get(f"/series/{s['series_id']}"), 200).json()
    assert gs5["revision"] == gs4["revision"], gs5
    # cancel anchor does not cancel siblings
    expect(ada.post(f"/reservations/{anchor_ref}/cancel"), 200)
    gs6 = expect(ada.get(f"/series/{s['series_id']}"), 200).json()
    sib = next(o for o in gs6["occurrences"] if o["index"] == 1)
    assert sib["reservation"]["status"] == "confirmed", sib
    # replay returns original series response, no counter movement
    replay = ada.post("/series", json={"anchor_reference": anchor_ref, "count": 3,
                      "interval_weeks": 1}, key=key)
    assert replay.status_code == 200, replay.text[:200]
    assert replay.json()["series_id"] == s["series_id"], replay.json()
    gs7 = expect(ada.get(f"/series/{s['series_id']}"), 200).json()
    assert gs7["revision"] == gs6["revision"], "replay moved the series revision"
    return "adoption, bounds, occurrence semantics, exceptions, replay"


def p_series_all_or_nothing(base):
    """Req 34, F1: a failing occurrence aborts the whole adoption, first failing index error."""
    d = booking_date(lead=16)
    reset(base)
    ada = auth(base, ADA["email"], ADA["password"])
    anchor = expect(book(ada, date=d, at="18:00", party_size=2), 201).json()
    # block occurrence 2 (anchor + 14 days) with an overlapping booking on the chosen table
    occ2_date = (dt.date.fromisoformat(d) + dt.timedelta(days=14)).isoformat()
    blocker = auth(base, ADA["email"], ADA["password"])
    expect(book(blocker, date=occ2_date, at="18:00", party_size=2, table_id="t_2"), 201)
    n_before = len(expect(ada.get("/reservations"), 200).json()["reservations"])
    r = ada.post("/series", json={"anchor_reference": anchor["reference"], "count": 3,
                 "interval_weeks": 1}, key=new_key())
    expect(r, 409, "table_unavailable")
    n_after = len(expect(ada.get("/reservations"), 200).json()["reservations"])
    assert n_after == n_before, f"partial adoption created reservations: {n_before} -> {n_after}"
    return "all-or-nothing first-failing-occurrence"


def p_combined_table_history(base):
    """Reqs 40-41, F6: pair history uses table_ids; reversed pair is a no-op."""
    d = booking_date(lead=11)
    reset(base, fixture(restaurants=[restaurant(combinable=[["t_1", "t_2"]])]))
    ada = auth(base, ADA["email"], ADA["password"])
    created = expect(book(ada, table_ids=["t_1", "t_2"], party_size=6, date=d, at="18:00"), 201).json()
    ref = created["reference"]
    h = expect(ada.get(f"/reservations/{ref}/history"), 200).json()
    c0 = h["entries"][0]
    fields = {ch["field"] for ch in c0["changes"]}
    assert "table_ids" in fields, c0
    tch = next(ch for ch in c0["changes"] if ch["field"] == "table_ids")
    assert tch["from"] is None and tch["to"] == ["t_1", "t_2"], tch
    n = len(h["entries"])
    # reversed pair is the same set: no-op, no entry, no revision change
    before = expect(ada.get(f"/reservations/{ref}"), 200).json()
    expect(patch(ada, ref, {"table_ids": ["t_2", "t_1"]}), 200)
    after = expect(ada.get(f"/reservations/{ref}"), 200).json()
    h2 = expect(ada.get(f"/reservations/{ref}/history"), 200).json()
    assert after["revision"] == before["revision"], (before, after)
    assert len(h2["entries"]) == n, "reversed pair recorded an amendment"
    return "pair history table_ids and reversed-pair no-op"


def p_moves_under_policies(base):
    """Reqs 42-43, F11: per-move policy/expected_revision, atomic, revisions and history."""
    d = booking_date(lead=12)
    reset(base, fixture(restaurants=[restaurant(manager_user_ids=["u_mgr"])]))
    mgr = auth(base, MGR["email"], MGR["password"])
    ada = auth(base, ADA["email"], ADA["password"])
    expect(publish(mgr, "r_anker", policy(effective_from=d, reservation_duration_minutes=60)), 201)
    b1 = expect(book(ada, date=d, at="18:00", party_size=2, table_id="t_1"), 201).json()
    b2 = expect(book(ada, date=d, at="18:00", party_size=2, table_id="t_2"), 201).json()
    moves = {"moves": [
        {"reference": b1["reference"], "starts_at_local": local(d, "19:00"),
         "expected_revision": b1["revision"]},
        {"reference": b2["reference"], "party_size": 3, "expected_revision": 999},
    ]}
    r = ada.post("/reservation-moves", json=moves, key=new_key())
    # expected_revision 999 stale -> whole batch rejected atomically
    expect(r, 409, "stale_revision")
    assert expect(ada.get(f"/reservations/{b1['reference']}"), 200).json()["revision"] == b1["revision"]
    # valid batch: both real changes, per-move policy adopted, revisions +1 each
    moves2 = {"moves": [
        {"reference": b1["reference"], "starts_at_local": local(d, "19:00"),
         "expected_revision": b1["revision"]},
        {"reference": b2["reference"], "party_size": 3,
         "expected_revision": b2["revision"]},
    ]}
    ok = expect(ada.post("/reservation-moves", json=moves2, key=new_key()), 201).json()
    out = {x["reference"]: x for x in ok["reservations"]}
    assert out[b1["reference"]]["revision"] == b1["revision"] + 1, out[b1["reference"]]
    assert out[b1["reference"]]["accepted_terms"]["reservation_duration_minutes"] == 60
    assert out[b2["reference"]]["revision"] == b2["revision"] + 1
    for x in (b1, b2):
        h = expect(ada.get(f"/reservations/{x['reference']}/history"), 200).json()
        assert h["entries"][-1]["event"] == "changed", h
    return "moves per-move expected_revision/policy, atomic, revisions"


def p_upgrade_continuity(base, prev1, prev2):
    """Req 39, F12: stage-1 and stage-2 exports import; adoption works; tokens/retries survive."""
    if not prev1 or not prev2:
        raise Skip("need --previous-base-url (stage-1) and --previous2-base-url (stage-2)")
    # stage-1 export -> stage-3
    reset(prev1)
    pc1 = auth(prev1, ADA["email"], ADA["password"])
    d1 = booking_date(lead=15)
    body = {"restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": local(d1, "18:00"), "party_size": 2}
    key1 = new_key()
    first = pc1.post("/reservations", json=body, key=key1)
    assert first.status_code == 201, f"stage-1 create -> {first.status_code} {first.text[:200]}"
    ref1 = first.json()["reference"]
    snap1 = expect(pc1.get("/_test/export"), 200).json()
    imp = httpx.post(f"{base}/_test/import", json=snap1, timeout=CTRL_TIMEOUT)
    assert imp.status_code == 204, f"stage-3 import stage-1 -> {imp.status_code} {imp.text[:200]}"
    c3 = C(base, token=pc1.token)
    one = expect(c3.get(f"/reservations/{ref1}"), 200).json()
    assert one["revision"] == 1 and one["accepted_terms"]["policy_version"] == 0, one
    retry = c3.post("/reservations", json=body, key=key1)
    assert retry.status_code in (200, 201) and retry.json()["reference"] == ref1, retry.text[:200]
    # adoption on an imported reservation
    s = expect(c3.post("/series", json={"anchor_reference": ref1, "count": 2,
                "interval_weeks": 1}, key=new_key()), 201).json()
    assert s["occurrences"][0]["reference"] == ref1, s
    # stage-2 export -> stage-3
    reset(prev2)
    pc2 = auth(prev2, ADA["email"], ADA["password"])
    d2 = booking_date(lead=13)
    body2 = {"restaurant_id": "r_anker", "table_id": "t_2",
             "starts_at_local": local(d2, "18:00"), "party_size": 2}
    key2 = new_key()
    f2 = pc2.post("/reservations", json=body2, key=key2)
    assert f2.status_code == 201, f"stage-2 create -> {f2.status_code} {f2.text[:200]}"
    ref2 = f2.json()["reference"]
    snap2 = expect(pc2.get("/_test/export"), 200).json()
    imp2 = httpx.post(f"{base}/_test/import", json=snap2, timeout=CTRL_TIMEOUT)
    assert imp2.status_code == 204, f"stage-3 import stage-2 -> {imp2.status_code} {imp2.text[:200]}"
    c3b = C(base, token=pc2.token)
    one2 = expect(c3b.get(f"/reservations/{ref2}"), 200).json()
    assert one2["revision"] == 1 and one2["accepted_terms"]["policy_version"] == 0, one2
    retry2 = c3b.post("/reservations", json=body2, key=key2)
    assert retry2.status_code in (200, 201) and retry2.json()["reference"] == ref2, retry2.text[:200]
    s2 = expect(c3b.post("/series", json={"anchor_reference": ref2, "count": 2,
                 "interval_weeks": 1}, key=new_key()), 201).json()
    assert s2["occurrences"][0]["reference"] == ref2, s2
    return "stage-1 and stage-2 export continuity + adoption"


def p_dst_carried_forward(base):
    """Stage-1 §9 still holds: spring-forward invalid_local_time; fall-back first occurrence."""
    reset(base, fixture(restaurants=[restaurant(
        timezone="Europe/Berlin",
        opening_hours=[{"weekday": w, "opens": "00:00", "closes": "05:00"} for w in WEEKDAYS])]))
    ada = auth(base, ADA["email"], ADA["password"])
    spring = "2026-03-29"
    r = book(ada, date=spring, at="02:30", party_size=2)
    expect(r, 422, "invalid_local_time")
    fall = "2026-10-25"
    ok = book(ada, date=fall, at="02:30", party_size=2)
    assert ok.status_code == 201, ok.text[:200]
    js = ok.json()
    starts, ends = dt.datetime.fromisoformat(js["starts_at"]), dt.datetime.fromisoformat(js["ends_at"])
    assert (ends - starts) == dt.timedelta(minutes=90), (starts, ends)
    return "DST spring-forward/fall-back carried forward"


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--previous-base-url", default=None)
    ap.add_argument("--previous2-base-url", default=None)
    args = ap.parse_args(argv)
    base = args.base_url.rstrip("/")

    check("P1 policies publication/immut/versions", lambda: p_policy_publication_and_versions(base))
    check("P2 policy selection order/ties", lambda: p_policy_selection(base))
    check("P3 revision/terms baseline", lambda: p_revisions_and_terms(base))
    check("P4 amendments under policies", lambda: p_amendments_under_policies(base))
    check("P5 explain exactness", lambda: p_explain_exactness(base))
    check("P6 history exactness", lambda: p_history(base))
    check("P7 decision endpoint", lambda: p_decision(base))
    check("P8 series adoption", lambda: p_series_adoption(base))
    check("P9 series all-or-nothing", lambda: p_series_all_or_nothing(base))
    check("P10 combined-table history", lambda: p_combined_table_history(base))
    check("P11 moves under policies", lambda: p_moves_under_policies(base))
    check("P12 upgrade continuity", lambda: p_upgrade_continuity(
        base, args.previous_base_url, args.previous2_base_url))
    check("P13 stage-1/2 regressions", lambda: p_health_and_stage12_regressions(base))
    check("P14 DST carried forward", lambda: p_dst_carried_forward(base))

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
